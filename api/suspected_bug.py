"""Platform-level `suspected_bug` reporting: contract, grouping and email outbox.

`suspected_bug` is an ERROR **classification**, never a run status. A component
that detects a likely defect, a data-integrity conflict, a violated invariant or
an impossible state reports it here; the platform then

  1. writes a durable ERROR log row with `context.classification = suspected_bug`,
  2. upserts the logical incident identified by a deterministic fingerprint,
  3. appends the occurrence,
  4. enqueues at most one alert email into the transactional outbox,

all inside one transaction. Delivery happens later in
`ops/suspected_bug_email_worker.py`; a failed or unconfigured email never rolls
back the log or the incident, and never replaces the original business error.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Any, Mapping, Sequence

try:
    from api.timezone_utils import business_time_log_context, get_business_timezone, get_business_timezone_name
except ImportError:  # inside the API container `api/` is the working directory
    from timezone_utils import business_time_log_context, get_business_timezone, get_business_timezone_name

CLASSIFICATION = "suspected_bug"
SEVERITIES = ("warning", "error", "critical")
DEFAULT_SEVERITY = "error"

STATE_OPEN = "open"
STATE_RESOLVED = "resolved"

REASON_NEW = "new"
REASON_MATERIAL_CHANGE = "material_change"
REASON_REMINDER = "reminder"

SUPPRESSED_ALERTS_DISABLED = "alerts_disabled"
SUPPRESSED_RECIPIENTS_NOT_CONFIGURED = "recipients_not_configured"
SUPPRESSED_COOLDOWN = "cooldown"
SUPPRESSED_DUPLICATE_NOTIFICATION = "duplicate_notification_key"

# Two suppressions that look alike in `suspected_bug_occurrences` mean opposite
# things operationally.
#
# `cooldown` and `duplicate_notification_key` are the mechanism working: one
# continuing root cause, one email. They are *intentional throttles*.
#
# `alerts_disabled` and `recipients_not_configured` mean nothing will ever be
# sent for this incident, or any other, until a human changes configuration.
# They are *configuration defects*, and treating them as ordinary suppression is
# what made a completely inert alert path indistinguishable from a healthy one:
# production accumulated 23 job-originated occurrences over three weeks, every
# one of them recorded and none of them sent, with no signal anywhere.
CONFIGURATION_SUPPRESSION_REASONS = frozenset(
    {SUPPRESSED_ALERTS_DISABLED, SUPPRESSED_RECIPIENTS_NOT_CONFIGURED}
)
THROTTLE_SUPPRESSION_REASONS = frozenset(
    {SUPPRESSED_COOLDOWN, SUPPRESSED_DUPLICATE_NOTIFICATION}
)


def is_configuration_suppression(reason: str | None) -> bool:
    """True when suppression means 'nothing will ever be delivered'."""
    return str(reason or "") in CONFIGURATION_SUPPRESSION_REASONS

OUTBOX_PENDING = "pending"
OUTBOX_SENDING = "sending"
OUTBOX_SENT = "sent"
OUTBOX_RETRY = "retry"
OUTBOX_DEAD_LETTER = "dead_letter"
OUTBOX_SUPPRESSED = "suppressed"

INCIDENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")

# ---------------------------------------------------------------- sanitization

MAX_STRING_LENGTH = 500
MAX_TEXT_LENGTH = 2000
MAX_LIST_ITEMS = 25
MAX_MAPPING_KEYS = 40
MAX_DEPTH = 4
MAX_STACK_TRACE_LINES = 40
MAX_STACK_TRACE_CHARS = 4000
TRUNCATION_MARKER = "…[truncated]"

_SECRET_KEY_RE = re.compile(
    r"(pass|pwd|secret|token|credential|authorization|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|dsn|conn(ection)?[_-]?string|smtp|imap|cookie|session[_-]?id)",
    re.IGNORECASE,
)
_URI_CREDENTIALS_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@", re.IGNORECASE)
# `password=...`, `api_key: ...` and friends embedded in free text (log lines, tracebacks).
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:pass(?:word)?|pwd|secret|token|credential|authorization|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|dsn|conn(?:ection)?[_-]?string|cookie)\w*)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_REDACTED = "[redacted]"


def _redact_email_text(value: str) -> str:
    def _mask(match: re.Match) -> str:
        local, _, domain = match.group(0).partition("@")
        return f"{local[:1]}***@{domain}"

    return _EMAIL_RE.sub(_mask, value)


def redact_email(address: str) -> str:
    return _redact_email_text(str(address or ""))


class _Sanitizer:
    """Allow-list free-form metadata into bounded, secret-free JSON values."""

    def __init__(self) -> None:
        self.truncated = False

    def text(self, value: Any, *, limit: int = MAX_STRING_LENGTH) -> str | None:
        if value is None:
            return None
        text = str(value)
        if _PEM_RE.search(text):
            return _REDACTED
        text = _URI_CREDENTIALS_RE.sub(lambda m: m.group(0).split("://")[0] + "://" + _REDACTED + "@", text)
        text = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
        text = _redact_email_text(text)
        text = text.replace("\x00", "")
        if len(text) > limit:
            self.truncated = True
            text = text[:limit] + TRUNCATION_MARKER
        return text

    def scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        return self.text(value)

    def value(self, value: Any, *, depth: int = 0) -> Any:
        if isinstance(value, Mapping):
            if depth >= MAX_DEPTH:
                self.truncated = True
                return TRUNCATION_MARKER
            out: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_MAPPING_KEYS:
                    self.truncated = True
                    out["truncated_keys"] = True
                    break
                name = str(key)[:80]
                if _SECRET_KEY_RE.search(name):
                    out[name] = _REDACTED
                    continue
                out[name] = self.value(item, depth=depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            if depth >= MAX_DEPTH:
                self.truncated = True
                return TRUNCATION_MARKER
            items = list(value)
            if isinstance(value, (set, frozenset)):
                items = sorted(items, key=lambda item: str(item))
            out_list = [self.value(item, depth=depth + 1) for item in items[:MAX_LIST_ITEMS]]
            if len(items) > MAX_LIST_ITEMS:
                self.truncated = True
                out_list.append(f"{TRUNCATION_MARKER} {len(items) - MAX_LIST_ITEMS} more")
            return out_list
        return self.scalar(value)

    def mapping(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            return {"value": self.value(value)}
        result = self.value(value)
        return result if isinstance(result, dict) else {}

    def stack_trace(self, value: Any) -> str | None:
        if value is None:
            return None
        lines = str(value).splitlines()
        if len(lines) > MAX_STACK_TRACE_LINES:
            self.truncated = True
            lines = lines[-MAX_STACK_TRACE_LINES:]
        cleaned = [self.text(line, limit=MAX_STRING_LENGTH) or "" for line in lines]
        text = "\n".join(cleaned)
        if len(text) > MAX_STACK_TRACE_CHARS:
            self.truncated = True
            text = text[-MAX_STACK_TRACE_CHARS:]
        return text


# ------------------------------------------------------------------- the event


class SuspectedBugContractError(ValueError):
    """The reported event does not satisfy the suspected_bug contract."""


@dataclass(frozen=True)
class SuspectedBugEvent:
    """Strongly typed suspected_bug event.

    Only `incident_code`, `title`, `summary`, `occurred_at`, `environment` and
    `component` are required. Every other field is optional provenance: missing
    values are represented explicitly as `null` and never make reporting fail.
    """

    incident_code: str
    title: str
    summary: str
    occurred_at: datetime
    environment: str
    component: str
    severity: str = DEFAULT_SEVERITY
    classification: str = CLASSIFICATION

    workflow_name: str | None = None
    stage_name: str | None = None
    job_name: str | None = None
    client_id: str | None = None
    client_code: str | None = None
    run_id: str | None = None
    report_type: str | None = None
    dataset_name: str | None = None

    database_name: str | None = None
    schema_name: str | None = None
    table_name: str | None = None
    raw_file_id: str | None = None
    file_id: str | None = None
    raw_artifact_id: str | None = None
    normalized_artifact_id: str | None = None
    cleaned_artifact_id: str | None = None
    stage3_result_artifact_id: str | None = None

    subject_type: str | None = None
    subject_key: str | None = None
    subject_value: str | None = None

    affected_record_count: int | None = None
    affected_period_start: datetime | None = None
    affected_period_end: datetime | None = None
    processing_outcome: str | None = None
    rows_modified: int | None = None

    details: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    suggested_action: str | None = None
    fingerprint_fields: Mapping[str, Any] = field(default_factory=dict)
    exception_type: str | None = None
    stack_trace: str | None = None

    # -------------------------------------------------------------- validation
    def validate(self) -> None:
        code = (self.incident_code or "").strip()
        if not INCIDENT_CODE_RE.fullmatch(code):
            raise SuspectedBugContractError(
                "incident_code must be an UPPER_SNAKE_CASE code of 3-80 characters"
            )
        for name in ("title", "summary", "environment", "component"):
            if not str(getattr(self, name) or "").strip():
                raise SuspectedBugContractError(f"{name} is required")
        if self.classification != CLASSIFICATION:
            raise SuspectedBugContractError(f"classification must be exactly {CLASSIFICATION!r}")
        if self.severity not in SEVERITIES:
            raise SuspectedBugContractError(f"severity must be one of {SEVERITIES}")
        if not isinstance(self.occurred_at, datetime):
            raise SuspectedBugContractError("occurred_at must be a datetime")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise SuspectedBugContractError("occurred_at must be timezone-aware")
        for name in ("affected_period_start", "affected_period_end"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise SuspectedBugContractError(f"{name} must be timezone-aware")
        if self.affected_record_count is not None and int(self.affected_record_count) < 0:
            raise SuspectedBugContractError("affected_record_count must not be negative")

    # ------------------------------------------------------------ sanitization
    def sanitized_payload(self) -> dict[str, Any]:
        """Bounded, secret-free JSON payload. All contract keys are always present."""
        self.validate()
        s = _Sanitizer()
        payload: dict[str, Any] = {
            "classification": CLASSIFICATION,
            "incident_code": self.incident_code.strip(),
            "title": s.text(self.title, limit=MAX_STRING_LENGTH),
            "summary": s.text(self.summary, limit=MAX_TEXT_LENGTH),
            "occurred_at": self.occurred_at.astimezone(timezone.utc).isoformat(),
            "environment": s.text(self.environment),
            "severity": self.severity,
            "component": s.text(self.component),
            "workflow_name": s.text(self.workflow_name),
            "stage_name": s.text(self.stage_name),
            "job_name": s.text(self.job_name),
            "client_id": s.text(self.client_id),
            "client_code": s.text(self.client_code),
            "run_id": s.text(self.run_id),
            "report_type": s.text(self.report_type),
            "dataset_name": s.text(self.dataset_name),
            "database_name": s.text(self.database_name),
            "schema_name": s.text(self.schema_name),
            "table_name": s.text(self.table_name),
            "raw_file_id": s.text(self.raw_file_id),
            "file_id": s.text(self.file_id),
            "raw_artifact_id": s.text(self.raw_artifact_id),
            "normalized_artifact_id": s.text(self.normalized_artifact_id),
            "cleaned_artifact_id": s.text(self.cleaned_artifact_id),
            "stage3_result_artifact_id": s.text(self.stage3_result_artifact_id),
            "subject_type": s.text(self.subject_type),
            "subject_key": s.text(self.subject_key),
            "subject_value": s.text(self.subject_value),
            "affected_record_count": self.affected_record_count,
            "affected_period_start": _iso(self.affected_period_start),
            "affected_period_end": _iso(self.affected_period_end),
            "processing_outcome": s.text(self.processing_outcome),
            "rows_modified": self.rows_modified,
            "details": s.mapping(self.details),
            "evidence": s.mapping(self.evidence),
            "suggested_action": s.text(self.suggested_action, limit=MAX_TEXT_LENGTH),
            "fingerprint_fields": s.mapping(self.fingerprint_fields),
            "exception_type": s.text(self.exception_type, limit=200),
            "stack_trace": s.stack_trace(self.stack_trace),
        }
        payload["truncated"] = bool(s.truncated)
        payload["fingerprint"] = self.fingerprint()
        return payload

    # -------------------------------------------------------------- fingerprint
    def fingerprint_identity(self) -> dict[str, Any]:
        """Identity of the logical cause. Excludes run/raw-file/artifact ids,
        timestamps, counts and affected record ids: those change per occurrence."""
        return {
            "classification": CLASSIFICATION,
            "environment": _identity_text(self.environment),
            "component": _identity_text(self.component),
            "incident_code": _identity_text(self.incident_code),
            "client": _identity_text(self.client_code) or _identity_text(self.client_id),
            "report_type": _identity_text(self.report_type),
            "dataset_name": _identity_text(self.dataset_name),
            "database_name": _identity_text(self.database_name),
            "schema_name": _identity_text(self.schema_name),
            "table_name": _identity_text(self.table_name),
            "subject_type": _identity_text(self.subject_type),
            "subject_key": _identity_text(self.subject_key),
            "subject_value": normalize_subject_value(self.subject_value),
            "fingerprint_fields": _identity_value(self.fingerprint_fields),
        }

    def fingerprint(self) -> str:
        return _sha256_json(self.fingerprint_identity())

    def material_signature(self, *, scope_growth_factor: int = 2) -> str:
        """Evidence that re-alerts inside one fingerprint (bounded scope growth)."""
        return _sha256_json(
            {
                "incident_code": _identity_text(self.incident_code),
                "table_name": _identity_text(self.table_name),
                "subject_value": normalize_subject_value(self.subject_value),
                "identity": _identity_value(self.fingerprint_fields),
                "scope_bucket": scope_bucket(self.affected_record_count, scope_growth_factor),
            }
        )

    # -------------------------------------------------------------- transport
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec in dataclass_fields(self):
            value = getattr(self, spec.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, Mapping):
                value = dict(value)
            out[spec.name] = value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SuspectedBugEvent":
        if not isinstance(data, Mapping):
            raise SuspectedBugContractError("event must be an object")
        known = {spec.name for spec in dataclass_fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise SuspectedBugContractError(f"unsupported suspected_bug event fields: {unknown}")
        values = dict(data)
        for name in ("occurred_at", "affected_period_start", "affected_period_end"):
            if values.get(name) is not None and not isinstance(values[name], datetime):
                values[name] = _parse_aware(str(values[name]), name)
        for name in ("details", "evidence", "fingerprint_fields"):
            if values.get(name) is None:
                values.pop(name, None)
        try:
            return cls(**values)
        except TypeError as exc:
            raise SuspectedBugContractError(f"invalid suspected_bug event: {exc}") from exc


def normalize_subject_value(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value).strip()).upper()
    return text or None


def scope_bucket(count: Any, factor: int = 2) -> int:
    try:
        value = int(count)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    base = max(2, int(factor or 2))
    return int(math.floor(math.log(value, base)))


def _identity_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    return text or None


def _identity_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _identity_value(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted((_identity_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, bool):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _identity_text(value)


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else None


def _parse_aware(text: str, name: str) -> datetime:
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SuspectedBugContractError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SuspectedBugContractError(f"{name} must be timezone-aware")
    return parsed


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class SuspectedBugAlertConfig:
    enabled: bool = True
    recipients: tuple[str, ...] = ()
    recipient_config_ref: str = "SUSPECTED_BUG_ALERT_TO"
    from_addr: str | None = None
    cooldown: timedelta = timedelta(minutes=120)
    reminder_interval: timedelta | None = timedelta(hours=24)
    max_attempts: int = 6
    initial_retry_delay: timedelta = timedelta(seconds=60)
    max_retry_delay: timedelta = timedelta(seconds=3600)
    worker_batch_size: int = 10
    stale_claim_timeout: timedelta = timedelta(seconds=900)
    scope_growth_factor: int = 2
    environment: str = "unknown"
    portal_base_url: str | None = None

    @property
    def recipients_configured(self) -> bool:
        return bool(self.recipients)

    @property
    def recipient_fingerprint(self) -> str:
        return _sha256_json(sorted(address.lower() for address in self.recipients))

    def redacted_recipients(self) -> list[str]:
        return [redact_email(address) for address in self.recipients]

    def retry_delay(self, attempt: int) -> timedelta:
        """Bounded exponential backoff for attempt number `attempt` (1-based)."""
        exponent = max(0, int(attempt) - 1)
        seconds = self.initial_retry_delay.total_seconds() * (2 ** min(exponent, 16))
        return timedelta(seconds=min(seconds, self.max_retry_delay.total_seconds()))


def parse_recipients(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = [item.strip() for item in str(value).replace(";", ",").split(",")]
    return tuple(item for item in parts if item and _EMAIL_RE.fullmatch(item))


def load_alert_config(env: Mapping[str, str] | None = None) -> SuspectedBugAlertConfig:
    values = env if env is not None else os.environ
    return SuspectedBugAlertConfig(
        enabled=_env_bool(values, "SUSPECTED_BUG_ALERTS_ENABLED", True),
        recipients=parse_recipients(values.get("SUSPECTED_BUG_ALERT_TO")),
        from_addr=(values.get("SUSPECTED_BUG_ALERT_FROM") or "").strip() or None,
        cooldown=timedelta(minutes=_env_number(values, "SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES", 120, minimum=0)),
        reminder_interval=_optional_interval(values, "SUSPECTED_BUG_ALERT_REMINDER_HOURS", 24),
        max_attempts=int(_env_number(values, "SUSPECTED_BUG_EMAIL_MAX_ATTEMPTS", 6, minimum=1)),
        initial_retry_delay=timedelta(seconds=_env_number(values, "SUSPECTED_BUG_EMAIL_INITIAL_RETRY_SECONDS", 60, minimum=1)),
        max_retry_delay=timedelta(seconds=_env_number(values, "SUSPECTED_BUG_EMAIL_MAX_RETRY_SECONDS", 3600, minimum=1)),
        worker_batch_size=int(_env_number(values, "SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE", 10, minimum=1)),
        stale_claim_timeout=timedelta(seconds=_env_number(values, "SUSPECTED_BUG_EMAIL_STALE_CLAIM_SECONDS", 900, minimum=30)),
        scope_growth_factor=int(_env_number(values, "SUSPECTED_BUG_ALERT_SCOPE_GROWTH_FACTOR", 2, minimum=2)),
        environment=(values.get("LOG_PLATFORM_TARGET_ENVIRONMENT") or "unknown").strip() or "unknown",
        portal_base_url=(values.get("ARTIFACT_EXPLORER_BASE_URL") or "").strip() or None,
    )


def _env_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_number(values: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = values.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = float(str(raw).strip())
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _optional_interval(values: Mapping[str, str], name: str, default_hours: float) -> timedelta | None:
    hours = _env_number(values, name, default_hours, minimum=0)
    return timedelta(hours=hours) if hours > 0 else None


# ------------------------------------------------------------------- reporting


@dataclass(frozen=True)
class SuspectedBugReportResult:
    fingerprint: str
    reported: bool = False
    log_id: int | None = None
    incident_id: str | None = None
    occurrence_id: str | None = None
    occurrence_count: int = 0
    incident_created: bool = False
    email_enqueued: bool = False
    email_suppressed: bool = False
    suppression_reason: str | None = None
    notification_reason: str | None = None
    outbox_id: str | None = None
    error: str | None = None

    @property
    def delivery_not_configured(self) -> bool:
        """This incident was suppressed by a configuration defect, not a throttle.

        Derived, not stored: `suspected_bug_occurrences.email_decision_reason`
        already records the reason, so no schema change is needed to ask the
        question — only a name for it.
        """
        return self.email_suppressed and is_configuration_suppression(self.suppression_reason)

    def to_dict(self) -> dict[str, Any]:
        value = {spec.name: getattr(self, spec.name) for spec in dataclass_fields(self)}
        value["delivery_not_configured"] = self.delivery_not_configured
        return value


@dataclass(frozen=True)
class _EmailDecision:
    enqueue: bool
    reason: str | None = None
    suppression_reason: str | None = None


def _decide_email(
    *,
    config: SuspectedBugAlertConfig,
    incident_created: bool,
    previous_state: str | None,
    previous_material_signature: str | None,
    previous_email_enqueued_at: datetime | None,
    material_signature: str,
    now: datetime,
) -> _EmailDecision:
    if not config.enabled:
        return _EmailDecision(False, suppression_reason=SUPPRESSED_ALERTS_DISABLED)
    if not config.recipients_configured:
        return _EmailDecision(False, suppression_reason=SUPPRESSED_RECIPIENTS_NOT_CONFIGURED)
    if incident_created or previous_email_enqueued_at is None:
        return _EmailDecision(True, reason=REASON_NEW)
    if previous_state == STATE_RESOLVED:
        return _EmailDecision(True, reason=REASON_MATERIAL_CHANGE)
    if previous_material_signature != material_signature:
        return _EmailDecision(True, reason=REASON_MATERIAL_CHANGE)
    elapsed = now - previous_email_enqueued_at
    if elapsed < config.cooldown:
        return _EmailDecision(False, suppression_reason=SUPPRESSED_COOLDOWN)
    if config.reminder_interval is not None and elapsed >= config.reminder_interval:
        return _EmailDecision(True, reason=REASON_REMINDER)
    return _EmailDecision(False, suppression_reason=SUPPRESSED_COOLDOWN)


def _notification_key(*, fingerprint: str, decision: _EmailDecision, material_signature: str,
                      previous_email_enqueued_at: datetime | None) -> str:
    if decision.reason == REASON_NEW:
        return f"{fingerprint}:new"
    cycle = int(previous_email_enqueued_at.timestamp()) if previous_email_enqueued_at else 0
    if decision.reason == REASON_MATERIAL_CHANGE:
        return f"{fingerprint}:material:{material_signature}:{cycle}"
    return f"{fingerprint}:reminder:{cycle}"


def report_suspected_bug(
    conn,
    event: SuspectedBugEvent,
    *,
    config: SuspectedBugAlertConfig | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> SuspectedBugReportResult:
    """Atomically persist log + incident + occurrence + optional outbox row.

    `conn` is the transaction context: everything below happens inside the caller's
    transaction on the platform database. Raises on failure — callers that must not
    be disturbed by reporting problems should use `safe_report_suspected_bug`.
    """
    config = config or load_alert_config()
    event.validate()
    payload = event.sanitized_payload()
    fingerprint = payload["fingerprint"]
    material_signature = event.material_signature(scope_growth_factor=config.scope_growth_factor)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise SuspectedBugContractError("now must be timezone-aware")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO suspected_bug_incidents (
              fingerprint, classification, incident_code, title, severity, state,
              environment, component, client_id, client_code,
              first_seen_at, last_seen_at, occurrence_count, material_signature
            )
            VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s::uuid, %s, %s, %s, 0, %s)
            ON CONFLICT (fingerprint) DO NOTHING
            RETURNING incident_id
            """,
            (
                fingerprint, CLASSIFICATION, payload["incident_code"], payload["title"],
                event.severity, payload["environment"], payload["component"],
                _optional_uuid(event.client_id), payload["client_code"],
                event.occurred_at, event.occurred_at, material_signature,
            ),
        )
        created_row = cur.fetchone()
        incident_created = created_row is not None

        cur.execute(
            """
            SELECT incident_id, state, occurrence_count, material_signature,
                   last_email_enqueued_at, first_seen_at
            FROM suspected_bug_incidents
            WHERE fingerprint = %s
            FOR UPDATE
            """,
            (fingerprint,),
        )
        incident = dict(cur.fetchone() or {})
        if not incident:
            raise RuntimeError("suspected_bug incident row disappeared during reporting")

        incident_id = str(incident["incident_id"])
        previous_state = str(incident.get("state") or STATE_OPEN)
        previous_material_signature = incident.get("material_signature")
        previous_email_enqueued_at = incident.get("last_email_enqueued_at")
        occurrence_no = int(incident.get("occurrence_count") or 0) + 1

        decision = _decide_email(
            config=config,
            incident_created=incident_created,
            previous_state=previous_state,
            previous_material_signature=previous_material_signature,
            previous_email_enqueued_at=previous_email_enqueued_at,
            material_signature=material_signature,
            now=now,
        )

        run_id = _existing_run_id(cur, event.run_id)
        log_id = _insert_log(
            cur,
            event=event,
            payload=payload,
            incident_id=incident_id,
            occurrence_no=occurrence_no,
            run_id=run_id,
        )

        cur.execute(
            """
            UPDATE suspected_bug_incidents
            SET last_seen_at = GREATEST(last_seen_at, %s),
                first_seen_at = LEAST(first_seen_at, %s),
                occurrence_count = occurrence_count + 1,
                latest_log_id = %s,
                latest_run_id = %s::uuid,
                latest_payload = %s::jsonb,
                material_signature = %s,
                title = %s,
                severity = %s,
                state = 'open',
                resolved_at = NULL,
                updated_at = now()
            WHERE incident_id = %s::uuid
            """,
            (
                event.occurred_at, event.occurred_at, log_id, run_id,
                json.dumps(payload, ensure_ascii=False, default=str),
                material_signature, payload["title"], event.severity, incident_id,
            ),
        )

        cur.execute(
            """
            INSERT INTO suspected_bug_occurrences (
              incident_id, occurrence_no, occurred_at, log_id, run_id, component,
              payload, email_decision, email_decision_reason
            )
            VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s, %s::jsonb, %s, %s)
            RETURNING occurrence_id
            """,
            (
                incident_id, occurrence_no, event.occurred_at, log_id, run_id,
                payload["component"], json.dumps(payload, ensure_ascii=False, default=str),
                "enqueued" if decision.enqueue else "suppressed",
                decision.reason or decision.suppression_reason,
            ),
        )
        occurrence_id = str(cur.fetchone()["occurrence_id"])

        outbox_id = None
        suppression_reason = decision.suppression_reason
        if decision.enqueue:
            outbox_id = _enqueue_email(
                cur,
                config=config,
                event=event,
                payload=payload,
                incident_id=incident_id,
                occurrence_id=occurrence_id,
                occurrence_no=occurrence_no,
                first_seen_at=min(incident.get("first_seen_at") or event.occurred_at, event.occurred_at),
                decision=decision,
                material_signature=material_signature,
                previous_email_enqueued_at=previous_email_enqueued_at,
                now=now,
            )
            if outbox_id is None:
                suppression_reason = SUPPRESSED_DUPLICATE_NOTIFICATION
                cur.execute(
                    """
                    UPDATE suspected_bug_occurrences
                    SET email_decision = 'suppressed', email_decision_reason = %s
                    WHERE occurrence_id = %s::uuid
                    """,
                    (SUPPRESSED_DUPLICATE_NOTIFICATION, occurrence_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE suspected_bug_incidents
                    SET last_email_enqueued_at = %s, updated_at = now()
                    WHERE incident_id = %s::uuid
                    """,
                    (now, incident_id),
                )

    if commit:
        conn.commit()

    enqueued = decision.enqueue and outbox_id is not None
    return SuspectedBugReportResult(
        fingerprint=fingerprint,
        reported=True,
        log_id=log_id,
        incident_id=incident_id,
        occurrence_id=occurrence_id,
        occurrence_count=occurrence_no,
        incident_created=incident_created,
        email_enqueued=enqueued,
        email_suppressed=not enqueued,
        suppression_reason=None if enqueued else suppression_reason,
        notification_reason=decision.reason if enqueued else None,
        outbox_id=outbox_id,
    )


def _optional_uuid(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def _existing_run_id(cur, run_id: Any) -> str | None:
    """`logs.run_id` and the occurrence FK reference `runs`. An unknown run id must
    not abort the reporting transaction, so it is dropped to NULL and kept in the payload."""
    normalized = _optional_uuid(run_id)
    if normalized is None:
        return None
    cur.execute("SELECT 1 AS present FROM runs WHERE run_id = %s::uuid", (normalized,))
    return normalized if cur.fetchone() else None


def _insert_log(cur, *, event: SuspectedBugEvent, payload: Mapping[str, Any],
                incident_id: str, occurrence_no: int, run_id: str | None) -> int:
    context: dict[str, Any] = {
        "classification": CLASSIFICATION,
        "incident_code": payload["incident_code"],
        "incident_id": incident_id,
        "fingerprint": payload["fingerprint"],
        "occurrence_no": occurrence_no,
        "severity": event.severity,
        "environment": payload["environment"],
        "component": payload["component"],
        "workflow_name": payload["workflow_name"],
        "stage_name": payload["stage_name"],
        "job_name": payload["job_name"],
        "client_id": payload["client_id"],
        "client_code": payload["client_code"],
        "reported_run_id": payload["run_id"],
        "run_id_linked": run_id is not None,
        "report_type": payload["report_type"],
        "dataset_name": payload["dataset_name"],
        "database_name": payload["database_name"],
        "schema_name": payload["schema_name"],
        "table_name": payload["table_name"],
        "raw_file_id": payload["raw_file_id"],
        "file_id": payload["file_id"],
        "raw_artifact_id": payload["raw_artifact_id"],
        "normalized_artifact_id": payload["normalized_artifact_id"],
        "cleaned_artifact_id": payload["cleaned_artifact_id"],
        "stage3_result_artifact_id": payload["stage3_result_artifact_id"],
        "subject_type": payload["subject_type"],
        "subject_key": payload["subject_key"],
        "subject_value": payload["subject_value"],
        "affected_record_count": payload["affected_record_count"],
        "rows_modified": payload["rows_modified"],
        "processing_outcome": payload["processing_outcome"],
        "suggested_action": payload["suggested_action"],
        "exception_type": payload["exception_type"],
        "truncated": payload["truncated"],
        "details": payload["details"],
        "evidence": payload["evidence"],
    }
    for key, value in business_time_log_context(event.occurred_at).items():
        context.setdefault(key, value)
    cur.execute(
        """
        INSERT INTO logs (ts, level, type, source, run_id, message, context, error)
        VALUES (%s, 'ERROR', 'SCRIPT', %s, %s::uuid, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            event.occurred_at, payload["component"], run_id,
            f"[{CLASSIFICATION}] {payload['incident_code']}: {payload['title']}",
            json.dumps(context, ensure_ascii=False, default=str),
            payload["stack_trace"],
        ),
    )
    return int(cur.fetchone()["id"])


def _enqueue_email(cur, *, config: SuspectedBugAlertConfig, event: SuspectedBugEvent,
                   payload: Mapping[str, Any], incident_id: str, occurrence_id: str,
                   occurrence_no: int, first_seen_at: datetime, decision: _EmailDecision,
                   material_signature: str, previous_email_enqueued_at: datetime | None,
                   now: datetime) -> str | None:
    subject, body_text, body_html = render_incident_email(
        payload=payload,
        config=config,
        incident_id=incident_id,
        fingerprint=payload["fingerprint"],
        state=STATE_OPEN,
        first_seen_at=first_seen_at,
        last_seen_at=event.occurred_at,
        occurrence_count=occurrence_no,
        notification_reason=decision.reason or REASON_NEW,
    )
    notification_key = _notification_key(
        fingerprint=payload["fingerprint"],
        decision=decision,
        material_signature=material_signature,
        previous_email_enqueued_at=previous_email_enqueued_at,
    )
    cur.execute(
        """
        INSERT INTO suspected_bug_email_outbox (
          incident_id, occurrence_id, notification_key, notification_reason,
          recipient_config_ref, recipient_fingerprint, recipients,
          subject, body_text, body_html, status, attempts, max_attempts, available_at
        )
        VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 'pending', 0, %s, %s)
        ON CONFLICT (notification_key) DO NOTHING
        RETURNING outbox_id
        """,
        (
            incident_id, occurrence_id, notification_key, decision.reason,
            config.recipient_config_ref, config.recipient_fingerprint,
            json.dumps(list(config.recipients)), subject, body_text, body_html,
            config.max_attempts, now,
        ),
    )
    row = cur.fetchone()
    return str(row["outbox_id"]) if row else None


# --------------------------------------------------------- safe entry points

_reentrancy = threading.local()


def platform_db_conn():
    """Connection to the platform database, built like `api.main.db_conn`."""
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return psycopg.connect(dsn, row_factory=dict_row)


def safe_report_suspected_bug(
    event: SuspectedBugEvent,
    *,
    conn=None,
    config: SuspectedBugAlertConfig | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> SuspectedBugReportResult:
    """Report without ever disturbing the caller.

    Never raises, never replaces the original business exception, and refuses to
    recurse: a failure inside reporting is written to stderr as a plain
    operational error, not as another suspected_bug.
    """
    fingerprint = ""
    if getattr(_reentrancy, "active", False):
        _fallback_error("suspected_bug_report_reentrancy_refused", incident_code=str(event.incident_code))
        return SuspectedBugReportResult(fingerprint="", error="reentrancy_refused")

    _reentrancy.active = True
    owns_conn = conn is None
    try:
        fingerprint = event.fingerprint()
        if owns_conn:
            conn = platform_db_conn()
        try:
            return report_suspected_bug(conn, event, config=config, now=now, commit=commit)
        finally:
            if owns_conn:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as exc:
        if conn is not None and not owns_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        _fallback_error(
            "suspected_bug_report_failed",
            incident_code=str(event.incident_code),
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        return SuspectedBugReportResult(fingerprint=fingerprint, error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        _reentrancy.active = False


def _fallback_error(message: str, **fields: Any) -> None:
    """Conventional operational error path. Deliberately not a suspected_bug."""
    payload = {"level": "ERROR", "classification": "operational_error", "message": message}
    payload.update({key: value for key, value in fields.items() if value is not None})
    try:
        print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)
    except Exception:
        pass


# ------------------------------------------------------------ email rendering


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, str):
        try:
            value = _parse_aware(value, "timestamp")
        except SuspectedBugContractError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    business = value.astimezone(get_business_timezone())
    # DELIBERATELY NOT the platform display formatter (`portal_ui.formats`).
    # This is engineer-facing correlation output with both halves explicitly
    # zone-labelled: converting it would delete the UTC half, which is the whole
    # reason it exists. The platform-wide `dd.mm.yyyy gg:mm:ss` standardisation
    # covers how the product presents data in its VIEWS; this is an alert email.
    return (
        f"{business.strftime('%Y-%m-%d %H:%M:%S')} {get_business_timezone_name()}"
        f" / {value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


def _display(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "not available"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _artifact_link(config: SuspectedBugAlertConfig, artifact_id: Any) -> str | None:
    if not config.portal_base_url or not artifact_id:
        return None
    return f"{config.portal_base_url.rstrip('/')}/artifact-explorer/artifacts/{artifact_id}"


def _dedupe_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the first occurrence of each label so overlapping identity/detail keys
    (the same conflict values in `fingerprint_fields` and `details`) render once."""
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, value in rows:
        if label in seen:
            continue
        seen.add(label)
        unique.append((label, value))
    return unique


def _evidence_lines(mapping: Any) -> list[tuple[str, str]]:
    if not isinstance(mapping, Mapping) or not mapping:
        return []
    lines: list[tuple[str, str]] = []
    for key, value in mapping.items():
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(item) for item in value) or "not available"
        elif isinstance(value, Mapping):
            rendered = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        else:
            rendered = _display(value)
        lines.append((str(key), rendered))
    return lines


def render_incident_email(
    *,
    payload: Mapping[str, Any],
    config: SuspectedBugAlertConfig,
    incident_id: str,
    fingerprint: str,
    state: str,
    first_seen_at: Any,
    last_seen_at: Any,
    occurrence_count: int,
    notification_reason: str,
) -> tuple[str, str, str]:
    """Operational alert email. Text and HTML carry the same identifiers."""
    client = payload.get("client_code") or payload.get("client_id") or "platform"
    title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:120]
    subject = (
        f"[SUSPECTED_BUG][{payload.get('environment')}][{client}]"
        f"[{payload.get('incident_code')}] {title}"
    )

    sections: list[tuple[str, list[tuple[str, str]]]] = [
        ("Incident", [
            ("Incident code", _display(payload.get("incident_code"))),
            ("Incident id", _display(incident_id)),
            ("Classification", CLASSIFICATION),
            ("Severity", _display(payload.get("severity"))),
            ("State", _display(state)),
            ("Notification reason", _display(notification_reason)),
            ("First seen", _format_timestamp(first_seen_at)),
            ("This occurrence", _format_timestamp(payload.get("occurred_at"))),
            ("Last seen", _format_timestamp(last_seen_at)),
            ("Occurrence count", _display(occurrence_count)),
            ("Fingerprint", _display(fingerprint)),
        ]),
        ("Runtime context", [
            ("Environment", _display(payload.get("environment"))),
            ("Component", _display(payload.get("component"))),
            ("Workflow", _display(payload.get("workflow_name"))),
            ("Stage", _display(payload.get("stage_name"))),
            ("Job", _display(payload.get("job_name"))),
            ("Client", _display(payload.get("client_code"))),
            ("Client id", _display(payload.get("client_id"))),
            ("Run id", _display(payload.get("run_id"))),
            ("Report type", _display(payload.get("report_type"))),
            ("Dataset", _display(payload.get("dataset_name"))),
        ]),
        ("Data provenance", [
            ("Database", _display(payload.get("database_name"))),
            ("Schema", _display(payload.get("schema_name"))),
            ("Table", _display(payload.get("table_name"))),
            ("Raw file id", _display(payload.get("raw_file_id"))),
            ("File / artifact id", _display(payload.get("file_id"))),
            ("Raw artifact id", _display(payload.get("raw_artifact_id"))),
            ("Normalized artifact id", _display(payload.get("normalized_artifact_id"))),
            ("Cleaned artifact id", _display(payload.get("cleaned_artifact_id"))),
            ("Stage 3 result artifact id", _display(payload.get("stage3_result_artifact_id"))),
        ]),
        ("Affected information", _dedupe_rows([
            ("Subject type", _display(payload.get("subject_type"))),
            ("Subject key", _display(payload.get("subject_key"))),
            ("Subject value", _display(payload.get("subject_value"))),
            ("Affected record count", _display(payload.get("affected_record_count"))),
            ("Affected period start", _format_timestamp(payload.get("affected_period_start"))),
            ("Affected period end", _format_timestamp(payload.get("affected_period_end"))),
        ] + _evidence_lines(payload.get("fingerprint_fields")) + _evidence_lines(payload.get("details")))),
        ("Suggested diagnosis", [
            ("Summary", _display(payload.get("summary"))),
            ("Recommended first inspection", _display(payload.get("suggested_action"))),
            ("Processing outcome", _display(payload.get("processing_outcome"))),
            ("Rows modified", _display(payload.get("rows_modified"))),
            ("Exception type", _display(payload.get("exception_type"))),
            ("Payload truncated", _display(payload.get("truncated"))),
        ]),
    ]

    evidence = _evidence_lines(payload.get("evidence"))
    if evidence:
        sections.append(("Evidence", evidence))

    links: list[tuple[str, str]] = []
    for label, artifact_id in (
        ("Raw artifact", payload.get("raw_artifact_id")),
        ("Normalized artifact", payload.get("normalized_artifact_id")),
        ("Cleaned artifact", payload.get("cleaned_artifact_id")),
        ("Stage 3 result artifact", payload.get("stage3_result_artifact_id")),
    ):
        link = _artifact_link(config, artifact_id)
        if link:
            links.append((label, link))
    if links:
        sections.append(("Links", links))

    text_parts = [
        subject,
        "",
        "This is an automated suspected_bug alert. It reports a likely defect, "
        "data-integrity conflict or violated invariant that needs developer review.",
        "The originating run keeps its own status; suspected_bug is an error "
        "classification, not a run status.",
    ]
    html_parts = [
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:13px;\">",
        f"<h2>{escape(subject)}</h2>",
        "<p>This is an automated <strong>suspected_bug</strong> alert. It reports a likely defect, "
        "data-integrity conflict or violated invariant that needs developer review. "
        "The originating run keeps its own status; <strong>suspected_bug</strong> is an error "
        "classification, not a run status.</p>",
    ]
    for heading, rows in sections:
        text_parts.extend(["", f"== {heading} =="])
        html_parts.append(f"<h3>{escape(heading)}</h3>")
        html_parts.append('<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;">')
        for label, value in rows:
            text_parts.append(f"{label}: {value}")
            html_parts.append(
                f"<tr><td><strong>{escape(str(label))}</strong></td><td>{escape(str(value))}</td></tr>"
            )
        html_parts.append("</table>")
    html_parts.append("</body></html>")

    return subject, "\n".join(text_parts), "".join(html_parts)
