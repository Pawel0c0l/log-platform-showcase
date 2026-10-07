"""Fail-closed execution and reporting-period policy for Eco Driving email jobs.

Eco Driving aggregation stores half-open business-time ranges.  Both weekly
and monthly ``period_end_date`` values are *exclusive* boundaries at 00:00 in
Europe/Warsaw.  Weekly rows are cumulative month-to-date snapshots whose
valid boundaries are the first Monday after month start, subsequent Mondays,
and the first day of the next month (the final partial segment).  Monthly rows
are exactly ``[month_start, next_month_start)``.

Consequently a period ending on a given local date becomes send-eligible at
the first instant of that date, not at the end of that date.  This remains
correct across leap years and Warsaw DST changes because boundary instants are
constructed in the business timezone rather than the database session zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE_NAME = "Europe/Warsaw"
BUSINESS_TIMEZONE = ZoneInfo(BUSINESS_TIMEZONE_NAME)

PERIOD_NOT_CLOSED = "PERIOD_NOT_CLOSED"
PERIOD_END_IN_FUTURE = "PERIOD_END_IN_FUTURE"
INVALID_PERIOD_BOUNDARY = "INVALID_PERIOD_BOUNDARY"
NO_ELIGIBLE_CLOSED_PERIOD = "NO_ELIGIBLE_CLOSED_PERIOD"
SNAPSHOT_NOT_FINALIZED = "SNAPSHOT_NOT_FINALIZED"
EXECUTION_MODE_REQUIRED = "EXECUTION_MODE_REQUIRED"
INVALID_EXECUTION_MODE = "INVALID_EXECUTION_MODE"
TEST_RECIPIENT_REQUIRED = "TEST_RECIPIENT_REQUIRED"
NORMAL_RECIPIENT_SCOPE_REQUIRED = "NORMAL_RECIPIENT_SCOPE_REQUIRED"
FORCE_RESEND_REASON_REQUIRED = "FORCE_RESEND_REASON_REQUIRED"
UNCLOSED_OVERRIDE_NOT_ALLOWED = "UNCLOSED_OVERRIDE_NOT_ALLOWED"


class EcoEmailPreconditionError(RuntimeError):
    """Typed, machine-readable sender precondition failure."""

    def __init__(self, code: str, message: str, diagnostics: Mapping[str, Any] | None = None):
        self.code = code
        self.diagnostics = dict(diagnostics or {})
        super().__init__(f"{code}: {message}")


class ExecutionMode(str, Enum):
    RENDER_ONLY = "render_only"
    TEST_SEND = "test_send"
    NORMAL_SEND = "normal_send"
    FORCE_RESEND = "force_resend"


@dataclass(frozen=True)
class ExecutionContract:
    mode: ExecutionMode
    test_recipient_email: str | None
    force_resend_reason: str | None
    allow_unclosed_period_for_test: bool

    @property
    def sends_email(self) -> bool:
        return self.mode is not ExecutionMode.RENDER_ONLY

    @property
    def is_real_recipient_scope(self) -> bool:
        return self.mode in {ExecutionMode.NORMAL_SEND, ExecutionMode.FORCE_RESEND}

    @property
    def force_resend(self) -> bool:
        return self.mode is ExecutionMode.FORCE_RESEND

    @property
    def blocks_on_unresolved_ambiguous_send(self) -> bool:
        """Must this run check the send ledger BEFORE it causes remote effects?

        THE ANSWER IS THE SAME FOR BOTH PRODUCTION MODES, AND THAT IS THE POINT.

        `force_resend` exists to override an established `sent` — a fact the
        ledger holds. An unresolved ambiguous submission is the ABSENCE of a
        fact: example.invalid may already have accepted that message, and the driver may
        already be holding the e-mail with the capability link it carried.
        Forcing on the strength of an absence is the duplicate this contract
        exists to prevent, so a forced run is gated exactly as a normal one is:
        no snapshot publication, no capability mint or ROTATION, no ledger
        mutation, no reservation, no SMTP. Only an explicit operator
        reconciliation clears the row.

        `test_send` is the one bounded exemption, and it is bounded by
        construction rather than by trust: `resolve_execution_contract` requires
        an explicit `test_recipient_email` for that mode, forbids one for the
        two production modes, and `send_scope` is `test`, so a test send cannot
        reach the driver's production recipient and cannot reuse the normal or
        forced delivery scope. `render_only` sends nothing at all.

        The `send_scope` term is redundant for the two production modes by
        construction and is kept as a second, independent statement of the same
        exemption: if the scope mapping above ever changed, this predicate would
        still not silently start exempting a production send.
        """
        return (self.mode in {ExecutionMode.NORMAL_SEND, ExecutionMode.FORCE_RESEND}
                and self.send_scope != "test")

    @property
    def send_scope(self) -> str:
        return {
            ExecutionMode.RENDER_ONLY: "render_only",
            ExecutionMode.TEST_SEND: "test",
            ExecutionMode.NORMAL_SEND: "normal",
            ExecutionMode.FORCE_RESEND: "forced",
        }[self.mode]


@dataclass(frozen=True)
class PeriodCandidate:
    period_start_date: date
    period_end_date: date
    period_label: str | None = None
    snapshot_min_updated_at: datetime | None = None
    snapshot_max_updated_at: datetime | None = None

    def context(self) -> dict[str, Any]:
        return {
            "period_start_date": self.period_start_date.isoformat(),
            "period_end_date": self.period_end_date.isoformat(),
            "period_label": self.period_label,
            "snapshot_min_updated_at": self.snapshot_min_updated_at.isoformat() if self.snapshot_min_updated_at else None,
            "snapshot_max_updated_at": self.snapshot_max_updated_at.isoformat() if self.snapshot_max_updated_at else None,
        }


@dataclass(frozen=True)
class PeriodDecision:
    candidate: PeriodCandidate
    state: str
    rejection_code: str | None
    evaluated_at: datetime

    @property
    def eligible(self) -> bool:
        return self.state == "closed" and self.rejection_code is None

    def context(self) -> dict[str, Any]:
        return {
            **self.candidate.context(),
            "state": self.state,
            "rejection_reason": self.rejection_code,
            "evaluated_local_datetime": self.evaluated_at.isoformat(),
            "timezone": BUSINESS_TIMEZONE_NAME,
        }


@dataclass(frozen=True)
class PeriodSelection:
    selected: PeriodCandidate
    decisions: tuple[PeriodDecision, ...]
    selection_source: str
    override_used: bool = False

    def diagnostics(self) -> dict[str, Any]:
        rejected = [decision for decision in self.decisions if not decision.eligible]
        latest_rejected = max(
            rejected,
            key=lambda decision: (
                decision.candidate.period_end_date,
                decision.candidate.period_start_date,
                decision.candidate.period_label or "",
            ),
            default=None,
        )
        evaluated = self.decisions[0].evaluated_at if self.decisions else _now_local(None)
        reasons: dict[str, int] = {}
        for decision in rejected:
            reason = decision.rejection_code or INVALID_PERIOD_BOUNDARY
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "selected_period": self.selected.context(),
            "selection_source": self.selection_source,
            "candidate_count": len(self.decisions),
            "rejected_candidate_count": len(rejected),
            "rejection_reasons": reasons,
            "latest_rejected_period": latest_rejected.context() if latest_rejected else None,
            "evaluated_local_datetime": evaluated.isoformat(),
            "timezone": BUSINESS_TIMEZONE_NAME,
            "allow_unclosed_period_for_test": self.override_used,
        }


def _bool_param(params: Mapping[str, Any], name: str, default: bool = False) -> bool:
    if name not in params:
        return default
    value = params[name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise EcoEmailPreconditionError(
        INVALID_EXECUTION_MODE, f"{name} must be a boolean", {"parameter": name}
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_execution_contract(params: Mapping[str, Any]) -> ExecutionContract:
    """Resolve an explicit sender mode; omission is always render-only.

    Legacy ``dry_run=true`` remains a safe alias for the default.  Explicit
    ``dry_run=false`` without ``execution_mode`` is rejected so an old caller
    cannot silently retain the former real-send default.
    """

    raw_mode = _text(params.get("execution_mode"))
    if "send_scope" in params:
        raise EcoEmailPreconditionError(
            INVALID_EXECUTION_MODE,
            "send_scope is not an execution control; use execution_mode explicitly",
            {"send_scope": _text(params.get("send_scope"))},
        )
    if raw_mode is None and _text(params.get("mode")) in {mode.value for mode in ExecutionMode}:
        raise EcoEmailPreconditionError(
            EXECUTION_MODE_REQUIRED,
            "sender execution mode must be supplied as execution_mode; mode selects the stats period",
            {"mode": _text(params.get("mode"))},
        )
    legacy_force = _bool_param(params, "force_resend", False)
    if raw_mode is None:
        if legacy_force or ("dry_run" in params and not _bool_param(params, "dry_run")):
            raise EcoEmailPreconditionError(
                EXECUTION_MODE_REQUIRED,
                "real sending requires explicit execution_mode",
            )
        mode = ExecutionMode.RENDER_ONLY
    else:
        try:
            mode = ExecutionMode(raw_mode)
        except ValueError as exc:
            raise EcoEmailPreconditionError(
                INVALID_EXECUTION_MODE,
                "execution_mode must be render_only, test_send, normal_send, or force_resend",
                {"execution_mode": raw_mode},
            ) from exc

    if "dry_run" in params:
        legacy_dry_run = _bool_param(params, "dry_run")
        if legacy_dry_run != (mode is ExecutionMode.RENDER_ONLY):
            raise EcoEmailPreconditionError(
                INVALID_EXECUTION_MODE,
                "dry_run conflicts with execution_mode; remove dry_run from explicit-mode calls",
            )
    if legacy_force != (mode is ExecutionMode.FORCE_RESEND) and "force_resend" in params:
        raise EcoEmailPreconditionError(
            INVALID_EXECUTION_MODE,
            "force_resend conflicts with execution_mode; use execution_mode=force_resend",
        )

    test_recipient = _text(params.get("test_recipient_email"))
    force_reason = _text(params.get("force_resend_reason"))
    allow_unclosed = _bool_param(params, "allow_unclosed_period_for_test", False)

    if mode is ExecutionMode.TEST_SEND and not test_recipient:
        raise EcoEmailPreconditionError(
            TEST_RECIPIENT_REQUIRED,
            "test_send requires one explicit test_recipient_email",
        )
    if mode in {ExecutionMode.NORMAL_SEND, ExecutionMode.FORCE_RESEND} and test_recipient:
        raise EcoEmailPreconditionError(
            NORMAL_RECIPIENT_SCOPE_REQUIRED,
            "normal and force-resend modes cannot redirect to a test recipient",
        )
    if mode is ExecutionMode.FORCE_RESEND and not force_reason:
        raise EcoEmailPreconditionError(
            FORCE_RESEND_REASON_REQUIRED,
            "force_resend requires a non-empty force_resend_reason",
        )
    if mode is not ExecutionMode.FORCE_RESEND and force_reason:
        raise EcoEmailPreconditionError(
            INVALID_EXECUTION_MODE,
            "force_resend_reason is accepted only in force_resend mode",
        )
    if allow_unclosed and mode not in {ExecutionMode.RENDER_ONLY, ExecutionMode.TEST_SEND}:
        raise EcoEmailPreconditionError(
            UNCLOSED_OVERRIDE_NOT_ALLOWED,
            "allow_unclosed_period_for_test is restricted to render_only or test_send",
        )

    return ExecutionContract(mode, test_recipient, force_reason, allow_unclosed)


def _next_month_start(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def _weekly_boundaries(month_start: date) -> tuple[date, ...]:
    month_start = month_start.replace(day=1)
    month_end = _next_month_start(month_start)
    boundary = month_start
    ends: list[date] = []
    while boundary < month_end:
        next_monday = boundary + timedelta(days=8 - boundary.isoweekday())
        boundary = min(next_monday, month_end)
        ends.append(boundary)
    return tuple(ends)


def _now_local(clock: Callable[[], datetime] | datetime | None) -> datetime:
    value = clock() if callable(clock) else clock
    if value is None:
        value = datetime.now(tz=BUSINESS_TIMEZONE)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EcoEmailPreconditionError(
            INVALID_PERIOD_BOUNDARY,
            "the injected clock must return a timezone-aware datetime",
        )
    return value.astimezone(BUSINESS_TIMEZONE)


def _coerce_candidate(value: PeriodCandidate | Mapping[str, Any]) -> PeriodCandidate:
    if isinstance(value, PeriodCandidate):
        return value
    try:
        start = value.get("period_start_date") or value.get("month_start_date")
        end = value.get("period_end_date") or value.get("month_end_date")
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()
        if not isinstance(start, date):
            start = date.fromisoformat(str(start))
        if not isinstance(end, date):
            end = date.fromisoformat(str(end))
        label = _text(value.get("period_label"))
        snapshot_min = value.get("snapshot_min_updated_at")
        snapshot_max = value.get("snapshot_max_updated_at")
        for snapshot_value in (snapshot_min, snapshot_max):
            if snapshot_value is not None and (
                not isinstance(snapshot_value, datetime) or snapshot_value.tzinfo is None
            ):
                raise ValueError("snapshot timestamps must be timezone-aware")
        return PeriodCandidate(start, end, label, snapshot_min, snapshot_max)
    except Exception as exc:
        raise EcoEmailPreconditionError(
            INVALID_PERIOD_BOUNDARY,
            "period boundaries must be valid ISO dates",
        ) from exc


def evaluate_period(
    candidate: PeriodCandidate | Mapping[str, Any],
    *,
    report_type: str,
    clock: Callable[[], datetime] | datetime | None = None,
    require_finalized_snapshot: bool = False,
) -> PeriodDecision:
    now_local = _now_local(clock)
    try:
        parsed = _coerce_candidate(candidate)
    except EcoEmailPreconditionError:
        raise

    valid_shape = parsed.period_end_date > parsed.period_start_date
    if report_type == "weekly":
        month_start = parsed.period_start_date.replace(day=1)
        valid_shape = (
            valid_shape
            and parsed.period_start_date == month_start
            and parsed.period_end_date in _weekly_boundaries(month_start)
        )
    elif report_type == "monthly":
        valid_shape = (
            valid_shape
            and parsed.period_start_date.day == 1
            and parsed.period_end_date == _next_month_start(parsed.period_start_date)
        )
    else:
        valid_shape = False

    if not valid_shape:
        return PeriodDecision(parsed, "invalid", INVALID_PERIOD_BOUNDARY, now_local)

    start_boundary = datetime.combine(parsed.period_start_date, time.min, BUSINESS_TIMEZONE)
    end_boundary = datetime.combine(parsed.period_end_date, time.min, BUSINESS_TIMEZONE)
    if now_local >= end_boundary:
        if require_finalized_snapshot:
            finalized_at = parsed.snapshot_min_updated_at
            if finalized_at is None or finalized_at.astimezone(BUSINESS_TIMEZONE) < end_boundary:
                return PeriodDecision(parsed, "invalid_snapshot", SNAPSHOT_NOT_FINALIZED, now_local)
        return PeriodDecision(parsed, "closed", None, now_local)
    if now_local < start_boundary:
        return PeriodDecision(parsed, "future", PERIOD_END_IN_FUTURE, now_local)
    return PeriodDecision(parsed, "open", PERIOD_NOT_CLOSED, now_local)


def select_latest_closed_period(
    candidates: Iterable[PeriodCandidate | Mapping[str, Any]],
    *,
    report_type: str,
    clock: Callable[[], datetime] | datetime | None = None,
    require_finalized_snapshot: bool = False,
) -> PeriodSelection:
    now_local = _now_local(clock)
    decisions: list[PeriodDecision] = []
    for raw in candidates:
        try:
            decisions.append(evaluate_period(raw, report_type=report_type, clock=now_local, require_finalized_snapshot=require_finalized_snapshot))
        except EcoEmailPreconditionError:
            # A malformed candidate is retained as a deterministic invalid row
            # when its dates can be represented; otherwise fail closed because
            # its relative ordering cannot be established safely.
            raise
    eligible = [decision for decision in decisions if decision.eligible]
    if not eligible:
        diagnostics = _selection_failure_diagnostics(decisions, now_local)
        raise EcoEmailPreconditionError(
            NO_ELIGIBLE_CLOSED_PERIOD,
            "no valid closed reporting period is available",
            diagnostics,
        )
    selected = max(
        eligible,
        key=lambda decision: (
            decision.candidate.period_end_date,
            decision.candidate.period_start_date,
            decision.candidate.period_label or "",
        ),
    ).candidate
    return PeriodSelection(selected, tuple(decisions), "automatic")


def require_explicit_period(
    candidate: PeriodCandidate | Mapping[str, Any],
    *,
    report_type: str,
    contract: ExecutionContract,
    clock: Callable[[], datetime] | datetime | None = None,
    require_finalized_snapshot: bool = False,
) -> PeriodSelection:
    decision = evaluate_period(
        candidate, report_type=report_type, clock=clock,
        require_finalized_snapshot=require_finalized_snapshot,
    )
    if decision.eligible:
        return PeriodSelection(decision.candidate, (decision,), "explicit")
    if (
        contract.allow_unclosed_period_for_test
        and decision.rejection_code in {PERIOD_NOT_CLOSED, PERIOD_END_IN_FUTURE}
    ):
        return PeriodSelection(
            decision.candidate,
            (decision,),
            "explicit",
            override_used=True,
        )
    raise EcoEmailPreconditionError(
        decision.rejection_code or INVALID_PERIOD_BOUNDARY,
        "explicit reporting period is not eligible for this execution mode",
        decision.context(),
    )


def select_period_for_send(
    candidates: Iterable[PeriodCandidate | Mapping[str, Any]],
    *,
    report_type: str,
    contract: ExecutionContract,
    explicit_period: tuple[date, date] | None = None,
    clock: Callable[[], datetime] | datetime | None = None,
) -> PeriodSelection:
    """Select a DB-backed, finalized snapshot for a sender invocation."""

    rows = tuple(candidates)
    if explicit_period is None:
        return select_latest_closed_period(
            rows, report_type=report_type, clock=clock,
            require_finalized_snapshot=True,
        )
    start, end = explicit_period
    matches = [
        row for row in rows
        if _coerce_candidate(row).period_start_date == start
        and _coerce_candidate(row).period_end_date == end
    ]
    if not matches:
        raise EcoEmailPreconditionError(
            NO_ELIGIBLE_CLOSED_PERIOD,
            "the explicit period does not identify a persisted stats snapshot",
            {
                "period_start_date": start.isoformat(),
                "period_end_date": end.isoformat(),
                "candidate_count": len(rows),
                "timezone": BUSINESS_TIMEZONE_NAME,
            },
        )
    return require_explicit_period(
        matches[0], report_type=report_type, contract=contract, clock=clock,
        require_finalized_snapshot=True,
    )


def _selection_failure_diagnostics(
    decisions: Iterable[PeriodDecision], evaluated_at: datetime
) -> dict[str, Any]:
    rows = tuple(decisions)
    reasons: dict[str, int] = {}
    for decision in rows:
        reason = decision.rejection_code or INVALID_PERIOD_BOUNDARY
        reasons[reason] = reasons.get(reason, 0) + 1
    latest = max(
        rows,
        key=lambda decision: (
            decision.candidate.period_end_date,
            decision.candidate.period_start_date,
            decision.candidate.period_label or "",
        ),
        default=None,
    )
    return {
        "candidate_count": len(rows),
        "rejected_candidate_count": len(rows),
        "rejection_reasons": reasons,
        "latest_rejected_period": latest.context() if latest else None,
        "evaluated_local_datetime": evaluated_at.isoformat(),
        "timezone": BUSINESS_TIMEZONE_NAME,
    }


def fetch_period_candidates(
    cur, *, stats_table: str, client_id: str, report_type: str
) -> list[dict[str, Any]]:
    """Read deterministic period candidates from one report-type stats table."""

    if report_type == "weekly":
        cur.execute(
            f"""
            SELECT period_start_date, period_end_date, min(period_label) AS period_label,
                   min(updated_at) AS snapshot_min_updated_at,
                   max(updated_at) AS snapshot_max_updated_at
            FROM {stats_table}
            WHERE client_id = %s
            GROUP BY period_start_date, period_end_date
            ORDER BY period_end_date DESC, period_start_date DESC, min(period_label) DESC
            """,
            (client_id,),
        )
    elif report_type == "monthly":
        cur.execute(
            f"""
            SELECT month_start_date AS period_start_date,
                   month_end_date AS period_end_date,
                   to_char(month_start_date, 'YYYY-MM') AS period_label,
                   min(updated_at) AS snapshot_min_updated_at,
                   max(updated_at) AS snapshot_max_updated_at
            FROM {stats_table}
            WHERE client_id = %s
            GROUP BY month_start_date, month_end_date
            ORDER BY month_end_date DESC, month_start_date DESC
            """,
            (client_id,),
        )
    else:
        raise EcoEmailPreconditionError(
            INVALID_PERIOD_BOUNDARY, "report_type must be weekly or monthly"
        )
    return [dict(row) if not isinstance(row, dict) else row for row in cur.fetchall()]
