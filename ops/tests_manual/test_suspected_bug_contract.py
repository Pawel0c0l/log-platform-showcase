#!/usr/bin/env python3
"""Unit tests for the suspected_bug event contract, sanitization and fingerprint.

No database, no SMTP. Run:
  .venv/bin/python ops/tests_manual/test_suspected_bug_contract.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import (  # noqa: E402
    CLASSIFICATION,
    REASON_MATERIAL_CHANGE,
    REASON_NEW,
    REASON_REMINDER,
    SUPPRESSED_ALERTS_DISABLED,
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_RECIPIENTS_NOT_CONFIGURED,
    STATE_OPEN,
    STATE_RESOLVED,
    SuspectedBugAlertConfig,
    SuspectedBugContractError,
    SuspectedBugEvent,
    _decide_email,
    load_alert_config,
    parse_recipients,
    redact_email,
    render_incident_email,
    safe_report_suspected_bug,
    scope_bucket,
)

NOW = datetime(2026, 7, 27, 10, 30, tzinfo=timezone.utc)


def event(**overrides) -> SuspectedBugEvent:
    values = dict(
        incident_code="ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT",
        title="Conflicting Dysponent_ID assignments for WZ457JN",
        summary="Two source ids are assigned to one registration on one date.",
        occurred_at=NOW,
        environment="local_dev",
        component="jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment",
        client_code="ALPHA00001",
        report_type="Alpha_GPS_Baza_LOG",
        database_name="alpha_main",
        schema_name="telematics_reports",
        table_name="Alpha_GPS_Baza_LOG",
        subject_type="vehicle_registration",
        subject_key="registration",
        subject_value="WZ457JN",
        affected_record_count=37,
        fingerprint_fields={
            "target_table": 'public."client_trips"',
            "conflicting_assignment_ids": ["447352", "551392"],
            "effective_assignment_date": "2025-02-13",
        },
    )
    values.update(overrides)
    return SuspectedBugEvent(**values)


def test_required_fields_and_classification() -> None:
    event().validate()
    assert event().classification == CLASSIFICATION == "suspected_bug"

    for bad in (
        {"incident_code": "lowercase_code"},
        {"incident_code": ""},
        {"title": "  "},
        {"summary": ""},
        {"environment": ""},
        {"component": ""},
        {"severity": "fatal"},
        {"occurred_at": datetime(2026, 7, 27, 10, 30)},  # naive
        {"affected_record_count": -1},
    ):
        try:
            event(**bad).validate()
        except SuspectedBugContractError:
            continue
        raise AssertionError(f"expected contract error for {bad}")

    try:
        SuspectedBugEvent(**{**event().to_dict(), "classification": "bug"}).validate()
    except SuspectedBugContractError:
        pass
    else:
        raise AssertionError("classification must be exactly suspected_bug")
    print("PASS: required fields, tz-aware timestamp and classification are enforced")


def test_sanitization_and_truncation() -> None:
    payload = event(
        details={
            "smtp_password": "hunter2",
            "db_connection_string": "postgresql://user:secret@host:5432/db",
            "api_token": "abc123",
            "operator_email": "owner@example.invalid",
            "note": "x" * 900,
            "trip_ids": list(range(100)),
            "safe_value": 42,
        },
        evidence={"nested": {"deep": {"deeper": {"deepest": {"too_far": 1}}}}},
        stack_trace="\n".join(f"line {n} password=hunter2" for n in range(120)),
    ).sanitized_payload()

    details = payload["details"]
    assert details["smtp_password"] == "[redacted]", details["smtp_password"]
    assert details["api_token"] == "[redacted]"
    assert details["db_connection_string"] == "[redacted]"
    assert "hunter2" not in str(payload), "secret leaked into the payload"
    assert details["operator_email"] == "p***@telematics.pl", details["operator_email"]
    assert details["note"].endswith("…[truncated]") and len(details["note"]) < 900
    assert len(details["trip_ids"]) == 26 and "…[truncated]" in str(details["trip_ids"][-1])
    assert details["safe_value"] == 42
    assert payload["truncated"] is True
    assert len(payload["stack_trace"].splitlines()) <= 40
    assert "password=[redacted]" in payload["stack_trace"], payload["stack_trace"][:200]
    assert payload["details"] and payload["evidence"]

    clean = event().sanitized_payload()
    assert clean["truncated"] is False
    for optional in ("raw_file_id", "run_id", "cleaned_artifact_id", "stage3_result_artifact_id"):
        assert optional in clean and clean[optional] is None, "missing provenance must be explicit"
    assert redact_email("someone@example.com") == "s***@example.com"
    print("PASS: allow-listing, redaction, bounded lists and explicit missing provenance")


def test_fingerprint_is_deterministic_and_scoped() -> None:
    base = event().fingerprint()
    assert base == event().fingerprint(), "fingerprint must be deterministic"

    reordered = event(fingerprint_fields={
        "target_table": 'public."client_trips"',
        "conflicting_assignment_ids": ["551392", "447352"],
        "effective_assignment_date": "2025-02-13",
    }).fingerprint()
    assert reordered == base, "conflict id order must not change the fingerprint"

    occurrence_noise = event(
        occurred_at=NOW + timedelta(days=3),
        run_id="11111111-1111-1111-1111-111111111111",
        raw_file_id="22222222-2222-2222-2222-222222222222",
        cleaned_artifact_id="33333333-3333-3333-3333-333333333333",
        affected_record_count=99,
        evidence={"sample_affected_trip_ids": [1, 2, 3]},
        stack_trace="different traceback",
    ).fingerprint()
    assert occurrence_noise == base, "run/raw-file/artifact/count changes must not regroup"

    assert event(subject_value=" wz 457 jn ").fingerprint() == base, "subject value must be normalized"

    for changed in (
        {"fingerprint_fields": {"target_table": 'public."client_trips"',
                                "conflicting_assignment_ids": ["447352", "999999"],
                                "effective_assignment_date": "2025-02-13"}},
        {"subject_value": "WX111AA"},
        {"incident_code": "OTHER_INVARIANT_VIOLATION"},
        {"table_name": "other_table"},
        {"client_code": "OTHER00001"},
        {"environment": "production"},
    ):
        assert event(**changed).fingerprint() != base, f"{changed} must change the fingerprint"
    print("PASS: fingerprint groups the logical cause and excludes occurrence noise")


def test_material_signature_tracks_scope_growth() -> None:
    small = event(affected_record_count=37).material_signature()
    same_bucket = event(affected_record_count=40).material_signature()
    doubled = event(affected_record_count=90).material_signature()
    assert small == same_bucket, "small growth must not re-alert"
    assert small != doubled, "doubled scope must be a material change"
    assert scope_bucket(37) == 5 and scope_bucket(0) == 0 and scope_bucket(None) == 0
    print("PASS: material signature only moves on bounded scope growth")


def test_email_decision_policy() -> None:
    configured = SuspectedBugAlertConfig(
        recipients=("ops@example.com",),
        cooldown=timedelta(hours=2),
        reminder_interval=timedelta(hours=24),
    )
    kwargs = dict(material_signature="sig", now=NOW)

    first = _decide_email(config=configured, incident_created=True, previous_state=None,
                          previous_material_signature=None, previous_email_enqueued_at=None, **kwargs)
    assert first.enqueue and first.reason == REASON_NEW

    repeat = _decide_email(config=configured, incident_created=False, previous_state=STATE_OPEN,
                           previous_material_signature="sig",
                           previous_email_enqueued_at=NOW - timedelta(minutes=30), **kwargs)
    assert not repeat.enqueue and repeat.suppression_reason == SUPPRESSED_COOLDOWN

    changed = _decide_email(config=configured, incident_created=False, previous_state=STATE_OPEN,
                            previous_material_signature="other",
                            previous_email_enqueued_at=NOW - timedelta(minutes=30), **kwargs)
    assert changed.enqueue and changed.reason == REASON_MATERIAL_CHANGE

    reopened = _decide_email(config=configured, incident_created=False, previous_state=STATE_RESOLVED,
                             previous_material_signature="sig",
                             previous_email_enqueued_at=NOW - timedelta(minutes=1), **kwargs)
    assert reopened.enqueue and reopened.reason == REASON_MATERIAL_CHANGE

    reminder = _decide_email(config=configured, incident_created=False, previous_state=STATE_OPEN,
                             previous_material_signature="sig",
                             previous_email_enqueued_at=NOW - timedelta(hours=30), **kwargs)
    assert reminder.enqueue and reminder.reason == REASON_REMINDER

    between = _decide_email(config=configured, incident_created=False, previous_state=STATE_OPEN,
                            previous_material_signature="sig",
                            previous_email_enqueued_at=NOW - timedelta(hours=5), **kwargs)
    assert not between.enqueue and between.suppression_reason == SUPPRESSED_COOLDOWN

    unconfigured = _decide_email(config=SuspectedBugAlertConfig(recipients=()), incident_created=True,
                                 previous_state=None, previous_material_signature=None,
                                 previous_email_enqueued_at=None, **kwargs)
    assert not unconfigured.enqueue
    assert unconfigured.suppression_reason == SUPPRESSED_RECIPIENTS_NOT_CONFIGURED

    disabled = _decide_email(config=SuspectedBugAlertConfig(enabled=False, recipients=("ops@example.com",)),
                             incident_created=True, previous_state=None, previous_material_signature=None,
                             previous_email_enqueued_at=None, **kwargs)
    assert not disabled.enqueue and disabled.suppression_reason == SUPPRESSED_ALERTS_DISABLED
    print("PASS: first send, cooldown, material change, reopen and reminder policy")


def test_configuration_has_no_recipient_fallback() -> None:
    empty = load_alert_config({})
    assert empty.recipients == () and not empty.recipients_configured
    assert empty.enabled is True and empty.recipient_config_ref == "SUSPECTED_BUG_ALERT_TO"

    configured = load_alert_config({
        "SUSPECTED_BUG_ALERT_TO": "a@example.com; b@example.com,broken",
        "SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES": "30",
        "SUSPECTED_BUG_ALERT_REMINDER_HOURS": "0",
        "SUSPECTED_BUG_EMAIL_MAX_ATTEMPTS": "3",
        "LOG_PLATFORM_TARGET_ENVIRONMENT": "local_dev",
    })
    assert configured.recipients == ("a@example.com", "b@example.com")
    assert configured.cooldown == timedelta(minutes=30)
    assert configured.reminder_interval is None
    assert configured.max_attempts == 3 and configured.environment == "local_dev"
    assert configured.redacted_recipients() == ["a***@example.com", "b***@example.com"]

    # No recipient list anywhere in the environment must ever fall back to report
    # or customer recipients used by other senders.
    with_other_senders = load_alert_config({
        "STAGE2_PENDING_REVIEW_NOTIFY_TO": "reports@example.com",
        "ECO_WEEKLY_EMAIL_FROM_EMAIL": "eco@example.com",
        "AUTOMATION_SMTP_FROM": "automation@example.com",
    })
    assert with_other_senders.recipients == ()
    assert parse_recipients(None) == () and parse_recipients("not-an-email") == ()

    backoff = SuspectedBugAlertConfig(initial_retry_delay=timedelta(seconds=60),
                                      max_retry_delay=timedelta(seconds=600))
    delays = [int(backoff.retry_delay(n).total_seconds()) for n in (1, 2, 3, 4, 5, 9)]
    assert delays == [60, 120, 240, 480, 600, 600], delays
    print("PASS: configuration is explicit, has no recipient fallback and bounds backoff")


def test_email_rendering() -> None:
    payload = event(
        run_id="44444444-4444-4444-4444-444444444444",
        raw_file_id="55555555-5555-5555-5555-555555555555",
        details={"conflicting_assignment_ids": ["447352", "551392"],
                 "operator_email": "owner@example.invalid"},
        evidence={"sample_affected_trip_ids": [901, 902], "html_probe": "<script>alert(1)</script>"},
        rows_modified=0,
        processing_outcome="ambiguous trips skipped; no target rows modified",
    ).sanitized_payload()

    subject, text, html = render_incident_email(
        payload=payload,
        config=load_alert_config({"SUSPECTED_BUG_ALERT_TO": "ops@example.com",
                                  "AUTOMATION_SMTP_PASSWORD": "hunter2"}),
        incident_id="66666666-6666-6666-6666-666666666666",
        fingerprint="f" * 64,
        state=STATE_OPEN,
        first_seen_at=NOW - timedelta(days=1),
        last_seen_at=NOW,
        occurrence_count=3,
        notification_reason=REASON_NEW,
    )

    assert subject == (
        "[SUSPECTED_BUG][local_dev][ALPHA00001][ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT] "
        "Conflicting Dysponent_ID assignments for WZ457JN"
    ), subject
    for needle in ("66666666-6666-6666-6666-666666666666", "f" * 64, "WZ457JN", "447352", "551392",
                   "44444444-4444-4444-4444-444444444444", "55555555-5555-5555-5555-555555555555",
                   "alpha_main", "telematics_reports", "Alpha_GPS_Baza_LOG", "Occurrence count: 3"):
        assert needle in text, f"missing {needle!r} in the text body"
    assert "Europe/Warsaw" in text and "UTC" in text, "both timezones must be unambiguous"
    assert "2026-07-27 12:30:00 Europe/Warsaw / 2026-07-27 10:30:00 UTC" in text
    assert "not available" in text, "missing optional provenance must render explicitly"
    assert "hunter2" not in text and "hunter2" not in html
    assert "<script>" not in html and "&lt;script&gt;" in html, "HTML must be escaped"
    assert "p***@telematics.pl" in text and "owner@example.invalid" not in text
    assert "suspected_bug" in text and "not a run status" in text
    print("PASS: email subject, identifiers, dual timestamps, escaping and redaction")


def test_reporter_never_recurses_or_raises() -> None:
    import api.suspected_bug as module

    calls = []
    original = module.report_suspected_bug

    def exploding(conn, event_arg, **kwargs):
        calls.append(event_arg.incident_code)
        # A reporter failure must not be reported as another suspected_bug.
        nested = safe_report_suspected_bug(event(incident_code="NESTED_FAILURE"))
        assert nested.error == "reentrancy_refused", nested
        raise RuntimeError("fixture reporting failure")

    module.report_suspected_bug = exploding
    try:
        result = safe_report_suspected_bug(event(), conn=object())
    finally:
        module.report_suspected_bug = original

    assert result.reported is False and result.error and "fixture reporting failure" in result.error
    assert result.fingerprint == event().fingerprint(), "fingerprint is still reported back"
    assert calls == ["ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"], calls
    print("PASS: reporting failures are contained, non-recursive and non-raising")


def main() -> None:
    test_required_fields_and_classification()
    test_sanitization_and_truncation()
    test_fingerprint_is_deterministic_and_scoped()
    test_material_signature_tracks_scope_growth()
    test_email_decision_policy()
    test_configuration_has_no_recipient_fallback()
    test_email_rendering()
    test_reporter_never_recurses_or_raises()
    print("OK - suspected_bug contract tests passed")


if __name__ == "__main__":
    main()
