#!/usr/bin/env python3
"""Per-client Telematics coverage bootstrap: a mixed fleet must not block a target.

The writer originally refused whenever *any* client already ran
`data_invariants_v1`. That was an initial-rollout assumption, and once
`BRAVO00016` became an accepted production compatibility client it made every
subsequent per-client bootstrap impossible. These tests pin the corrected
contract: the gates are scoped to the one target client, and the rest of the
fleet is observed but never gated on.

Every check here needs a *disposable* PostgreSQL 16 database — never logdb:

  docker run -d --rm --name c10-multi-pg -e POSTGRES_PASSWORD=... \\
      -e POSTGRES_USER=loguser -e POSTGRES_DB=c10_multi_test \\
      -p 55708:5432 postgres:16
  TELEMATICS_BOOTSTRAP_MULTI_TEST_DSN='postgresql://loguser:...@127.0.0.1:55708/c10_multi_test' \\
      .venv/bin/python ops/tests_manual/test_telematics_coverage_bootstrap_multi_client_postgres.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timezone
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
    ENVIRONMENT,
    MIGRATIONS_DIR,
    PLATFORM_UUID,
    PREREQUISITE_MIGRATIONS,
)

UTC = timezone.utc

RECOVERY_MIGRATION = "058_telematics_trips_manual_recovery.sql"

# The real production identities this defect was found with. ALPHA00001 is the
# strict target awaiting its first bootstrap; BRAVO00016 is the already
# approved, already bootstrapped, already recovered compatibility client.
TARGET_CODE = "ALPHA00001"
TARGET_CLIENT_ID = "d67285b1-65ec-4060-8fd5-169d1ed9fc37"
TARGET_SCHEDULE = "d5ea5d6c-0fb0-495b-83d2-93ba1d1370d1"

COMPAT_CODE = "BRAVO00016"
COMPAT_CLIENT_ID = "ce9c5e76-b476-4162-8bd9-de3c62dd257b"
COMPAT_SCHEDULE = "2815032b-17f4-44c0-8223-2829ddde07ab"

# Two further approved compatibility clients, to prove the gate does not
# merely tolerate exactly one.
EXTRA_COMPAT = (
    ("FOXTROT00001", "778145a8-bfb3-4a12-8b1a-1ce50e46411e",
     "8d80819a-9a3a-4823-8da0-41be5f5b8f75"),
    ("DELTA00001", "1852d49d-3501-48ae-8d9a-738b2de2c4ab",
     "c05ea538-48d6-43d7-83d5-7a4341c3dfc6"),
)

# A fully proven daily history for the target: no hole, so the audit classifies
# the range COMPLETE_INTERVALS_INVENTORIED and the selection is unobstructed.
TARGET_HISTORY = tuple(
    (f"2026-07-{day:02d}T02:00:00Z", "SUCCESS") for day in range(20, 32)
) + (("2026-08-01T02:00:00Z", "SUCCESS"),)

RANGE_START = "2026-07-20T00:00:00Z"
RANGE_END = "2026-08-01T12:00:00Z"
GOOD_A = "2026-07-21T00:00:00Z"
GOOD_W = "2026-08-01T02:00:00Z"

SEEDED_BY = "operator@example.invalid"
APPROVAL_REF = "OPS-ALPHA-2026-08-03"

COVERAGE_TABLE = "workflow_a_control.client_dataset_coverage"
RECOVERY_TABLE = "workflow_a_control.client_dataset_recovery_run"
HISTORY_TABLE = "workflow_a_control.client_schedule_run_history"
ACCOUNT_TABLE = "workflow_a_control.client_account"
SCHEDULE_TABLE = "workflow_a_control.client_dataset_schedule"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _reset(conn) -> None:
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.execute(
        "CREATE TABLE public.schema_migrations ("
        " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for name in PREREQUISITE_MIGRATIONS + (RECOVERY_MIGRATION,):
        conn.execute(_sql(name))
        conn.execute(
            "INSERT INTO public.schema_migrations (filename) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (name,),
        )
    conn.execute(
        """
        INSERT INTO ops_control.environment_identity
          (identity_key, environment, database_identity_id, database_role,
           database_name, provisioned_by)
        VALUES ('primary', %s, %s, 'platform', current_database(), 'test')
        """,
        (ENVIRONMENT, PLATFORM_UUID),
    )


def _add_client(
    conn,
    *,
    client_id: str,
    client_code: str,
    schedule_id: str,
    mode: str,
    history: tuple = (),
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_account
          (client_id, client_code, client_name, provider_type,
           provider_base_url, provider_basic_auth_username,
           provider_basic_auth_password_secret_ref, client_db_host,
           client_db_port, client_db_name, client_db_user,
           client_db_password_secret_ref, speed_trigger_filter_text,
           trips_pagination_mode)
        VALUES (%s, %s, %s, 'telematics', 'https://provider.invalid',
                'user', 'TEST_PROVIDER_KEY', '127.0.0.1', 5432, 'clientdb',
                'clientuser', 'TEST_DB_KEY', 'SPEEDING', %s)
        """,
        (client_id, client_code, f"{client_code} test", mode),
    )
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id, client_id, client_code, dataset_name, enabled,
           frequency, run_time, timezone, lookback_days)
        VALUES (%s, %s, %s, 'trips_sync', true, 'daily', '02:00:00', 'UTC', 1)
        """,
        (schedule_id, client_id, client_code),
    )
    for fire, status in history:
        conn.execute(
            f"""
            INSERT INTO {HISTORY_TABLE}
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s, %s, %s, 'trips_sync',
                    (%s::timestamptz - interval '1 day'), %s, %s, %s)
            """,
            (schedule_id, client_id, client_code, fire, fire, fire, status),
        )


def _add_coverage(
    conn,
    *,
    client_id: str,
    client_code: str,
    schedule_id: str,
    coverage_start: str,
    covered_through: str,
    source: str = "manual_recovery",
) -> None:
    conn.execute(
        f"""
        INSERT INTO {COVERAGE_TABLE}
          (schedule_id, client_id, client_code, dataset_name,
           coverage_start_ts, covered_through_ts, bootstrap_status,
           bootstrap_evidence_ref, seeded_at, seeded_by,
           covered_through_source, updated_at)
        VALUES (%s, %s, %s, 'trips_sync', %s, %s, 'READY',
                'telematics-coverage-bootstrap/1:sha256=' || repeat('a', 64)
                  || ':approval=OPS-EXISTING',
                '2026-08-03T08:07:48Z', 'someone-else', %s,
                '2026-08-03T08:07:48Z')
        """,
        (schedule_id, client_id, client_code, coverage_start, covered_through,
         source),
    )


def _add_recovery(
    conn,
    *,
    client_id: str,
    client_code: str,
    schedule_id: str,
    status: str,
    approval_ref: str,
    window_start: str = "2026-07-27T00:00:00Z",
    window_end: str = "2026-08-03T00:00:00Z",
) -> None:
    terminal = status in {"SUCCESS", "FAILED", "FINALIZATION_CONFLICT"}
    conn.execute(
        f"""
        INSERT INTO {RECOVERY_TABLE}
          (client_id, client_code, schedule_id, dataset_name,
           window_start_ts, window_end_ts, expected_old_covered_through_ts,
           status, reason, approval_ref, repository_head, pagination_mode,
           stabilization_delay_seconds, overlap_seconds,
           max_recovery_span_seconds, initial_coverage_snapshot,
           initial_coverage_fingerprint, started_at, finished_at)
        VALUES (%s, %s, %s, 'trips_sync', %s, %s, %s, %s,
                'reviewed test recovery', %s, %s, 'data_invariants_v1',
                10800, 3600, 2678400, '{{}}'::jsonb, %s, %s, %s)
        """,
        (
            client_id, client_code, schedule_id, window_start, window_end,
            window_start, status, approval_ref, "0" * 40, "b" * 64,
            "2026-08-03T09:00:00Z" if terminal else None,
            "2026-08-03T09:30:00Z" if terminal else None,
        ),
    )


def build_fleet(
    conn,
    *,
    target_mode: str = "strict_meta",
    compat_clients: tuple = (),
    compat_coverage: bool = True,
    compat_recovery: bool = True,
) -> None:
    """Rebuild a disposable control plane: one strict target plus a real fleet."""
    _reset(conn)
    _add_client(
        conn,
        client_id=TARGET_CLIENT_ID,
        client_code=TARGET_CODE,
        schedule_id=TARGET_SCHEDULE,
        mode=target_mode,
        history=TARGET_HISTORY,
    )
    for code, client_id, schedule_id in compat_clients:
        _add_client(
            conn,
            client_id=client_id,
            client_code=code,
            schedule_id=schedule_id,
            mode="data_invariants_v1",
        )
        if compat_coverage:
            _add_coverage(
                conn,
                client_id=client_id,
                client_code=code,
                schedule_id=schedule_id,
                coverage_start="2026-07-01T00:00:00Z",
                covered_through="2026-08-03T00:00:00Z",
            )
        if compat_recovery and code == COMPAT_CODE:
            _add_recovery(
                conn,
                client_id=client_id,
                client_code=code,
                schedule_id=schedule_id,
                status="SUCCESS",
                approval_ref="TELEMATICS-C11-BRAVO00016-2026-08-03",
            )
    conn.commit()


FULL_FLEET = ((COMPAT_CODE, COMPAT_CLIENT_ID, COMPAT_SCHEDULE),) + EXTRA_COMPAT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_bundle(dsn: str, tmp: Path) -> tuple[dict, Path]:
    bundle, path = audit.run_audit(Namespace(
        client_code=TARGET_CODE,
        dataset="trips_sync",
        output=str(tmp / "bundle.json"),
        expected_environment=ENVIRONMENT,
        expected_platform_uuid=PLATFORM_UUID,
        range_start=RANGE_START,
        range_end=RANGE_END,
        dsn=dsn,
    ))
    return bundle, path


def _writer_args(dsn: str, bundle_path: Path, digest: str, **overrides) -> Namespace:
    values = {
        "client_code": TARGET_CODE,
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
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (ACCOUNT_TABLE, SCHEDULE_TABLE, HISTORY_TABLE,
                      COVERAGE_TABLE, RECOVERY_TABLE)
    }


def _non_target_coverage(conn) -> list:
    return conn.execute(
        f"SELECT * FROM {COVERAGE_TABLE} WHERE client_id <> %s "
        "ORDER BY schedule_id",
        (TARGET_CLIENT_ID,),
    ).fetchall()


def _expect_refusal(dsn, path, digest, expected_code, **overrides) -> None:
    try:
        writer.run(_writer_args(dsn, path, digest, **overrides))
    except writer.BootstrapRefused as exc:
        assert exc.code == expected_code, (expected_code, exc.code, str(exc))
        return
    raise AssertionError(f"{overrides or expected_code} was accepted")


@contextmanager
def _no_external_network():
    """Fail loudly on any connection that leaves the loopback interface."""
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def _guard(address) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in {"127.0.0.1", "::1", "localhost", ""}:
            raise AssertionError(f"external network access attempted: {host!r}")

    def _connect(self, address):
        _guard(address)
        return real_connect(self, address)

    def _create_connection(address, *args, **kwargs):
        _guard(address)
        return real_create(address, *args, **kwargs)

    socket.socket.connect = _connect
    socket.create_connection = _create_connection
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.create_connection = real_create


# ---------------------------------------------------------------------------
# Pure — the static contract
# ---------------------------------------------------------------------------

def test_writer_never_touches_a_non_target_surface() -> None:
    text = (REPO_ROOT / "ops" / "bootstrap_telematics_trips_coverage.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import requests", "provider_client", "import subprocess",
        "subprocess.run", "subprocess.Popen", "Popen(",
        "os.system", "os.exec", "runner.py", "resolve_secret",
        "SET trips_pagination_mode",
        "UPDATE workflow_a_control.client_account",
        "UPDATE workflow_a_control.client_dataset_schedule",
        "INSERT INTO workflow_a_control.client_schedule_run_history",
        "UPDATE workflow_a_control.client_schedule_run_history",
        "INSERT INTO workflow_a_control.client_dataset_recovery_run",
        "UPDATE workflow_a_control.client_dataset_recovery_run",
        "DELETE FROM workflow_a_control.client_dataset_recovery_run",
        "UPDATE workflow_a_control.client_dataset_coverage",
        "DELETE FROM workflow_a_control.client_dataset_coverage",
        "ON CONFLICT",
    ):
        assert forbidden not in text, forbidden

    # Exactly one coverage INSERT exists, and it lives in the named function.
    assert text.count(f"INSERT INTO {COVERAGE_TABLE}") == 1
    insert_at = text.index(f"INSERT INTO {COVERAGE_TABLE}")
    owner = text.rindex("def _insert_initial_coverage_row", 0, insert_at)
    following = text.find("\ndef ", owner + 1)
    assert following > insert_at, "the coverage INSERT escaped its owning function"


def test_write_predicate_is_bound_to_the_target_schedule() -> None:
    """The insert and its read-back key on the preflight-verified schedule id."""
    text = (REPO_ROOT / "ops" / "bootstrap_telematics_trips_coverage.py").read_text(
        encoding="utf-8"
    )
    assert "\"client_code\": client[\"client_code\"]," in text
    assert "\"dataset_name\": schedule[\"dataset_name\"]," in text
    assert "WHERE schedule_id = %s" in text


# ---------------------------------------------------------------------------
# Disposable PostgreSQL — a mixed fleet must not block the target
# ---------------------------------------------------------------------------

def test_fully_strict_fleet_passes_dry_run(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=())
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        assert bundle["audit_classification"] == audit.CLASSIFICATION_COMPLETE
        before = _snapshot(conn)
        code, plan = writer.run(_writer_args(dsn, path, bundle["bundle_sha256"]))
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["non_target_compatibility_client_count"] == 0
    assert plan["non_target_compatibility_client_codes"] == []
    assert plan["target_pagination_mode"] == "strict_meta"
    assert plan["target_coverage_row_count"] == 0
    assert plan["target_recovery_row_count"] == 0
    assert plan["target_active_recovery_count"] == 0
    assert plan["rows_to_insert"] == 1
    assert plan["database_writes_performed"] == 0
    assert _snapshot(conn) == before


def test_one_non_target_compatibility_client_does_not_block(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=((COMPAT_CODE, COMPAT_CLIENT_ID,
                                       COMPAT_SCHEDULE),))
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        before = _snapshot(conn)
        code, plan = writer.run(_writer_args(dsn, path, bundle["bundle_sha256"]))
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["non_target_compatibility_client_count"] == 1
    assert plan["non_target_compatibility_client_codes"] == [COMPAT_CODE]
    # The non-target's own coverage and recovery rows are invisible to the
    # target's gates.
    assert plan["target_coverage_row_count"] == 0
    assert plan["target_recovery_row_count"] == 0
    assert _snapshot(conn) == before


def test_bravo_compatibility_state_does_not_block_alpha(conn, dsn: str) -> None:
    """The exact production shape the fleet-wide gate made impossible."""
    build_fleet(conn, compat_clients=FULL_FLEET)
    assert conn.execute(
        f"SELECT count(*) AS n FROM {ACCOUNT_TABLE} "
        "WHERE trips_pagination_mode = 'data_invariants_v1'"
    ).fetchone()["n"] == 3
    assert conn.execute(
        f"SELECT count(*) AS n FROM {RECOVERY_TABLE}"
    ).fetchone()["n"] == 1
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        code, plan = writer.run(_writer_args(dsn, path, bundle["bundle_sha256"]))
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["target_client_code"] == TARGET_CODE
    assert plan["non_target_compatibility_client_count"] == 3
    assert plan["non_target_compatibility_client_codes"] == [
        COMPAT_CODE, "FOXTROT00001", "DELTA00001",
    ]
    assert plan["target_recovery_row_count"] == 0, \
        "another client's recovery must never count against this target"


def test_multiple_compatibility_clients_do_not_block(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=EXTRA_COMPAT)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        code, plan = writer.run(_writer_args(dsn, path, bundle["bundle_sha256"]))
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["non_target_compatibility_client_count"] == 2


def test_dry_run_makes_no_external_network_call(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        before = _snapshot(conn)
        with _no_external_network():
            code, plan = writer.run(
                _writer_args(dsn, path, bundle["bundle_sha256"])
            )
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["provider_requests"] == 0
    assert plan["subprocesses_launched"] == 0
    assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# Disposable PostgreSQL — the target's own gates still fail closed
# ---------------------------------------------------------------------------

def test_target_in_compatibility_mode_is_refused(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        conn.execute(
            f"UPDATE {ACCOUNT_TABLE} SET trips_pagination_mode = "
            "'data_invariants_v1' WHERE client_id = %s", (TARGET_CLIENT_ID,)
        )
        conn.commit()
        before = _snapshot(conn)
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT")
        _expect_refusal(
            dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT",
            execute=True, confirm_client_code=TARGET_CODE,
        )
        conn.rollback()
        assert _snapshot(conn) == before, "a refused target must not be written"


def test_target_with_existing_coverage_row_is_refused(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        _add_coverage(
            conn,
            client_id=TARGET_CLIENT_ID,
            client_code=TARGET_CODE,
            schedule_id=TARGET_SCHEDULE,
            coverage_start=GOOD_A,
            covered_through=GOOD_W,
            source="bootstrap",
        )
        conn.commit()
        before = _snapshot(conn)
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_ROW")
        _expect_refusal(
            dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_ROW",
            execute=True, confirm_client_code=TARGET_CODE,
        )
        conn.rollback()
        assert _snapshot(conn) == before


def test_target_with_active_recovery_is_refused(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        for status in ("PLANNED", "RUNNING"):
            _add_recovery(
                conn,
                client_id=TARGET_CLIENT_ID,
                client_code=TARGET_CODE,
                schedule_id=TARGET_SCHEDULE,
                status=status,
                approval_ref=f"OPS-ACTIVE-{status}",
            )
            conn.commit()
            before = _snapshot(conn)
            _expect_refusal(
                dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_RECOVERY"
            )
            _expect_refusal(
                dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_RECOVERY",
                execute=True, confirm_client_code=TARGET_CODE,
            )
            conn.rollback()
            assert _snapshot(conn) == before
            conn.execute(
                f"DELETE FROM {RECOVERY_TABLE} WHERE client_id = %s",
                (TARGET_CLIENT_ID,),
            )
            conn.commit()

        # A terminal recovery for the target is equally disqualifying: the
        # client is no longer in a fresh pre-bootstrap state.
        _add_recovery(
            conn,
            client_id=TARGET_CLIENT_ID,
            client_code=TARGET_CODE,
            schedule_id=TARGET_SCHEDULE,
            status="SUCCESS",
            approval_ref="OPS-TERMINAL",
        )
        conn.commit()
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_RECOVERY")
        conn.execute(
            f"DELETE FROM {RECOVERY_TABLE} WHERE client_id = %s",
            (TARGET_CLIENT_ID,),
        )
        conn.commit()
    conn.rollback()


def test_target_with_running_history_row_is_refused(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        conn.execute(
            f"""
            INSERT INTO {HISTORY_TABLE}
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s, %s, %s, 'trips_sync', '2026-08-02T02:00:00Z',
                    '2026-08-02T02:00:00Z', '2026-08-02T02:00:00Z', 'RUNNING')
            """,
            (TARGET_SCHEDULE, TARGET_CLIENT_ID, TARGET_CODE),
        )
        conn.commit()
        before = _snapshot(conn)
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT")
        conn.rollback()
        assert _snapshot(conn) == before
        conn.execute(
            f"DELETE FROM {HISTORY_TABLE} WHERE status = 'RUNNING'"
        )
        conn.commit()

        # A RUNNING row belonging to another client is not the target's problem.
        conn.execute(
            f"""
            INSERT INTO {HISTORY_TABLE}
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s, %s, %s, 'trips_sync', '2026-08-02T02:00:00Z',
                    '2026-08-02T02:00:00Z', '2026-08-02T02:00:00Z', 'RUNNING')
            """,
            (COMPAT_SCHEDULE, COMPAT_CLIENT_ID, COMPAT_CODE),
        )
        conn.commit()
        code, plan = writer.run(_writer_args(dsn, path, digest))
        assert code == writer.EXIT_OK
        assert plan["target_running_history_row_count"] == 0
    conn.rollback()


def test_evidence_for_another_client_or_schedule_is_refused(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        for changes, expected in (
            ({"client_code": COMPAT_CODE}, "BOOTSTRAP_REFUSED_EVIDENCE"),
            ({"client_id": COMPAT_CLIENT_ID}, "BOOTSTRAP_REFUSED_PREFLIGHT"),
            ({"schedule_id": COMPAT_SCHEDULE}, "BOOTSTRAP_REFUSED_PREFLIGHT"),
        ):
            mutated = {**bundle, **changes}
            mutated["bundle_sha256"] = audit.bundle_sha256(mutated)
            path.write_text(
                audit.canonical_json(mutated) + "\n", encoding="utf-8"
            )
            _expect_refusal(dsn, path, mutated["bundle_sha256"], expected)
    conn.rollback()


# ---------------------------------------------------------------------------
# Disposable PostgreSQL — execution against a mixed fleet
# ---------------------------------------------------------------------------

def test_execute_inserts_only_the_target_row(conn, dsn: str) -> None:
    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]

        accounts_before = conn.execute(
            f"SELECT * FROM {ACCOUNT_TABLE} ORDER BY client_id"
        ).fetchall()
        schedules_before = conn.execute(
            f"SELECT * FROM {SCHEDULE_TABLE} ORDER BY schedule_id"
        ).fetchall()
        history_before = conn.execute(
            f"SELECT * FROM {HISTORY_TABLE} ORDER BY 1"
        ).fetchall()
        recovery_before = conn.execute(
            f"SELECT * FROM {RECOVERY_TABLE} ORDER BY recovery_run_id"
        ).fetchall()
        non_target_coverage_before = _non_target_coverage(conn)
        assert len(non_target_coverage_before) == 3

        with _no_external_network():
            code, plan = writer.run(_writer_args(
                dsn, path, digest,
                execute=True, confirm_client_code=TARGET_CODE,
            ))
        assert code == writer.EXIT_OK
        assert plan["mode"] == "EXECUTE"
        assert plan["affected_row_count"] == 1
        assert plan["database_writes_performed"] == 1
        assert plan["transaction_result"] == "COMMITTED"
        assert plan["client_mode_changes"] == 0
        assert plan["schedule_changes"] == 0
        assert plan["history_mutations"] == 0
        assert plan["recovery_rows_created"] == 0
        assert plan["non_target_coverage_rows_touched"] == 0
        assert plan["non_target_compatibility_client_count"] == 3

        conn.rollback()

        # Exactly one new coverage row, and it belongs to the target.
        target_rows = conn.execute(
            f"SELECT * FROM {COVERAGE_TABLE} WHERE client_id = %s",
            (TARGET_CLIENT_ID,),
        ).fetchall()
        assert len(target_rows) == 1
        row = dict(target_rows[0])
        assert str(row["schedule_id"]) == TARGET_SCHEDULE
        assert row["client_code"] == TARGET_CODE
        assert row["dataset_name"] == "trips_sync"
        assert audit.iso_utc(row["coverage_start_ts"]) == GOOD_A
        assert audit.iso_utc(row["covered_through_ts"]) == GOOD_W
        assert row["bootstrap_status"] == "READY"
        assert row["covered_through_source"] == "bootstrap"
        assert row["last_gap_detected_ts"] is None
        assert row["seeded_by"] == SEEDED_BY

        # Every non-target coverage row is byte-identical.
        assert _non_target_coverage(conn) == non_target_coverage_before
        assert conn.execute(
            f"SELECT count(*) AS n FROM {COVERAGE_TABLE}"
        ).fetchone()["n"] == 4

        # No mode, schedule, history or recovery mutation of any kind.
        assert conn.execute(
            f"SELECT * FROM {ACCOUNT_TABLE} ORDER BY client_id"
        ).fetchall() == accounts_before
        assert conn.execute(
            f"SELECT * FROM {SCHEDULE_TABLE} ORDER BY schedule_id"
        ).fetchall() == schedules_before
        assert conn.execute(
            f"SELECT * FROM {HISTORY_TABLE} ORDER BY 1"
        ).fetchall() == history_before
        assert conn.execute(
            f"SELECT * FROM {RECOVERY_TABLE} ORDER BY recovery_run_id"
        ).fetchall() == recovery_before
        assert conn.execute(
            f"SELECT count(*) AS n FROM {ACCOUNT_TABLE} "
            "WHERE trips_pagination_mode = 'data_invariants_v1'"
        ).fetchone()["n"] == 3, "bootstrap never changes a pagination mode"

        # A second execution is refused and overwrites nothing.
        _expect_refusal(
            dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_ROW",
            execute=True, confirm_client_code=TARGET_CODE,
        )
        conn.rollback()
        assert conn.execute(
            f"SELECT * FROM {COVERAGE_TABLE} WHERE client_id = %s",
            (TARGET_CLIENT_ID,),
        ).fetchall() == target_rows
    conn.rollback()


def test_concurrent_target_insert_refuses_without_overwriting(conn, dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    build_fleet(conn, compat_clients=FULL_FLEET)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        params = {
            "schedule_id": TARGET_SCHEDULE,
            "client_id": TARGET_CLIENT_ID,
            "client_code": TARGET_CODE,
            "dataset_name": "trips_sync",
            "coverage_start_ts": audit.parse_iso_utc(GOOD_A, label="A"),
            "covered_through_ts": audit.parse_iso_utc(GOOD_W, label="W"),
            "bootstrap_status": "READY",
            "bootstrap_evidence_ref": writer.build_evidence_ref(
                evidence_sha256=digest, approval_ref=APPROVAL_REF
            ),
            "seeded_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
            "seeded_by": "the-winner",
            "covered_through_source": "bootstrap",
            "updated_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        }
        other = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        try:
            with other.cursor() as cur:
                writer._insert_initial_coverage_row(cur, params)
        finally:
            other.close()
        conn.rollback()
        winner = conn.execute(
            f"SELECT * FROM {COVERAGE_TABLE} WHERE client_id = %s",
            (TARGET_CLIENT_ID,),
        ).fetchall()

        write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        try:
            writer.execute_bootstrap(
                write_conn, schedule_id=TARGET_SCHEDULE,
                params={**params, "seeded_by": SEEDED_BY},
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
            f"SELECT * FROM {COVERAGE_TABLE} WHERE client_id = %s",
            (TARGET_CLIENT_ID,),
        ).fetchall() == winner, "the loser must not overwrite the winning row"
        assert len(_non_target_coverage(conn)) == 3
    conn.rollback()


def test_recovery_appearing_before_the_insert_is_a_conflict(conn, dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    build_fleet(conn, compat_clients=FULL_FLEET)
    _add_recovery(
        conn,
        client_id=TARGET_CLIENT_ID,
        client_code=TARGET_CODE,
        schedule_id=TARGET_SCHEDULE,
        status="PLANNED",
        approval_ref="OPS-RACE",
    )
    conn.commit()
    params = {
        "schedule_id": TARGET_SCHEDULE,
        "client_id": TARGET_CLIENT_ID,
        "client_code": TARGET_CODE,
        "dataset_name": "trips_sync",
        "coverage_start_ts": audit.parse_iso_utc(GOOD_A, label="A"),
        "covered_through_ts": audit.parse_iso_utc(GOOD_W, label="W"),
        "bootstrap_status": "READY",
        "bootstrap_evidence_ref": writer.build_evidence_ref(
            evidence_sha256="a" * 64, approval_ref=APPROVAL_REF
        ),
        "seeded_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        "seeded_by": SEEDED_BY,
        "covered_through_source": "bootstrap",
        "updated_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    }
    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        writer.execute_bootstrap(
            write_conn, schedule_id=TARGET_SCHEDULE, params=params
        )
    except writer.BootstrapRefused as exc:
        assert exc.code == "BOOTSTRAP_WRITE_CONFLICT"
    else:
        raise AssertionError("a recovery race was accepted")
    finally:
        write_conn.close()
    conn.rollback()
    assert conn.execute(
        f"SELECT count(*) AS n FROM {COVERAGE_TABLE} WHERE client_id = %s",
        (TARGET_CLIENT_ID,),
    ).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        test_fully_strict_fleet_passes_dry_run(conn, dsn)
        test_one_non_target_compatibility_client_does_not_block(conn, dsn)
        test_bravo_compatibility_state_does_not_block_alpha(conn, dsn)
        test_multiple_compatibility_clients_do_not_block(conn, dsn)
        test_dry_run_makes_no_external_network_call(conn, dsn)
        test_target_in_compatibility_mode_is_refused(conn, dsn)
        test_target_with_existing_coverage_row_is_refused(conn, dsn)
        test_target_with_active_recovery_is_refused(conn, dsn)
        test_target_with_running_history_row_is_refused(conn, dsn)
        test_evidence_for_another_client_or_schedule_is_refused(conn, dsn)
        test_execute_inserts_only_the_target_row(conn, dsn)
        test_concurrent_target_insert_refuses_without_overwriting(conn, dsn)
        test_recovery_appearing_before_the_insert_is_a_conflict(conn, dsn)
        conn.rollback()


def main() -> None:
    test_writer_never_touches_a_non_target_surface()
    test_write_predicate_is_bound_to_the_target_schedule()
    dsn = os.getenv("TELEMATICS_BOOTSTRAP_MULTI_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_BOOTSTRAP_MULTI_TEST_DSN",
        )
        test_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL per-client bootstrap checks")
    else:
        print("SKIP: set TELEMATICS_BOOTSTRAP_MULTI_TEST_DSN for PostgreSQL checks")
    print("OK - Telematics per-client coverage bootstrap checks passed")


if __name__ == "__main__":
    main()
