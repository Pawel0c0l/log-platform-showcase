#!/usr/bin/env python3
"""Disposable-Postgres tests for suspected_bug persistence, dedup and the email worker.

Requires a throwaway database; never point this at logdb:

  createdb suspected_bug_test
  SUSPECTED_BUG_TEST_DSN='postgresql://loguser:...@127.0.0.1:5432/suspected_bug_test' \
      .venv/bin/python ops/tests_manual/test_suspected_bug_outbox_postgres.py

Email delivery is exercised with an in-memory fake transport only.
"""
from __future__ import annotations

import os
import smtplib
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import api.suspected_bug as sb  # noqa: E402
import ops.suspected_bug_email_worker as worker  # noqa: E402

MIGRATION = REPO_ROOT / "db" / "migrations" / "052_suspected_bug_incidents_and_email_outbox.sql"

# Mirrors the runs/logs bootstrap in api/main.py SCHEMA_SQL.
PLATFORM_BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS runs (
  run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  status TEXT NOT NULL, trigger TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
  params JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL, type TEXT NOT NULL,
  source TEXT NOT NULL, run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
);
"""

RUN_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 7, 27, 10, 30, tzinfo=timezone.utc)

CONFIG = sb.SuspectedBugAlertConfig(
    recipients=("ops@example.com",),
    cooldown=timedelta(hours=2),
    reminder_interval=timedelta(hours=24),
    max_attempts=3,
    initial_retry_delay=timedelta(seconds=60),
    max_retry_delay=timedelta(seconds=600),
    stale_claim_timeout=timedelta(seconds=900),
    environment="local_dev",
)
NO_RECIPIENTS = sb.SuspectedBugAlertConfig(recipients=(), environment="local_dev")


class FakeSmtp:
    """In-memory transport. Nothing leaves the process."""

    sent: list[dict] = []

    def __init__(self, failure: Exception | None = None):
        self.failure = failure

    def __call__(self, *, to_addrs, subject, html_body, text_body, message_id=None, **kwargs):
        if self.failure is not None:
            raise self.failure
        FakeSmtp.sent.append({"to": list(to_addrs), "subject": subject, "message_id": message_id})
        return type("SendResult", (), {"message_id": message_id, "recipients": tuple(to_addrs)})()


def event(**overrides) -> sb.SuspectedBugEvent:
    values = dict(
        incident_code="ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT",
        title="Conflicting Dysponent_ID assignments for WZ457JN",
        summary="Two source ids share one registration and effective date.",
        occurred_at=NOW,
        environment="local_dev",
        component="jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment",
        client_code="ALPHA00001",
        client_id="9536f715-2fd0-4ffd-86ed-ba06f5490c5e",
        run_id=RUN_ID,
        report_type="Alpha_GPS_Baza_LOG",
        database_name="alpha_main",
        schema_name="telematics_reports",
        table_name="Alpha_GPS_Baza_LOG",
        subject_type="vehicle_registration",
        subject_key="registration",
        subject_value="WZ457JN",
        affected_record_count=37,
        rows_modified=0,
        fingerprint_fields={"conflicting_assignment_ids": ["447352", "551392"],
                            "effective_assignment_date": "2025-02-13"},
    )
    values.update(overrides)
    return sb.SuspectedBugEvent(**values)


def connect(dsn: str):
    return psycopg.connect(dsn, row_factory=dict_row)


def reset(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
                    "suspected_bug_incidents RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM logs")
        cur.execute("DELETE FROM runs")
        cur.execute(
            "INSERT INTO runs(run_id, started_at, status, trigger, source) "
            "VALUES (%s, now(), 'RUNNING', 'MANUAL', 'fixture')",
            (RUN_ID,),
        )
    conn.commit()
    FakeSmtp.sent.clear()


def one(conn, sql: str, params=None) -> dict:
    with conn.cursor() as cur:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
        row = cur.fetchone()
    conn.rollback()
    return dict(row or {})


def rows(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
        found = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return found


# --------------------------------------------------------------------- schema


def test_migration_applies_cleanly_and_reruns(conn) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(PLATFORM_BOOTSTRAP)
        cur.execute("INSERT INTO runs(run_id, started_at, status, trigger, source) "
                    "VALUES (gen_random_uuid(), now(), 'SUCCESS', 'MANUAL', 'historic')")
        cur.execute("INSERT INTO logs(ts, level, type, source, message) "
                    "VALUES (now(), 'INFO', 'SCRIPT', 'historic', 'pre-existing log')")
    conn.commit()

    before = one(conn, "SELECT count(*)::int AS logs, (SELECT count(*)::int FROM runs) AS runs FROM logs")

    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    with conn.cursor() as cur:  # rerun must be safe (ledger-independent idempotency)
        cur.execute(sql)
    conn.commit()

    after = one(conn, "SELECT count(*)::int AS logs, (SELECT count(*)::int FROM runs) AS runs FROM logs")
    assert before == after, "migration must not rewrite or delete historical logs/runs"

    tables = {row["table_name"] for row in rows(
        conn,
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
        "AND table_name LIKE 'suspected_bug%'")}
    assert tables == {"suspected_bug_incidents", "suspected_bug_occurrences",
                      "suspected_bug_email_outbox"}, tables

    indexes = {row["indexname"] for row in rows(
        conn, "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename LIKE 'suspected_bug%'")}
    for expected in ("idx_suspected_bug_incidents_open_last_seen", "idx_suspected_bug_email_outbox_due",
                     "idx_suspected_bug_email_outbox_lease", "uq_suspected_bug_occurrences_log",
                     "suspected_bug_incidents_fingerprint_key",
                     "suspected_bug_email_outbox_notification_key_key"):
        assert expected in indexes, f"missing index {expected}: {sorted(indexes)}"

    checks = {row["conname"] for row in rows(
        conn, "SELECT conname FROM pg_constraint WHERE conrelid::regclass::text LIKE 'suspected_bug%'")}
    assert "suspected_bug_incidents_classification_check" in checks
    assert "suspected_bug_email_outbox_status_check" in checks

    with conn.cursor() as cur:
        try:
            cur.execute("INSERT INTO suspected_bug_incidents (fingerprint, classification, incident_code, title) "
                        "VALUES ('x', 'not_suspected_bug', 'CODE', 't')")
        except psycopg.errors.CheckViolation:
            pass
        else:
            raise AssertionError("classification check must reject other classifications")
    conn.rollback()
    print("PASS: migration applies cleanly, reruns safely and preserves history")


# ------------------------------------------------------------------ reporting


def test_atomic_report_creates_log_incident_occurrence_and_outbox(conn) -> None:
    reset(conn)
    result = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)

    assert result.reported and result.incident_created and result.email_enqueued
    assert result.occurrence_count == 1 and result.notification_reason == sb.REASON_NEW

    log = one(conn, "SELECT * FROM logs WHERE id = %s", (result.log_id,))
    assert log["level"] == "ERROR", log["level"]
    assert log["context"]["classification"] == "suspected_bug"
    assert log["context"]["incident_code"] == "ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"
    assert log["context"]["incident_id"] == result.incident_id
    assert log["context"]["fingerprint"] == result.fingerprint
    assert str(log["run_id"]) == RUN_ID

    incident = one(conn, "SELECT * FROM suspected_bug_incidents WHERE incident_id = %s::uuid",
                   (result.incident_id,))
    assert incident["classification"] == "suspected_bug" and incident["state"] == "open"
    assert incident["occurrence_count"] == 1 and incident["latest_log_id"] == result.log_id
    assert incident["last_email_enqueued_at"] is not None
    assert incident["latest_payload"]["subject_value"] == "WZ457JN"

    occurrence = one(conn, "SELECT * FROM suspected_bug_occurrences WHERE incident_id = %s::uuid",
                     (result.incident_id,))
    assert occurrence["log_id"] == result.log_id and occurrence["occurrence_no"] == 1
    assert occurrence["email_decision"] == "enqueued"

    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox WHERE incident_id = %s::uuid",
                 (result.incident_id,))
    assert outbox["status"] == "pending" and outbox["attempts"] == 0
    assert outbox["recipients"] == ["ops@example.com"]
    assert outbox["recipient_config_ref"] == "SUSPECTED_BUG_ALERT_TO"
    assert outbox["subject"].startswith("[SUSPECTED_BUG][local_dev][ALPHA00001]")
    assert "WZ457JN" in outbox["body_text"] and "447352" in outbox["body_text"]
    print("PASS: one transaction writes ERROR log, incident, occurrence and outbox row")


def test_repeated_occurrence_is_grouped_and_suppressed(conn) -> None:
    reset(conn)
    first = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    second = sb.report_suspected_bug(conn, event(occurred_at=NOW + timedelta(minutes=5)),
                                     config=CONFIG, now=NOW + timedelta(minutes=5))

    assert second.incident_id == first.incident_id and not second.incident_created
    assert second.occurrence_count == 2
    assert not second.email_enqueued and second.suppression_reason == sb.SUPPRESSED_COOLDOWN

    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_occurrences")["n"] == 2
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 2
    print("PASS: repeats keep logging and counting while the email is deduplicated")


def test_material_change_and_reminder_enqueue_again(conn) -> None:
    reset(conn)
    first = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)

    grown = sb.report_suspected_bug(conn, event(affected_record_count=200),
                                    config=CONFIG, now=NOW + timedelta(minutes=10))
    assert grown.incident_id == first.incident_id
    assert grown.email_enqueued and grown.notification_reason == sb.REASON_MATERIAL_CHANGE

    later = NOW + timedelta(hours=30)
    reminder = sb.report_suspected_bug(conn, event(affected_record_count=200,
                                                   occurred_at=later), config=CONFIG, now=later)
    assert reminder.email_enqueued and reminder.notification_reason == sb.REASON_REMINDER

    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 3
    reasons = [row["notification_reason"] for row in rows(
        conn, "SELECT notification_reason FROM suspected_bug_email_outbox ORDER BY created_at")]
    assert reasons == ["new", "material_change", "reminder"], reasons
    print("PASS: material scope growth and the reminder interval re-alert")


def test_no_configured_recipient_still_persists(conn) -> None:
    reset(conn)
    result = sb.report_suspected_bug(conn, event(), config=NO_RECIPIENTS, now=NOW)

    assert result.reported and not result.email_enqueued
    assert result.suppression_reason == sb.SUPPRESSED_RECIPIENTS_NOT_CONFIGURED
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 0
    occurrence = one(conn, "SELECT * FROM suspected_bug_occurrences")
    assert occurrence["email_decision"] == "suppressed"
    assert occurrence["email_decision_reason"] == "recipients_not_configured"
    incident = one(conn, "SELECT * FROM suspected_bug_incidents")
    assert incident["last_email_enqueued_at"] is None

    # Once configured, the next occurrence alerts instead of being swallowed.
    followup = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW + timedelta(minutes=1))
    assert followup.email_enqueued and followup.notification_reason == sb.REASON_NEW
    print("PASS: missing recipients suppress delivery only, never persistence")


def test_failed_enqueue_rolls_back_the_whole_report(conn) -> None:
    reset(conn)
    original = sb._enqueue_email
    sb._enqueue_email = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture enqueue failure"))
    try:
        sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    except RuntimeError as exc:
        assert "fixture enqueue failure" in str(exc)
    else:
        raise AssertionError("enqueue failure must surface")
    finally:
        sb._enqueue_email = original
        conn.rollback()

    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 0
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_occurrences")["n"] == 0
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 0
    print("PASS: the atomic boundary rolls log, incident, occurrence and outbox together")


def test_original_business_exception_is_never_replaced(conn) -> None:
    reset(conn)
    broken = object()  # not a connection: reporting cannot work at all

    def business_operation():
        try:
            raise ValueError("original business failure")
        except ValueError:
            result = sb.safe_report_suspected_bug(event(), conn=broken, config=CONFIG, now=NOW)
            assert result.reported is False and result.error
            raise

    try:
        business_operation()
    except ValueError as exc:
        assert str(exc) == "original business failure", exc
    else:
        raise AssertionError("the original exception must propagate unchanged")
    print("PASS: a broken reporter never hides or replaces the business exception")


def test_concurrent_detection_creates_one_incident_and_one_email(dsn: str) -> None:
    first = connect(dsn)
    second = connect(dsn)
    reset(first)
    barrier = threading.Barrier(2)
    outcomes: dict[str, sb.SuspectedBugReportResult] = {}
    errors: list[Exception] = []

    def report(name: str, conn, moment: datetime) -> None:
        try:
            barrier.wait(timeout=10)
            outcomes[name] = sb.report_suspected_bug(conn, event(occurred_at=moment),
                                                     config=CONFIG, now=moment)
        except Exception as exc:  # surfaced by the assertions below
            errors.append(exc)

    threads = [
        threading.Thread(target=report, args=("a", first, NOW)),
        threading.Thread(target=report, args=("b", second, NOW + timedelta(seconds=1))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(outcomes) == 2
    assert {result.incident_id for result in outcomes.values()} == {outcomes["a"].incident_id}
    assert sum(1 for result in outcomes.values() if result.email_enqueued) == 1
    assert one(first, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 1
    assert one(first, "SELECT count(*)::int AS n FROM suspected_bug_occurrences")["n"] == 2
    assert one(first, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 1
    assert one(first, "SELECT occurrence_count FROM suspected_bug_incidents")["occurrence_count"] == 2
    first.close()
    second.close()
    print("PASS: concurrent detections collapse to one incident and one immediate email")


def test_prune_of_logs_keeps_incident_history(conn) -> None:
    reset(conn)
    result = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM logs WHERE id = %s", (result.log_id,))
    conn.commit()
    incident = one(conn, "SELECT * FROM suspected_bug_incidents")
    occurrence = one(conn, "SELECT * FROM suspected_bug_occurrences")
    assert incident["latest_log_id"] is None and incident["occurrence_count"] == 1
    assert occurrence["log_id"] is None and occurrence["occurrence_no"] == 1
    print("PASS: pruning logs leaves incidents and occurrences intact")


# --------------------------------------------------------------------- worker


def claim_all(conn) -> list[dict]:
    return worker.claim_batch(conn, CONFIG, limit=10)


def test_worker_delivers_and_records(conn) -> None:
    reset(conn)
    report = sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    counts = worker.run_once(config=CONFIG, sender=FakeSmtp(), conn=conn)

    assert counts["claimed"] == 1 and counts["sent"] == 1
    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox")
    assert outbox["status"] == "sent" and outbox["sent_at"] is not None
    assert outbox["attempts"] == 1 and outbox["claim_token"] is None
    assert outbox["provider_message_id"] == worker.message_id_for(str(outbox["outbox_id"]))
    assert FakeSmtp.sent and FakeSmtp.sent[-1]["to"] == ["ops@example.com"]

    incident = one(conn, "SELECT * FROM suspected_bug_incidents WHERE incident_id=%s::uuid",
                   (report.incident_id,))
    assert incident["last_email_sent_at"] is not None
    log = one(conn, "SELECT * FROM logs WHERE id = %s", (report.log_id,))
    assert log["context"]["classification"] == "suspected_bug", "delivery must not rewrite the log"
    print("PASS: successful delivery records sent state and the provider message id")


def test_worker_retries_transient_and_dead_letters_permanent(conn) -> None:
    reset(conn)
    sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)

    counts = worker.run_once(config=CONFIG, sender=FakeSmtp(TimeoutError("smtp timeout")), conn=conn)
    assert counts["retry"] == 1, counts
    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox")
    assert outbox["status"] == "retry" and outbox["attempts"] == 1
    assert outbox["available_at"] > datetime.now(timezone.utc), "retry must be delayed"
    assert "TimeoutError" in outbox["last_error"]
    assert not FakeSmtp.sent

    with conn.cursor() as cur:  # make it due again
        cur.execute("UPDATE suspected_bug_email_outbox SET available_at = now() - interval '1 minute'")
    conn.commit()

    refused = smtplib.SMTPRecipientsRefused({"ops@example.com": (550, b"mailbox unavailable")})
    counts = worker.run_once(config=CONFIG, sender=FakeSmtp(refused), conn=conn)
    assert counts["dead_letter"] == 1, counts
    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox")
    assert outbox["status"] == "dead_letter" and outbox["attempts"] == 2
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 1
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 1, \
        "delivery failure must not create another suspected_bug log"
    print("PASS: transient failures retry with backoff, permanent failures dead-letter")


def test_worker_dead_letters_exhausted_attempts(conn) -> None:
    reset(conn)
    sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    for _ in range(CONFIG.max_attempts):
        with conn.cursor() as cur:
            cur.execute("UPDATE suspected_bug_email_outbox SET available_at = now() - interval '1 minute' "
                        "WHERE status IN ('pending','retry')")
        conn.commit()
        worker.run_once(config=CONFIG, sender=FakeSmtp(TimeoutError("smtp timeout")), conn=conn)

    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox")
    assert outbox["status"] == "dead_letter" and outbox["attempts"] == CONFIG.max_attempts
    assert worker.run_once(config=CONFIG, sender=FakeSmtp(), conn=conn)["claimed"] == 0
    assert not FakeSmtp.sent
    print("PASS: exhausted attempts dead-letter and are never claimed again")


def test_concurrent_workers_never_send_twice(dsn: str) -> None:
    first = connect(dsn)
    second = connect(dsn)
    reset(first)
    sb.report_suspected_bug(first, event(), config=CONFIG, now=NOW)

    claimed_a = claim_all(first)
    claimed_b = claim_all(second)
    assert len(claimed_a) == 1 and claimed_b == [], "a claimed row must be invisible to the second worker"

    stolen = dict(claimed_a[0])
    stolen["claim_token"] = uuid.uuid4()
    assert worker.mark_sent(second, stolen, "spoofed") is False, "a wrong claim token cannot finish a row"

    assert worker.deliver_row(first, claimed_a[0], config=CONFIG, sender=FakeSmtp()) == "sent"
    assert len(FakeSmtp.sent) == 1
    assert one(first, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox WHERE status='sent'")["n"] == 1
    first.close()
    second.close()
    print("PASS: SKIP LOCKED claiming and claim tokens prevent double delivery")


def test_stale_claims_are_recoverable(conn) -> None:
    reset(conn)
    sb.report_suspected_bug(conn, event(), config=CONFIG, now=NOW)
    claimed = claim_all(conn)
    assert len(claimed) == 1
    with conn.cursor() as cur:  # simulate a worker killed mid-delivery
        cur.execute("UPDATE suspected_bug_email_outbox SET lease_expires_at = now() - interval '1 hour'")
    conn.commit()

    assert worker.recover_stale_claims(conn, CONFIG) == 1
    outbox = one(conn, "SELECT * FROM suspected_bug_email_outbox")
    assert outbox["status"] == "retry" and outbox["claim_token"] is None
    assert outbox["last_error"] == "stale_claim_recovered"

    counts = worker.run_once(config=CONFIG, sender=FakeSmtp(), conn=conn)
    assert counts["sent"] == 1 and len(FakeSmtp.sent) == 1
    print("PASS: stale claims return to the queue and deliver exactly once")


def test_api_transport_uses_the_same_store(dsn: str) -> None:
    """POST /suspected-bugs is a transport over `report_suspected_bug`, not a second contract."""
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    os.environ.update({
        "POSTGRES_HOST": parsed.get("host", "127.0.0.1"),
        "POSTGRES_PORT": str(parsed.get("port", "5432")),
        "POSTGRES_DB": parsed.get("dbname", ""),
        "POSTGRES_USER": parsed.get("user", ""),
        "POSTGRES_PASSWORD": parsed.get("password", ""),
        "API_WRITE_TOKEN": "fixture-write-token",
        "API_READ_TOKEN": "fixture-read-token",
        "SUSPECTED_BUG_ALERT_TO": "ops@example.com",
    })
    import importlib

    import api.main as api_main
    importlib.reload(api_main)

    conn = connect(dsn)
    reset(conn)
    try:
        assert any(getattr(route, "path", "") == "/suspected-bugs" for route in api_main.app.routes)

        payload = api_main.report_suspected_bug_event(
            event_json=__import__("json").dumps(event().to_dict(), default=str),
            authorization="Bearer fixture-write-token",
        )
        assert payload["reported"] and payload["email_enqueued"]
        assert payload["incident_id"] and payload["log_id"]

        log = one(conn, "SELECT * FROM logs WHERE id = %s", (payload["log_id"],))
        assert log["level"] == "ERROR" and log["context"]["classification"] == "suspected_bug"
        assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 1

        try:
            api_main.report_suspected_bug_event(event_json="{}", authorization="Bearer fixture-write-token")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 400, exc
        else:
            raise AssertionError("an invalid event must be rejected with HTTP 400")

        try:
            api_main.report_suspected_bug_event(event_json="{}", authorization="Bearer wrong")
        except Exception as exc:
            assert getattr(exc, "status_code", None) in (401, 403), exc
        else:
            raise AssertionError("the write token must be required")
    finally:
        conn.close()
    print("PASS: the API transport writes through the same atomic store")


def main() -> None:
    dsn = os.environ.get("SUSPECTED_BUG_TEST_DSN")
    if not dsn:
        raise SystemExit("SUSPECTED_BUG_TEST_DSN must point to a disposable database")
    if "logdb" in dsn:
        raise SystemExit("refusing to run against logdb; use a disposable database")

    conn = connect(dsn)
    try:
        test_migration_applies_cleanly_and_reruns(conn)
        test_atomic_report_creates_log_incident_occurrence_and_outbox(conn)
        test_repeated_occurrence_is_grouped_and_suppressed(conn)
        test_material_change_and_reminder_enqueue_again(conn)
        test_no_configured_recipient_still_persists(conn)
        test_failed_enqueue_rolls_back_the_whole_report(conn)
        test_original_business_exception_is_never_replaced(conn)
        test_prune_of_logs_keeps_incident_history(conn)
        test_worker_delivers_and_records(conn)
        test_worker_retries_transient_and_dead_letters_permanent(conn)
        test_worker_dead_letters_exhausted_attempts(conn)
        test_stale_claims_are_recoverable(conn)
    finally:
        conn.close()

    test_concurrent_detection_creates_one_incident_and_one_email(dsn)
    test_concurrent_workers_never_send_twice(dsn)
    test_api_transport_uses_the_same_store(dsn)
    print("OK - suspected_bug persistence, dedup and worker tests passed")


if __name__ == "__main__":
    main()
