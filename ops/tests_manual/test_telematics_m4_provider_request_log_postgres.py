#!/usr/bin/env python3
"""M4 — durable evidence, the shared transaction boundary, and first-seen.

WHAT THIS PINS, and why each part needs a real database.

    1. **Both migrations apply and replay.** The platform relation and the
       additive client-business column, each applied twice, because a migration
       that cannot be replayed cannot be rolled out safely.
    2. **The constraints are real.** The terminal-state vocabulary, the natural
       uniqueness that makes a duplicate request record structurally impossible,
       and the interval checks are asserted against PostgreSQL rather than
       against the Python that also enforces them. A check that exists only in
       Python is not a check a direct SQL writer has to obey.
    3. **Hinge 2 holds.** The durable evidence and the watermark share one
       transaction: neither commits without the other, in either direction. This
       is the M4 invariant and it is not observable without a real commit.
    4. **First-seen provenance is immutable.** Set on INSERT, unchanged by an
       overlapping re-upsert that rewrites `synced_at` — proven by running the
       real `ON CONFLICT` shape, not by reading the source.

    The tiling and completeness *decisions* are pure and live in
    `test_telematics_m4_window_completeness.py`. Nothing here re-litigates them.

DESTRUCTIVE. Drops and recreates its own schemas. The loopback guard runs
before any connection is attempted.
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobs.api.telematics import dispatcher as d  # noqa: E402
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED, COVERAGE_STATUS_READY, CoverageGateResult,
)
from jobs.trips_pagination_mode import (  # noqa: E402
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 as MODE,
)
from jobs.api.telematics.request_evidence import (  # noqa: E402
    REQUEST_STATUS_FINALIZED,
    REQUEST_STATUS_PENDING,
    persist_pending_request_facts,
)
from ops.tests_manual import telematics_execution_outcome_fixtures as eo_fixtures  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_M4_EVIDENCE_TEST_DSN"

PLATFORM_MIGRATIONS = (
    "008_workflow_a_control_plane.sql", "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "061_workflow_a_provider_request_log.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
)
CLIENT_MIGRATION = "047_client_trips_first_seen_request_id.sql"

CID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
SID = "7cac378a-5787-4d62-85d1-282bed208c8c"
PRID = "44444444-4444-4444-8444-444444444444"
CODE = "TST00001"
F = datetime(2026, 8, 2, tzinfo=timezone.utc)
WS, WE = F - timedelta(days=4), F - timedelta(hours=3)
A, W = datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc)
SEEDED = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
OLD = datetime(2026, 7, 1, 13, tzinfo=timezone.utc)

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _refuses(label: str, fn, conn) -> None:
    """Assert PostgreSQL itself rejects `fn()`, not just the Python above it."""
    try:
        fn()
        conn.rollback()
        _check(label, False, "the database accepted it")
    except Exception as exc:
        conn.rollback()
        _check(label, True)
        del exc


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def apply_platform(conn, *, drop: bool = True) -> None:
    if drop:
        conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in PLATFORM_MIGRATIONS:
        conn.execute((ROOT / "db/migrations" / name).read_text())
    conn.commit()


def seed_control_plane(conn) -> None:
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
           enabled,trips_pagination_mode)
          VALUES (%s,%s,'Test','telematics','https://example.invalid','u','REF',
                  '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s)
           ON CONFLICT (client_id) DO NOTHING""",
        (CID, CODE, MODE),
    )
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
           run_time,timezone,lookback_days,overwrite_existing)
          VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',4,true)
           ON CONFLICT (schedule_id) DO NOTHING""",
        (SID, CID, CODE),
    )
    conn.commit()


def schedule_row():
    return d.ScheduleRow(
        SID, CID, CODE, "Test", "trips_sync",
        "jobs.api.telematics.sync_trips_and_speeding", True, "daily", None, None,
        False, time(2), "UTC", 4, True, "enabled", MODE,
    )


def gate():
    return CoverageGateResult(
        True, COVERAGE_GATE_ALLOWED, None, "fixture", None, False, A, W,
        COVERAGE_STATUS_READY,
    )


def seed_claim(conn, *, w=W):
    conn.execute("DELETE FROM workflow_a_control.provider_request_log")
    conn.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_coverage
          (schedule_id,client_id,client_code,dataset_name,coverage_start_ts,
           covered_through_ts,bootstrap_status,bootstrap_evidence_ref,seeded_at,
           seeded_by,covered_through_source,last_gap_detected_ts,updated_at)
          VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY','artifact:safe',%s,
                  'operator','bootstrap',NULL,%s)""",
        (SID, CID, CODE, A, w, SEEDED, OLD),
    )
    hid = conn.execute(
        """INSERT INTO workflow_a_control.client_schedule_run_history
          (schedule_id,client_id,client_code,dataset_name,window_start_ts,
           window_end_ts,scheduled_fire_ts,status,started_at,
           nominal_window_start_ts,nominal_window_end_ts,
           stabilization_delay_seconds,overlap_seconds,trips_pagination_mode)
          VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'RUNNING',%s,%s,%s,10800,3600,%s)
          RETURNING run_history_id::text""",
        (SID, CID, CODE, WS, WE, F, F, WS, F, MODE),
    ).fetchone()[0]
    conn.commit()
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    conn.rollback()
    prepared = d.PreparedDispatcherRun(
        conn, schedule_row(), F, WS, WE, hid, [], 720, False, WS, F, state,
        gate(),
    )
    return prepared


def seed_claim_keep_evidence(conn, *, w=W):
    """A fresh claim that preserves existing request facts.

    `seed_claim` wipes the evidence table so each case starts clean. P1 needs
    two fires' facts to coexist, which is the whole point of the case.
    """
    conn.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.execute(
        """INSERT INTO workflow_a_control.client_dataset_coverage
          (schedule_id,client_id,client_code,dataset_name,coverage_start_ts,
           covered_through_ts,bootstrap_status,bootstrap_evidence_ref,seeded_at,
           seeded_by,covered_through_source,last_gap_detected_ts,updated_at)
          VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY','artifact:safe',%s,
                  'operator','bootstrap',NULL,%s)""",
        (SID, CID, CODE, A, w, SEEDED, OLD),
    )
    hid = conn.execute(
        """INSERT INTO workflow_a_control.client_schedule_run_history
          (schedule_id,client_id,client_code,dataset_name,window_start_ts,
           window_end_ts,scheduled_fire_ts,status,started_at,
           nominal_window_start_ts,nominal_window_end_ts,
           stabilization_delay_seconds,overlap_seconds,trips_pagination_mode)
          VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'RUNNING',%s,%s,%s,10800,3600,%s)
          RETURNING run_history_id::text""",
        (SID, CID, CODE, WS, WE, F, F, WS, F, MODE),
    ).fetchone()[0]
    conn.commit()
    state = d._load_coverage_state(conn, client_id=CID, dataset_name="trips_sync")
    conn.rollback()
    return d.PreparedDispatcherRun(
        conn, schedule_row(), F, WS, WE, hid, [], 720, False, WS, F, state, gate(),
    )


def coverage(conn):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id=%s", (SID,),
        )
        row = dict(cur.fetchone())
    conn.rollback()
    return row


def evidence(conn):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM workflow_a_control.provider_request_log "
            "ORDER BY sub_window_index, page"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


# ---------------------------------------------------------------------------
# 1 — migrations
# ---------------------------------------------------------------------------

def test_platform_migration_applies_and_replays(conn) -> None:
    print("\n## test_platform_migration_applies_and_replays")
    apply_platform(conn)
    _check("platform migration 061 applies", True)

    exists = conn.execute(
        "SELECT to_regclass('workflow_a_control.provider_request_log')"
    ).fetchone()[0]
    conn.rollback()
    _check("the relation exists", exists is not None)

    # Replay. `ops/db_migrate.sh` records applied filenames and applies each
    # file exactly once, so the property that matters is that THIS file
    # converges when re-applied — not that the whole historical set can be
    # replayed onto an already-migrated database. (It cannot, and deliberately
    # so: migration 014 TRUNCATEs `client_schedule_run_history`, which this
    # table now references. The 061 header states that consequence explicitly.)
    conn.execute((ROOT / "db/migrations" / PLATFORM_MIGRATIONS[-1]).read_text())
    conn.commit()
    conn.execute((ROOT / "db/migrations" / PLATFORM_MIGRATIONS[-1]).read_text())
    conn.commit()
    _check("platform migration 061 replays idempotently", True)

    # And the documented consequence is real, not hypothetical.
    try:
        conn.execute(
            "TRUNCATE workflow_a_control.client_schedule_run_history"
        )
        conn.rollback()
        truncate_blocked = False
    except Exception:
        conn.rollback()
        truncate_blocked = True
    _check("the run-history FK really does block a bare TRUNCATE, as documented",
           truncate_blocked)

    cols = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='workflow_a_control' "
            "AND table_name='provider_request_log'"
        ).fetchall()
    }
    conn.rollback()
    for column in (
        "request_id", "run_history_id", "platform_run_id", "client_id",
        "schedule_id", "dataset_name", "endpoint", "sub_window_index",
        "sub_window_label", "covers_from_ts", "covers_to_ts",
        "requested_from_ts", "requested_to_ts", "wire_start_value",
        "wire_end_value", "page", "request_started_at_utc",
        "response_received_at_utc", "http_status", "row_count",
        "subwindow_complete", "termination_reason", "total_reconciliation",
        "recorded_at",
    ):
        _check(f"column {column} exists", column in cols)
    _check("request_id is a uuid", cols.get("request_id") == "uuid")
    _check("subwindow_complete is a boolean",
           cols.get("subwindow_complete") == "boolean")


def test_no_cross_database_foreign_key_is_attempted(conn) -> None:
    print("\n## test_no_cross_database_foreign_key_is_attempted")
    fks = conn.execute(
        """SELECT confrelid::regclass::text
             FROM pg_constraint
            WHERE contype='f'
              AND conrelid='workflow_a_control.provider_request_log'::regclass
            ORDER BY 1"""
    ).fetchall()
    conn.rollback()
    targets = {row[0] for row in fks}
    _check("FK to the run-history row exists",
           "workflow_a_control.client_schedule_run_history" in targets)
    _check("FK to the client account exists",
           "workflow_a_control.client_account" in targets)
    # `platform_run_id` must NOT be a foreign key: `api/platform_prune.py`
    # deletes `public.runs` rows on its own horizon, and an enforced FK would
    # either abort that prune or cascade-delete evidence this table must retain.
    _check("no foreign key to public.runs",
           not any("runs" in target for target in targets),
           f"targets={sorted(targets)}")
    # And no attempt at the impossible cross-database reference.
    _check("no foreign key to client_trips",
           not any("client_trips" in target for target in targets))


def test_client_business_migration_applies_and_replays(conn) -> None:
    print("\n## test_client_business_migration_applies_and_replays")
    conn.execute("DROP SCHEMA IF EXISTS m4_client CASCADE")
    conn.execute("CREATE SCHEMA m4_client")
    # A minimal stand-in carrying only what M4 touches: the conflict target and
    # the two last-touched columns the column must NOT behave like.
    conn.execute(
        """CREATE TABLE m4_client.client_trips (
             client_id UUID NOT NULL,
             provider_trip_id BIGINT NOT NULL,
             synced_at TIMESTAMPTZ NULL,
             sync_run_id TEXT NULL,
             "Dysponent_ID" TEXT NULL,
             PRIMARY KEY (client_id, provider_trip_id)
           )"""
    )
    conn.commit()

    ddl = (ROOT / "db/client_business" / CLIENT_MIGRATION).read_text()
    scoped = ddl.replace("public.client_trips", "m4_client.client_trips")
    # No backfill, ever (docs/20 §21.6). Asserted against the executable
    # statements only — the header prose legitimately discusses
    # `ON CONFLICT DO UPDATE SET`, and a substring match over comments would
    # make this check about wording rather than about behaviour.
    executable = " ".join(
        line for line in ddl.splitlines() if not line.strip().startswith("--")
    )
    # Strip single-quoted literals before splitting on `;`: the COMMENT body is
    # prose and contains both semicolons and the word UPDATE, and neither is a
    # statement. This keeps the check about executable SQL.
    executable = re.sub(r"'(?:[^']|'')*'", "''", executable)
    verbs = [
        statement.strip().split(None, 1)[0].upper()
        for statement in executable.split(";")
        if statement.strip()
    ]
    _check("the migration is pure DDL with no data backfill",
           set(verbs) <= {"ALTER", "COMMENT"},
           "a first-seen backfill would fabricate exactly the evidence M4 "
           f"exists to make trustworthy; statement verbs={verbs}")
    conn.execute(scoped)
    conn.commit()
    conn.execute(scoped)
    conn.commit()
    _check("client-business migration 047 applies and replays", True)

    column = conn.execute(
        """SELECT data_type, is_nullable FROM information_schema.columns
            WHERE table_schema='m4_client' AND table_name='client_trips'
              AND column_name='first_seen_request_id'"""
    ).fetchone()
    conn.rollback()
    _check("first_seen_request_id exists", column is not None)
    if column is not None:
        _check("first_seen_request_id is uuid", column[0] == "uuid")
        _check("first_seen_request_id is nullable", column[1] == "YES")

    fks = conn.execute(
        """SELECT count(*) FROM pg_constraint
            WHERE contype='f' AND conrelid='m4_client.client_trips'::regclass"""
    ).fetchone()[0]
    conn.rollback()
    _check("no foreign key is attempted across databases", fks == 0)


def test_new_client_and_existing_client_paths_agree(conn) -> None:
    print("\n## test_new_client_and_existing_client_paths_agree")
    # Client-business rollout must go through BOTH paths or the two diverge
    # silently: the onboarding baseline for a new client, and the migration
    # runner for existing ones (docs/20 §21.4).
    onboarding = (ROOT / "scripts/onboard_workflow_a_client.py").read_text()
    _check("the new-client baseline applies the migration",
           f'"{CLIENT_MIGRATION}"' in onboarding
           and f"client_business\" / \"{CLIENT_MIGRATION}" in onboarding)
    _check("the new-client baseline records it as applied",
           onboarding.count(f'"{CLIENT_MIGRATION}"') >= 2,
           "a new database would otherwise have the runner re-apply it")
    _check("the existing-client runner discovers it by directory scan",
           (ROOT / "db/client_business" / CLIENT_MIGRATION).exists())


# ---------------------------------------------------------------------------
# 2 — the constraints are real
# ---------------------------------------------------------------------------

def _evidence_values(conn, **overrides):
    """A FINALIZED evidence row, built field by field.

    FINALIZED is the fully-attributed shape, so it is the one that exercises
    every CHECK at once. The PENDING shape is exercised through the real writer
    in the lifecycle tests below, which is where it belongs — a hand-built
    PENDING row would prove nothing about what the job actually writes.
    """
    prepared = overrides.pop("_prepared")
    base = dict(
        request_id=str(uuid.uuid4()), status="FINALIZED",
        run_history_id=prepared.run_history_id,
        platform_run_id=PRID, client_id=CID, client_code=CODE, schedule_id=SID,
        dataset_name="trips_sync", endpoint="/trips",
        effective_window_start_ts=WS, effective_window_end_ts=WE,
        sub_window_index=1, sub_window_label="sw", covers_from_ts=WS,
        covers_to_ts=WE, requested_from_ts=WS, requested_to_ts=WE,
        wire_start_value="2026-07-29 02:00:00",
        wire_end_value="2026-08-01 21:00:00", page=1,
        request_started_at_utc=F, response_received_at_utc=F, http_status=200,
        row_count=5, subwindow_complete=True, termination_reason="short_page",
        total_reconciliation="absent", finalized_at=F,
    )
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join(["%s"] * len(base))
    return (
        f"INSERT INTO workflow_a_control.provider_request_log ({columns}) "
        f"VALUES ({placeholders})",
        tuple(base.values()),
    )


def test_the_database_enforces_the_evidence_contract(conn) -> None:
    print("\n## test_the_database_enforces_the_evidence_contract")
    prepared = seed_claim(conn)

    sql, params = _evidence_values(conn, _prepared=prepared)
    conn.execute(sql, params)
    conn.commit()
    _check("a well-formed evidence row is accepted", True)

    # Natural uniqueness makes a duplicate request record structurally
    # impossible rather than something to detect at read time.
    dup_sql, dup_params = _evidence_values(
        conn, _prepared=prepared, sub_window_index=1, page=1,
    )
    _refuses("a duplicate (run, endpoint, sub-window, page) is rejected",
             lambda: conn.execute(dup_sql, dup_params), conn)

    for label, overrides in (
        ("a COMPLETE unit with no termination",
         dict(subwindow_complete=True, termination_reason=None,
              total_reconciliation=None)),
        ("a COMPLETE unit with an unknown reconciliation state",
         dict(total_reconciliation="approximately")),
        ("a COMPLETE unit terminating on something else",
         dict(termination_reason="last_page")),
        ("an INCOMPLETE unit claiming a valid termination",
         dict(subwindow_complete=False)),
        ("a zero-width tiling unit", dict(covers_to_ts=WS)),
        ("a backwards tiling unit", dict(covers_to_ts=WS - timedelta(days=1))),
        ("page zero", dict(page=0)),
        ("a negative row count", dict(row_count=-1)),
        ("a response received before its request",
         dict(response_received_at_utc=F - timedelta(hours=1))),
        ("a sub-window index of zero", dict(sub_window_index=0)),
        ("an unknown lifecycle status", dict(status="MAYBE")),
        # The constraint that keeps "provenance exists" from masquerading as
        # "coverage-complete". A PENDING row may carry no fire attribution and
        # no completeness claim, enforced by the database and not only by the
        # writer.
        ("a PENDING row claiming completeness",
         dict(status="PENDING", run_history_id=None, client_id=None,
              client_code=None, schedule_id=None, dataset_name=None,
              finalized_at=None)),
        ("a PENDING row attributed to a fire",
         dict(status="PENDING", subwindow_complete=None,
              termination_reason=None, total_reconciliation=None,
              finalized_at=None)),
        # ...and the mirror: a FINALIZED row must be fully attributed.
        ("a FINALIZED row with no run_history_id", dict(run_history_id=None)),
        ("a FINALIZED row with no completeness verdict",
         dict(subwindow_complete=None, termination_reason=None,
              total_reconciliation=None)),
        ("a FINALIZED row with no finalized_at", dict(finalized_at=None)),
    ):
        bad_sql, bad_params = _evidence_values(
            conn, _prepared=prepared, **overrides
        )
        _refuses(f"{label} is rejected",
                 lambda s=bad_sql, p=bad_params: conn.execute(s, p), conn)

    # An INCOMPLETE unit that claims neither is legitimate forensic evidence.
    ok_sql, ok_params = _evidence_values(
        conn, _prepared=prepared, sub_window_index=2, subwindow_complete=False,
        termination_reason=None, total_reconciliation=None,
    )
    conn.execute(ok_sql, ok_params)
    conn.commit()
    _check("an INCOMPLETE unit is storable as forensic evidence", True)

    _check("the evidence really landed", len(evidence(conn)) == 2)


def test_evidence_is_removed_with_its_run_history_row(conn) -> None:
    print("\n## test_evidence_is_removed_with_its_run_history_row")
    prepared = seed_claim(conn)
    sql, params = _evidence_values(conn, _prepared=prepared)
    conn.execute(sql, params)
    conn.commit()
    _check("evidence exists before the cascade", len(evidence(conn)) == 1)
    conn.execute(
        "DELETE FROM workflow_a_control.client_schedule_run_history "
        "WHERE run_history_id=%s", (prepared.run_history_id,),
    )
    conn.commit()
    _check("evidence does not outlive the fire it describes",
           evidence(conn) == [])


# ---------------------------------------------------------------------------
# 3 — hinge 2: the shared transaction boundary
# ---------------------------------------------------------------------------

def _pending(conn, proof, *, platform_run_id: str = PRID) -> int:
    """Write this execution's request facts exactly as the child job does."""
    written = persist_pending_request_facts(
        conn, platform_run_id=platform_run_id, completeness=proof,
    )
    conn.commit()
    return written


def test_evidence_and_watermark_commit_together(conn) -> None:
    print("\n## test_evidence_and_watermark_commit_together")
    prepared = seed_claim(conn)
    proof = eo_fixtures.complete_window(
        window_start_ts=WS, window_end_ts=WE, subwindows=2,
    )
    # Step 1 of the handoff: the child made its request facts durable before
    # its business transaction committed. Finalization promotes them.
    _pending(conn, proof)
    pending_rows = evidence(conn)
    _check("request facts are durable before finalization",
           len(pending_rows) == proof.page_count
           and all(r["status"] == "PENDING" for r in pending_rows))
    _check("a request fact claims no fire and no completeness",
           all(r["run_history_id"] is None and r["subwindow_complete"] is None
               for r in pending_rows))

    moved = d._finalize_compat_success(
        conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
    )
    _check("the watermark advanced", moved is True)
    cov = coverage(conn)
    _check("covered_through_ts is exactly E_end", cov["covered_through_ts"] == WE)

    rows = evidence(conn)
    _check("one durable row per page request", len(rows) == proof.page_count)
    _check("the tiling is reconstructible from the durable rows alone",
           [(r["covers_from_ts"], r["covers_to_ts"]) for r in rows]
           == [(s.covers_from_ts, s.covers_to_ts) for s in proof.subwindows])
    _check("every durable row is marked complete",
           all(r["subwindow_complete"] for r in rows))
    _check("identity columns come from the dispatcher's claim",
           all(str(r["run_history_id"]) == prepared.run_history_id
               and str(r["platform_run_id"]) == PRID
               and str(r["client_id"]) == CID
               and str(r["schedule_id"]) == SID
               and r["dataset_name"] == "trips_sync"
               for r in rows))
    _check("the request identities are the ones the child minted",
           {str(r["request_id"]) for r in rows}
           == {p.request_id for s in proof.subwindows for p in s.pages})


def test_a_failed_evidence_projection_leaves_the_watermark_alone(conn) -> None:
    print("\n## test_a_failed_evidence_projection_leaves_the_watermark_alone")
    import psycopg
    saved = d._platform_pg_conn
    d._platform_pg_conn = lambda: psycopg.connect(os.environ[ENV])
    try:
        prepared = seed_claim(conn)
        proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
        # The child already made its request facts durable — that commit is
        # what the business transaction's `first_seen_request_id` depends on,
        # and it must survive this failure.
        _pending(conn, proof)
        before = coverage(conn)
        # Force the promotion to fail the way a real defect would: a trigger
        # that rejects the UPDATE. Everything else about the fire is healthy.
        conn.execute(
            """CREATE OR REPLACE FUNCTION workflow_a_control.m4_block_evidence()
               RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'forced evidence promotion failure'; END $$"""
        )
        conn.execute(
            """CREATE TRIGGER m4_block_evidence
               BEFORE UPDATE ON workflow_a_control.provider_request_log
               FOR EACH ROW EXECUTE FUNCTION workflow_a_control.m4_block_evidence()"""
        )
        conn.commit()
        try:
            d._finalize_compat_success(
                conn, prepared=prepared, completeness=proof,
                platform_run_id=PRID,
            )
            _check("a failed promotion refuses", False)
        except d.CoverageFinalizationError as exc:
            _check("a failed promotion refuses",
                   exc.code == d.TRIPS_WINDOW_EVIDENCE_PROJECTION_FAILED,
                   f"code={exc.code}")

        after = coverage(conn)
        _check("the watermark did not move",
               after["covered_through_ts"] == before["covered_through_ts"]
               and after["covered_through_source"]
               == before["covered_through_source"]
               and after["updated_at"] == before["updated_at"])
        # THE BLOCKER-1 PROPERTY. The promotion rolled back, so nothing became
        # coverage evidence — and the request facts are still there, so a
        # committed `first_seen_request_id` pointing at one of them still
        # resolves. Before the fix this was an empty table and a permanently
        # dangling reference.
        surviving = evidence(conn)
        _check("the request facts survived the failed promotion",
               len(surviving) == proof.page_count)
        _check("and they are still PENDING, so they authorize nothing",
               all(r["status"] == "PENDING" and r["run_history_id"] is None
                   and r["subwindow_complete"] is None for r in surviving))

        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status FROM workflow_a_control.client_schedule_run_history "
                "WHERE run_history_id=%s", (prepared.run_history_id,),
            )
            status = cur.fetchone()["status"]
        conn.rollback()
        _check("the fire is still finalized, not left RUNNING",
               status == "FAILED", f"status={status}")

        conn.execute(
            "DROP TRIGGER m4_block_evidence "
            "ON workflow_a_control.provider_request_log"
        )
        conn.execute("DROP FUNCTION workflow_a_control.m4_block_evidence()")
        conn.commit()
    finally:
        d._platform_pg_conn = saved


def test_the_finalizer_refuses_without_a_proof(conn) -> None:
    print("\n## test_the_finalizer_refuses_without_a_proof")
    prepared = seed_claim(conn)
    before = coverage(conn)
    for label, kwargs in (
        ("no completeness", dict(completeness=None, platform_run_id=PRID)),
        ("no platform run identity", dict(
            completeness=eo_fixtures.complete_window(
                window_start_ts=WS, window_end_ts=WE),
            platform_run_id=None)),
    ):
        try:
            d._finalize_compat_success(conn, prepared=prepared, **kwargs)
            _check(f"{label}: the finalizer refuses", False)
        except ValueError:
            conn.rollback()
            _check(f"{label}: the finalizer refuses", True)
    after = coverage(conn)
    _check("no coverage SQL was performed on refusal",
           after["covered_through_ts"] == before["covered_through_ts"]
           and after["updated_at"] == before["updated_at"])
    _check("no evidence was written on refusal", evidence(conn) == [])


# ---------------------------------------------------------------------------
# 4 — first-seen provenance
# ---------------------------------------------------------------------------

def test_first_seen_is_set_on_insert_and_never_overwritten(conn) -> None:
    print("\n## test_first_seen_is_set_on_insert_and_never_overwritten")
    conn.execute("DELETE FROM m4_client.client_trips")
    conn.commit()

    first_request = str(uuid.uuid4())
    later_request = str(uuid.uuid4())
    first_sync = datetime(2026, 8, 1, tzinfo=timezone.utc)
    later_sync = datetime(2026, 8, 2, tzinfo=timezone.utc)

    # Exactly the shape the job emits: the column is in the INSERT list and
    # deliberately ABSENT from `DO UPDATE SET`, as `Dysponent_ID` is.
    upsert = """
        INSERT INTO m4_client.client_trips
          (client_id, provider_trip_id, synced_at, sync_run_id,
           first_seen_request_id)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
          synced_at = EXCLUDED.synced_at,
          sync_run_id = EXCLUDED.sync_run_id
    """
    conn.execute(upsert, (CID, 1001, first_sync, "run-a", first_request))
    conn.commit()
    row = conn.execute(
        "SELECT first_seen_request_id::text, synced_at, sync_run_id "
        "FROM m4_client.client_trips WHERE provider_trip_id=1001"
    ).fetchone()
    conn.rollback()
    _check("first insert sets first_seen_request_id", row[0] == first_request)

    # The overlapping re-request the new cadence deliberately produces.
    conn.execute(upsert, (CID, 1001, later_sync, "run-b", later_request))
    conn.commit()
    row = conn.execute(
        "SELECT first_seen_request_id::text, synced_at, sync_run_id "
        "FROM m4_client.client_trips WHERE provider_trip_id=1001"
    ).fetchone()
    conn.rollback()
    _check("an overlapping re-upsert preserves the ORIGINAL provenance",
           row[0] == first_request, f"stored={row[0]} later={later_request}")
    _check("while synced_at really was rewritten (it is last-touched)",
           row[1] == later_sync and row[2] == "run-b")
    _check("the two requests were genuinely different identities",
           first_request != later_request)

    # A trip observed on a path that captured no provenance stores the honest
    # NULL, never a sentinel and never `synced_at`.
    conn.execute(upsert, (CID, 1002, first_sync, "run-a", None))
    conn.commit()
    row = conn.execute(
        "SELECT first_seen_request_id FROM m4_client.client_trips "
        "WHERE provider_trip_id=1002"
    ).fetchone()
    conn.rollback()
    _check("an uncaptured provenance stores NULL", row[0] is None)

    # And a later run that DOES have provenance must not retro-fill it: the row
    # already exists, so `DO UPDATE SET` governs, and it does not mention the
    # column. NULL keeps meaning "never captured".
    conn.execute(upsert, (CID, 1002, later_sync, "run-b", later_request))
    conn.commit()
    row = conn.execute(
        "SELECT first_seen_request_id FROM m4_client.client_trips "
        "WHERE provider_trip_id=1002"
    ).fetchone()
    conn.rollback()
    _check("a historical NULL row is never back-filled", row[0] is None)


def test_the_job_upsert_omits_the_column_from_do_update(conn) -> None:
    print("\n## test_the_job_upsert_omits_the_column_from_do_update")
    # The behaviour above is only guaranteed while the real job's SQL keeps the
    # column out of its update list, so that is asserted against the source of
    # the statement that actually runs in production.
    source = (ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    start = source.index("ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET")
    end = source.index('"""', start)
    update_clause = source[start:end]
    _check("first_seen_request_id is absent from DO UPDATE SET",
           "first_seen_request_id" not in update_clause)
    _check("Dysponent_ID is still absent from DO UPDATE SET",
           "Dysponent_ID" not in update_clause)
    _check("synced_at is present, confirming this really is the update list",
           "synced_at=EXCLUDED.synced_at" in update_clause)
    # M-LAG appended `first_seen_response_received_at_utc` after this column, so
    # the INSERT list no longer ends with it. Both halves are asserted instead,
    # which is stronger: the pairing CHECK on `client_trips` refuses a row
    # carrying one without the other, so an INSERT naming only one cannot work.
    _check("first_seen_request_id is in the INSERT list",
           "first_seen_request_id,\n                      "
           "first_seen_response_received_at_utc\n                    )" in source)
    _check("first_seen_response_received_at_utc is absent from DO UPDATE SET",
           "first_seen_response_received_at_utc" not in update_clause)




# ---------------------------------------------------------------------------
# 5 — the cross-database provenance lifecycle (Codex blocker 1)
#
# The defect: `client_trips.first_seen_request_id` was committed in the client
# business transaction BEFORE the platform ever recorded the request. A platform
# failure in between left a committed, correctly-immutable reference to evidence
# that never became durable — permanently.
#
# The fix is ordering plus a two-state row, not a distributed transaction:
# request facts are committed PENDING before the business transaction, and
# promoted to FINALIZED only inside the coverage transaction.
# ---------------------------------------------------------------------------

def test_A1_normal_lifecycle_resolves(conn) -> None:
    print("\n## test_A1_normal_lifecycle_resolves")
    prepared = seed_claim(conn)
    proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
    _pending(conn, proof)
    request_id = proof.subwindows[0].pages[0].request_id

    # The business transaction commits a trip carrying this identity. It can
    # only be committed because the fact is already durable.
    conn.execute("DELETE FROM m4_client.client_trips")
    conn.execute(
        "INSERT INTO m4_client.client_trips "
        "(client_id, provider_trip_id, synced_at, sync_run_id, first_seen_request_id) "
        "VALUES (%s,%s,%s,%s,%s)",
        (CID, 5001, F, "run-a", request_id),
    )
    conn.commit()

    d._finalize_compat_success(
        conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
    )
    rows = {str(r["request_id"]): r for r in evidence(conn)}
    _check("A1: the referenced request resolves", request_id in rows)
    _check("A1: and it is now coverage evidence",
           rows[request_id]["status"] == REQUEST_STATUS_FINALIZED)
    _check("A1: the watermark advanced", coverage(conn)["covered_through_ts"] == WE)


def test_A2_platform_failure_leaves_provenance_resolvable(conn) -> None:
    print("\n## test_A2_platform_failure_leaves_provenance_resolvable")
    import psycopg
    saved = d._platform_pg_conn
    d._platform_pg_conn = lambda: psycopg.connect(os.environ[ENV])
    try:
        prepared = seed_claim(conn)
        proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
        _pending(conn, proof)
        request_id = proof.subwindows[0].pages[0].request_id
        before = coverage(conn)

        conn.execute("DELETE FROM m4_client.client_trips")
        conn.execute(
            "INSERT INTO m4_client.client_trips "
            "(client_id, provider_trip_id, synced_at, sync_run_id, first_seen_request_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (CID, 5002, F, "run-a", request_id),
        )
        conn.commit()

        # Platform finalization now fails outright.
        conn.execute(
            """CREATE OR REPLACE FUNCTION workflow_a_control.m4_block()
               RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'forced platform failure'; END $$"""
        )
        conn.execute(
            """CREATE TRIGGER m4_block BEFORE UPDATE
               ON workflow_a_control.provider_request_log
               FOR EACH ROW EXECUTE FUNCTION workflow_a_control.m4_block()"""
        )
        conn.commit()
        try:
            d._finalize_compat_success(
                conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
            )
            _check("A2: finalization refuses", False)
        except d.CoverageFinalizationError:
            _check("A2: finalization refuses", True)
        conn.execute("DROP TRIGGER m4_block ON workflow_a_control.provider_request_log")
        conn.execute("DROP FUNCTION workflow_a_control.m4_block()")
        conn.commit()

        stored = conn.execute(
            "SELECT first_seen_request_id::text FROM m4_client.client_trips "
            "WHERE provider_trip_id=5002"
        ).fetchone()[0]
        conn.rollback()
        _check("A2: the client row retains its immutable identity",
               stored == request_id)

        rows = {str(r["request_id"]): r for r in evidence(conn)}
        _check("A2: THE ORPHAN IS GONE — the identity still resolves",
               request_id in rows)
        _check("A2: as a request fact, not as coverage evidence",
               rows[request_id]["status"] == REQUEST_STATUS_PENDING)
        after = coverage(conn)
        _check("A2: no watermark advanced during the failed attempt",
               after["covered_through_ts"] == before["covered_through_ts"]
               and after["updated_at"] == before["updated_at"])

        # SAME-EXECUTION finalization retry — same platform run, same minted
        # request identities. This is what can promote them, and it is the only
        # thing that can. Deliberately NOT described as "a later fire": a later
        # fire mints a new platform run and new identities and cannot reach
        # these rows at all, which `test_P1_a_later_fire_cannot_promote_them`
        # proves directly.
        prepared2 = seed_claim(conn)
        _pending(conn, proof)   # idempotent replay of the same identities
        d._finalize_compat_success(
            conn, prepared=prepared2, completeness=proof, platform_run_id=PRID,
        )
        rows = {str(r["request_id"]): r for r in evidence(conn)}
        _check("A2: a same-execution finalization retry promotes the identity",
               rows[request_id]["status"] == REQUEST_STATUS_FINALIZED)
    finally:
        d._platform_pg_conn = saved


def test_A3_cas_conflict_is_not_a_false_success(conn) -> None:
    print("\n## test_A3_cas_conflict_is_not_a_false_success")
    import psycopg
    saved = d._platform_pg_conn
    d._platform_pg_conn = lambda: psycopg.connect(os.environ[ENV])
    try:
        prepared = seed_claim(conn)
        proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
        _pending(conn, proof)
        # Move the watermark under the claim so the CAS must refuse.
        conn.execute(
            "UPDATE workflow_a_control.client_dataset_coverage "
            "SET covered_through_ts=%s WHERE schedule_id=%s",
            (W + timedelta(seconds=1), SID),
        )
        conn.commit()
        before = coverage(conn)
        try:
            d._finalize_compat_success(
                conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
            )
            _check("A3: the CAS refuses", False)
        except d.CoverageFinalizationError as exc:
            _check("A3: the CAS refuses",
                   exc.code == d.TRIPS_COVERAGE_ADVANCE_CONFLICT, f"code={exc.code}")
        after = coverage(conn)
        _check("A3: no false advancement",
               after["covered_through_ts"] == before["covered_through_ts"])
        rows = evidence(conn)
        _check("A3: nothing was promoted", all(r["status"] == REQUEST_STATUS_PENDING for r in rows))
        _check("A3: but the request facts are intact and still resolvable",
               len(rows) == proof.page_count)
    finally:
        d._platform_pg_conn = saved


def test_A4_A7_handoff_replay_is_idempotent(conn) -> None:
    print("\n## test_A4_A7_handoff_replay_is_idempotent")
    prepared = seed_claim(conn)
    proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE, subwindows=2)

    first = _pending(conn, proof)
    # A4/A7: a crash between the write and the business commit is replayed by
    # re-running the same step with the same minted identities.
    second = _pending(conn, proof)
    third = _pending(conn, proof)
    rows = evidence(conn)
    _check("A4: replaying the handoff creates no duplicate rows",
           len(rows) == proof.page_count, f"rows={len(rows)} pages={proof.page_count}")
    _check("A4: each replay reports the same intended row count",
           first == second == third == proof.page_count)
    _check("A7: every identity appears exactly once",
           len({str(r["request_id"]) for r in rows}) == len(rows))

    d._finalize_compat_success(
        conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
    )
    _check("A4: promotion still succeeds after replays",
           all(r["status"] == REQUEST_STATUS_FINALIZED for r in evidence(conn)))

    # A7: replaying the *handoff* after finalization must not demote or
    # duplicate anything — the writer never updates an existing row.
    _pending(conn, proof)
    rows = evidence(conn)
    _check("A7: a late replay does not demote finalized evidence",
           all(r["status"] == REQUEST_STATUS_FINALIZED for r in rows)
           and len(rows) == proof.page_count)


def test_A7_promotion_refuses_evidence_it_did_not_record(conn) -> None:
    print("\n## test_A7_promotion_refuses_evidence_it_did_not_record")
    import psycopg
    saved = d._platform_pg_conn
    d._platform_pg_conn = lambda: psycopg.connect(os.environ[ENV])
    try:
        # A proof naming request identities this platform run never recorded.
        prepared = seed_claim(conn)
        proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
        before = coverage(conn)
        try:
            d._finalize_compat_success(
                conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
            )
            _check("promotion refuses evidence that was never recorded", False)
        except d.CoverageFinalizationError:
            _check("promotion refuses evidence that was never recorded", True)
        after = coverage(conn)
        _check("and the watermark did not move",
               after["covered_through_ts"] == before["covered_through_ts"])

        # A proof whose facts belong to a DIFFERENT platform run.
        prepared = seed_claim(conn)
        proof = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
        _pending(conn, proof, platform_run_id=str(uuid.uuid4()))
        try:
            d._finalize_compat_success(
                conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
            )
            _check("promotion refuses another run's request facts", False)
        except d.CoverageFinalizationError:
            _check("promotion refuses another run's request facts", True)
        _check("the other run's facts stay PENDING and unattributed",
               all(r["status"] == REQUEST_STATUS_PENDING
                   and r["run_history_id"] is None for r in evidence(conn)))
    finally:
        d._platform_pg_conn = saved


def test_A5_A6_first_seen_immutability_under_the_new_lifecycle(conn) -> None:
    print("\n## test_A5_A6_first_seen_immutability_under_the_new_lifecycle")
    conn.execute("DELETE FROM m4_client.client_trips")
    conn.commit()
    upsert = """
        INSERT INTO m4_client.client_trips
          (client_id, provider_trip_id, synced_at, sync_run_id, first_seen_request_id)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
          synced_at = EXCLUDED.synced_at,
          sync_run_id = EXCLUDED.sync_run_id
    """
    original, later = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(upsert, (CID, 6001, F, "run-a", original))
    conn.execute(upsert, (CID, 6002, F, "run-a", None))     # historical NULL
    conn.commit()

    # A5: an overlapping re-upsert with a different identity changes nothing.
    conn.execute(upsert, (CID, 6001, F + timedelta(days=1), "run-b", later))
    # A6: a historical NULL row is never back-filled by a later run that does
    # have provenance — the retry mechanism must not fabricate first-seen.
    conn.execute(upsert, (CID, 6002, F + timedelta(days=1), "run-b", later))
    conn.commit()

    rows = {
        int(r[0]): r[1]
        for r in conn.execute(
            "SELECT provider_trip_id, first_seen_request_id::text "
            "FROM m4_client.client_trips ORDER BY provider_trip_id"
        ).fetchall()
    }
    conn.rollback()
    _check("A5: an existing first_seen is never replaced", rows[6001] == original)
    _check("A6: a historical NULL row stays NULL", rows[6002] is None)


def test_zero_row_execution_records_facts_but_no_trip_provenance(conn) -> None:
    print("\n## test_zero_row_execution_records_facts_but_no_trip_provenance")
    prepared = seed_claim(conn)
    # A genuine zero-row window: one page per unit, every page empty.
    proof = eo_fixtures.complete_window(
        window_start_ts=WS, window_end_ts=WE, subwindows=2, row_count=0,
    )
    _pending(conn, proof)
    _check("zero-row: request facts still exist",
           len(evidence(conn)) == proof.page_count)
    moved = d._finalize_compat_success(
        conn, prepared=prepared, completeness=proof, platform_run_id=PRID,
    )
    _check("zero-row: coverage still advances", moved is True)
    _check("zero-row: the evidence is finalized and complete",
           all(r["status"] == REQUEST_STATUS_FINALIZED and r["subwindow_complete"]
               for r in evidence(conn)))
    _check("zero-row: and no trip provenance was created",
           conn.execute("SELECT count(*) FROM m4_client.client_trips "
                        "WHERE first_seen_request_id = ANY(%s::uuid[])",
                        ([p.request_id for s in proof.subwindows for p in s.pages],)
                        ).fetchone()[0] == 0)
    conn.rollback()




def test_P1_a_later_fire_cannot_promote_them(conn) -> None:
    print("\n## test_P1_a_later_fire_cannot_promote_them")
    # Fire A fails finalization and leaves R_A PENDING.
    prepared_a = seed_claim(conn)
    proof_a = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
    _pending(conn, proof_a, platform_run_id=PRID)
    r_a = proof_a.subwindows[0].pages[0].request_id
    _check("P1: fire A left its request fact PENDING",
           {str(r["request_id"]): r["status"] for r in evidence(conn)}[r_a]
           == REQUEST_STATUS_PENDING)

    # Fire B is a genuinely different execution: its own platform run and its
    # own freshly minted request identities, as every natural launch produces.
    prid_b = str(uuid.uuid4())
    proof_b = eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE)
    r_b = proof_b.subwindows[0].pages[0].request_id
    _check("P1: fire B minted different request identities", r_a != r_b)

    prepared_b = seed_claim_keep_evidence(conn)
    _pending(conn, proof_b, platform_run_id=prid_b)
    moved = d._finalize_compat_success(
        conn, prepared=prepared_b, completeness=proof_b, platform_run_id=prid_b,
    )
    _check("P1: fire B advances coverage on its own evidence", moved is True)

    rows = {str(r["request_id"]): r for r in evidence(conn)}
    _check("P1: fire A's request fact is STILL PENDING",
           rows[r_a]["status"] == REQUEST_STATUS_PENDING,
           f"status={rows[r_a]['status']}")
    _check("P1: and was never attributed to fire B",
           rows[r_a]["run_history_id"] is None
           and rows[r_a]["subwindow_complete"] is None)
    _check("P1: fire B finalized only its own evidence",
           rows[r_b]["status"] == REQUEST_STATUS_FINALIZED
           and str(rows[r_b]["run_history_id"]) == prepared_b.run_history_id)
    _check("P1: fire A's identity still resolves as provenance", r_a in rows)
    del prepared_a


def main() -> int:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    # Destructive: drops schemas and applies migrations. The loopback guard is
    # the actual gate and runs before any connection is attempted.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    import psycopg
    with psycopg.connect(dsn) as conn:
        test_platform_migration_applies_and_replays(conn)
        seed_control_plane(conn)
        test_no_cross_database_foreign_key_is_attempted(conn)
        test_client_business_migration_applies_and_replays(conn)
        test_new_client_and_existing_client_paths_agree(conn)
        test_the_database_enforces_the_evidence_contract(conn)
        test_evidence_is_removed_with_its_run_history_row(conn)
        test_evidence_and_watermark_commit_together(conn)
        test_a_failed_evidence_projection_leaves_the_watermark_alone(conn)
        test_the_finalizer_refuses_without_a_proof(conn)
        test_first_seen_is_set_on_insert_and_never_overwritten(conn)
        test_the_job_upsert_omits_the_column_from_do_update(conn)
        test_A1_normal_lifecycle_resolves(conn)
        test_A2_platform_failure_leaves_provenance_resolvable(conn)
        test_A3_cas_conflict_is_not_a_false_success(conn)
        test_A4_A7_handoff_replay_is_idempotent(conn)
        test_A7_promotion_refuses_evidence_it_did_not_record(conn)
        test_A5_A6_first_seen_immutability_under_the_new_lifecycle(conn)
        test_zero_row_execution_records_facts_but_no_trip_provenance(conn)
        test_P1_a_later_fire_cannot_promote_them(conn)

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
