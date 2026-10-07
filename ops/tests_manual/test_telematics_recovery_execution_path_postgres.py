#!/usr/bin/env python3
"""Real execution-path integration tests for Telematics manual recovery.

WHAT MAKES THESE DIFFERENT FROM THE OTHER RECOVERY SUITES.
    `test_telematics_cold_start_recovery_postgres.py` replaces
    `recover_telematics_trips_window.launch_sync` wholesale, so it proves the
    launcher's gates but never runs the business job's own disabled-schedule
    guard. That is exactly the seam the ECHO00001 defect lived in: the launcher
    believed a return code the job had produced without doing anything.

    This suite therefore mocks **nothing** on the path

        recovery launcher
          -> runner parameter construction
          -> the real jobs.api.telematics.sync_trips_and_speeding schedule guard
          -> the real manual-recovery authority check
          -> the real structured terminal record
          -> the real recovery finalization decision

    The single substitution is `subprocess.run` inside the launcher, replaced by
    an in-process executor that mirrors `ops/runner.py`: it honours the exact
    environment the launcher built (including the launch attestation and the
    execution-record path) and then calls the genuine `run()` of the genuine job
    module. The provider HTTP client is faked — and only after the authority and
    schedule gates have already been passed, since the job constructs it well
    below them.

    Both databases are real. The platform control plane and the client business
    database are two databases on one disposable PostgreSQL 16 server, so
    "transaction status = COMMITTED" means a transaction that actually
    committed rows into a real `public.client_trips`.

Set:
    TELEMATICS_EXECUTION_PATH_TEST_DSN            platform database DSN
    TELEMATICS_EXECUTION_PATH_BUSINESS_DSN        client business database DSN

Both must be disposable. The suite drops and recreates their schemas.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics import coverage_finalization as cf  # noqa: E402
from jobs.api.telematics import execution_outcome as eo  # noqa: E402
from jobs.api.telematics import manual_recovery_authority as mra  # noqa: E402
from jobs.api.telematics import sync_trips_and_speeding as sync  # noqa: E402
from ops import recover_telematics_trips_window as rc  # noqa: E402
from ops.tests_manual.telematics_execution_outcome_fixtures import (  # noqa: E402
    outcome_for_window,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_cold_start_audit import (  # noqa: E402
    install_network_guard,
)

ENV = "TELEMATICS_EXECUTION_PATH_TEST_DSN"
BUSINESS_ENV = "TELEMATICS_EXECUTION_PATH_BUSINESS_DSN"

MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    # The business job reads `trip_metrics_population_source`, so this suite
    # needs a control plane the real job can actually load a client from.
    "040_workflow_a_trip_metrics_population_source.sql",
    "042_platform_environment_identity.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
)

CLIENT_BUSINESS_DDL = (
    "020_client_trips_final_schema.sql",
    "021_add_trip_mode_to_client_trips.sql",
    "019_add_speeding_violation_count_columns.sql",
    "024_alpha00001_client_trips_dysponent_id.sql",
    "025_alpha00001_dysponent_id_batch_indexes.sql",
    "026_add_driver_restrictions_to_client_trips.sql",
    # M4. The job's trip INSERT names `first_seen_request_id` unconditionally,
    # exactly as it names `trip_mode` and `Driver_Restrictions`, so a client
    # business database without this column cannot ingest at all. That is the
    # established shape for an additive client column and it is fail-closed —
    # but it makes the rollout ORDER load-bearing: the client-business migration
    # must be applied to every enabled client BEFORE the M4 release is
    # activated. Applying it here is what keeps that requirement honest instead
    # of leaving it to be discovered in production.
    "047_client_trips_first_seen_request_id.sql",
)

CID = "5ec47d1b-4db8-4357-889c-11e5662e7d39"
SID = "a0597d7f-8998-459a-8e59-d0ca9aa29db8"
OTHER_SID = "baeeb265-557a-4878-8cc6-be5d1d15cf57"
CODE = "NEWC00001"
# A second, fully real client. Used so "the recovery row belongs to another
# client" is tested against a client that genuinely exists, rather than against
# an id that fails earlier for the unrelated reason of having no account row.
OTHER_CID = "614cbf79-237a-443e-8ab7-8f3ba0f4ec51"
OTHER_CODE = "NEWC00002"
OTHER_CLIENT_SID = "1d3997d9-c778-490f-8a97-b9d6b53e8d84"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
MODE = cf.TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1
CHAIN = "TELEMATICS-COLD-START-NEWC00001-2026-08"

UTC = timezone.utc
DELAY = 10800
SPAN = 2678400
HEAD = "0" * 39 + "2"

PROVIDER_SECRET_ENV = "TELEMATICS_EXECUTION_PATH_PROVIDER_SECRET"
BUSINESS_SECRET_ENV = "TELEMATICS_EXECUTION_PATH_BUSINESS_SECRET"

BASELINE = datetime(2026, 7, 1, tzinfo=UTC)
SEEDED = datetime(2026, 7, 1, tzinfo=UTC)
EVIDENCE = ""

BUSINESS = SimpleNamespace(
    host="127.0.0.1", port=5432, dbname="clientbiz", user="test",
    password="test",
)


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


def _parse_iso(raw: str) -> datetime:
    text = str(raw)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _parse_business_dsn(dsn: str) -> SimpleNamespace:
    parts = dict(
        piece.split("=", 1) for piece in dsn.split() if "=" in piece
    )
    return SimpleNamespace(
        host=parts.get("host", "127.0.0.1"),
        port=int(parts.get("port", "5432")),
        dbname=parts["dbname"],
        user=parts["user"],
        password=parts.get("password", ""),
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def bootstrap(conn, business_conn) -> None:
    global BASELINE, SEEDED, EVIDENCE

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT date_trunc('hour', now()) AS now")
        db_now = dict(cur.fetchone())["now"].astimezone(UTC)
    conn.rollback()
    BASELINE = db_now - timedelta(days=20)
    SEEDED = db_now - timedelta(hours=1)
    EVIDENCE = (
        "telematics-cold-start-bootstrap/1:sha256=" + ("cd" * 32)
        + ":approval=" + CHAIN + "-1"
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

    with business_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        # The rebuild/swap migrations leave a backup table behind and refuse to
        # rerun while one exists; a disposable database starts from neither.
        cur.execute(
            "DROP TABLE IF EXISTS public.client_trips_legacy_backup_021 CASCADE"
        )
        cur.execute(
            "DROP TABLE IF EXISTS public.client_trips_legacy_backup_020 CASCADE"
        )
        for name in CLIENT_BUSINESS_DDL:
            cur.execute(
                (ROOT / "db/client_business" / name).read_text(encoding="utf-8")
            )
    business_conn.commit()


def reset(
    conn,
    business_conn,
    *,
    mode=MODE,
    schedule_enabled=False,
    event_enrichment_mode="disabled",
    covered_through=None,
    coverage_start=None,
    source="bootstrap",
) -> None:
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
            VALUES (%s,%s,'New Client','telematics','https://example.invalid','u',
                    %s,%s,%s,%s,%s,%s,'public','speeding',true,%s,%s,3600,%s)
            """,
            (CID, CODE, PROVIDER_SECRET_ENV, BUSINESS.host, BUSINESS.port,
             BUSINESS.dbname, BUSINESS.user, BUSINESS_SECRET_ENV, mode,
             DELAY, SPAN),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days, overwrite_existing,
               event_enrichment_mode)
            VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00','UTC',1,true,%s)
            """,
            (SID, CID, CODE, schedule_enabled, event_enrichment_mode),
        )
        # The second real client, so identity refusals are tested against
        # something that exists rather than against an unresolvable id.
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
            VALUES (%s,%s,'Other Client','telematics','https://example.invalid',
                    'u',%s,%s,%s,%s,%s,%s,'public','speeding',true,%s,%s,3600,%s)
            """,
            (OTHER_CID, OTHER_CODE, PROVIDER_SECRET_ENV, BUSINESS.host,
             BUSINESS.port, BUSINESS.dbname, BUSINESS.user,
             BUSINESS_SECRET_ENV, mode, DELAY, SPAN),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days, overwrite_existing,
               event_enrichment_mode)
            VALUES (%s,%s,%s,'trips_sync',false,'daily','02:00','UTC',1,true,%s)
            """,
            (OTHER_CLIENT_SID, OTHER_CID, OTHER_CODE, event_enrichment_mode),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_coverage
              (schedule_id, client_id, client_code, dataset_name,
               coverage_start_ts, covered_through_ts, bootstrap_status,
               bootstrap_evidence_ref, seeded_at, seeded_by,
               covered_through_source, last_gap_detected_ts, updated_at)
            VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY',%s,%s,'operator',%s,
                    NULL,%s)
            """,
            (SID, CID, CODE, coverage_start, covered_through, EVIDENCE,
             SEEDED, source, SEEDED),
        )
    conn.commit()
    with business_conn.cursor() as cur:
        cur.execute("DELETE FROM public.client_trips")
    business_conn.commit()


def coverage(conn) -> dict:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT coverage_start_ts, covered_through_ts,"
            " covered_through_source, bootstrap_status"
            " FROM workflow_a_control.client_dataset_coverage"
            " WHERE schedule_id=%s",
            (SID,),
        )
        row = cur.fetchone()
    conn.rollback()
    return dict(row)


def recovery_rows(conn) -> list:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT recovery_run_id::text AS recovery_run_id, status,"
            " error_classification, error_summary, job_summary"
            " FROM workflow_a_control.client_dataset_recovery_run"
            " ORDER BY created_at"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


def schedule_enabled(conn) -> bool:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT enabled FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id=%s",
            (SID,),
        )
        row = cur.fetchone()
    conn.rollback()
    return bool(row["enabled"])


def trip_count(business_conn) -> int:
    with business_conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT count(*) AS n FROM public.client_trips")
        n = int(dict(cur.fetchone())["n"])
    business_conn.rollback()
    return n


# ---------------------------------------------------------------------------
# The provider — the only thing faked, and only below the gates
# ---------------------------------------------------------------------------

class FakeProvider:
    """Stands in for `TelematicsFleetProviderClient` only.

    Constructed by the job itself, far below the schedule and authority gates,
    so a run that never reaches provider execution never constructs one. The
    suite asserts on `constructed` to prove exactly that.
    """

    constructed = 0

    def __init__(self, *, trips=(), **kwargs):
        type(self).constructed += 1
        self._trips = list(trips)
        self.page_limit = 100
        self.requests = 0

    def metrics_snapshot(self):
        return {
            "total_requests": self.requests,
            "request_count_by_endpoint": {},
            "request_elapsed_seconds_by_endpoint": {},
            "response_parse_elapsed_seconds_by_endpoint": {},
        }

    def fetch_trips(self, *args, **kwargs):
        # The job splits the window into chunks and calls once per chunk. The
        # trips belong to the first chunk only, so the row counts in the
        # terminal record are the distinct trips rather than one set repeated.
        self.requests += 1
        if self.requests > 1:
            return []
        return list(self._trips)

    def fetch_vehicles_fleet(self, *args, **kwargs):
        self.requests += 1
        return []

    def fetch_drivers_fleet(self, *args, **kwargs):
        self.requests += 1
        return []


def make_trips(count: int, *, window_start: datetime) -> list:
    def fmt(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    trips = []
    for index in range(count):
        start = window_start + timedelta(hours=index + 1)
        trips.append({
            "trip_id": 900000 + index,
            "registration": f"TEST{index:03d}",
            "vehicle_id": 1000 + index,
            "start_timestamp": fmt(start),
            "end_timestamp": fmt(start + timedelta(minutes=30)),
            "trip_distance": 12000,
            "trip_duration": 1800,
        })
    return trips


class RecordingClient:
    """The `client` a job receives. Records logs; uploads nothing."""

    def __init__(self):
        self.logs = []

    def log(self, level, kind, source, message, run_id=None, context=None,
            error=None):
        self.logs.append((level, message, dict(context or {})))

    def upload_artifact(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("this job uploads no artifact")

    def messages(self):
        return [message for _, message, _ in self.logs]


# ---------------------------------------------------------------------------
# In-process stand-in for `subprocess.run`, mirroring ops/runner.py
# ---------------------------------------------------------------------------

class InProcessSubprocess:
    """Runs the *real* job in-process under the *real* launcher environment.

    `ops/recover_telematics_trips_window.launch_sync` builds the temporary
    directory, the launch attestation and the execution-record path, then calls
    `subprocess.run`. Replacing only that call keeps every one of those
    behaviors under test while removing the need for a live platform API.

    What it reproduces from `ops/runner.py`: the run-id file handoff, the params
    JSON contract and the exception-to-non-zero-exit mapping. What it does not
    reproduce — `POST /runs`, logging over HTTP — is irrelevant to the coverage
    gate and is separately covered by the runner's own suite.
    """

    PIPE = -1

    def __init__(self, *, dsn, trips=(), after_job=None,
                 business_conn_factory=None, write_run_id_file=True):
        self.dsn = dsn
        self.trips = list(trips)
        self.after_job = after_job
        self.business_conn_factory = business_conn_factory
        # A real `ops/runner.py` always writes this file, but the launcher
        # tolerates its absence (unreadable file, killed process). Suppressing it
        # reproduces the launcher having *no* expected platform-run identity.
        self.write_run_id_file = write_run_id_file
        self.calls = []
        self.observed_env = []
        self.clients = []

    def run(self, cmd, cwd=None, env=None, stdout=None, stderr=None,
            text=None):
        params = json.loads(cmd[3])
        self.calls.append(params)
        self.observed_env.append(dict(env or {}))

        run_id = str(uuid.uuid4())
        if self.write_run_id_file:
            run_id_path = Path(env["LOG_PLATFORM_RUN_ID_FILE"])
            run_id_path.parent.mkdir(parents=True, exist_ok=True)
            run_id_path.write_text(run_id + "\n", encoding="utf-8")

        recording = RecordingClient()
        self.clients.append(recording)

        original_environ = dict(os.environ)
        original_provider = sync.TelematicsFleetProviderClient
        original_business = sync._client_business_pg_conn
        provider_trips = self.trips
        os.environ.clear()
        os.environ.update(env or {})
        sync.TelematicsFleetProviderClient = (
            lambda **kwargs: FakeProvider(trips=provider_trips, **kwargs)
        )
        if self.business_conn_factory is not None:
            sync._client_business_pg_conn = self.business_conn_factory
        status = "SUCCESS"
        returncode = 0
        stderr_text = ""
        try:
            sync.run(client=recording, run_id=run_id, params=params)
        except BaseException as exc:  # mirrors runner + run_context
            status = "FAILED"
            returncode = 1
            stderr_text = f"{type(exc).__name__}: {exc}"
        finally:
            sync.TelematicsFleetProviderClient = original_provider
            sync._client_business_pg_conn = original_business
            os.environ.clear()
            os.environ.update(original_environ)

        # The real runner registers the platform run through the API; here the
        # row is written directly so the chain correspondence still holds.
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True) as other:
            other.execute(
                "INSERT INTO public.runs (run_id, status, params)"
                " VALUES (%s,%s,%s::jsonb)",
                (run_id, status, json.dumps(
                    {"client_id": CID, "client_code": CODE}
                )),
            )
        if self.after_job is not None:
            self.after_job(Path(env[eo.EXECUTION_OUTCOME_FILE_ENV]))
        return SimpleNamespace(
            returncode=returncode, stdout="", stderr=stderr_text,
        )


# ---------------------------------------------------------------------------
# Driving the launcher
# ---------------------------------------------------------------------------

def args(conn, dsn, *, execute=False, ordinal=1, window_start=None,
         window_end=None, **overrides):
    from ops import telematics_cold_start_chain as ccsc

    current = coverage(conn)
    start = window_start or current["covered_through_ts"].astimezone(UTC)
    end = window_end or _safe_end(conn)
    base = [
        "--client-code", CODE,
        "--dataset", "trips_sync",
        "--window-start", rc._iso(start),
        "--window-end", rc._iso(end),
        "--expected-old-covered-through", rc._iso(start),
        "--reason", "execution-path integration test",
        "--approval-ref", ccsc.window_approval_ref(CHAIN, ordinal),
        "--expected-environment", "production",
        "--expected-platform-uuid", PLATFORM_UUID,
        "--dsn", dsn,
        "--allow-disabled-schedule-for-cold-start",
        "--confirm-schedule-disabled",
        "--expected-schedule-id", SID,
        "--expected-coverage-fingerprint", fingerprint(conn),
        "--approved-shifted-cutoff-boundary", rc._iso(end),
        "--approved-final-chain-boundary", rc._iso(end),
        "--cold-start-chain-ref", CHAIN,
    ]
    if execute:
        base += ["--execute", "--confirm-client-code", CODE]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if flag in base:
            base[base.index(flag) + 1] = value
        else:
            base += [flag, value]
    return rc.build_parser().parse_args(base)


def enabled_schedule_args(conn, dsn, *, execute=False):
    """The historical enabled-schedule recovery invocation, unchanged."""
    current = coverage(conn)
    start = current["covered_through_ts"].astimezone(UTC)
    base = [
        "--client-code", CODE,
        "--dataset", "trips_sync",
        "--window-start", rc._iso(start),
        "--window-end", rc._iso(_safe_end(conn)),
        "--expected-old-covered-through", rc._iso(start),
        "--reason", "enabled-schedule recovery",
        "--approval-ref", "TELEMATICS-C11-NEWC00001-1",
        "--expected-environment", "production",
        "--expected-platform-uuid", PLATFORM_UUID,
        "--dsn", dsn,
    ]
    if execute:
        base += ["--execute", "--confirm-client-code", CODE]
    return rc.build_parser().parse_args(base)


def _safe_end(conn) -> datetime:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT date_trunc('second', now()) AS now")
        now = dict(cur.fetchone())["now"].astimezone(UTC)
    conn.rollback()
    return now - timedelta(seconds=DELAY + 60)


def fingerprint(conn) -> str:
    with conn.cursor(row_factory=_dict_row()) as cur:
        row = cf.read_coverage_row(cur, schedule_id=SID)
    conn.rollback()
    return cf.coverage_fingerprint(row)


def drive(conn, dsn, runner, *, arg_builder=None, **kwargs):
    """Run the launcher with the real code path and an in-process subprocess.

    The platform and secret variables are placed in this process's environment
    rather than injected further down, because `launch_sync` builds the child
    environment with `os.environ.copy()` — so this is the same inheritance the
    real subprocess relies on, and it stays under test.
    """
    original_sub, original_repo = rc.subprocess, rc.repository_state
    original_environ = dict(os.environ)
    rc.subprocess = runner
    rc.repository_state = lambda *, require_clean: {
        "repository_head": HEAD, "worktree_clean": True,
    }
    os.environ.update(_job_environment(dsn))
    try:
        builder = arg_builder or args
        return rc.run(builder(conn, dsn, **kwargs))
    finally:
        rc.subprocess = original_sub
        rc.repository_state = original_repo
        os.environ.clear()
        os.environ.update(original_environ)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def test_valid_authority_passes_the_disabled_schedule_guard(
    conn, business_conn, dsn,
) -> None:
    """The whole point: a genuine recovery runs while the schedule stays off."""
    reset(conn, business_conn)
    FakeProvider.constructed = 0
    runner = InProcessSubprocess(dsn=dsn, trips=make_trips(3, window_start=BASELINE))
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_OK, plan
    assert plan["recovery_status"] == "SUCCESS", plan
    assert plan["coverage_advance_permitted"] is True, plan
    assert plan["execution_outcome_verified"] is True, plan
    assert plan["execution_outcome"]["outcome"] == "EXECUTED_COMMITTED", plan
    assert plan["execution_outcome"]["transaction_status"] == "COMMITTED", plan
    assert plan["execution_outcome"]["upserted_count"] == 3, plan
    # The job really did enter provider execution and really did commit rows.
    assert FakeProvider.constructed == 1
    assert trip_count(business_conn) == 3
    # Coverage advanced, and the schedule is still disabled.
    assert coverage(conn)["covered_through_source"] == "manual_recovery"
    assert schedule_enabled(conn) is False
    # The attestation reached the subprocess environment, and the parameters
    # carried the explicit opt-in.
    env = runner.observed_env[0]
    assert mra.MANUAL_RECOVERY_AUTHORITY_ENV in env
    assert runner.calls[0][mra.PARAM_DISABLED_SCHEDULE_FLAG] is True
    assert runner.calls[0][mra.PARAM_EXPECTED_SCHEDULE_ID] == SID
    # And the job logged that it executed under authority rather than skipping.
    messages = runner.clients[0].messages()
    assert any("validated manual-recovery authority" in m for m in messages), messages
    assert not any("skipping run" in m for m in messages), messages


def test_zero_row_committed_execution_advances_coverage(
    conn, business_conn, dsn,
) -> None:
    """A window the provider had no trips for is still committed work."""
    reset(conn, business_conn)
    runner = InProcessSubprocess(dsn=dsn, trips=[])
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_OK, plan
    assert plan["execution_outcome"]["outcome"] == "EXECUTED_ZERO_ROWS_COMMITTED"
    assert plan["execution_outcome"]["upserted_count"] == 0
    assert plan["execution_outcome"]["transaction_status"] == "COMMITTED"
    assert plan["coverage_advanced"] is True, plan
    assert trip_count(business_conn) == 0
    assert coverage(conn)["covered_through_source"] == "manual_recovery"


def test_disabled_schedule_without_authority_skips(
    conn, business_conn, dsn,
) -> None:
    """The unchanged half of the contract, proved through the real guard."""
    reset(conn, business_conn)
    FakeProvider.constructed = 0
    runner = InProcessSubprocess(dsn=dsn, trips=make_trips(2, window_start=BASELINE))
    recording = RecordingClient()

    outcome_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"eo-{uuid.uuid4()}.json"
    original_environ = dict(os.environ)
    original_provider = sync.TelematicsFleetProviderClient
    try:
        os.environ.update(_job_environment(dsn))
        os.environ[eo.EXECUTION_OUTCOME_FILE_ENV] = str(outcome_path)
        os.environ.pop(mra.MANUAL_RECOVERY_AUTHORITY_ENV, None)
        sync.TelematicsFleetProviderClient = (
            lambda **kwargs: FakeProvider(trips=[], **kwargs)
        )
        sync.run(
            client=recording, run_id=str(uuid.uuid4()),
            params={
                "client_id": CID,
                "client_code": CODE,
                "window_start_ts": rc._iso(BASELINE),
                "window_end_ts": rc._iso(_safe_end(conn)),
            },
        )
    finally:
        sync.TelematicsFleetProviderClient = original_provider
        os.environ.clear()
        os.environ.update(original_environ)

    record = eo.read_outcome(outcome_path)
    outcome_path.unlink()
    assert record.outcome == eo.OUTCOME_SKIPPED_DISABLED_SCHEDULE
    assert record.skipped is True
    assert record.skip_reason == eo.SKIP_REASON_DISABLED_SCHEDULE
    assert record.provider_execution_entered is False
    assert record.business_transaction_entered is False
    assert record.transaction_status == eo.TRANSACTION_NOT_ENTERED
    assert record.prepared_count == 0 and record.upserted_count == 0
    # Zero provider work: the client was never even constructed.
    assert FakeProvider.constructed == 0
    assert trip_count(business_conn) == 0
    assert any("skipping run" in m for m in recording.messages())


def test_returncode_zero_plus_skipped_outcome_does_not_advance_coverage(
    conn, business_conn, dsn,
) -> None:
    """The exact ECHO00001 sequence, replayed against the fixed launcher.

    The pre-hardening launcher is reproduced faithfully: no disabled-schedule
    opt-in, no launch attestation, no recovery-run parameter — the shape that
    used to reach the guard and be counted as a success. The job hits its real
    guard, skips, and exits 0; the coverage gate now refuses anyway.

    A partly-formed authority — the flag or the trigger without the rest — is a
    different case and fails the run outright rather than skipping; that is
    covered by `test_a_bare_flag_without_a_recovery_row_refuses`.
    """
    reset(conn, business_conn)
    FakeProvider.constructed = 0
    runner = InProcessSubprocess(dsn=dsn, trips=make_trips(5, window_start=BASELINE))
    original_build = rc.build_job_params

    def legacy_params(**kwargs):
        params = original_build(**{**kwargs, "schedule_disabled": False})
        # Exactly what the historical launcher sent to a disabled schedule.
        params["trigger"] = "MANUAL"
        params.pop("manual_recovery_run_id", None)
        return params

    original_launch = rc.launch_sync

    def launch_without_attestation(*, job_params, authority=None):
        # The pre-hardening launcher had no attestation to pass at all.
        return original_launch(job_params=job_params, authority=None)

    rc.build_job_params = legacy_params
    rc.launch_sync = launch_without_attestation
    try:
        exit_code, plan = drive(conn, dsn, runner, execute=True)
    finally:
        rc.build_job_params = original_build
        rc.launch_sync = original_launch

    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["business_returncode"] == 0, plan
    assert plan["recovery_status"] == "FAILED", plan
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
    assert plan["coverage_advanced"] is False, plan
    assert plan["coverage_unchanged"] is True, plan
    record = plan["execution_outcome"]
    assert record["outcome"] == "SKIPPED_DISABLED_SCHEDULE", record
    assert record["skipped"] is True, record
    assert record["provider_execution_entered"] is False, record
    assert record["business_transaction_entered"] is False, record
    assert record["prepared_count"] == 0 and record["upserted_count"] == 0
    # Nothing happened anywhere: no provider, no rows, no watermark move.
    assert FakeProvider.constructed == 0
    assert trip_count(business_conn) == 0
    assert coverage(conn)["covered_through_source"] == "bootstrap"
    assert coverage(conn)["covered_through_ts"].astimezone(UTC) == BASELINE
    assert schedule_enabled(conn) is False

    # The same genuine skip record, this time with identities that match the
    # recovery exactly, so the refusal is provably about the *skip* and not
    # about an identity mismatch that happened to fire first.
    rows = recovery_rows(conn)
    assert len(rows) == 1, rows
    recovery_run_id = rows[0]["recovery_run_id"]
    aligned = dict(record)
    aligned["recovery_run_id"] = recovery_run_id
    verdict = rc.evaluate_execution_evidence(
        result={
            "returncode": 0,
            "platform_run_id": None,
            "execution_outcome": aligned,
            "execution_outcome_error": None,
            "stderr_tail": "",
        },
        orchestration_error=None,
        client_id=CID,
        client_code=CODE,
        schedule_id=SID,
        dataset_name="trips_sync",
        recovery_run_id=recovery_run_id,
        window_start_ts=_parse_iso(aligned["requested_window_start_ts"]),
        window_end_ts=_parse_iso(aligned["requested_window_end_ts"]),
    )
    assert verdict["returncode_is_zero"] is True, verdict
    assert verdict["execution_outcome_verified"] is True, verdict
    assert verdict["coverage_advance_permitted"] is False, verdict
    assert verdict["refusal_code"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED, verdict
    assert "schedule was disabled" in verdict["refusal_detail"], verdict


def test_returncode_zero_without_a_record_does_not_advance_coverage(
    conn, business_conn, dsn,
) -> None:
    """An absent terminal record fails closed, whatever the exit code says."""
    reset(conn, business_conn)
    runner = InProcessSubprocess(
        dsn=dsn, trips=make_trips(2, window_start=BASELINE),
        after_job=lambda path: path.unlink(missing_ok=True),
    )
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["business_returncode"] == 0, plan
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
    assert plan["execution_outcome"] is None, plan
    assert plan["coverage_advanced"] is False, plan
    assert coverage(conn)["covered_through_source"] == "bootstrap"


def test_a_malformed_record_does_not_advance_coverage(
    conn, business_conn, dsn,
) -> None:
    reset(conn, business_conn)

    def corrupt(path: Path) -> None:
        path.write_text("{not json", encoding="utf-8")

    runner = InProcessSubprocess(
        dsn=dsn, trips=make_trips(1, window_start=BASELINE), after_job=corrupt,
    )
    exit_code, plan = drive(conn, dsn, runner, execute=True)
    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
    assert plan["coverage_advanced"] is False, plan
    assert coverage(conn)["covered_through_source"] == "bootstrap"


def test_a_mismatched_record_never_advances_coverage(
    conn, business_conn, dsn,
) -> None:
    """A well-formed record describing a different execution is refused."""
    for field, value in (
        ("client_id", "e8d95748-107a-4eb9-8f7f-ab2901c84c97"),
        ("schedule_id", OTHER_SID),
        ("recovery_run_id", "88888888-8888-4888-8888-888888888888"),
        ("dataset_name", "fuel_daily_aggregation"),
        ("requested_window_end_ts", "2020-01-01T00:00:00Z"),
    ):
        reset(conn, business_conn)

        def tamper(path: Path, field=field, value=value) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload[field] = value
            path.write_text(json.dumps(payload), encoding="utf-8")

        runner = InProcessSubprocess(
            dsn=dsn, trips=make_trips(1, window_start=BASELINE),
            after_job=tamper,
        )
        exit_code, plan = drive(conn, dsn, runner, execute=True)
        assert exit_code == rc.EXIT_BUSINESS_FAILED, (field, plan)
        assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
        assert plan["coverage_advanced"] is False, (field, plan)
        assert coverage(conn)["covered_through_source"] == "bootstrap", field


def test_returncode_zero_without_platform_run_identity_does_not_advance_coverage(
    conn, business_conn, dsn,
) -> None:
    """rc=0 plus a genuine committed record is still refused with no identity.

    The launcher's expected platform-run id is absent whenever the run-id file
    never appeared. Previously the comparison was skipped when either side was
    falsy, so a coverage-eligible record sailed through unverified.
    """
    reset(conn, business_conn)
    runner = InProcessSubprocess(
        dsn=dsn, trips=make_trips(2, window_start=BASELINE),
        write_run_id_file=False,
    )
    exit_code, plan = drive(conn, dsn, runner, execute=True)
    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
    assert plan["coverage_advanced"] is False, plan
    # The durable recovery row must say *why*, so an operator reading it later
    # sees the identity failure rather than a bare classification.
    rows = recovery_rows(conn)
    assert len(rows) == 1, rows
    assert "platform_run_id" in str(rows[0]["error_summary"]), rows
    assert coverage(conn)["covered_through_source"] == "bootstrap"


def test_a_record_without_a_usable_platform_run_id_never_advances_coverage(
    conn, business_conn, dsn,
) -> None:
    """Absent, empty, malformed and foreign identities each refuse on their own."""
    for value in (None, "", "   ", "not-a-uuid",
                  "f2da1f95-33d4-4ea3-8e5d-c22bbd7e2357"):
        reset(conn, business_conn)

        def tamper(path: Path, value=value) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["platform_run_id"] = value
            path.write_text(json.dumps(payload), encoding="utf-8")

        runner = InProcessSubprocess(
            dsn=dsn, trips=make_trips(2, window_start=BASELINE),
            after_job=tamper,
        )
        exit_code, plan = drive(conn, dsn, runner, execute=True)
        assert exit_code == rc.EXIT_BUSINESS_FAILED, (value, plan)
        assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
        assert plan["coverage_advanced"] is False, (value, plan)
        assert coverage(conn)["covered_through_source"] == "bootstrap", value


def test_an_exactly_matching_platform_run_identity_advances_coverage(
    conn, business_conn, dsn,
) -> None:
    """The permitted case, so the new requirement is not vacuously strict."""
    reset(conn, business_conn)
    runner = InProcessSubprocess(dsn=dsn, trips=make_trips(2, window_start=BASELINE))
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_OK, plan
    assert plan["coverage_advanced"] is True, plan
    # The record's identity is exactly the platform run the launcher observed.
    assert plan["execution_outcome"]["platform_run_id"] == plan["platform_run_id"]
    assert uuid.UUID(plan["platform_run_id"])
    assert coverage(conn)["covered_through_source"] == "manual_recovery"


def test_an_uncommitted_transaction_never_advances_coverage(
    conn, business_conn, dsn,
) -> None:
    """A business transaction that is entered but never commits is not success."""
    reset(conn, business_conn)

    class FailingCommitConnection:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self, *a, **kw):
            return self._inner.cursor(*a, **kw)

        def commit(self):
            raise RuntimeError("business commit refused by the test")

        def rollback(self):
            return self._inner.rollback()

        def close(self):
            return self._inner.close()

    import psycopg

    business_dsn = _business_dsn()

    def failing_factory(cfg):
        return FailingCommitConnection(psycopg.connect(business_dsn))

    runner = InProcessSubprocess(
        dsn=dsn, trips=make_trips(2, window_start=BASELINE),
        business_conn_factory=failing_factory,
    )
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["business_returncode"] == 1, plan
    assert plan["execution_outcome"]["outcome"] == "FAILED", plan
    assert plan["execution_outcome"]["business_transaction_entered"] is True
    assert plan["execution_outcome"]["transaction_status"] == "NOT_COMMITTED"
    assert plan["coverage_advanced"] is False, plan
    assert trip_count(business_conn) == 0
    assert coverage(conn)["covered_through_source"] == "bootstrap"


# --- authority refusals ----------------------------------------------------

def _job_environment(dsn: str) -> dict:
    parts = dict(piece.split("=", 1) for piece in dsn.split() if "=" in piece)
    return {
        "POSTGRES_HOST": parts.get("host", "127.0.0.1"),
        "POSTGRES_PORT": parts.get("port", "5432"),
        "POSTGRES_DB": parts["dbname"],
        "POSTGRES_USER": parts["user"],
        "POSTGRES_PASSWORD": parts.get("password", ""),
        PROVIDER_SECRET_ENV: "provider-secret",
        BUSINESS_SECRET_ENV: BUSINESS.password,
    }


def _business_dsn() -> str:
    return (
        f"host={BUSINESS.host} port={BUSINESS.port} dbname={BUSINESS.dbname} "
        f"user={BUSINESS.user} password={BUSINESS.password}"
    )


def _claim_recovery_row(conn, *, window_start, window_end, status="RUNNING",
                        client_id=CID, schedule_id=SID,
                        dataset_name="trips_sync",
                        approval_ref=None) -> str:
    # `approval_ref` is overridable so one test can seed several rows for the
    # same target window; the approved-window uniqueness constraint is keyed on
    # it, and reproducing a *distinct* approval is the honest way to do that.
    approval_ref = approval_ref or (CHAIN + "-W01")
    recovery_run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_recovery_run
              (recovery_run_id, client_id, client_code, schedule_id,
               dataset_name, window_start_ts, window_end_ts,
               expected_old_covered_through_ts, status, reason, approval_ref,
               repository_head, pagination_mode, stabilization_delay_seconds,
               overlap_seconds, max_recovery_span_seconds,
               initial_coverage_snapshot, initial_coverage_fingerprint,
               created_at, started_at, finished_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'authority test',%s,%s,
                    'data_invariants_v1',%s,3600,%s,'{}'::jsonb,%s,
                    now(),now(),
                    CASE WHEN %s THEN now() ELSE NULL END, now())
            """,
            (recovery_run_id, client_id, CODE, schedule_id, dataset_name,
             window_start, window_end, window_start, status,
             approval_ref, HEAD, DELAY, SPAN, "a" * 64,
             status in ("SUCCESS", "FAILED", "FINALIZATION_CONFLICT")),
        )
    conn.commit()
    return recovery_run_id


def _invoke_job_directly(conn, dsn, *, params, attestation=None):
    """Call the real job with the real guard, exactly as an operator could."""
    recording = RecordingClient()
    outcome_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"eo-{uuid.uuid4()}.json"
    original_environ = dict(os.environ)
    original_provider = sync.TelematicsFleetProviderClient
    try:
        os.environ.update(_job_environment(dsn))
        os.environ[eo.EXECUTION_OUTCOME_FILE_ENV] = str(outcome_path)
        os.environ.pop(mra.MANUAL_RECOVERY_AUTHORITY_ENV, None)
        if attestation is not None:
            os.environ[mra.MANUAL_RECOVERY_AUTHORITY_ENV] = attestation
        sync.TelematicsFleetProviderClient = (
            lambda **kwargs: FakeProvider(trips=[], **kwargs)
        )
        sync.run(client=recording, run_id=str(uuid.uuid4()), params=params)
        return None
    except mra.ManualRecoveryAuthorityError as exc:
        return exc
    finally:
        sync.TelematicsFleetProviderClient = original_provider
        os.environ.clear()
        os.environ.update(original_environ)
        outcome_path.unlink(missing_ok=True)


def _authority_params(conn, *, recovery_run_id, window_start, window_end,
                      client_id=CID, schedule_id=SID, **overrides):
    params = {
        "client_id": client_id,
        "client_code": CODE,
        "trigger": mra.MANUAL_RECOVERY_TRIGGER,
        "window_start_ts": rc._iso(window_start),
        "window_end_ts": rc._iso(window_end),
        "trips_pagination_mode": MODE,
        "manual_recovery_run_id": recovery_run_id,
        mra.PARAM_DISABLED_SCHEDULE_FLAG: True,
        mra.PARAM_EXPECTED_SCHEDULE_ID: schedule_id,
    }
    params.update(overrides)
    return params


def _attestation(*, recovery_run_id, window_start, window_end,
                 client_id=CID, schedule_id=SID, dataset_name="trips_sync"):
    return mra.build_launch_attestation(
        client_id=client_id, client_code=CODE, schedule_id=schedule_id,
        dataset_name=dataset_name, recovery_run_id=recovery_run_id,
        window_start_ts=window_start, window_end_ts=window_end,
    )


def test_a_bare_flag_without_a_recovery_row_refuses(
    conn, business_conn, dsn,
) -> None:
    """Parameters and an attestation are not enough without the durable row."""
    reset(conn, business_conn)
    start, end = BASELINE, _safe_end(conn)
    orphan = str(uuid.uuid4())
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=orphan, window_start=start, window_end=end,
        ),
        attestation=_attestation(
            recovery_run_id=orphan, window_start=start, window_end=end,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_RECOVERY", exc

    # And a truly bare boolean, with neither attestation nor recovery row.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=orphan, window_start=start, window_end=end,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_ATTESTATION", exc


def test_a_direct_job_invocation_cannot_use_the_authority(
    conn, business_conn, dsn,
) -> None:
    """A genuine RUNNING recovery row still does not authorize a hand-typed run.

    The row exists and every parameter is correct; only the launch attestation
    is missing, which is exactly the difference between the reviewed path and
    an operator typing a runner command.
    """
    reset(conn, business_conn)
    start, end = BASELINE, _safe_end(conn)
    recovery_run_id = _claim_recovery_row(
        conn, window_start=start, window_end=end,
    )
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_ATTESTATION", exc
    assert trip_count(business_conn) == 0

    # The same run *with* the attestation is accepted, so the refusal above is
    # about the attestation and nothing else.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start, window_end=end,
        ),
    )
    assert exc is None, exc


def test_wrong_recovery_uuid_client_schedule_or_window_refuses(
    conn, business_conn, dsn,
) -> None:
    """Each identity mismatch refuses on its own."""
    reset(conn, business_conn)
    start, end = BASELINE, _safe_end(conn)
    recovery_run_id = _claim_recovery_row(
        conn, window_start=start, window_end=end,
    )
    other_recovery = str(uuid.uuid4())

    # Wrong recovery UUID: attestation and parameters agree, but no such row.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=other_recovery,
            window_start=start, window_end=end,
        ),
        attestation=_attestation(
            recovery_run_id=other_recovery, window_start=start, window_end=end,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_RECOVERY", exc

    # Wrong client: a second client that really exists, whose own schedule is
    # also disabled, so the only thing wrong is that the recovery row belongs
    # to somebody else.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
            client_id=OTHER_CID, schedule_id=OTHER_CLIENT_SID,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start,
            window_end=end, client_id=OTHER_CID,
            schedule_id=OTHER_CLIENT_SID,
        ),
    )
    assert exc is not None, "a wrong client_id was accepted"
    assert exc.code == "AUTHORITY_REFUSED_RECOVERY_IDENTITY", exc

    # Wrong schedule: a second, unrelated schedule id.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end, schedule_id=OTHER_SID,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start,
            window_end=end, schedule_id=OTHER_SID,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_SCHEDULE", exc

    # Wrong window: the row was approved for a different interval.
    shifted = end - timedelta(hours=1)
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=shifted,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start,
            window_end=shifted,
        ),
    )
    assert exc is not None
    assert exc.code == "AUTHORITY_REFUSED_RECOVERY_WINDOW", exc

    # An attestation that disagrees with the parameters is refused before any
    # database read at all.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start,
            window_end=shifted,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_WINDOW", exc


def test_a_terminal_recovery_row_authorizes_nothing(
    conn, business_conn, dsn,
) -> None:
    reset(conn, business_conn)
    start, end = BASELINE, _safe_end(conn)
    recovery_run_id = _claim_recovery_row(
        conn, window_start=start, window_end=end, status="SUCCESS",
    )
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
        ),
        attestation=_attestation(
            recovery_run_id=recovery_run_id, window_start=start, window_end=end,
        ),
    )
    assert exc is not None and exc.code == "AUTHORITY_REFUSED_RECOVERY_STATE", exc


def test_an_enabled_schedule_refuses_the_authority(
    conn, business_conn, dsn,
) -> None:
    """The authority is for a disabled schedule and nothing else."""
    reset(conn, business_conn, schedule_enabled=True)
    start, end = BASELINE, _safe_end(conn)
    recovery_run_id = _claim_recovery_row(
        conn, window_start=start, window_end=end,
    )
    # An enabled schedule never reaches the guard at all: the job simply runs.
    exc = _invoke_job_directly(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=recovery_run_id,
            window_start=start, window_end=end,
        ),
    )
    assert exc is None, exc
    assert trip_count(business_conn) == 0  # the fake provider returned no trips


# --- the dispatcher --------------------------------------------------------

def test_the_dispatcher_cannot_build_or_inherit_the_authority() -> None:
    """Two independent guards, tested independently."""
    from jobs.api.telematics import dispatcher

    # 1) It cannot construct authority parameters.
    params = dispatcher._build_job_params(
        client_id=CID, client_code=CODE, dataset_name="trips_sync",
        event_enrichment_mode="enabled",
        window_start_ts=BASELINE, window_end_ts=BASELINE + timedelta(days=1),
    )
    assert params["trigger"] == "SCHEDULED"
    for key in mra.AUTHORITY_PARAM_KEYS:
        assert key not in params, key
    # The ordinary trips parameters are exactly what they were before the guard
    # moved onto the shared exit: nothing added, nothing reordered.
    assert params == {
        "client_id": CID,
        "trigger": "SCHEDULED",
        "client_code": CODE,
        "window_start_ts": "2026-07-01T00:00:00Z",
        "window_end_ts": "2026-07-02T00:00:00Z",
        "event_enrichment_mode": "enabled",
    }, params

    # And the deny-by-default check fires if a future edit adds one.
    try:
        mra.reject_authority_params(
            {**params, mra.PARAM_DISABLED_SCHEDULE_FLAG: True},
            surface="test",
        )
    except ValueError as exc:
        assert mra.PARAM_DISABLED_SCHEDULE_FLAG in str(exc)
    else:
        raise AssertionError("the dispatcher parameter guard did not fire")

    # 2) It cannot inherit an attestation from its own environment.
    captured = {}

    class CapturingPopen:
        def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None,
                     text=None):
            captured["env"] = dict(env or {})
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    original_popen = dispatcher.subprocess.Popen
    original_environ = dict(os.environ)
    dispatcher.subprocess.Popen = CapturingPopen
    try:
        os.environ[mra.MANUAL_RECOVERY_AUTHORITY_ENV] = "{}"
        os.environ[eo.EXECUTION_OUTCOME_FILE_ENV] = "/tmp/should-not-leak.json"
        dispatcher._launch_job(
            job_module="jobs.api.telematics.sync_trips_and_speeding",
            job_params=params,
            log_fn=lambda *a, **k: None,
        )
    finally:
        dispatcher.subprocess.Popen = original_popen
        os.environ.clear()
        os.environ.update(original_environ)

    assert mra.MANUAL_RECOVERY_AUTHORITY_ENV not in captured["env"]
    assert eo.EXECUTION_OUTCOME_FILE_ENV not in captured["env"]

    # 3) When the dispatcher *does* collect a terminal record (M3, compatibility
    #    trips fire), the child is still never handed the inherited path. It gets
    #    a fresh per-launch file, and that file is gone once the launch returns —
    #    so no record can outlive its own launch and be read by a later one.
    captured.clear()
    original_environ = dict(os.environ)
    dispatcher.subprocess.Popen = CapturingPopen
    try:
        os.environ[eo.EXECUTION_OUTCOME_FILE_ENV] = "/tmp/should-not-leak.json"
        _, _, _, _, collected = dispatcher._launch_job(
            job_module="jobs.api.telematics.sync_trips_and_speeding",
            job_params=params,
            log_fn=lambda *a, **k: None,
            collect_execution_outcome=True,
        )
    finally:
        dispatcher.subprocess.Popen = original_popen
        os.environ.clear()
        os.environ.update(original_environ)

    child_path = captured["env"].get(eo.EXECUTION_OUTCOME_FILE_ENV)
    assert child_path and child_path != "/tmp/should-not-leak.json", child_path
    assert not Path(child_path).exists(), "per-launch record outlived its launch"
    # This fake child wrote nothing, so the launcher reports absence as data
    # rather than raising — the refusal decision belongs to `run_prepared`.
    assert collected.requested is True
    assert collected.outcome is None
    assert collected.error is not None
    assert collected.error.code == "EXECUTION_OUTCOME_ABSENT"


# --- the dispatcher: every dataset branch, not just trips ------------------
#
# The Eco Driving branch used to return before the deny-by-default parameter
# guard ran, so the guard's own comment — and the contract in
# `manual_recovery_authority` — were true only for the datasets that happened to
# fall through to it. The three tests below pin the corrected shape: the ordinary
# Eco parameters are unchanged, a future Eco mode entry cannot smuggle an
# authority parameter past the guard, and *every* successful return is guarded.

#: The Eco parameters each dataset must keep producing, spelled out here rather
#: than read back from `ECO_DRIVING_SCHEDULE_MODES`, so a silent edit to that
#: table is caught by this suite instead of being mirrored by it.
EXPECTED_ECO_MODES = {
    "eco_driving_weekly_snapshot": {
        "mode": "weekly_cumulative_snapshot",
        "include_weekly": True, "include_monthly": False,
    },
    "eco_driving_month_end_weekly_snapshot": {
        "mode": "final_month_weekly_snapshot",
        "include_weekly": True, "include_monthly": False,
    },
    "eco_driving_monthly_aggregation": {
        "mode": "monthly_full_aggregation",
        "include_weekly": False, "include_monthly": True,
    },
    "eco_person_driving_weekly_snapshot": {
        "mode": "weekly_cumulative_snapshot",
        "include_weekly": True, "include_monthly": False,
    },
    "eco_person_driving_month_end_weekly_snapshot": {
        "mode": "final_month_weekly_snapshot",
        "include_weekly": True, "include_monthly": False,
    },
    "eco_person_driving_monthly_aggregation": {
        "mode": "monthly_full_aggregation",
        "include_weekly": False, "include_monthly": True,
    },
    "eco_person_driving_weekly_email_notifications": {},
    "eco_person_driving_monthly_email_notifications": {},
}

ECO_WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
ECO_WINDOW_END = datetime(2026, 7, 8, tzinfo=UTC)


def _eco_build_kwargs() -> dict:
    return dict(
        client_id=CID, client_code=CODE, event_enrichment_mode="enabled",
        window_start_ts=ECO_WINDOW_START, window_end_ts=ECO_WINDOW_END,
    )


def test_every_eco_driving_dataset_still_builds_its_exact_parameters() -> None:
    """The guard is now on the Eco path, and changes nothing it produces."""
    from jobs.api.telematics import dispatcher

    assert set(dispatcher.ECO_DRIVING_SCHEDULE_MODES) == set(
        EXPECTED_ECO_MODES
    ), "the Eco dataset set changed; extend EXPECTED_ECO_MODES deliberately"

    for dataset_name, expected_mode in EXPECTED_ECO_MODES.items():
        params = dispatcher._build_job_params(
            dataset_name=dataset_name, **_eco_build_kwargs()
        )
        # The complete parameter set, asserted exactly — nothing added, nothing
        # dropped by routing this branch through the common guard.
        assert params == {
            "client_id": CID,
            "trigger": "SCHEDULED",
            "client_code": CODE,
            **expected_mode,
            "scheduled_fire_ts": "2026-07-08T00:00:00Z",
        }, (dataset_name, params)
        assert params["trigger"] == "SCHEDULED", dataset_name
        assert params["scheduled_fire_ts"] == "2026-07-08T00:00:00Z", dataset_name
        # An Eco fire carries a mode, never a window.
        assert "window_start_ts" not in params, dataset_name
        assert "window_end_ts" not in params, dataset_name
        for key in mra.AUTHORITY_PARAM_KEYS:
            assert key not in params, (dataset_name, key)


def test_no_eco_driving_mode_entry_can_emit_authority_parameters() -> None:
    """The future-edit case the guard exists for, on the Eco branch.

    `ECO_DRIVING_SCHEDULE_MODES` is module data that is merged wholesale into the
    dispatcher's parameters, so it is exactly the surface a later edit could add
    an authority key through. Every current entry is probed with every prohibited
    key; the central guard must refuse each one, and no prohibited parameter may
    ever be returned.
    """
    from jobs.api.telematics import dispatcher

    probes = {
        mra.PARAM_DISABLED_SCHEDULE_FLAG: True,
        mra.PARAM_RECOVERY_RUN_ID: "d1f40c04-514c-4b07-8fb3-701cf4112906",
        mra.PARAM_EXPECTED_SCHEDULE_ID: SID,
    }
    # The two keys the review named explicitly are covered by the loop below.
    assert mra.PARAM_DISABLED_SCHEDULE_FLAG in probes
    assert mra.PARAM_RECOVERY_RUN_ID in probes

    for dataset_name in dispatcher.ECO_DRIVING_SCHEDULE_MODES:
        for key, value in probes.items():
            original = dict(dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name])
            try:
                dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name][key] = value
                try:
                    returned = dispatcher._build_job_params(
                        dataset_name=dataset_name, **_eco_build_kwargs()
                    )
                except ValueError as exc:
                    # Refused by the central guard, naming the offending key and
                    # the dispatcher surface.
                    assert key in str(exc), (dataset_name, key, exc)
                    assert "_build_job_params" in str(exc), (dataset_name, exc)
                else:
                    raise AssertionError(
                        "the dispatcher returned parameters for "
                        f"{dataset_name} carrying {key}: {returned}"
                    )
            finally:
                dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name] = original

    # The injection is fully undone: the ordinary parameters build again.
    test_every_eco_driving_dataset_still_builds_its_exact_parameters()


def test_no_eco_driving_mode_entry_can_force_a_manual_trigger() -> None:
    """`trigger` is an authority surface too, and it is module-data reachable.

    `ECO_DRIVING_SCHEDULE_MODES` is merged wholesale over the parameter dict
    *after* `trigger` has been set to `SCHEDULED`, so an entry carrying
    `trigger` would overwrite the canonical value rather than be ignored by it.
    The earlier injection coverage probed only the three authority *parameter*
    keys, so this exact route was untested.

    The invariant asserted here is the strong one: a dispatcher-produced
    parameter set either carries `trigger == SCHEDULED` or does not exist. A
    module-data edit must never be able to turn a scheduled Eco job into a
    manual recovery.
    """
    from jobs.api.telematics import dispatcher

    for dataset_name in dispatcher.ECO_DRIVING_SCHEDULE_MODES:
        original = dict(dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name])
        try:
            dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name]["trigger"] = (
                mra.MANUAL_RECOVERY_TRIGGER
            )
            try:
                returned = dispatcher._build_job_params(
                    dataset_name=dataset_name, **_eco_build_kwargs()
                )
            except ValueError as exc:
                # Refused by the central guard, naming the offending key and
                # the dispatcher surface.
                assert "trigger" in str(exc), (dataset_name, exc)
                assert "_build_job_params" in str(exc), (dataset_name, exc)
            else:
                raise AssertionError(
                    "the dispatcher returned parameters for "
                    f"{dataset_name} carrying "
                    f"trigger={returned.get('trigger')!r}: {returned}"
                )
        finally:
            dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name] = original

    # The injection is fully undone: every dataset builds SCHEDULED again.
    for dataset_name in dispatcher.ECO_DRIVING_SCHEDULE_MODES:
        params = dispatcher._build_job_params(
            dataset_name=dataset_name, **_eco_build_kwargs()
        )
        assert params["trigger"] == "SCHEDULED", (dataset_name, params)


def test_a_manual_trigger_combined_with_an_authority_key_is_refused() -> None:
    """The combinations, not only each key alone.

    A future edit is more likely to add a coherent-looking pair — a manual
    trigger *and* the flag, or a manual trigger *and* a recovery UUID — than a
    lone key. Each combination must still be refused, for every Eco dataset.
    """
    from jobs.api.telematics import dispatcher

    combinations = (
        ("boolean flag", {mra.PARAM_DISABLED_SCHEDULE_FLAG: True}),
        ("recovery uuid",
         {mra.PARAM_RECOVERY_RUN_ID: "d1f40c04-514c-4b07-8fb3-701cf4112906"}),
        ("expected schedule id", {mra.PARAM_EXPECTED_SCHEDULE_ID: SID}),
        ("the complete authority", {
            mra.PARAM_DISABLED_SCHEDULE_FLAG: True,
            mra.PARAM_RECOVERY_RUN_ID: "d1f40c04-514c-4b07-8fb3-701cf4112906",
            mra.PARAM_EXPECTED_SCHEDULE_ID: SID,
        }),
    )

    for dataset_name in dispatcher.ECO_DRIVING_SCHEDULE_MODES:
        for label, extra in combinations:
            original = dict(dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name])
            try:
                dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name].update(
                    {"trigger": mra.MANUAL_RECOVERY_TRIGGER, **extra}
                )
                try:
                    returned = dispatcher._build_job_params(
                        dataset_name=dataset_name, **_eco_build_kwargs()
                    )
                except ValueError as exc:
                    assert "trigger" in str(exc), (dataset_name, label, exc)
                else:
                    raise AssertionError(
                        f"{dataset_name} + manual trigger + {label} returned "
                        f"{returned}"
                    )
            finally:
                dispatcher.ECO_DRIVING_SCHEDULE_MODES[dataset_name] = original

    test_every_eco_driving_dataset_still_builds_its_exact_parameters()


def test_the_dispatcher_trigger_is_always_scheduled() -> None:
    """The invariant stated once, over every dataset branch there is.

    Not Eco-specific and not a dataset denylist: the guard is central, so this
    drives every structural branch of `_build_job_params` and requires the same
    single answer from each.
    """
    from jobs.api.telematics import dispatcher

    branches = [
        ("trips_sync", {}),
        ("trips_sync", dict(
            scheduled_fire_ts=datetime(2026, 7, 8, 3, tzinfo=UTC),
            nominal_window_start_ts=ECO_WINDOW_START,
            nominal_window_end_ts=ECO_WINDOW_END,
            trips_stabilization_delay_seconds=DELAY,
            trips_overlap_seconds=60,
            trips_pagination_mode=MODE,
        )),
        ("fuel_daily_aggregation", {}),
    ] + [(name, {}) for name in dispatcher.ECO_DRIVING_SCHEDULE_MODES]

    for dataset_name, extra in branches:
        params = dispatcher._build_job_params(
            dataset_name=dataset_name, **_eco_build_kwargs(), **extra
        )
        assert params["trigger"] == "SCHEDULED", (dataset_name, params)
        assert params["trigger"] != mra.MANUAL_RECOVERY_TRIGGER


def test_every_build_job_params_return_passes_the_authority_guard() -> None:
    """No dataset branch may reach a successful return unguarded.

    Behavioral: the central `reject_authority_params` is replaced by a recorder,
    every branch of `_build_job_params` is driven, and each returned dictionary
    must be the exact object the guard was handed. A branch that returns early —
    as the Eco branch once did — records nothing and fails here.
    """
    import ast
    import inspect

    from jobs.api.telematics import dispatcher

    seen = []
    original_guard = mra.reject_authority_params

    def recording_guard(params, *, surface):
        seen.append((dict(params), surface))
        return original_guard(params, surface=surface)

    branches = [
        # dataset_name, extra kwargs — one per structural branch.
        ("trips_sync", {}),
        ("trips_sync", dict(
            scheduled_fire_ts=datetime(2026, 7, 8, 3, tzinfo=UTC),
            nominal_window_start_ts=ECO_WINDOW_START,
            nominal_window_end_ts=ECO_WINDOW_END,
            trips_stabilization_delay_seconds=DELAY,
            trips_overlap_seconds=60,
            trips_pagination_mode=MODE,
        )),
        # A dataset that is neither Eco nor trips: the plain-window branch.
        ("fuel_daily_aggregation", {}),
    ] + [(name, {}) for name in dispatcher.ECO_DRIVING_SCHEDULE_MODES]

    mra.reject_authority_params = recording_guard  # type: ignore[assignment]
    try:
        for dataset_name, extra in branches:
            before = len(seen)
            returned = dispatcher._build_job_params(
                dataset_name=dataset_name, **_eco_build_kwargs(), **extra
            )
            assert len(seen) == before + 1, (
                f"{dataset_name} returned without reaching the guard"
            )
            guarded_params, surface = seen[-1]
            assert guarded_params == returned, (dataset_name, guarded_params)
            assert surface == (
                "jobs.api.telematics.dispatcher._build_job_params"
            ), surface
    finally:
        mra.reject_authority_params = original_guard  # type: ignore[assignment]

    assert len(seen) == len(branches)

    # Supplementary, not the proof: the function has exactly one return, so no
    # future branch can acquire an unguarded exit without changing this shape.
    tree = ast.parse(inspect.getsource(dispatcher._build_job_params))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert len(returns) == 1, f"{len(returns)} returns in _build_job_params"


# --- the enabled-schedule path is unchanged --------------------------------

def test_enabled_schedule_recovery_behavior_is_unchanged(
    conn, business_conn, dsn,
) -> None:
    """An enabled schedule launches with no attestation and no opt-in flag."""
    reset(conn, business_conn, schedule_enabled=True)
    runner = InProcessSubprocess(dsn=dsn, trips=make_trips(2, window_start=BASELINE))
    exit_code, plan = drive(
        conn, dsn, runner, arg_builder=enabled_schedule_args, execute=True,
    )

    assert exit_code == rc.EXIT_OK, plan
    assert plan["recovery_status"] == "SUCCESS", plan
    assert plan["cold_start_path"] is False, plan
    params = runner.calls[0]
    # The recovery identity is still carried, exactly as before; only the
    # disabled-schedule opt-in is absent, and so is any attestation.
    assert params["manual_recovery_run_id"] == plan["recovery_run_id"]
    for key in mra.DISABLED_SCHEDULE_AUTHORITY_PARAM_KEYS:
        assert key not in params, key
    assert mra.MANUAL_RECOVERY_AUTHORITY_ENV not in runner.observed_env[0]
    # Still gated on committed execution: the record is required either way.
    assert plan["execution_outcome"]["outcome"] == "EXECUTED_COMMITTED"
    assert trip_count(business_conn) == 2
    assert coverage(conn)["covered_through_source"] == "manual_recovery"


# ---------------------------------------------------------------------------

def test_the_attestation_carries_no_secret_or_capability() -> None:
    """The truthful trust level, asserted rather than described.

    This deliberately does **not** claim the attestation is unforgeable. It
    asserts the opposite property that the documentation now states: every field
    is derivable from module constants and the invocation's own identifiers, so
    an independent caller reproduces it byte for byte. What makes the mechanism
    worth having is tested elsewhere — the durable recovery row and dispatcher
    isolation.
    """
    window_start = datetime(2026, 7, 1, tzinfo=UTC)
    window_end = datetime(2026, 8, 1, tzinfo=UTC)
    RRID = "d1f40c04-514c-4b07-8fb3-701cf4112906"
    identifiers = dict(
        client_id=CID, client_code=CODE, schedule_id=SID,
        dataset_name="trips_sync", recovery_run_id=RRID,
        window_start_ts=window_start, window_end_ts=window_end,
    )
    attestation = mra.build_launch_attestation(**identifiers)

    # Reproducible from the same public identifiers by anyone who has them.
    assert mra.build_launch_attestation(**identifiers) == attestation

    payload = json.loads(attestation)
    # Every value is either a module constant or a supplied identifier. No
    # nonce, no secret, no launcher-only material.
    derivable = {
        mra.AUTHORITY_VERSION, mra.AUTHORIZED_LAUNCHER, CID, CODE, SID,
        "trips_sync", RRID,
        "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z",
    }
    assert set(payload.values()) <= derivable, payload
    assert payload["launcher"] == mra.AUTHORIZED_LAUNCHER


def test_platform_run_identity_is_required_for_coverage_eligible_proof() -> None:
    """The exact contract: missing/empty/malformed/different refuse; equal passes."""
    expected = str(uuid.uuid4())
    RRID = "d1f40c04-514c-4b07-8fb3-701cf4112906"
    window_start = datetime(2026, 7, 1, tzinfo=UTC)
    window_end = datetime(2026, 8, 1, tzinfo=UTC)

    def outcome(platform_run_id, *, kind=eo.OUTCOME_EXECUTED_COMMITTED):
        skipped = kind in eo.SKIPPED_OUTCOMES
        return eo.ExecutionOutcome(
            outcome=kind, client_id=CID, client_code=CODE, schedule_id=SID,
            dataset_name="trips_sync", recovery_run_id=RRID,
            platform_run_id=platform_run_id,
            requested_window_start_ts=window_start,
            requested_window_end_ts=window_end,
            provider_execution_entered=not skipped,
            business_transaction_entered=not skipped,
            transaction_status=(
                eo.TRANSACTION_NOT_ENTERED if skipped
                else eo.TRANSACTION_COMMITTED
            ),
            prepared_count=0 if skipped else 2,
            upserted_count=0 if skipped else 2,
            malformed_count=0, skipped=skipped,
            skip_reason=eo.SKIP_REASON_DISABLED_SCHEDULE if skipped else None,
            terminal_ts=datetime.now(UTC).replace(microsecond=0),
        )

    def check(record, platform_run_id):
        eo.verify_outcome(
            record, client_id=CID, client_code=CODE, schedule_id=SID,
            dataset_name="trips_sync", recovery_run_id=RRID,
            window_start_ts=window_start, window_end_ts=window_end,
            platform_run_id=platform_run_id,
        )

    def refuses(record, platform_run_id, *, code, label):
        try:
            check(record, platform_run_id)
        except eo.ExecutionOutcomeError as exc:
            assert exc.code == code, (label, exc.code)
        else:
            raise AssertionError(f"{label} was accepted")

    # --- expected side ---
    for missing in (None, "", "   "):
        refuses(
            outcome(expected), missing,
            code="EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING",
            label=f"expected={missing!r}",
        )
    refuses(
        outcome(expected), "not-a-uuid",
        code="EXECUTION_OUTCOME_PLATFORM_RUN_ID_MALFORMED",
        label="expected malformed",
    )
    # --- observed side ---
    for missing in (None, "", "   "):
        refuses(
            outcome(missing), expected,
            code="EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING",
            label=f"observed={missing!r}",
        )
    refuses(
        outcome("not-a-uuid"), expected,
        code="EXECUTION_OUTCOME_PLATFORM_RUN_ID_MALFORMED",
        label="observed malformed",
    )
    # --- a different, perfectly valid identity ---
    refuses(
        outcome(str(uuid.uuid4())), expected,
        code="EXECUTION_OUTCOME_IDENTITY_MISMATCH",
        label="different platform run",
    )
    # --- the permitted case ---
    check(outcome(expected), expected)
    # Equality is exact, not case-insensitive or prefix-based.
    refuses(
        outcome(expected), expected[:-1] + ("0" if expected[-1] != "0" else "1"),
        code="EXECUTION_OUTCOME_IDENTITY_MISMATCH",
        label="near-miss identity",
    )

    # A skipped record is not coverage-eligible, so it does not acquire a new
    # presence requirement — but it is also never eligible to advance coverage.
    skipped = outcome(None, kind=eo.OUTCOME_SKIPPED_DISABLED_SCHEDULE)
    check(skipped, None)
    assert eo.is_coverage_eligible(skipped) is False
    assert eo.is_coverage_eligible(outcome(expected)) is True


# ---------------------------------------------------------------------------
# Provider entry is a separate claim from the transaction, and it is required
# ---------------------------------------------------------------------------
#
# A coverage-eligible outcome asserts three independent things: that the run
# entered provider execution, that it entered its business transaction, and that
# the transaction committed. None implies another. The review found that only
# the last two were enforced, so a record could claim a committed execution of a
# window it had never asked the provider for. These pin the corrected contract.

def _record(
    *,
    outcome=eo.OUTCOME_EXECUTED_COMMITTED,
    provider_entered=True,
    transaction_entered=True,
    transaction_status=eo.TRANSACTION_COMMITTED,
    upserted=2,
    skipped=False,
    skip_reason=None,
    platform_run_id=None,
    recovery_run_id="d1f40c04-514c-4b07-8fb3-701cf4112906",
    window_start=datetime(2026, 7, 1, tzinfo=UTC),
    window_end=datetime(2026, 8, 1, tzinfo=UTC),
    window_completeness=None,
) -> dict:
    """A raw terminal-record mapping, built field by field.

    Deliberately a plain dict rather than an `ExecutionOutcome`: these cases
    must go through `from_mapping`, which is where the strict semantic
    validation lives and where a contradictory record has to be refused.
    """
    return {
        "version": eo.EXECUTION_OUTCOME_VERSION,
        "outcome": outcome,
        "client_id": CID,
        "client_code": CODE,
        "schedule_id": SID,
        "dataset_name": "trips_sync",
        "recovery_run_id": recovery_run_id,
        "platform_run_id": platform_run_id,
        "requested_window_start_ts": rc._iso(window_start),
        "requested_window_end_ts": rc._iso(window_end),
        "provider_execution_entered": provider_entered,
        "business_transaction_entered": transaction_entered,
        "transaction_status": transaction_status,
        "prepared_count": upserted,
        "upserted_count": upserted,
        "malformed_count": 0,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "terminal_ts": rc._iso(datetime.now(UTC).replace(microsecond=0)),
        # M4 added this field at record version `/2`. These cases are all about
        # the M3 self-contradiction rules, so it defaults to `None` — a record
        # with no completeness proof, which parses and is M3-eligible and which
        # the dispatcher's condition 6 then refuses. That separation is the
        # point: none of the refusals below may depend on M4 evidence.
        "window_completeness": window_completeness,
    }


def _refuses_parse(payload: dict, *, label: str) -> None:
    try:
        eo.ExecutionOutcome.from_mapping(payload)
    except eo.ExecutionOutcomeError as exc:
        assert exc.code == "EXECUTION_OUTCOME_MALFORMED", (label, exc.code)
    else:
        raise AssertionError(f"{label} parsed as a valid terminal record")


def test_a_committed_outcome_without_provider_entry_is_refused() -> None:
    """Both executed outcomes, with provider entry false, refuse at parse time.

    Refusing is preferred over merely classifying the record non-eligible: the
    record contradicts itself, and a self-contradictory record must not survive
    to reach any gate that might read only part of it.
    """
    _refuses_parse(
        _record(outcome=eo.OUTCOME_EXECUTED_COMMITTED, provider_entered=False),
        label="EXECUTED_COMMITTED with provider entry false",
    )
    _refuses_parse(
        _record(
            outcome=eo.OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED,
            provider_entered=False, upserted=0,
        ),
        label="EXECUTED_ZERO_ROWS_COMMITTED with provider entry false",
    )
    # The error names the reason, so an operator reading it is not left guessing
    # which of the three claims was missing.
    try:
        eo.ExecutionOutcome.from_mapping(_record(provider_entered=False))
    except eo.ExecutionOutcomeError as exc:
        assert "provider" in str(exc).lower(), exc


def test_the_three_execution_claims_are_enforced_independently() -> None:
    """Each conjunct refuses on its own; none is implied by another."""
    # provider entered, but the business transaction never was
    _refuses_parse(
        _record(provider_entered=True, transaction_entered=False),
        label="provider entered without a business transaction",
    )
    # provider and transaction entered, but the transaction never committed
    _refuses_parse(
        _record(
            provider_entered=True, transaction_entered=True,
            transaction_status=eo.TRANSACTION_NOT_COMMITTED,
        ),
        label="entered transaction that never committed",
    )
    _refuses_parse(
        _record(
            provider_entered=True, transaction_entered=True,
            transaction_status=eo.TRANSACTION_NOT_ENTERED,
        ),
        label="committed outcome claiming an unentered transaction status",
    )


def test_valid_committed_records_remain_accepted() -> None:
    """The corrected contract must not have narrowed a genuine execution."""
    rows = eo.ExecutionOutcome.from_mapping(
        _record(outcome=eo.OUTCOME_EXECUTED_COMMITTED, upserted=2)
    )
    assert rows.provider_execution_entered is True
    assert eo.is_coverage_eligible(rows) is True

    zero = eo.ExecutionOutcome.from_mapping(
        _record(outcome=eo.OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED, upserted=0)
    )
    assert zero.provider_execution_entered is True
    assert eo.is_coverage_eligible(zero) is True

    # A skip legitimately reports provider entry false and stays parseable —
    # only *coverage-eligible* outcomes acquired the new requirement.
    skipped = eo.ExecutionOutcome.from_mapping(_record(
        outcome=eo.OUTCOME_SKIPPED_DISABLED_SCHEDULE, provider_entered=False,
        transaction_entered=False,
        transaction_status=eo.TRANSACTION_NOT_ENTERED, upserted=0,
        skipped=True, skip_reason=eo.SKIP_REASON_DISABLED_SCHEDULE,
    ))
    assert eo.is_coverage_eligible(skipped) is False
    # A FAILED record may also legitimately report provider entry false.
    failed = eo.ExecutionOutcome.from_mapping(_record(
        outcome=eo.OUTCOME_FAILED, provider_entered=False,
        transaction_entered=False,
        transaction_status=eo.TRANSACTION_NOT_ENTERED, upserted=0,
    ))
    assert eo.is_coverage_eligible(failed) is False


def test_is_coverage_eligible_requires_provider_entry_directly() -> None:
    """The predicate itself checks it, not only the parser.

    A record constructed in-process never passes through `from_mapping`, so the
    predicate must not rely on parse-time validation having happened.
    """
    built = eo.ExecutionOutcome(
        outcome=eo.OUTCOME_EXECUTED_COMMITTED, client_id=CID, client_code=CODE,
        schedule_id=SID, dataset_name="trips_sync",
        recovery_run_id="d1f40c04-514c-4b07-8fb3-701cf4112906",
        platform_run_id=str(uuid.uuid4()),
        requested_window_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
        requested_window_end_ts=datetime(2026, 8, 1, tzinfo=UTC),
        provider_execution_entered=False,
        business_transaction_entered=True,
        transaction_status=eo.TRANSACTION_COMMITTED,
        prepared_count=2, upserted_count=2, malformed_count=0,
        skipped=False, skip_reason=None,
        terminal_ts=datetime.now(UTC).replace(microsecond=0),
    )
    assert eo.is_coverage_eligible(built) is False


def test_an_exact_platform_run_id_cannot_hide_a_false_provider_entry() -> None:
    """Identity verification is not a substitute for semantic validity.

    The platform-run identity matrix is about *whose* execution the record
    describes. A record that names exactly the right run and still claims it
    committed without entering the provider must not be rescued by that match.
    """
    run_id = str(uuid.uuid4())
    payload = _record(provider_entered=False, platform_run_id=run_id)
    _refuses_parse(payload, label="false provider entry with an exact run id")

    # And built directly, bypassing the parser: identity verification passes,
    # eligibility still does not.
    built = eo.ExecutionOutcome(
        outcome=eo.OUTCOME_EXECUTED_COMMITTED, client_id=CID, client_code=CODE,
        schedule_id=SID, dataset_name="trips_sync",
        recovery_run_id="d1f40c04-514c-4b07-8fb3-701cf4112906",
        platform_run_id=run_id,
        requested_window_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
        requested_window_end_ts=datetime(2026, 8, 1, tzinfo=UTC),
        provider_execution_entered=False,
        business_transaction_entered=True,
        transaction_status=eo.TRANSACTION_COMMITTED,
        prepared_count=2, upserted_count=2, malformed_count=0,
        skipped=False, skip_reason=None,
        terminal_ts=datetime.now(UTC).replace(microsecond=0),
    )
    eo.verify_outcome(
        built, client_id=CID, client_code=CODE, schedule_id=SID,
        dataset_name="trips_sync",
        recovery_run_id="d1f40c04-514c-4b07-8fb3-701cf4112906",
        window_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
        window_end_ts=datetime(2026, 8, 1, tzinfo=UTC),
        platform_run_id=run_id,
    )
    assert eo.is_coverage_eligible(built) is False


def test_the_recorder_never_writes_a_committed_record_without_provider_entry(
) -> None:
    """The writer agrees with the parser instead of producing a rejected record.

    A recorder that reached its terminal statement with a committed transaction
    but no provider entry would otherwise emit a record every reader refuses.
    It reports `FAILED`, which is the truthful statement about that run.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "outcome.json"
        recorder = eo.ExecutionOutcomeRecorder(
            params={
                "client_id": CID,
                "manual_recovery_run_id": "d1f40c04-514c-4b07-8fb3-701cf4112906",
            },
            dataset_name="trips_sync",
            env={eo.EXECUTION_OUTCOME_FILE_ENV: str(path)},
        )
        recorder.bind_target(
            client_id=CID, client_code=CODE,
            window_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
            window_end_ts=datetime(2026, 8, 1, tzinfo=UTC),
            platform_run_id=str(uuid.uuid4()),
        )
        recorder.bind_schedule(SID)
        # Deliberately never `mark_provider_entered()`.
        recorder.mark_transaction_entered()
        recorder.mark_transaction_committed()
        recorder.record_counts(prepared=2, upserted=2, malformed=0)
        recorder.record_executed()

        written = eo.read_outcome(path)
        assert written.outcome == eo.OUTCOME_FAILED, written.outcome
        assert eo.is_coverage_eligible(written) is False


def test_recovery_coverage_does_not_advance_when_provider_entry_is_false(
    conn, business_conn, dsn,
) -> None:
    """End to end: rc=0, committed transaction, provider never entered.

    The launcher gets a return code of 0 and a record that would otherwise look
    like a committed execution. Coverage must stay byte-identical, because the
    record is refused before any gate can act on it.
    """
    reset(conn, business_conn)
    before = coverage(conn)

    def clear_provider_entry(path: Path) -> None:
        # The job genuinely executed and committed; only the record is tampered
        # with, which is the exact shape a forged or buggy proof would take.
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["outcome"] == eo.OUTCOME_EXECUTED_COMMITTED, payload
        assert payload["provider_execution_entered"] is True, payload
        payload["provider_execution_entered"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")

    runner = InProcessSubprocess(
        dsn=dsn, trips=make_trips(2, window_start=BASELINE),
        after_job=clear_provider_entry,
    )
    exit_code, plan = drive(conn, dsn, runner, execute=True)

    assert exit_code == rc.EXIT_BUSINESS_FAILED, plan
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_NOT_EXECUTED
    assert plan["coverage_advanced"] is False, plan
    after = coverage(conn)
    assert after["covered_through_ts"] == before["covered_through_ts"], (
        before, after,
    )
    assert after["coverage_start_ts"] == before["coverage_start_ts"]
    assert after["covered_through_source"] == before["covered_through_source"]
    assert schedule_enabled(conn) is False


def test_activation_refuses_a_chain_proof_with_provider_entry_false() -> None:
    """Activation reads the same structured proof, so it refuses the same record.

    Asserted at the contract the activation tool consults — the parse and the
    eligibility predicate — rather than by duplicating the activation fixture,
    because that is the single place activation's acceptance is decided.
    """
    proof = outcome_for_window(
        client_id=CID, client_code=CODE, schedule_id=SID,
        recovery_run_id="d1f40c04-514c-4b07-8fb3-701cf4112906",
        window_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
        window_end_ts=datetime(2026, 8, 1, tzinfo=UTC),
        platform_run_id=str(uuid.uuid4()),
    )
    assert proof["provider_execution_entered"] is True
    assert eo.is_coverage_eligible(
        eo.ExecutionOutcome.from_mapping(proof)
    ) is True

    forged = dict(proof, provider_execution_entered=False)
    _refuses_parse(forged, label="chain window proof with provider entry false")


# ---------------------------------------------------------------------------
# Authority is decided before any provider secret is resolved
# ---------------------------------------------------------------------------

def _drive_job_with_instrumented_secrets(
    conn, dsn, *, params, attestation=None,
):
    """Run the real job with an instrumented secret resolver and provider client.

    Returns `(refusal, provider_secret_resolutions, provider_clients_built)`.
    The resolver is substituted at the module attribute the job actually calls,
    so this counts real resolutions rather than a stand-in's, and the provider
    class is replaced by a recorder that raises if it is ever constructed before
    a secret was resolved.
    """
    recording = RecordingClient()
    outcome_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"eo-{uuid.uuid4()}.json"
    original_environ = dict(os.environ)
    original_provider = sync.TelematicsFleetProviderClient
    original_resolver = sync.resolve_secret

    events: list = []

    def instrumented_resolve_secret(ref):
        if ref == PROVIDER_SECRET_ENV:
            events.append(("provider_secret", ref))
        return original_resolver(ref)

    def instrumented_provider(**kwargs):
        events.append(("provider_client", None))
        return FakeProvider(trips=[], **kwargs)

    try:
        os.environ.update(_job_environment(dsn))
        os.environ[eo.EXECUTION_OUTCOME_FILE_ENV] = str(outcome_path)
        os.environ.pop(mra.MANUAL_RECOVERY_AUTHORITY_ENV, None)
        if attestation is not None:
            os.environ[mra.MANUAL_RECOVERY_AUTHORITY_ENV] = attestation
        sync.resolve_secret = instrumented_resolve_secret
        sync.TelematicsFleetProviderClient = instrumented_provider
        refusal = None
        try:
            sync.run(client=recording, run_id=str(uuid.uuid4()), params=params)
        except Exception as exc:
            # Any refusal, not only `ManualRecoveryAuthorityError`: a request
            # naming a client that does not exist is refused earlier still, by
            # the control-plane load, and that is also a path which must resolve
            # no provider secret.
            refusal = exc
        return refusal, events
    finally:
        sync.resolve_secret = original_resolver
        sync.TelematicsFleetProviderClient = original_provider
        os.environ.clear()
        os.environ.update(original_environ)
        outcome_path.unlink(missing_ok=True)


def _assert_zero_secret_resolutions(events, *, label: str) -> None:
    resolutions = [e for e in events if e[0] == "provider_secret"]
    clients = [e for e in events if e[0] == "provider_client"]
    assert not resolutions, (
        f"{label}: {len(resolutions)} provider secret resolution(s) happened "
        "before the authority was accepted"
    )
    assert not clients, (
        f"{label}: a provider client was constructed without an authority"
    )


def test_no_provider_secret_is_resolved_before_authority_is_accepted(
    conn, business_conn, dsn,
) -> None:
    """Every refusal path performs zero provider-secret resolutions.

    The provider password used to be resolved immediately after the client
    account was loaded — before the schedule was even read — so an unauthorized
    or malformed manual-recovery request had already caused the credential to be
    read out of the environment. Each case below drives the *real* job through
    the *real* guard and requires the count to be exactly zero.
    """
    reset(conn, business_conn)
    window_start, window_end = BASELINE, _safe_end(conn)

    # 1) a disabled schedule with no authority at all: an ordinary skip
    _, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params={
            "client_id": CID, "client_code": CODE,
            "window_start_ts": rc._iso(window_start),
            "window_end_ts": rc._iso(window_end),
        },
    )
    _assert_zero_secret_resolutions(events, label="disabled schedule, no authority")

    # 2) a bare boolean — the flag alone, no attestation, no recovery row
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params={
            "client_id": CID, "client_code": CODE,
            "window_start_ts": rc._iso(window_start),
            "window_end_ts": rc._iso(window_end),
            mra.PARAM_DISABLED_SCHEDULE_FLAG: True,
        },
    )
    assert refusal is not None
    _assert_zero_secret_resolutions(events, label="bare boolean")

    # 3) a malformed attestation
    valid_rrid = _claim_recovery_row(
        conn, window_start=window_start, window_end=window_end,
    )
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=valid_rrid,
            window_start=window_start, window_end=window_end,
        ),
        attestation="{not json at all",
    )
    assert isinstance(refusal, mra.ManualRecoveryAuthorityError), refusal
    assert refusal.code == "AUTHORITY_REFUSED_ATTESTATION", refusal.code
    _assert_zero_secret_resolutions(events, label="malformed attestation")

    # 4) an absent recovery row
    absent = str(uuid.uuid4())
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=absent,
            window_start=window_start, window_end=window_end,
        ),
        attestation=_attestation(
            recovery_run_id=absent,
            window_start=window_start, window_end=window_end,
        ),
    )
    assert isinstance(refusal, mra.ManualRecoveryAuthorityError), refusal
    assert refusal.code == "AUTHORITY_REFUSED_RECOVERY", refusal.code
    _assert_zero_secret_resolutions(events, label="absent recovery row")

    # 5) a terminal recovery row
    terminal = _claim_recovery_row(
        conn, window_start=window_start, window_end=window_end, status="SUCCESS",
        approval_ref=CHAIN + "-W02",
    )
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=terminal,
            window_start=window_start, window_end=window_end,
        ),
        attestation=_attestation(
            recovery_run_id=terminal,
            window_start=window_start, window_end=window_end,
        ),
    )
    assert isinstance(refusal, mra.ManualRecoveryAuthorityError), refusal
    assert refusal.code == "AUTHORITY_REFUSED_RECOVERY_STATE", refusal.code
    _assert_zero_secret_resolutions(events, label="terminal recovery row")

    # 6) the wrong client, schedule and window, each on its own
    wrong_client = str(uuid.uuid4())
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=valid_rrid,
            window_start=window_start, window_end=window_end,
            client_id=wrong_client,
        ),
        attestation=_attestation(
            recovery_run_id=valid_rrid, client_id=wrong_client,
            window_start=window_start, window_end=window_end,
        ),
    )
    assert refusal is not None
    _assert_zero_secret_resolutions(events, label="wrong client")

    wrong_schedule = str(uuid.uuid4())
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=valid_rrid,
            window_start=window_start, window_end=window_end,
            schedule_id=wrong_schedule,
        ),
        attestation=_attestation(
            recovery_run_id=valid_rrid, schedule_id=wrong_schedule,
            window_start=window_start, window_end=window_end,
        ),
    )
    assert refusal is not None
    _assert_zero_secret_resolutions(events, label="wrong schedule")

    shifted = window_end + timedelta(seconds=1)
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=valid_rrid,
            window_start=window_start, window_end=shifted,
        ),
        attestation=_attestation(
            recovery_run_id=valid_rrid,
            window_start=window_start, window_end=shifted,
        ),
    )
    assert refusal is not None
    _assert_zero_secret_resolutions(events, label="wrong window")


def test_a_valid_authority_resolves_the_secret_once_after_authorization(
    conn, business_conn, dsn,
) -> None:
    """The permitted case still works, and the ordering is provable.

    Exactly one provider-secret resolution, and the provider client is
    constructed only after it — never before the authority was accepted.
    """
    reset(conn, business_conn)
    window_start, window_end = BASELINE, _safe_end(conn)
    rrid = _claim_recovery_row(
        conn, window_start=window_start, window_end=window_end,
    )
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params=_authority_params(
            conn, recovery_run_id=rrid,
            window_start=window_start, window_end=window_end,
        ),
        attestation=_attestation(
            recovery_run_id=rrid,
            window_start=window_start, window_end=window_end,
        ),
    )
    assert refusal is None, refusal
    kinds = [kind for kind, _ in events]
    assert kinds.count("provider_secret") == 1, events
    assert kinds.count("provider_client") == 1, events
    assert kinds.index("provider_secret") < kinds.index("provider_client"), (
        "the provider client was constructed before its secret was resolved"
    )


def test_ordinary_enabled_scheduled_execution_is_unchanged(
    conn, business_conn, dsn,
) -> None:
    """An enabled schedule still resolves its secret exactly once, as before."""
    reset(conn, business_conn, schedule_enabled=True)
    window_start, window_end = BASELINE, _safe_end(conn)
    refusal, events = _drive_job_with_instrumented_secrets(
        conn, dsn,
        params={
            "client_id": CID, "client_code": CODE, "trigger": "SCHEDULED",
            "window_start_ts": rc._iso(window_start),
            "window_end_ts": rc._iso(window_end),
        },
    )
    assert refusal is None, refusal
    kinds = [kind for kind, _ in events]
    assert kinds.count("provider_secret") == 1, events
    assert kinds.count("provider_client") == 1, events
    assert kinds.index("provider_secret") < kinds.index("provider_client")


def test_the_secret_is_not_resolved_before_the_schedule_is_even_read() -> None:
    """Source-level: no provider-secret resolution precedes the guard.

    Behavioral tests prove the refusal paths resolve nothing. This additionally
    pins the *ordering* in the source, so a future edit that reintroduces an
    early resolution — on a path no current test happens to drive — is caught.
    """
    import inspect

    source = inspect.getsource(sync._run)
    secret_at = source.index("resolve_secret(cfg.provider_basic_auth_password")
    guard_at = source.index("authorize_disabled_schedule_recovery")
    schedule_at = source.index("load_dataset_schedule(")
    assert schedule_at < secret_at, (
        "the provider secret is resolved before the schedule is loaded"
    )
    assert guard_at < secret_at, (
        "the provider secret is resolved before the manual-recovery authority "
        "is validated"
    )
    # And it is resolved exactly once, in one place.
    assert source.count("resolve_secret(cfg.provider_basic_auth_password") == 1


def test_on_disposable_postgres(dsn: str, business_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn, \
            psycopg.connect(
                business_dsn, row_factory=dict_row, autocommit=False
            ) as business_conn:
        bootstrap(conn, business_conn)

        # --- the real path, end to end ---
        test_valid_authority_passes_the_disabled_schedule_guard(
            conn, business_conn, dsn,
        )
        test_zero_row_committed_execution_advances_coverage(
            conn, business_conn, dsn,
        )
        test_disabled_schedule_without_authority_skips(conn, business_conn, dsn)
        test_returncode_zero_plus_skipped_outcome_does_not_advance_coverage(
            conn, business_conn, dsn,
        )
        test_returncode_zero_without_a_record_does_not_advance_coverage(
            conn, business_conn, dsn,
        )
        test_a_malformed_record_does_not_advance_coverage(
            conn, business_conn, dsn,
        )
        test_a_mismatched_record_never_advances_coverage(
            conn, business_conn, dsn,
        )
        test_an_uncommitted_transaction_never_advances_coverage(
            conn, business_conn, dsn,
        )

        # --- platform-run identity ---
        test_returncode_zero_without_platform_run_identity_does_not_advance_coverage(
            conn, business_conn, dsn,
        )
        test_a_record_without_a_usable_platform_run_id_never_advances_coverage(
            conn, business_conn, dsn,
        )
        test_an_exactly_matching_platform_run_identity_advances_coverage(
            conn, business_conn, dsn,
        )

        # --- provider entry is required for a coverage-eligible outcome ---
        test_recovery_coverage_does_not_advance_when_provider_entry_is_false(
            conn, business_conn, dsn,
        )

        # --- authority is decided before any provider secret is resolved ---
        test_no_provider_secret_is_resolved_before_authority_is_accepted(
            conn, business_conn, dsn,
        )
        test_a_valid_authority_resolves_the_secret_once_after_authorization(
            conn, business_conn, dsn,
        )
        test_ordinary_enabled_scheduled_execution_is_unchanged(
            conn, business_conn, dsn,
        )

        # --- authority refusals ---
        test_a_bare_flag_without_a_recovery_row_refuses(conn, business_conn, dsn)
        test_a_direct_job_invocation_cannot_use_the_authority(
            conn, business_conn, dsn,
        )
        test_wrong_recovery_uuid_client_schedule_or_window_refuses(
            conn, business_conn, dsn,
        )
        test_a_terminal_recovery_row_authorizes_nothing(conn, business_conn, dsn)
        test_an_enabled_schedule_refuses_the_authority(conn, business_conn, dsn)

        # --- unchanged behavior ---
        test_enabled_schedule_recovery_behavior_is_unchanged(
            conn, business_conn, dsn,
        )
        conn.rollback()


def main() -> None:
    global BUSINESS

    install_network_guard()
    test_the_dispatcher_cannot_build_or_inherit_the_authority()
    test_every_eco_driving_dataset_still_builds_its_exact_parameters()
    test_no_eco_driving_mode_entry_can_emit_authority_parameters()
    test_no_eco_driving_mode_entry_can_force_a_manual_trigger()
    test_a_manual_trigger_combined_with_an_authority_key_is_refused()
    test_the_dispatcher_trigger_is_always_scheduled()
    test_every_build_job_params_return_passes_the_authority_guard()
    test_the_attestation_carries_no_secret_or_capability()
    test_platform_run_identity_is_required_for_coverage_eligible_proof()
    test_a_committed_outcome_without_provider_entry_is_refused()
    test_the_three_execution_claims_are_enforced_independently()
    test_valid_committed_records_remain_accepted()
    test_is_coverage_eligible_requires_provider_entry_directly()
    test_an_exact_platform_run_id_cannot_hide_a_false_provider_entry()
    test_the_recorder_never_writes_a_committed_record_without_provider_entry()
    test_activation_refuses_a_chain_proof_with_provider_entry_false()
    test_the_secret_is_not_resolved_before_the_schedule_is_even_read()
    dsn = os.getenv(ENV)
    business_dsn = os.getenv(BUSINESS_ENV)
    if not dsn or not business_dsn:
        print(f"SKIP: set {ENV} and {BUSINESS_ENV} to disposable "
              "PostgreSQL 16 DSNs")
        return
    # Destructive on both databases: prove each is loopback-only before
    # opening a connection.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    require_loopback_dsn_or_exit(business_dsn, label=BUSINESS_ENV)
    BUSINESS = _parse_business_dsn(business_dsn)
    test_on_disposable_postgres(dsn, business_dsn)
    print("OK - Telematics recovery execution-path checks passed")


if __name__ == "__main__":
    main()
