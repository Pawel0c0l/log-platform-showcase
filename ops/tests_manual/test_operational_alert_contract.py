#!/usr/bin/env python3
"""Contract tests for the shared operational-alert boundary (P0-1 / P0-2).

Pure stdlib: no database, no SMTP, no production access.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_operational_alert_contract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import SuspectedBugAlertConfig, SuspectedBugEvent  # noqa: E402
from ops.operational_alert import (  # noqa: E402
    INCIDENT_JOB_TERMINAL_FAILURE,
    alerting_readiness,
    error_signature,
    is_self_alerting,
    report_operational_failure,
    utcnow,
)


def _event_for(message: str, *, component: str = "jobs.api.telematics.sync_trips_and_speeding",
               exception_type: str = "TelematicsProviderSafetyError") -> SuspectedBugEvent:
    """Build the event `report_job_terminal_failure` would build, without a DB."""
    identity = {"exception_type": exception_type}
    signature = error_signature(message)
    if signature:
        identity["error_signature"] = signature
    return SuspectedBugEvent(
        incident_code=INCIDENT_JOB_TERMINAL_FAILURE,
        title=f"Job terminated with an unhandled failure: {component}",
        summary="terminal failure",
        occurred_at=utcnow(),
        environment="production",
        component=component,
        subject_type="job_module",
        subject_key=component,
        exception_type=exception_type,
        fingerprint_fields=identity,
    )


def test_error_signature_collapses_per_attempt_noise() -> None:
    assert error_signature("chunk 1/1 at 2026-08-03T02:00:00+00:00") == "chunk <n>/<n> at <ts>"
    assert error_signature(None) == ""
    assert error_signature("   ") == ""
    # A UUID and a bare number are attempt-scoped, not cause-scoped.
    signature = error_signature(
        "client_id=f1c7f1ed-bcb2-4fba-8753-a951b2d952ad rows=1234"
    )
    assert "66a57660" not in signature and "1234" not in signature
    print("PASS: error_signature removes timestamps, uuids, hashes and counts")


def test_dispatcher_storm_collapses_to_one_incident_identity() -> None:
    """The 2026-08-01→08-04 outage: 135 five-minute ticks, one root cause.

    Each tick reported a different chunk window and a different wall-clock time.
    If any of that reached the fingerprint, the operator would have received one
    email per tick. Identity must be stable across all of them.
    """
    template = (
        "Response meta current_page does not match requested page (suspected loop or "
        "API mismatch) while fetching /trips chunk 1/1 for "
        "client_id=f1c7f1ed-bcb2-4fba-8753-a951b2d952ad "
        "chunk_start_ts=2026-08-{day:02d}T02:00:00+00:00 "
        "chunk_end_ts=2026-08-{day:02d}T02:05:00+00:00"
    )
    fingerprints = {
        _event_for(template.format(day=day)).fingerprint() for day in range(1, 5)
    }
    assert len(fingerprints) == 1, f"expected one incident identity, got {len(fingerprints)}"

    # A genuinely different root cause must NOT be folded into the same incident.
    other = _event_for("connection to server failed", exception_type="OperationalError")
    assert other.fingerprint() not in fingerprints

    # A different client is a different operational problem.
    different_client = SuspectedBugEvent(
        incident_code=INCIDENT_JOB_TERMINAL_FAILURE,
        title="t", summary="s", occurred_at=utcnow(), environment="production",
        component="jobs.api.telematics.sync_trips_and_speeding",
        client_code="ALPHA00001",
        fingerprint_fields={"exception_type": "TelematicsProviderSafetyError"},
    )
    assert different_client.fingerprint() not in fingerprints
    print("PASS: 135 dispatcher ticks share one incident identity; distinct causes do not")


def test_material_signature_is_stable_for_a_continuing_failure() -> None:
    """A stable material signature is what lets cooldown suppress repeats."""
    first = _event_for("boom at 2026-08-01T02:00:00+00:00")
    second = _event_for("boom at 2026-08-03T04:10:00+00:00")
    assert first.material_signature() == second.material_signature()
    print("PASS: material signature is stable across attempts of one root cause")


def test_self_alerting_components_are_refused() -> None:
    assert is_self_alerting("ops.suspected_bug_email_worker")
    assert is_self_alerting("suspected-bug-email-worker.service")
    assert not is_self_alerting("jobs.reports.workflow_b.orchestrator")

    # Refusal must be a returned result, never an exception, and must not touch a DB.
    result = report_operational_failure(
        incident_code=INCIDENT_JOB_TERMINAL_FAILURE,
        title="t", summary="s", component="ops.suspected_bug_email_worker",
    )
    assert result.error == "self_alerting_component_refused"
    assert not result.reported
    print("PASS: the alert path refuses to alert about itself (no recursion)")


def test_alerting_readiness_makes_missing_recipients_explicit() -> None:
    from ops.operational_alert import AlertingReadiness

    smtp = {"AUTOMATION_SMTP_HOST": "smtp.example.com"}

    unconfigured = AlertingReadiness(
        SuspectedBugAlertConfig(enabled=True, recipients=(), from_addr=None), env=smtp
    )
    assert not unconfigured.ready
    assert "recipients_not_configured" in unconfigured.problems
    assert unconfigured.as_dict()["recipient_config_ref"] == "SUSPECTED_BUG_ALERT_TO"

    disabled = AlertingReadiness(
        SuspectedBugAlertConfig(enabled=False, recipients=("ops@example.com",),
                                from_addr="alerts@example.com"), env=smtp,
    )
    assert not disabled.ready and "alerts_disabled" in disabled.problems

    healthy = AlertingReadiness(
        SuspectedBugAlertConfig(enabled=True, recipients=("ops@example.com",),
                                from_addr="alerts@example.com"), env=smtp,
    )
    assert healthy.ready and healthy.problems == []
    # Recipients are never exposed in the clear.
    assert healthy.as_dict()["recipients"] == ["o***@example.com"]
    print("PASS: unconfigured alerting is an explicit, machine-detectable state")


def test_readiness_checks_the_transport_that_actually_delivers() -> None:
    """Readiness must track `jobs.common.emailer`, not a lookalike variable.

    `load_smtp_config()` raises without `AUTOMATION_SMTP_HOST`, so every alert
    dead-letters. Readiness previously ignored it while flagging an unset
    `SUSPECTED_BUG_ALERT_FROM`, which has a documented fallback and breaks
    nothing — a green readiness check on an undeliverable configuration.
    """
    from ops.operational_alert import AlertingReadiness

    configured = SuspectedBugAlertConfig(
        enabled=True, recipients=("ops@example.com",), from_addr=None
    )

    no_transport = AlertingReadiness(configured, env={})
    assert not no_transport.ready
    assert "smtp_host_not_configured" in no_transport.problems

    # No SUSPECTED_BUG_ALERT_FROM is fine: the SMTP identity supplies the sender.
    with_transport = AlertingReadiness(
        configured,
        env={"AUTOMATION_SMTP_HOST": "smtp.example.com",
             "AUTOMATION_SMTP_FROM": "robot@example.com"},
    )
    assert with_transport.ready, with_transport.problems
    assert with_transport.effective_from == "robot@example.com"
    assert with_transport.as_dict()["smtp_host_configured"] is True
    assert with_transport.as_dict()["effective_from"] == "r***@example.com"

    # And the emailer's own default is what readiness reports when neither is set.
    from jobs.common.emailer import load_smtp_config_from_env

    defaulted = AlertingReadiness(configured, env={"AUTOMATION_SMTP_HOST": "smtp.example.com"})
    real = load_smtp_config_from_env()
    assert defaulted.effective_from == AlertingReadiness.SMTP_FROM_DEFAULT
    assert real.from_addr == AlertingReadiness.SMTP_FROM_DEFAULT, (
        "readiness default drifted from the transport default"
    )
    print("PASS: readiness reflects the SMTP transport that actually sends the mail")


def test_systemd_adapter_refuses_to_alert_about_the_alert_path() -> None:
    """`OnFailure=` routing must not be able to loop through the mail worker."""
    import ops.systemd_failure_adapter as adapter

    for unit in ("suspected-bug-email-worker.service", "log-platform-unit-failure@.service"):
        outcome = adapter.report_unit_failure(unit)
        assert outcome["reported"] is False
        assert outcome["reason"] == "self_alerting_unit_refused"

    assert adapter.normalize_unit("log-workflow-b") == "log-workflow-b.service"
    assert adapter.normalize_unit("log-workflow-b.service") == "log-workflow-b.service"
    assert adapter.normalize_unit("  ") == ""
    assert adapter.report_unit_failure("")["reason"] == "missing_unit"

    # Neither the handler nor the mail worker may carry an active OnFailure=
    # directive. Comments explaining why are expected, so only real directives
    # are inspected.
    for unit_name in (
        "log-platform-unit-failure@.service", "suspected-bug-email-worker.service"
    ):
        text = (REPO_ROOT / "ops/systemd/proposed" / unit_name).read_text(encoding="utf-8")
        directives = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("OnFailure=")
        ]
        assert directives == [], f"{unit_name} must not route its own failure: {directives}"
    print("PASS: systemd failure routing is non-recursive by construction")


def _directive_section(text: str, directive: str) -> str | None:
    """Which INI section a directive actually lands in.

    Presence is not enough. `OnFailure=` is a `[Unit]` option; systemd silently
    ignores it under `[Service]` with only an "Unknown key name" warning, so a
    misplaced directive produces a unit that looks routed and alerts about
    nothing. That is precisely the silent-inertness this milestone removes, so
    the section is asserted, not just the string.
    """
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line.startswith(directive):
            return section
    return None


def test_monitored_units_route_failures_into_the_alert_path() -> None:
    base = REPO_ROOT / "ops/systemd/proposed"
    routed = "OnFailure=log-platform-unit-failure@%n.service"
    targets = [
        base / unit_dir / "95-onfailure.conf"
        for unit_dir in (
            "log-job@dispatcher.service.d", "log-workflow-b.service.d",
            "log-backup.service.d", "log-platform-prune.service.d",
        )
    ] + [
        base / unit for unit in (
            "execution-watchdog.service", "disk-space-monitor.service",
            "backup-retention.service",
        )
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert routed in text, path.name
        section = _directive_section(text, "OnFailure=")
        assert section == "Unit", (
            f"{path.name}: OnFailure= is in [{section}]; systemd only honours it in [Unit]"
        )
    print("PASS: every monitored unit routes failure into the alert path from [Unit]")


def test_job_failure_integration(dsn: str) -> None:
    """End to end: a job exception becomes one incident and one outbox row.

    Proves the P0-2 anti-storm property against real persistence, not just
    fingerprint arithmetic: many repeats of one root cause must enqueue one email.
    """
    import os

    import psycopg
    from psycopg.rows import dict_row

    import api.suspected_bug as sb
    from ops.operational_alert import report_job_terminal_failure

    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    os.environ.update(
        {
            "POSTGRES_HOST": parsed.get("host", "127.0.0.1"),
            "POSTGRES_PORT": str(parsed.get("port", "5432")),
            "POSTGRES_DB": parsed.get("dbname", ""),
            "POSTGRES_USER": parsed.get("user", ""),
            "POSTGRES_PASSWORD": parsed.get("password", ""),
            "LOG_PLATFORM_TARGET_ENVIRONMENT": "test",
        }
    )
    config = sb.SuspectedBugAlertConfig(
        recipients=("ops@example.com",), from_addr="alerts@example.com",
        environment="test",
    )

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
                "suspected_bug_incidents RESTART IDENTITY CASCADE"
            )
            cur.execute("DELETE FROM logs")
        conn.commit()

        # Replay the shape of the 2026-08-01→08-04 outage: repeated five-minute
        # ticks, each with its own chunk window and wall clock.
        for tick in range(24):
            exc = RuntimeError(
                "Response meta current_page does not match requested page while fetching "
                f"/trips chunk 1/1 chunk_start_ts=2026-08-03T{tick:02d}:00:00+00:00"
            )
            result = report_job_terminal_failure(
                job_module="jobs.api.telematics.sync_trips_and_speeding",
                exc=exc, params={"client_code": "FOXTROT00001", "window_start_ts": str(tick)},
                conn=conn, config=config,
            )
            assert result.reported, result.error

        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_incidents")
            incidents = cur.fetchone()["n"]
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_occurrences")
            occurrences = cur.fetchone()["n"]
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_email_outbox")
            emails = cur.fetchone()["n"]
            cur.execute("SELECT count(*)::int AS n FROM logs WHERE level = 'ERROR'")
            error_logs = cur.fetchone()["n"]
        conn.rollback()

        assert incidents == 1, f"24 ticks of one root cause must group into 1 incident, got {incidents}"
        assert occurrences == 24, f"every occurrence must still be recorded, got {occurrences}"
        assert emails == 1, f"cooldown must hold the alert to 1 email, got {emails}"
        assert error_logs == 24, "every failure keeps its own durable ERROR log"
        print("PASS: 24 repeated job failures produce 1 incident, 1 email, 24 durable logs")

        # A genuinely different failure is a different incident and a new email.
        report_job_terminal_failure(
            job_module="jobs.reports.workflow_b.orchestrator",
            exc=ValueError("missing_report_policy"), params={"client_code": "BRAVO00016"},
            conn=conn, config=config,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_incidents")
            assert cur.fetchone()["n"] == 2
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_email_outbox")
            assert cur.fetchone()["n"] == 2
        conn.rollback()
        print("PASS: a distinct root cause still raises its own incident and email")

        # Unconfigured recipients: persistence continues, delivery is explicitly suppressed.
        silent = sb.SuspectedBugAlertConfig(recipients=(), environment="test")
        result = report_job_terminal_failure(
            job_module="jobs.ecodriving.job_eco_driving_aggregate",
            exc=RuntimeError("boom"), conn=conn, config=silent,
        )
        assert result.reported and not result.email_enqueued
        assert result.suppression_reason == sb.SUPPRESSED_RECIPIENTS_NOT_CONFIGURED
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM suspected_bug_incidents")
            assert cur.fetchone()["n"] == 3
        conn.rollback()
        print("PASS: without recipients the incident persists and suppression is explicit")

        _assert_systemd_adapter_persists(conn)
    finally:
        conn.close()


def _assert_systemd_adapter_persists(conn) -> None:
    """The infrastructure path must produce a real incident, not just refuse loops.

    This is the 2026-07-31 20:00 / 2026-08-01 06:00 class: `log-workflow-b.service`
    exited non-zero *before* any `public.runs` row existed, so nothing inside the
    application could ever have reported it. Only PID 1 saw it.
    """
    import os

    import ops.systemd_failure_adapter as adapter

    os.environ.update(
        {
            "SUSPECTED_BUG_ALERT_TO": "ops@example.com",
            "SUSPECTED_BUG_ALERT_FROM": "alerts@example.com",
            "SUSPECTED_BUG_ALERTS_ENABLED": "true",
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
            "suspected_bug_incidents RESTART IDENTITY CASCADE"
        )
    conn.commit()

    # systemctl is not consulted: the context is supplied, so this stays offline.
    context = {"result": "exit-code", "exec_main_status": "1", "active_state": "failed"}
    outcome = adapter.report_unit_failure("log-workflow-b.service", context=context, conn=conn)
    assert outcome["reported"] is True, outcome
    assert outcome["email_enqueued"] is True, outcome

    # A unit that keeps failing every scheduled fire is still one incident.
    for _ in range(5):
        adapter.report_unit_failure("log-workflow-b.service", context=context, conn=conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*)::int AS n FROM suspected_bug_incidents WHERE incident_code = %s",
            ("SYSTEMD_UNIT_FAILURE",),
        )
        incidents = cur.fetchone()["n"]
        cur.execute("SELECT count(*)::int AS n FROM suspected_bug_email_outbox")
        emails = cur.fetchone()["n"]
        cur.execute("SELECT count(*)::int AS n FROM suspected_bug_occurrences")
        occurrences = cur.fetchone()["n"]
    conn.rollback()
    assert incidents == 1, f"6 failures of one unit must be 1 incident, got {incidents}"
    assert emails == 1, f"cooldown must hold this to 1 email, got {emails}"
    assert occurrences == 6, f"every failure is still recorded, got {occurrences}"

    # A different failing unit is a different incident.
    adapter.report_unit_failure("log-job@dispatcher.service", context=context, conn=conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*)::int AS n FROM suspected_bug_incidents")
        assert cur.fetchone()["n"] == 2
    conn.rollback()
    print("PASS: a failed unit becomes one durable incident however often it repeats")


def main() -> None:
    test_error_signature_collapses_per_attempt_noise()
    test_dispatcher_storm_collapses_to_one_incident_identity()
    test_material_signature_is_stable_for_a_continuing_failure()
    test_self_alerting_components_are_refused()
    test_alerting_readiness_makes_missing_recipients_explicit()
    test_readiness_checks_the_transport_that_actually_delivers()
    test_systemd_adapter_refuses_to_alert_about_the_alert_path()
    test_monitored_units_route_failures_into_the_alert_path()

    import os

    dsn = os.environ.get("OPERATIONAL_ALERT_TEST_DSN")
    if dsn:
        if "logdb" in dsn:
            raise SystemExit("refusing to run against logdb; use a disposable database")
        test_job_failure_integration(dsn)
    else:
        print("SKIP: OPERATIONAL_ALERT_TEST_DSN unset - job failure integration not run")
    print("OK - operational alert contract tests passed")


if __name__ == "__main__":
    main()
