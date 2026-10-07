#!/usr/bin/env python3
"""Shared operator-alert boundary for terminal operational failures.

This module is the single place where operational subsystems turn a *terminal*
failure into a durable, deduplicated, emailable incident. It deliberately adds
no new alert system: it builds `SuspectedBugEvent` values and hands them to the
existing `api.suspected_bug` incident + outbox machinery, which already owns

  * incident identity and cooldown (`suspected_bug_incidents`),
  * transactional email enqueue (`suspected_bug_email_outbox`),
  * claim/lease/retry/dead-letter delivery (`ops.suspected_bug_email_worker`).

Two properties matter more than anything else here.

**Alerting must never change the outcome of the thing that failed.** Every entry
point returns a result object and never raises; `safe_report_suspected_bug`
already refuses to recurse and writes its own failures to stderr as ordinary
operational errors.

**A continuing root cause must produce one incident, not one email per retry.**
The Workflow A dispatcher fires every five minutes; the 2026-08-01→08-04
provider-safety outage produced 135 failed ticks. Incident identity therefore
excludes everything that varies per attempt — run id, timestamps, chunk
boundaries, counts — via `error_signature()`. 135 ticks collapse onto one
fingerprint, which the existing 120-minute cooldown and 24-hour reminder
interval turn into roughly one initial email plus a daily reminder.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import (  # noqa: E402
    SuspectedBugAlertConfig,
    SuspectedBugEvent,
    SuspectedBugReportResult,
    is_configuration_suppression,
    load_alert_config,
    redact_email,
    safe_report_suspected_bug,
)

# Incident codes owned by this module. Each is a distinct operator playbook.
INCIDENT_JOB_TERMINAL_FAILURE = "JOB_TERMINAL_FAILURE"
INCIDENT_UNIT_FAILURE = "SYSTEMD_UNIT_FAILURE"
INCIDENT_SCHEDULED_RUN_MISSING = "SCHEDULED_RUN_MISSING"
INCIDENT_SCHEDULED_RUN_STALE = "SCHEDULED_RUN_STALE"
INCIDENT_SCHEDULER_HEARTBEAT_LOST = "SCHEDULER_HEARTBEAT_LOST"
INCIDENT_DISK_SPACE_WARNING = "DISK_SPACE_WARNING"
INCIDENT_DISK_SPACE_CRITICAL = "DISK_SPACE_CRITICAL"
INCIDENT_BACKUP_RETENTION_FAILED = "BACKUP_RETENTION_FAILED"
INCIDENT_ALERT_DELIVERY_FAILED = "ALERT_DELIVERY_FAILED"

# Optional attribute a raised exception may expose to contribute sanitized,
# incident-shaped context that the runner boundary cannot infer from `params`.
#
# `ops/runner.py` sees only the job module and the params it was launched with,
# so a Workflow B failure arrived carrying no stage, no outcome and no exception
# detail. Rather than teach the shared runner about Workflow B, the exception
# carries its own evidence and this module consumes whatever is safely present.
# The contract is deliberately duck-typed and total: any exception may expose it,
# no exception must, and a malformed value degrades to "no extra context"
# instead of disturbing the failure it is describing.
INCIDENT_DETAILS_ATTRIBUTE = "operational_incident_details"

# Keys `report_job_terminal_failure` will lift out of that attribute into first
# class incident identity/context. Everything else it carries is recorded under
# `details`, which is excluded from the fingerprint.
_INCIDENT_DETAIL_CONTEXT_KEYS = (
    "workflow_name",
    "stage_name",
    "client_code",
    "client_id",
    "dataset_name",
    "report_type",
    "terminal_outcome",
    "failing_stage",
    "stage_exception_type",
)

# Components whose own failure must never be reported through this module: doing
# so would ask a broken alert path to alert about itself. See `is_self_alerting`.
#
# The rule is about the *reporter*, not the topic. These names are the alert
# machinery reporting itself, which is the loop. An independent observer
# describing that machinery is not: `ops/execution_watchdog.py` is a separate
# unit in a separate process, and its `alerting.delivery` /
# `alerting.email_worker` subjects are deliberately absent from this set so a
# dead mail path still produces a durable incident and a durable non-OK
# observation. That incident cannot be emailed while the channel is down — which
# is exactly why the watchdog also persists the observation and the worker exits
# non-zero. Adding those subjects here would silence the only reporter that can
# still speak.
SELF_ALERTING_COMPONENTS = frozenset(
    {
        "ops.suspected_bug_email_worker",
        "suspected-bug-email-worker.service",
        "log-platform-unit-failure@.service",
    }
)

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_HEXBLOB_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")
_WHITESPACE_RE = re.compile(r"\s+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def environment_name(env: Mapping[str, str] | None = None) -> str:
    values = env if env is not None else os.environ
    return (values.get("LOG_PLATFORM_TARGET_ENVIRONMENT") or "unknown").strip() or "unknown"


def error_signature(message: str | None, *, limit: int = 300) -> str:
    """Collapse an error message onto the identity of its *cause*.

    Timestamps, UUIDs, hashes and bare numbers vary between attempts of one
    continuing failure. Removing them is what makes 135 dispatcher ticks share a
    single incident fingerprint instead of producing 135 alert emails.
    """
    text = str(message or "").strip()
    if not text:
        return ""
    text = _UUID_RE.sub("<uuid>", text)
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _HEXBLOB_RE.sub("<hash>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def is_self_alerting(component: str | None) -> bool:
    """True when `component` is part of the alert path itself."""
    return str(component or "").strip() in SELF_ALERTING_COMPONENTS


# --------------------------------------------------------------- readiness


class AlertingReadiness:
    """Explicit, inspectable answer to 'can this platform actually alert me?'.

    Recipient configuration that is simply absent is the dangerous case: without
    it every incident is silently suppressed as `recipients_not_configured` and
    a completely inert alert path is indistinguishable from a healthy one. This
    makes that state a first-class, machine-detectable result instead.
    """

    # Delivery goes through `jobs.common.emailer.load_smtp_config()`, which raises
    # when AUTOMATION_SMTP_HOST is unset and defaults the sender to
    # AUTOMATION_SMTP_FROM. Readiness previously checked SUSPECTED_BUG_ALERT_FROM
    # — an optional override with a documented fallback — while ignoring the one
    # variable whose absence makes every send fail. It reported a problem for the
    # harmless case and stayed silent on the fatal one.
    SMTP_HOST_ENV = "AUTOMATION_SMTP_HOST"
    SMTP_FROM_ENV = "AUTOMATION_SMTP_FROM"
    SMTP_FROM_DEFAULT = "automations@example.invalid"

    def __init__(
        self, config: SuspectedBugAlertConfig, env: Mapping[str, str] | None = None
    ) -> None:
        values = env if env is not None else os.environ
        self.config = config
        self.smtp_host = (values.get(self.SMTP_HOST_ENV) or "").strip()
        self.effective_from = (
            (config.from_addr or "").strip()
            or (values.get(self.SMTP_FROM_ENV) or "").strip()
            or self.SMTP_FROM_DEFAULT
        )
        self.problems: list[str] = []
        if not config.enabled:
            self.problems.append("alerts_disabled")
        if not config.recipients_configured:
            self.problems.append("recipients_not_configured")
        if not self.smtp_host:
            self.problems.append("smtp_host_not_configured")
        if not self.effective_from:
            self.problems.append("sender_not_configured")

    @property
    def ready(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "log-platform-alerting-readiness/v1",
            "ready": self.ready,
            "problems": list(self.problems),
            "alerts_enabled": bool(self.config.enabled),
            "recipients_configured": bool(self.config.recipients_configured),
            "recipient_config_ref": self.config.recipient_config_ref,
            "recipient_count": len(self.config.recipients),
            "recipients": [redact_email(address) for address in self.config.recipients],
            "sender_configured": bool(self.effective_from),
            # The address delivery will actually use, after the SMTP fallback.
            "effective_from": redact_email(self.effective_from),
            "smtp_host_configured": bool(self.smtp_host),
            "smtp_transport_ref": self.SMTP_HOST_ENV,
            "environment": self.config.environment,
            "cooldown_minutes": int(self.config.cooldown.total_seconds() // 60),
            "reminder_hours": (
                int(self.config.reminder_interval.total_seconds() // 3600)
                if self.config.reminder_interval is not None
                else None
            ),
            "max_attempts": self.config.max_attempts,
        }


def alerting_readiness(config: SuspectedBugAlertConfig | None = None) -> AlertingReadiness:
    return AlertingReadiness(config or load_alert_config())


# ------------------------------------------------------------- reporting


def report_operational_failure(
    *,
    incident_code: str,
    title: str,
    summary: str,
    component: str,
    severity: str = "error",
    workflow_name: str | None = None,
    stage_name: str | None = None,
    job_name: str | None = None,
    client_code: str | None = None,
    client_id: str | None = None,
    dataset_name: str | None = None,
    run_id: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    subject_value: str | None = None,
    exception_type: str | None = None,
    error_message: str | None = None,
    stack_trace: str | None = None,
    suggested_action: str | None = None,
    details: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    extra_identity: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    conn=None,
    config: SuspectedBugAlertConfig | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> SuspectedBugReportResult:
    """Report one terminal operational failure. Never raises.

    `extra_identity` participates in incident identity; everything caller-supplied
    that varies per attempt belongs in `details`/`evidence` instead, which are
    recorded but excluded from the fingerprint.
    """
    if is_self_alerting(component):
        # Refusing here is the whole anti-recursion contract: a broken email
        # worker must surface through outbox dead-letter state and systemd, never
        # by asking itself to send another alert.
        _stderr_event(
            "operational_alert_self_report_refused",
            component=component,
            incident_code=incident_code,
        )
        return SuspectedBugReportResult(fingerprint="", error="self_alerting_component_refused")

    identity: dict[str, Any] = {}
    if exception_type:
        identity["exception_type"] = str(exception_type)
    signature = error_signature(error_message)
    if signature:
        identity["error_signature"] = signature
    if extra_identity:
        identity.update({str(key): value for key, value in extra_identity.items()})

    event = SuspectedBugEvent(
        incident_code=incident_code,
        title=title,
        summary=summary,
        occurred_at=occurred_at or utcnow(),
        environment=environment_name(),
        component=component,
        severity=severity,
        workflow_name=workflow_name,
        stage_name=stage_name,
        job_name=job_name,
        client_id=client_id,
        client_code=client_code,
        dataset_name=dataset_name,
        run_id=run_id,
        subject_type=subject_type,
        subject_key=subject_key,
        subject_value=subject_value,
        exception_type=exception_type,
        stack_trace=stack_trace,
        suggested_action=suggested_action,
        details=dict(details or {}),
        evidence=dict(evidence or {}),
        fingerprint_fields=identity,
    )
    result = safe_report_suspected_bug(event, conn=conn, config=config, now=now, commit=commit)
    _warn_if_delivery_not_configured(result, incident_code=incident_code, component=component)
    return result


def _warn_if_delivery_not_configured(
    result: SuspectedBugReportResult, *, incident_code: str, component: str
) -> None:
    """Make an inert alert path loud at the moment it swallows an incident.

    A configuration suppression is not throttling — it means this incident, and
    every future one, will be recorded and never sent. Emitting an
    `operational_error` here costs nothing, needs no schema and no delivery path,
    and is visible in journald immediately, including on a host where the mail
    channel is exactly what is broken.

    Only the *names* of the missing settings are emitted. Recipients, sender and
    SMTP transport values never appear.
    """
    if not getattr(result, "email_suppressed", False):
        return
    reason = getattr(result, "suppression_reason", None)
    if not is_configuration_suppression(reason):
        return
    try:
        problems = alerting_readiness().problems
    except Exception:  # pragma: no cover - readiness must never mask reporting
        problems = []
    _stderr_event(
        "operational_alert_delivery_not_configured",
        incident_code=incident_code,
        component=component,
        suppression_reason=reason,
        incident_id=getattr(result, "incident_id", None),
        readiness_problems=problems or None,
        remediation=(
            "The incident is persisted but no email will be sent. Ensure this "
            "unit sources /etc/log-platform/runtime.env (EnvironmentFile=), the "
            "same authoritative configuration the alert worker uses."
        ),
    )


def report_job_terminal_failure(
    *,
    job_module: str,
    exc: BaseException,
    run_id: str | None = None,
    params: Mapping[str, Any] | None = None,
    stack_trace: str | None = None,
    conn=None,
    config: SuspectedBugAlertConfig | None = None,
    now: datetime | None = None,
) -> SuspectedBugReportResult:
    """Terminal failure of a job executed through `ops/runner.py`.

    `ops/runner.py` is the one boundary every scheduled job crosses — Workflow A
    dispatcher and sync, the Workflow B orchestrator and its stages, retention
    purge, the Eco aggregate and email jobs. Reporting here covers all of them
    without scattering call sites through business logic, and without touching
    any job's own success semantics.

    The runner knows only the module and its params, which for the Workflow B
    orchestrator are empty — so the incident used to carry no stage, no outcome
    and no failing-component identity. `INCIDENT_DETAILS_ATTRIBUTE` lets the
    raised exception supply that itself; anything absent stays absent rather than
    being invented.
    """
    safe_params = _identity_params(params)
    carried = _incident_details_from_exception(exc)
    context = {key: carried.get(key) for key in _INCIDENT_DETAIL_CONTEXT_KEYS}
    stage_name = context.get("stage_name") or context.get("failing_stage")
    terminal_outcome = context.get("terminal_outcome")

    summary = (
        f"{job_module} raised {type(exc).__name__} and the platform run was "
        f"recorded FAILED. The job produced no successful completion for this "
        f"invocation and requires operator attention."
    )
    if terminal_outcome:
        summary += f" Terminal outcome: {terminal_outcome}."
    if stage_name:
        summary += f" Failing stage: {stage_name}."

    # `terminal_outcome` and the failing stage identify the *cause*, not the
    # attempt, so they belong in the fingerprint: a Stage 2 crash and a Stage 3
    # crash of the same job are different incidents and must not collapse onto
    # one another through the shared exception type.
    extra_identity = {
        key: value
        for key, value in (
            ("terminal_outcome", terminal_outcome),
            ("failing_stage", stage_name),
        )
        if value
    }

    return report_operational_failure(
        incident_code=INCIDENT_JOB_TERMINAL_FAILURE,
        title=f"Job terminated with an unhandled failure: {job_module}",
        summary=summary,
        component=job_module,
        job_name=job_module,
        workflow_name=context.get("workflow_name") or _workflow_of(job_module),
        stage_name=stage_name,
        client_code=safe_params.get("client_code") or context.get("client_code"),
        client_id=safe_params.get("client_id") or context.get("client_id"),
        dataset_name=safe_params.get("dataset_name") or context.get("dataset_name"),
        run_id=run_id,
        subject_type="job_module",
        subject_key=job_module,
        exception_type=type(exc).__name__,
        error_message=str(exc),
        stack_trace=stack_trace,
        extra_identity=extra_identity or None,
        suggested_action=(
            "Inspect the FAILED run in `public.runs` and its `public.logs` rows, "
            "then re-run the job once the root cause is resolved. Repeated "
            "occurrences of one root cause share this incident."
        ),
        details={"params": safe_params, **({"job_evidence": carried} if carried else {})},
        conn=conn,
        config=config,
        now=now,
    )


def _incident_details_from_exception(exc: BaseException) -> dict[str, Any]:
    """Read `INCIDENT_DETAILS_ATTRIBUTE` off an exception, defensively.

    Reporting must never disturb the failure it describes, so every failure mode
    here — attribute missing, wrong type, property that raises — degrades to an
    empty mapping. Values are coerced to short strings/scalars because they end
    up in an email body and in a JSONB payload.
    """
    try:
        raw = getattr(exc, INCIDENT_DETAILS_ATTRIBUTE, None)
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        name = str(key)[:64]
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            out[name] = value
        else:
            out[name] = str(value)[:500]
    return out


def _workflow_of(job_module: str) -> str | None:
    module = str(job_module or "")
    if module.startswith("jobs.api.telematics"):
        return "workflow_a"
    if module.startswith("jobs.reports.workflow_b") or module.startswith("jobs.mail"):
        return "workflow_b"
    if module.startswith("jobs.reports"):
        return "workflow_b"
    if module.startswith("jobs.ecodriving"):
        return "eco_driving"
    return None


_IDENTITY_PARAM_KEYS = ("client_code", "client_id", "dataset_name", "schedule_id", "execution_mode")


def _identity_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only stable, non-secret identity keys. Windows and limits vary per attempt."""
    if not isinstance(params, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in _IDENTITY_PARAM_KEYS:
        value = params.get(key)
        if value not in (None, ""):
            out[key] = str(value)[:200]
    return out


def _stderr_event(event: str, **fields: Any) -> None:
    payload = {
        "level": "ERROR",
        "classification": "operational_error",
        "component": "ops.operational_alert",
        "event": event,
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    try:
        print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI: report alerting readiness. Exits non-zero when alerting is not usable."""
    import argparse

    parser = argparse.ArgumentParser(description="Operator alerting readiness check")
    parser.add_argument(
        "--readiness", action="store_true", help="Print alerting readiness JSON (default)"
    )
    parser.parse_args(argv)

    readiness = alerting_readiness()
    print(json.dumps(readiness.as_dict(), indent=2, sort_keys=True))
    return 0 if readiness.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
