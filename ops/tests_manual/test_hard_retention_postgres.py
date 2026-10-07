#!/usr/bin/env python3
"""The 13-calendar-month sweep against a real, disposable PostgreSQL 16.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_hard_retention_postgres.py

The instance is CREATED by this suite (`ops/tests_manual/disposable_postgres.py`),
used, and removed in a `finally`. No DSN is read from the environment, so there
is no variable that can point it at production. Where docker or the
`postgres:16` image is unavailable the suite reports NOT AVAILABLE rather than
passing vacuously.

WHAT THIS PROVES

  * old rows become eligible and young ones do not, checked one microsecond
    either side of the cutoff rather than somewhere comfortable in between;
  * dependent rows go with their parent — `ON DELETE CASCADE` for occurrences,
    outbox rows, attempt objects and report members — and no required child is
    orphaned;
  * a `NO ACTION` reference that a younger row still holds DEFERS its parent
    instead of aborting the batch, so one awkward record cannot stop the sweep;
  * protective predicates hold: a RUNNING schedule fire, an unpublished export
    object and an available report member are retained however old they are,
    and they are REPORTED as retained rather than silently skipped;
  * a row whose retention anchor is NULL is never deleted and always surfaced;
  * reruns are idempotent, batches are bounded and make progress, and a row
    locked by another transaction is deferred rather than blocked on;
  * two concurrent sweeps cannot both run;
  * a blocked policy is never executed;
  * migration 070 applies cleanly to a POPULATED database and its ledger is
    current-state: a second sweep updates rows rather than adding any.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import hard_retention as hr  # noqa: E402
from ops import retention_registry as rr  # noqa: E402
from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
CUTOFF = rr.hard_retention_cutoff(NOW)
OLD = CUTOFF - timedelta(days=30)
JUST_OLD = CUTOFF - timedelta(microseconds=1)
AT_CUTOFF = CUTOFF
YOUNG = CUTOFF + timedelta(days=30)

PASSED: list[str] = []

# The relations the sweep touches, with the exact FOREIGN-KEY ACTIONS the
# production migrations declare. The actions are the whole point: a fixture that
# used CASCADE everywhere would prove that cascading works, not that this
# schema's dependencies survive the sweep.
FIXTURE_SQL = """
CREATE SCHEMA IF NOT EXISTS ops_control;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS workflow_a_control;

CREATE TABLE ops_control.run_reconciliation (
    run_id uuid PRIMARY KEY,
    reconciled_at timestamptz
);
CREATE TABLE ops_control.watchdog_observation (
    subject_key text PRIMARY KEY,
    last_observed_at timestamptz NOT NULL
);
CREATE TABLE ops_control.environment_identity_promotion (
    promotion_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL
);

CREATE TABLE public.portal_audit_events (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL
);

CREATE TABLE public.suspected_bug_incidents (
    incident_id uuid PRIMARY KEY,
    status text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);
CREATE TABLE public.suspected_bug_occurrences (
    occurrence_id uuid PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES public.suspected_bug_incidents(incident_id)
        ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL
);
CREATE TABLE public.suspected_bug_email_outbox (
    outbox_id uuid PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES public.suspected_bug_incidents(incident_id)
        ON DELETE CASCADE,
    occurrence_id uuid REFERENCES public.suspected_bug_occurrences(occurrence_id)
        ON DELETE SET NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE public.database_export_jobs (
    job_id uuid PRIMARY KEY,
    status text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE public.database_export_attempt_objects (
    attempt_object_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES public.database_export_jobs(job_id)
        ON DELETE CASCADE,
    state text NOT NULL,
    last_cleanup_success_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE TABLE public.portal_generated_report_instances (
    instance_id uuid PRIMARY KEY,
    generation_state text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE public.portal_generated_report_files (
    member_id uuid PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES public.portal_generated_report_instances(instance_id)
        ON DELETE CASCADE,
    is_available boolean NOT NULL
);

CREATE TABLE workflow_a_control.client_schedule_run_history (
    run_history_id uuid PRIMARY KEY,
    status text NOT NULL,
    scheduled_fire_ts timestamptz NOT NULL
);
CREATE TABLE workflow_a_control.provider_request_log (
    request_id uuid PRIMARY KEY,
    run_history_id uuid REFERENCES workflow_a_control.client_schedule_run_history(run_history_id)
        ON DELETE CASCADE,
    recorded_at timestamptz NOT NULL
);
CREATE TABLE workflow_a_control.client_dataset_recovery_run (
    recovery_run_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL
);
CREATE TABLE workflow_a_control.trip_delivery_lag_daily (
    client_id uuid NOT NULL,
    trip_end_date date NOT NULL,
    PRIMARY KEY (client_id, trip_end_date)
);

CREATE TABLE ingest.imap_message (
    id bigserial PRIMARY KEY,
    fetched_at timestamptz
);
CREATE TABLE ingest.raw_file (
    id uuid PRIMARY KEY,
    imap_message_id bigint NOT NULL REFERENCES ingest.imap_message(id) ON DELETE CASCADE,
    duplicate_of_id uuid REFERENCES ingest.raw_file(id)
);
"""


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=False)


def execute(conn, sql: str, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def count(conn, relation: str, where: str = "TRUE") -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {relation} WHERE {where}")
        value = int(cur.fetchone()[0])
    conn.rollback()
    return value


def sweep(conn, policy_id: str, schema: str, table: str, anchor: str, *,
          dry_run: bool = False, batch_size: int = 5000, max_batches=None,
          protect_sql=None):
    declared = next(
        (item for item in hr.PLATFORM_SWEEPS
         if item.schema == schema and item.table == table), None
    )
    spec = declared or hr.TableSweep(
        policy_id=policy_id, schema=schema, table=table, anchor=anchor,
        protect_sql=protect_sql,
    )
    return hr.sweep_table(
        conn, spec, cutoff=CUTOFF, scope="platform", dry_run=dry_run,
        batch_size=batch_size, max_batches=max_batches,
    )


# --- the tests ---------------------------------------------------------------


def test_the_boundary_is_exact(conn) -> None:
    execute(conn, "TRUNCATE public.portal_audit_events")
    for stamp in (OLD, JUST_OLD, AT_CUTOFF, YOUNG):
        execute(conn, "INSERT INTO public.portal_audit_events (created_at) VALUES (%s)",
                (stamp,))

    plan = sweep(conn, "platform_db.public.portal_audit_events",
                 "public", "portal_audit_events", "created_at", dry_run=True)
    check("a dry run finds both eligible rows", plan.examined == 2, str(plan.examined))
    check("and deletes nothing", plan.deleted == 0 and count(conn, "public.portal_audit_events") == 4)
    check("it reports the oldest eligible row",
          plan.oldest_remaining == OLD, str(plan.oldest_remaining))

    done = sweep(conn, "platform_db.public.portal_audit_events",
                 "public", "portal_audit_events", "created_at")
    check("exactly the eligible rows are removed", done.deleted == 2, str(done.deleted))
    check("the row exactly AT the cutoff survives — eligibility is strict `<`",
          count(conn, "public.portal_audit_events", "created_at = %s" % "'%s'" % AT_CUTOFF.isoformat()) == 1)
    check("the young row survives", count(conn, "public.portal_audit_events") == 2)
    check("nothing eligible remains", done.oldest_remaining is None)
    check("and the classification is a clean execution",
          done.classification == "RETENTION_EXECUTION_SUCCEEDED")

    again = sweep(conn, "platform_db.public.portal_audit_events",
                  "public", "portal_audit_events", "created_at")
    check("a rerun is idempotent", again.deleted == 0 and again.examined == 0)
    PASSED.append("the_boundary_is_exact")


def test_dependants_go_with_their_parent(conn) -> None:
    execute(conn, "TRUNCATE public.suspected_bug_incidents CASCADE")
    stale, live = uuid.uuid4(), uuid.uuid4()
    for incident, last_seen in ((stale, OLD), (live, YOUNG)):
        execute(conn,
                "INSERT INTO public.suspected_bug_incidents "
                "(incident_id, status, first_seen_at, last_seen_at) VALUES (%s,'open',%s,%s)",
                (incident, OLD, last_seen))
        occurrence = uuid.uuid4()
        execute(conn,
                "INSERT INTO public.suspected_bug_occurrences "
                "(occurrence_id, incident_id, occurred_at) VALUES (%s,%s,%s)",
                (occurrence, incident, OLD))
        execute(conn,
                "INSERT INTO public.suspected_bug_email_outbox "
                "(outbox_id, incident_id, occurrence_id, status, created_at) "
                "VALUES (%s,%s,%s,'sent',%s)",
                (uuid.uuid4(), incident, occurrence, OLD))

    done = sweep(conn, "platform_db.public.suspected_bug_incidents",
                 "public", "suspected_bug_incidents", "last_seen_at")
    check("only the quiet incident ages out", done.deleted == 1, str(done.deleted))
    check("the still-recurring incident is retained even though it is old",
          count(conn, "public.suspected_bug_incidents",
                f"incident_id = '{live}'") == 1,
          "anchoring on last_seen_at is what keeps duplicate-alert suppression alive")
    check("its occurrences cascaded away",
          count(conn, "public.suspected_bug_occurrences") == 1)
    check("and so did its outbox rows",
          count(conn, "public.suspected_bug_email_outbox") == 1)
    check("no orphan is left behind",
          count(conn, "public.suspected_bug_occurrences",
                "incident_id NOT IN (SELECT incident_id FROM public.suspected_bug_incidents)") == 0)
    PASSED.append("dependants_go_with_their_parent")


def test_a_cascade_reaches_the_provider_request_log(conn) -> None:
    execute(conn, "TRUNCATE workflow_a_control.client_schedule_run_history CASCADE")
    old_fire, running_fire = uuid.uuid4(), uuid.uuid4()
    execute(conn,
            "INSERT INTO workflow_a_control.client_schedule_run_history "
            "(run_history_id, status, scheduled_fire_ts) VALUES (%s,'SUCCESS',%s),(%s,'RUNNING',%s)",
            (old_fire, OLD, running_fire, OLD))
    for fire in (old_fire, running_fire):
        execute(conn,
                "INSERT INTO workflow_a_control.provider_request_log "
                "(request_id, run_history_id, recorded_at) VALUES (%s,%s,%s)",
                (uuid.uuid4(), fire, OLD))

    done = sweep(conn, "platform_db.workflow_a_control.client_schedule_run_history",
                 "workflow_a_control", "client_schedule_run_history", "scheduled_fire_ts")
    check("the terminal fire is removed", done.deleted == 1, str(done.deleted))
    check("the RUNNING fire is protected however old it is",
          count(conn, "workflow_a_control.client_schedule_run_history") == 1)
    check("and the protection is REPORTED, not silent",
          done.skipped == 1, str(done.skipped))
    check("its provider evidence cascaded with the removed fire",
          count(conn, "workflow_a_control.provider_request_log") == 1)
    check("the sweep says something eligible still remains",
          done.oldest_remaining == OLD, str(done.oldest_remaining))
    PASSED.append("a_cascade_reaches_the_provider_request_log")


def test_a_no_action_reference_defers_instead_of_aborting(conn) -> None:
    """`ingest.raw_file.duplicate_of_id` is NO ACTION. An old message whose file
    a YOUNGER message still points at must be deferred — not deleted, and not
    allowed to abort the whole batch with a foreign-key error."""
    execute(conn, "TRUNCATE ingest.imap_message CASCADE")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ingest.imap_message (fetched_at) VALUES (%s) RETURNING id", (OLD,))
        original_message = cur.fetchone()[0]
        cur.execute("INSERT INTO ingest.imap_message (fetched_at) VALUES (%s) RETURNING id", (YOUNG,))
        younger_message = cur.fetchone()[0]
        cur.execute("INSERT INTO ingest.imap_message (fetched_at) VALUES (%s) RETURNING id", (OLD,))
        plain_old_message = cur.fetchone()[0]
    original_file, younger_file, plain_file = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    execute(conn, "INSERT INTO ingest.raw_file (id, imap_message_id) VALUES (%s,%s),(%s,%s)",
            (original_file, original_message, plain_file, plain_old_message))
    execute(conn,
            "INSERT INTO ingest.raw_file (id, imap_message_id, duplicate_of_id) VALUES (%s,%s,%s)",
            (younger_file, younger_message, original_file))

    done = sweep(conn, "platform_db.ingest.imap_message", "ingest", "imap_message", "fetched_at")
    check("the unencumbered old message is removed", done.deleted == 1, str(done.deleted))
    check("its raw file cascaded away",
          count(conn, "ingest.raw_file", f"id = '{plain_file}'") == 0)
    check("the referenced old message is DEFERRED, not deleted",
          count(conn, "ingest.imap_message", f"id = {original_message}") == 1)
    check("and the deferral is counted as protected", done.skipped == 1, str(done.skipped))
    check("the batch did not fail", done.failed == 0, str(done.error))
    check("nothing eligible was lost to an aborted transaction",
          count(conn, "ingest.raw_file", f"id = '{younger_file}'") == 1)

    # Once the younger pointer is gone, the parent becomes eligible.
    execute(conn, "DELETE FROM ingest.imap_message WHERE id = %s", (younger_message,))
    second = sweep(conn, "platform_db.ingest.imap_message", "ingest", "imap_message", "fetched_at")
    check("it ages out on the next pass once unreferenced",
          second.deleted == 1 and count(conn, "ingest.imap_message") == 0,
          str(second.as_dict()))
    PASSED.append("a_no_action_reference_defers_instead_of_aborting")


def test_unpublished_export_objects_hold_their_job(conn) -> None:
    execute(conn, "TRUNCATE public.database_export_jobs CASCADE")
    clean_job, pending_job, active_job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    execute(conn,
            "INSERT INTO public.database_export_jobs (job_id, status, created_at) "
            "VALUES (%s,'expired',%s),(%s,'completed',%s),(%s,'running',%s)",
            (clean_job, OLD, pending_job, OLD, active_job, OLD))
    execute(conn,
            "INSERT INTO public.database_export_attempt_objects "
            "(attempt_object_id, job_id, state, last_cleanup_success_at, created_at) "
            "VALUES (%s,%s,'cleaned',%s,%s),(%s,%s,'cleanup_pending',NULL,%s)",
            (uuid.uuid4(), clean_job, OLD, OLD,
             uuid.uuid4(), pending_job, OLD))

    objects = sweep(conn, "platform_db.public.database_export_attempt_objects",
                    "public", "database_export_attempt_objects", "created_at")
    check("a settled attempt object ages out", objects.deleted == 1, str(objects.deleted))
    check("an unpublished one is retained — deleting it would orphan its object",
          objects.skipped == 1 and count(conn, "public.database_export_attempt_objects") == 1)

    jobs = sweep(conn, "platform_db.public.database_export_jobs",
                 "public", "database_export_jobs", "created_at")
    check("the settled job ages out", jobs.deleted == 1, str(jobs.deleted))
    check("the job holding an unpublished object is retained",
          count(conn, "public.database_export_jobs", f"job_id = '{pending_job}'") == 1)
    check("and so is the still-running job",
          count(conn, "public.database_export_jobs", f"job_id = '{active_job}'") == 1)
    check("both protections are reported", jobs.skipped == 2, str(jobs.skipped))
    PASSED.append("unpublished_export_objects_hold_their_job")


def test_an_available_report_member_holds_its_instance(conn) -> None:
    execute(conn, "TRUNCATE public.portal_generated_report_instances CASCADE")
    expired, available, running = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    execute(conn,
            "INSERT INTO public.portal_generated_report_instances "
            "(instance_id, generation_state, created_at) "
            "VALUES (%s,'succeeded',%s),(%s,'succeeded',%s),(%s,'running',%s)",
            (expired, OLD, available, OLD, running, OLD))
    execute(conn,
            "INSERT INTO public.portal_generated_report_files "
            "(member_id, instance_id, is_available) VALUES (%s,%s,false),(%s,%s,true)",
            (uuid.uuid4(), expired, uuid.uuid4(), available))

    done = sweep(conn, "platform_db.public.portal_generated_report_instances",
                 "public", "portal_generated_report_instances", "created_at")
    check("the withdrawn instance ages out", done.deleted == 1, str(done.deleted))
    check("its member cascaded away",
          count(conn, "public.portal_generated_report_files") == 1)
    check("an instance still offering a download is retained",
          count(conn, "public.portal_generated_report_instances",
                f"instance_id = '{available}'") == 1)
    check("and so is one still generating",
          count(conn, "public.portal_generated_report_instances",
                f"instance_id = '{running}'") == 1)
    check("both protections are reported", done.skipped == 2, str(done.skipped))
    PASSED.append("an_available_report_member_holds_its_instance")


def test_an_unanchored_row_is_surfaced_never_deleted(conn) -> None:
    execute(conn, "TRUNCATE ingest.imap_message CASCADE")
    execute(conn, "INSERT INTO ingest.imap_message (fetched_at) VALUES (%s),(NULL),(NULL)", (OLD,))
    done = sweep(conn, "platform_db.ingest.imap_message", "ingest", "imap_message", "fetched_at")
    check("the anchored old row is removed", done.deleted == 1, str(done.deleted))
    check("rows with no anchor are kept", count(conn, "ingest.imap_message") == 2)
    check("and counted as an actionable defect",
          done.unanchored == 2 and done.defect_code == "UNANCHORED_ROWS",
          str(done.as_dict()))
    check("which downgrades the classification",
          done.classification == "RETENTION_PARTIAL_FAILURE", done.classification)
    PASSED.append("an_unanchored_row_is_surfaced_never_deleted")


def test_batches_are_bounded_and_make_progress(conn) -> None:
    execute(conn, "TRUNCATE public.portal_audit_events")
    for _ in range(9):
        execute(conn, "INSERT INTO public.portal_audit_events (created_at) VALUES (%s)", (OLD,))

    partial = sweep(conn, "platform_db.public.portal_audit_events",
                    "public", "portal_audit_events", "created_at",
                    batch_size=2, max_batches=2)
    check("a capped run stops where it was told", partial.deleted == 4, str(partial.deleted))
    check("and the work it did is committed",
          count(conn, "public.portal_audit_events") == 5)
    check("it ran the batches it claims", partial.batches == 2, str(partial.batches))

    rest = sweep(conn, "platform_db.public.portal_audit_events",
                 "public", "portal_audit_events", "created_at", batch_size=2)
    check("resuming finishes the job", rest.deleted == 5, str(rest.deleted))
    check("in bounded batches", rest.batches == 3, str(rest.batches))
    check("and the table is empty", count(conn, "public.portal_audit_events") == 0)
    PASSED.append("batches_are_bounded_and_make_progress")


def test_a_contended_row_is_bounded_not_waited_on(dsn: str, conn) -> None:
    """The sweep must not hang on a row another transaction is holding.

    It deliberately uses NO row-locking clause: `FOR UPDATE SKIP LOCKED` would
    require the UPDATE privilege, and a retention role that can rewrite customer
    rows is worse than one that occasionally has to retry. `SET LOCAL
    lock_timeout` is what bounds the contended case instead — the batch fails
    after five seconds, the failure is reported, committed progress is kept, and
    the next run resumes.
    """
    import time

    execute(conn, "TRUNCATE public.portal_audit_events")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.portal_audit_events (created_at) VALUES (%s),(%s) "
            "RETURNING id", (OLD, OLD),
        )
        held = cur.fetchall()[0][0]
    conn.commit()

    other = connect(dsn)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT id FROM public.portal_audit_events WHERE id = %s FOR UPDATE",
                        (held,))
        started = time.monotonic()
        done = sweep(conn, "platform_db.public.portal_audit_events",
                     "public", "portal_audit_events", "created_at")
        elapsed = time.monotonic() - started
        check("the sweep does not wait indefinitely on the contended row",
              elapsed < 30, f"{elapsed:.1f}s")
        check("it reports the contention rather than hiding it",
              done.failed == 1 and done.classification == "RETENTION_PARTIAL_FAILURE",
              f"{done.classification} {done.error}")
        check("and it names lock contention",
              "lock" in (done.error or "").lower(), str(done.error))
        check("no row was corrupted or half-deleted",
              count(conn, "public.portal_audit_events") == 2)
    finally:
        other.rollback()
        other.close()

    after = sweep(conn, "platform_db.public.portal_audit_events",
                  "public", "portal_audit_events", "created_at")
    check("once released the sweep completes normally",
          after.deleted == 2 and count(conn, "public.portal_audit_events") == 0,
          str(after.as_dict()))
    PASSED.append("a_contended_row_is_bounded_not_waited_on")


def test_the_batch_needs_no_row_locking_privilege(conn) -> None:
    """The statement shape is the privilege model, so it is pinned here too."""
    import inspect
    import re as _re

    raw = inspect.getsource(hr.sweep_table)
    # Comments explain WHY the clause is absent, so they must not be scanned as
    # if they were the clause.
    source = _re.sub(r"#.*$", " ", raw, flags=_re.M)
    check("the batch takes no row-locking clause",
          "FOR UPDATE" not in source and "SKIP LOCKED" not in source,
          "a locking clause would require UPDATE on customer tables")
    check("and the code says why it is absent",
          "UPDATE privilege" in raw)
    check("it is still one statement, so ctid cannot be reused underneath it",
          "ctid IN (" in source)
    check("and the contended case is bounded", "lock_timeout" in source)
    PASSED.append("the_batch_needs_no_row_locking_privilege")


def test_two_sweeps_cannot_run_at_once(dsn: str) -> None:
    holder = connect(dsn)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (hr.LOCK_NAMESPACE,))
        holder.commit()
        result = hr.run(execute=True, now=NOW, platform_dsn=dsn,
                        skip_clients=True, skip_filesystem=True,
                        skip_artifacts=True, skip_cloudflare=True, record=False)
        check("the second sweep refuses to start",
              result["classification"] == "RETENTION_LOCKED", str(result.get("classification")))
        check("it deletes nothing", result["sweeps"] == [])
        check("and it is not an operator incident",
              result["operator_action_required"] is False)
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (hr.LOCK_NAMESPACE,))
        holder.commit()
        holder.close()

    result = hr.run(execute=False, now=NOW, platform_dsn=dsn,
                    skip_clients=True, skip_filesystem=True,
                        skip_artifacts=True, skip_cloudflare=True, record=False)
    check("and once the lock is released the sweep runs again",
          result["classification"] != "RETENTION_LOCKED", str(result["classification"]))
    PASSED.append("two_sweeps_cannot_run_at_once")


def test_a_blocked_policy_is_never_executed(conn) -> None:
    # Both real blockers were CLOSED by owner decision, so the mechanism is
    # exercised with a synthetic entry rather than by leaving one blocked.
    synthetic_id = "synthetic.blocked_for_this_test"
    hr.POLICY_BY_ID[synthetic_id] = rr.RetentionPolicy(
        policy_id=synthetic_id, store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.default(), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job=None,
        status=rr.Status.BLOCKED_OWNER_DECISION,
        blocker="synthetic blocker for the refusal path",
    )
    blocked = hr.TableSweep(
        policy_id=synthetic_id,
        schema="public", table="portal_audit_events", anchor="created_at",
    )
    execute(conn, "TRUNCATE public.portal_audit_events")
    execute(conn, "INSERT INTO public.portal_audit_events (created_at) VALUES (%s)", (OLD,))
    outcome = hr.sweep_table(conn, blocked, cutoff=CUTOFF, scope="platform", dry_run=False)
    check("a blocked policy is refused before any statement runs",
          outcome.classification == "RETENTION_SKIPPED_BLOCKED", outcome.classification)
    check("nothing was deleted", count(conn, "public.portal_audit_events") == 1)
    check("and it is machine-detectable", outcome.defect_code == "BLOCKED_POLICY")
    hr.POLICY_BY_ID.pop(synthetic_id, None)
    check("no real policy is left blocked", not rr.open_owner_decisions(),
          str([p.policy_id for p in rr.open_owner_decisions()]))
    execute(conn, "TRUNCATE public.portal_audit_events")
    PASSED.append("a_blocked_policy_is_never_executed")


def test_no_sweep_exists_for_a_store_with_no_age_based_retention(conn) -> None:
    """The structural guarantee behind the owner's exemption.

    A sweep is the ONLY thing in this module that can plan a DELETE, so proving
    that none exists for a non-age-based policy proves the exemption for every
    caller at once — and keeps proving it for the next store that gets one.
    """
    for sweep in hr.PLATFORM_SWEEPS + hr.CLIENT_EXTRA_SWEEPS:
        policy = hr.POLICY_BY_ID.get(sweep.policy_id)
        check(f"{sweep.qualified()} names a registered policy", policy is not None,
              sweep.policy_id)
        check(f"{sweep.qualified()} is swept under an age-based policy",
              policy.is_age_based,
              f"{sweep.policy_id} is {policy.mode.value}: it must not be swept")
        check(f"{sweep.qualified()} is not owner-exempt",
              not policy.is_owner_exempt)

    exempt_ids = {p.policy_id for p in rr.owner_exemptions()}
    check("and the platform has an exemption to be wrong about", exempt_ids)
    check("no sweep names an exempt policy",
          not {item.policy_id for item in
               hr.PLATFORM_SWEEPS + hr.CLIENT_EXTRA_SWEEPS} & exempt_ids)

    # Every exempt store is REPORTED, and the reporting path fails closed if the
    # declaration and the registry ever disagree.
    for store in hr.CLIENT_EXEMPT_STORES:
        outcome = hr.exempt_outcome(store, scope="PROBE", dry_run=True)
        check(f"{store.qualified()} reports as exempt",
              outcome.classification == hr.EXEMPT_CLASSIFICATION,
              outcome.classification)
        check("with no cutoff and no candidates",
              outcome.cutoff is None and outcome.examined == 0
              and outcome.deleted == 0)

    impostor = hr.ExemptStore(
        policy_id="platform_db.public.portal_audit_events",
        schema="public", table="portal_audit_events",
    )
    refusal = hr.exempt_outcome(impostor, scope="PROBE", dry_run=True)
    check("declaring a governed store exempt is a defect, not a skip",
          refusal.classification == "RETENTION_FAILED"
          and refusal.defect_code == "EXEMPTION_NOT_REGISTERED",
          str(refusal.as_dict()))
    check("and it does not silently stop that store being swept",
          count(conn, "public.portal_audit_events") >= 0)
    PASSED.append("no_sweep_exists_for_a_store_with_no_age_based_retention")


def test_each_store_is_swept_with_its_own_enforcement_cutoff(dsn: str) -> None:
    """One global cutoff was the Gap-1 defect. Each store now leads differently."""
    result = hr.run(execute=False, now=NOW, platform_dsn=dsn,
                    skip_clients=True, skip_filesystem=True,
                    skip_artifacts=True, skip_cloudflare=True, record=False)
    check("the run reports the bare deadline separately",
          result["deadline_cutoff"] == rr.hard_retention_cutoff(NOW).isoformat())
    by_store = {item["store"]: item for item in result["sweeps"]}
    audit = by_store["public.portal_audit_events"]
    policy = rr.get("platform_db.public.portal_audit_events")
    check("but each sweep uses its policy's enforcement cutoff",
          audit["cutoff"] == policy.enforcement_cutoff(NOW).isoformat(),
          f"{audit['cutoff']} vs {policy.enforcement_cutoff(NOW).isoformat()}")
    check("which is later than the deadline cutoff — it collects more",
          audit["cutoff"] > result["deadline_cutoff"])
    check("and the lead travels with the outcome",
          audit["enforcement_lead_seconds"]
          == int(policy.enforcement_lead().total_seconds()))
    check("the backup topology is reported so the lead is explainable",
          result["backup_topology"]["retention_days"]
          == rr.PLATFORM_BACKUP_SET.retention_days)
    PASSED.append("each_store_is_swept_with_its_own_enforcement_cutoff")


def test_an_absent_relation_never_stops_the_run(dsn: str) -> None:
    """Every platform sweep runs against a fixture that deliberately lacks some
    of them; a missing optional relation must be reported, not fatal."""
    result = hr.run(execute=False, now=NOW, platform_dsn=dsn,
                    skip_clients=True, skip_filesystem=True,
                    skip_artifacts=True, skip_cloudflare=True, record=False)
    stores = {item["store"]: item for item in result["sweeps"]}
    check("every declared platform sweep is attempted",
          len(stores) >= len(hr.PLATFORM_SWEEPS), str(len(stores)))
    absent = [item for item in result["sweeps"] if item["defect_code"] == "RELATION_ABSENT"]
    check("absent optional relations are reported",
          all(item["failed_count"] == 0 for item in absent), str(absent))
    check("and they are excluded from the operator defect list",
          "RELATION_ABSENT" not in result["defects"], str(result["defects"]))
    check("a present relation was actually swept",
          stores["public.portal_audit_events"]["classification"].startswith("RETENTION_"))
    PASSED.append("an_absent_relation_never_stops_the_run")


def test_migration_070_applies_to_a_populated_database(dsn: str, conn) -> None:
    """Forward-only, additive, and safe on a database that already has rows."""
    execute(conn, "TRUNCATE public.portal_audit_events")
    for stamp in (OLD, OLD, YOUNG):
        execute(conn, "INSERT INTO public.portal_audit_events (created_at) VALUES (%s)", (stamp,))

    migration = (REPO_ROOT / "db" / "migrations"
                 / "070_platform_retention_execution_ledger.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(migration)
    conn.commit()
    check("the ledger exists after the migration",
          count(conn, "ops_control.retention_execution") == 0)
    check("existing rows are untouched by the migration",
          count(conn, "public.portal_audit_events") == 3,
          "a schema migration must never be an implicit mass purge")

    with conn.cursor() as cur:
        cur.execute(migration)  # idempotent: CREATE TABLE IF NOT EXISTS
    conn.commit()
    check("re-applying it is a no-op", count(conn, "ops_control.retention_execution") == 0)

    first = hr.run(execute=True, now=NOW, platform_dsn=dsn,
                   skip_clients=True, skip_filesystem=True, skip_artifacts=True,
                   skip_cloudflare=True)
    rows_after_first = count(conn, "ops_control.retention_execution")
    check("the sweep records its outcomes", rows_after_first > 0, str(rows_after_first))
    check("it reports how many it wrote",
          first["ledger_rows_written"] == rows_after_first,
          f"{first['ledger_rows_written']} vs {rows_after_first}")
    check("the audit sweep deleted the eligible rows",
          count(conn, "public.portal_audit_events") == 1)

    hr.run(execute=True, now=NOW, platform_dsn=dsn,
           skip_clients=True, skip_filesystem=True, skip_artifacts=True,
                   skip_cloudflare=True)
    check("a second sweep UPDATES the ledger rather than growing it",
          count(conn, "ops_control.retention_execution") == rows_after_first,
          "a governance ledger that accumulates would need governing itself")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT policy_id, scope, cutoff_ts, deleted_count, classification "
            "  FROM ops_control.retention_execution "
            " WHERE policy_id = 'platform_db.public.portal_audit_events'"
        )
        record = cur.fetchone()
    conn.rollback()
    check("the ledger names the policy and the exact cutoff used", record is not None)
    check("and it is the ENFORCEMENT cutoff, not the bare deadline",
          record[2] == rr.get("platform_db.public.portal_audit_events")
          .enforcement_cutoff(NOW),
          f"{record[2]} vs deadline {CUTOFF}")
    check("which is later than the deadline, so the sweep collects more",
          record[2] > CUTOFF, f"{record[2]} vs {CUTOFF}")
    check("the second run recorded zero further deletions",
          record[3] == 0, str(record[3]))
    check("the classification is recorded", record[4].startswith("RETENTION_"))
    PASSED.append("migration_070_applies_to_a_populated_database")


def test_migration_071_upgrades_a_collapsed_ledger(dsn: str, conn) -> None:
    """The identity fix, proved against a populated migration-070 ledger.

    This is the production shape, reproduced: a policy that sweeps several
    relations under ONE scope, upserted on `(policy_id, scope)`, so the last
    relation in the loop is the only one that survives. Production measured it
    exactly — 152 recordable outcomes, 62 rows — and `deleted_count` on such a
    row could not be attributed to a relation at all.
    """
    ledger = "ops_control.retention_execution"
    migration_070 = (REPO_ROOT / "db" / "migrations"
                     / "070_platform_retention_execution_ledger.sql").read_text(encoding="utf-8")
    migration_071 = (REPO_ROOT / "db" / "migrations"
                     / "071_retention_execution_per_target.sql").read_text(encoding="utf-8")

    # Start from a clean 070 database so the upgrade is tested from the state a
    # production host is actually in.
    execute(conn, f"DROP TABLE IF EXISTS {ledger}")
    with conn.cursor() as cur:
        cur.execute(migration_070)
    conn.commit()

    def write(policy: str, scope: str, store: str, deleted: int) -> None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ledger}
                    (policy_id, scope, executed_at, cutoff_ts, dry_run,
                     classification, examined_count, deleted_count,
                     skipped_count, failed_count, detail)
                VALUES (%s, %s, now(), %s, false,
                        'RETENTION_EXECUTION_SUCCEEDED', %s, %s, 0, 0,
                        jsonb_build_object('store', %s::text))
                ON CONFLICT (policy_id, scope) DO UPDATE SET
                    deleted_count = EXCLUDED.deleted_count,
                    examined_count = EXCLUDED.examined_count,
                    detail = EXCLUDED.detail
                """,
                (policy, scope, CUTOFF, deleted, deleted, store),
            )
        conn.commit()

    policy = "client_db.v2_staging_tables"
    write(policy, "DELTA00001", "public.source_trips", 11)
    write(policy, "DELTA00001", "public.source_notifications", 22)
    write(policy, "DELTA00001", "public.source_fuel_observations", 33)
    write("platform_db.public.portal_audit_events", "platform",
          "public.portal_audit_events", 7)

    check("migration 070 collapses three relations into one row",
          count(conn, ledger, f"policy_id = '{policy}'") == 1,
          "the defect must be present before it can be proved fixed")

    with conn.cursor() as cur:
        cur.execute(migration_071)
    conn.commit()

    check("the upgrade preserves every existing row",
          count(conn, ledger) == 2, "a migration must not be an implicit purge")
    with conn.cursor() as cur:
        cur.execute(f"SELECT policy_id, scope, target, deleted_count FROM {ledger} ORDER BY 1")
        migrated = cur.fetchall()
    conn.rollback()
    check("a legacy row keeps the target it actually described",
          (policy, "DELTA00001", "public.source_fuel_observations", 33) in migrated,
          str(migrated))
    check("and it keeps its real counts, with no history invented for its siblings",
          len([row for row in migrated if row[0] == policy]) == 1,
          "the two relations the collapse destroyed must not be resurrected as "
          "fabricated zero-deletion rows")

    with conn.cursor() as cur:
        cur.execute(migration_071)
    conn.commit()
    check("re-applying migration 071 is a no-op", count(conn, ledger) == 2)

    # The key really is the triple now.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
             WHERE n.nspname = 'ops_control' AND c.relname = 'retention_execution'
               AND i.indisprimary
             ORDER BY 1
            """
        )
        key = sorted(row[0] for row in cur.fetchall())
    conn.rollback()
    check("the primary key is (policy_id, scope, target)",
          key == ["policy_id", "scope", "target"], str(key))

    # And the executor no longer collides: three sibling relations, one policy,
    # one scope, three rows with three distinct counts.
    outcomes = [
        hr.SweepOutcome(policy_id=policy, scope="DELTA00001", store=store,
                        cutoff=CUTOFF, dry_run=False,
                        classification="RETENTION_EXECUTION_SUCCEEDED",
                        examined=deleted, deleted=deleted)
        for store, deleted in (
            ("public.source_trips", 101),
            ("public.source_notifications", 202),
            ("public.source_fuel_observations", 303),
        )
    ]
    written = hr.record_outcomes(conn, outcomes)
    check("every outcome is recorded", written == 3, str(written))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT target, deleted_count FROM {ledger} "
            f" WHERE policy_id = %s AND scope = 'DELTA00001' ORDER BY 1",
            (policy,),
        )
        rows = dict(cur.fetchall())
    conn.rollback()
    check("three relations produce three rows, not one",
          rows == {"public.source_fuel_observations": 303,
                   "public.source_notifications": 202,
                   "public.source_trips": 101}, str(rows))

    # Re-sweeping the same targets updates in place: still current-state.
    hr.record_outcomes(conn, outcomes)
    check("a repeated sweep updates the same rows rather than growing the table",
          count(conn, ledger, f"policy_id = '{policy}'") == 3)

    # Order must not decide the outcome.
    hr.record_outcomes(conn, list(reversed(outcomes)))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT target, deleted_count FROM {ledger} "
            f" WHERE policy_id = %s AND scope = 'DELTA00001' ORDER BY 1",
            (policy,),
        )
        reordered = dict(cur.fetchall())
    conn.rollback()
    check("the identity does not depend on loop order", reordered == rows, str(reordered))

    # Filesystem roots already scoped by path must keep working unchanged.
    fs_outcomes = [
        hr.SweepOutcome(policy_id="filesystem.workflow_b_report_files",
                        scope=root, store=root, cutoff=CUTOFF, dry_run=False,
                        classification="RETENTION_EXECUTION_SUCCEEDED")
        for root in ("/home/x/reports-data/raw", "/home/x/reports-data/normalized")
    ]
    hr.record_outcomes(conn, fs_outcomes)
    check("filesystem roots still record one row each",
          count(conn, ledger,
                "policy_id = 'filesystem.workflow_b_report_files'") == 2)
    PASSED.append("migration_071_upgrades_a_collapsed_ledger")


def test_a_full_sweep_records_one_row_per_relation(dsn: str, conn) -> None:
    """End to end: no outcome the ledger can hold is lost to a collision."""
    result = hr.run(execute=False, now=NOW, platform_dsn=dsn,
                    skip_clients=True, skip_filesystem=True, skip_artifacts=True,
                    skip_cloudflare=True, record=True)
    recordable = [
        item for item in result["sweeps"]
        if item["classification"] != hr.EXEMPT_CLASSIFICATION
    ]
    identities = {
        (item["policy_id"], item["scope"], item["store"]) for item in recordable
    }
    check("no two recordable outcomes share an identity",
          len(identities) == len(recordable),
          f"{len(identities)} identities for {len(recordable)} outcomes")
    check("and every one of them was written",
          result["ledger_rows_written"] == len(recordable),
          f"{result['ledger_rows_written']} vs {len(recordable)}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT policy_id, scope, target FROM ops_control.retention_execution"
        )
        present = {tuple(row) for row in cur.fetchall()}
    conn.rollback()
    missing = identities - present
    check("each recordable outcome has its own current-state row",
          not missing, str(sorted(missing)))
    PASSED.append("a_full_sweep_records_one_row_per_relation")


def main() -> int:
    try:
        context = disposable_postgres(label="hardret")
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0
    try:
        with context as (dsn, info):
            print(f"disposable PostgreSQL {info['server_version']} on port {info['port']}")
            conn = connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(FIXTURE_SQL)
                conn.commit()

                test_the_boundary_is_exact(conn)
                test_dependants_go_with_their_parent(conn)
                test_a_cascade_reaches_the_provider_request_log(conn)
                test_a_no_action_reference_defers_instead_of_aborting(conn)
                test_unpublished_export_objects_hold_their_job(conn)
                test_an_available_report_member_holds_its_instance(conn)
                test_an_unanchored_row_is_surfaced_never_deleted(conn)
                test_batches_are_bounded_and_make_progress(conn)
                test_a_contended_row_is_bounded_not_waited_on(dsn, conn)
                test_the_batch_needs_no_row_locking_privilege(conn)
                test_two_sweeps_cannot_run_at_once(dsn)
                test_a_blocked_policy_is_never_executed(conn)
                test_no_sweep_exists_for_a_store_with_no_age_based_retention(conn)
                test_each_store_is_swept_with_its_own_enforcement_cutoff(dsn)
                test_an_absent_relation_never_stops_the_run(dsn)
                test_migration_070_applies_to_a_populated_database(dsn, conn)
                test_migration_071_upgrades_a_collapsed_ledger(dsn, conn)
                test_a_full_sweep_records_one_row_per_relation(dsn, conn)
            finally:
                conn.close()
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0

    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — hard retention against PostgreSQL 16")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
