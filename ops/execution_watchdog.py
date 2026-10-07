#!/usr/bin/env python3
"""Independent missing-run / stuck-run watchdog (P0-3).

The platform's in-process error handling can only report failures of things that
actually started. It cannot report a timer that was disabled, a unit that died
before `POST /runs`, or a dispatcher that stopped ticking — the exact class that
went undetected in production on 2026-07-31 and 2026-08-01, where
`log-workflow-b.service` failed with no `public.runs` row at all.

This watchdog runs as its own systemd unit, on its own schedule, and asserts the
opposite direction:

    "This execution was expected, and it must have reached a terminal state by
     this deadline."

Evidence, in order of authority:

  * `workflow_a_control.client_schedule_run_history` — the dispatcher's own
    claim record, unique per (schedule_id, scheduled_fire_ts);
  * `public.runs` — platform run lifecycle, used for systemd-driven workflows
    that have no DB schedule metadata;
  * `ops_control.scheduler_heartbeat` — liveness for schedulers whose idle ticks
    deliberately persist no run row.

journald is never consulted: it rotates, and correctness must not depend on it.

Expected fire times for Workflow A are computed with the dispatcher's *own*
`latest_scheduled_fire_local`, so the watchdog and the dispatcher can never
disagree about when a fire was due.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import STATE_OPEN, STATE_RESOLVED, platform_db_conn  # noqa: E402
from jobs.api.telematics.dispatcher import latest_scheduled_fire_local  # noqa: E402
from ops.operational_alert import (  # noqa: E402
    INCIDENT_ALERT_DELIVERY_FAILED,
    INCIDENT_SCHEDULED_RUN_MISSING,
    INCIDENT_SCHEDULED_RUN_STALE,
    INCIDENT_SCHEDULER_HEARTBEAT_LOST,
    alerting_readiness,
    report_operational_failure,
    utcnow,
)

WATCHDOG_NAME = "execution_watchdog"
DEFAULT_EXPECTATIONS_PATH = REPO_ROOT / "ops" / "watchdog_expectations.json"

#: Terminal `public.runs` statuses.
#:
#: `RUNNING` is the only non-terminal status the platform has: `api/main.py`
#: allows exactly {RUNNING, SUCCESS, FAILED, CANCELED}, guards one-way
#: terminalization on {SUCCESS, FAILED, CANCELED}, `api/platform_prune.py`
#: classifies the same three as terminal, and `docs/01_architecture.md`
#: §"Lifecycle run" states `ended_at` is set for the final statuses.
#:
#: This watchdog previously recognized only SUCCESS and FAILED, so a `CANCELED`
#: run aged into a STALE incident describing a "stuck" execution that had in
#: fact settled. Nothing wrote CANCELED before, which is why the gap was
#: invisible; `ops/reconcile_historical_run.py` makes it a real writer.
#:
#: Deliberately a local constant rather than an import: the authoritative
#: definitions live in `api/main.py` (FastAPI) and `api/platform_prune.py`
#: (boto3), and this watchdog runs as its own minimal systemd unit that must
#: not acquire either dependency. `ops/tests_manual/test_execution_watchdog.py`
#: asserts all three sets stay identical, so the duplication cannot drift.
TERMINAL_RUN_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELED"})

# Verdicts. Only MISSING / STALE / HEARTBEAT_LOST raise an operator incident.
VERDICT_OK = "OK"
VERDICT_EXPECTED_FAILED = "EXPECTED_FAILED"
VERDICT_MISSING = "MISSING"
VERDICT_STALE = "STALE"
VERDICT_DISABLED = "DISABLED"
VERDICT_IN_WINDOW = "IN_WINDOW"
VERDICT_HEARTBEAT_LOST = "HEARTBEAT_LOST"
# Eligible now, but this fire predates the subject becoming eligible. A schedule
# enabled this morning must not be reported as having missed last night.
VERDICT_NOT_YET_EXPECTED = "NOT_YET_EXPECTED"

ALERTING_VERDICTS = frozenset({VERDICT_MISSING, VERDICT_STALE, VERDICT_HEARTBEAT_LOST})
# The only incident codes this watchdog may ever resolve. Scoping resolution to
# them is what stops watchdog recovery from closing a business incident.
WATCHDOG_INCIDENT_CODES = (
    INCIDENT_SCHEDULED_RUN_MISSING,
    INCIDENT_SCHEDULED_RUN_STALE,
    INCIDENT_SCHEDULER_HEARTBEAT_LOST,
    INCIDENT_ALERT_DELIVERY_FAILED,
)
HEALTHY_VERDICTS = frozenset(
    {VERDICT_OK, VERDICT_DISABLED, VERDICT_IN_WINDOW, VERDICT_NOT_YET_EXPECTED}
)
# Verdicts that mean "nothing was expected", as opposed to "something was
# expected and arrived". Only a genuine success may resolve an open incident:
# disabling a broken schedule must not silently close its incident.
NON_EXPECTING_VERDICTS = frozenset(
    {VERDICT_DISABLED, VERDICT_NOT_YET_EXPECTED, VERDICT_IN_WINDOW}
)

# Grace defaults, all explicitly justified.
#
#   completion grace — how long after a scheduled fire the execution is still
#   legitimately in flight. Workflow A trips syncs have run for over an hour, and
#   the dispatcher serialises all datasets globally, so a downstream fire can
#   legitimately wait behind an upstream one.
DEFAULT_COMPLETION_GRACE_MINUTES = 180
#
#   stale grace — how long a non-terminal execution may stay RUNNING before it is
#   treated as stuck. Deliberately shorter than the dispatcher's own 720-minute
#   stale reaper (`DEFAULT_STALE_RUNNING_TIMEOUT_MINUTES`) so the operator hears
#   about a wedged job well before the dispatcher silently auto-fails it.
DEFAULT_STALE_GRACE_MINUTES = 240
#
#   heartbeat grace — the dispatcher ticks every 5 minutes; 30 minutes is six
#   consecutive missed ticks, comfortably past transient host load.
DEFAULT_HEARTBEAT_GRACE_MINUTES = 30


# --------------------------------------------------------------------- model


@dataclass(frozen=True)
class Observation:
    subject_key: str
    verdict: str
    title: str
    summary: str
    component: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    client_code: str | None = None
    client_id: str | None = None
    dataset_name: str | None = None
    incident_code: str | None = None
    # Which watchdog produced this observation. `ops/disk_space_monitor.py`
    # reuses `record_observation`, so the stored `watchdog_name` must follow the
    # producer rather than this module, or `idx_watchdog_observation_open` files
    # disk rows under the execution watchdog.
    watchdog_name: str = WATCHDOG_NAME
    # Eligibility epoch, persisted so the next scan can detect a transition.
    # None means "this subject type has no eligibility notion" (systemd,
    # heartbeat, disk), in which case the stored columns are left untouched.
    eligible: bool | None = None
    eligible_since: datetime | None = None

    @property
    def alerting(self) -> bool:
        return self.verdict in ALERTING_VERDICTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_key": self.subject_key,
            "verdict": self.verdict,
            "component": self.component,
            "client_code": self.client_code,
            "dataset_name": self.dataset_name,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class SystemdExpectation:
    """A scheduled workflow that has no DB schedule metadata of its own."""

    subject: str
    component: str
    run_source: str
    times: tuple[str, ...]
    timezone_name: str = "Europe/Warsaw"
    days_of_week: tuple[int, ...] | None = None
    completion_grace_minutes: int = DEFAULT_COMPLETION_GRACE_MINUTES
    stale_grace_minutes: int = DEFAULT_STALE_GRACE_MINUTES
    enabled: bool = True

    def parsed_times(self) -> list[time]:
        out: list[time] = []
        for raw in self.times:
            hh, _, rest = str(raw).partition(":")
            mm, _, ss = rest.partition(":")
            out.append(time(int(hh), int(mm or 0), int(ss or 0)))
        return out


@dataclass(frozen=True)
class HeartbeatExpectation:
    subject: str
    component: str
    heartbeat_component: str
    grace_minutes: int = DEFAULT_HEARTBEAT_GRACE_MINUTES
    enabled: bool = True
    # What a lost heartbeat actually means for this subject. The dispatcher's
    # consequence ("no schedule it owns can fire") is not the alert worker's
    # ("no queued alert can be delivered"), and an alert that states the wrong
    # consequence sends the operator to the wrong place first.
    consequence: str = "No schedule it owns can fire."


@dataclass(frozen=True)
class AlertDeliveryExpectation:
    """Durable observability for the outbox the alert path itself depends on.

    The worker's non-zero exit makes a dead-letter *transition* visible as a
    failed unit. That signal is momentary: the row is never re-claimed, so the
    next batch succeeds and the unit stops being failed while the alert is still
    undelivered. This subject is the durable half — it keeps answering "there are
    alerts nobody will ever receive" until an operator clears them.
    """

    subject: str = "alert_delivery"
    component: str = "alerting.delivery"
    # Grace before an unsent queued row counts as stuck. The worker fires every
    # 5 minutes and retries with bounded backoff up to `max_retry_delay`
    # (default 3600s), so anything below ~2h would alert on healthy backoff.
    backlog_grace_minutes: int = 180
    enabled: bool = True


@dataclass
class WatchdogConfig:
    completion_grace_minutes: int = DEFAULT_COMPLETION_GRACE_MINUTES
    stale_grace_minutes: int = DEFAULT_STALE_GRACE_MINUTES
    systemd_expectations: tuple[SystemdExpectation, ...] = ()
    heartbeats: tuple[HeartbeatExpectation, ...] = ()
    alert_delivery: AlertDeliveryExpectation | None = None
    dataset_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def grace_for(self, dataset_name: str | None) -> int:
        override = self.dataset_overrides.get(str(dataset_name or ""), {})
        return int(override.get("completion_grace_minutes", self.completion_grace_minutes))

    def stale_for(self, dataset_name: str | None) -> int:
        override = self.dataset_overrides.get(str(dataset_name or ""), {})
        return int(override.get("stale_grace_minutes", self.stale_grace_minutes))


def load_config(path: Path | None = None) -> WatchdogConfig:
    """Load declarative expectations. Missing file yields DB-only monitoring."""
    target = path or DEFAULT_EXPECTATIONS_PATH
    if not target.exists():
        return WatchdogConfig()
    raw = json.loads(target.read_text(encoding="utf-8"))
    defaults = raw.get("defaults") or {}
    systemd = tuple(
        SystemdExpectation(
            subject=item["subject"],
            component=item.get("component") or item["subject"],
            run_source=item["run_source"],
            times=tuple(item.get("times") or ()),
            timezone_name=item.get("timezone", "Europe/Warsaw"),
            days_of_week=tuple(item["days_of_week"]) if item.get("days_of_week") else None,
            completion_grace_minutes=int(
                item.get(
                    "completion_grace_minutes",
                    defaults.get("completion_grace_minutes", DEFAULT_COMPLETION_GRACE_MINUTES),
                )
            ),
            stale_grace_minutes=int(
                item.get(
                    "stale_grace_minutes",
                    defaults.get("stale_grace_minutes", DEFAULT_STALE_GRACE_MINUTES),
                )
            ),
            enabled=bool(item.get("enabled", True)),
        )
        for item in raw.get("systemd_schedules") or []
    )
    heartbeats = tuple(
        HeartbeatExpectation(
            subject=item["subject"],
            component=item.get("component") or item["subject"],
            heartbeat_component=item["heartbeat_component"],
            grace_minutes=int(item.get("grace_minutes", DEFAULT_HEARTBEAT_GRACE_MINUTES)),
            enabled=bool(item.get("enabled", True)),
            consequence=str(
                item.get("consequence") or HeartbeatExpectation.consequence
            ),
        )
        for item in raw.get("heartbeats") or []
    )
    alert_delivery_raw = raw.get("alert_delivery")
    alert_delivery = (
        AlertDeliveryExpectation(
            subject=alert_delivery_raw.get("subject") or "alert_delivery",
            component=alert_delivery_raw.get("component") or "alerting.delivery",
            backlog_grace_minutes=int(
                alert_delivery_raw.get(
                    "backlog_grace_minutes",
                    AlertDeliveryExpectation.backlog_grace_minutes,
                )
            ),
            enabled=bool(alert_delivery_raw.get("enabled", True)),
        )
        if isinstance(alert_delivery_raw, Mapping)
        else None
    )
    return WatchdogConfig(
        completion_grace_minutes=int(
            defaults.get("completion_grace_minutes", DEFAULT_COMPLETION_GRACE_MINUTES)
        ),
        stale_grace_minutes=int(
            defaults.get("stale_grace_minutes", DEFAULT_STALE_GRACE_MINUTES)
        ),
        systemd_expectations=systemd,
        heartbeats=heartbeats,
        alert_delivery=alert_delivery,
        dataset_overrides=raw.get("dataset_overrides") or {},
    )


# --------------------------------------------------- Workflow A identity

#: The schedule role every pre-M5 Workflow A schedule carries. Migration 062
#: added `run_type` with `DEFAULT 'DAILY'` and backfilled every existing row to
#: it, so a row without the column, or with it unset, is a DAILY schedule.
DEFAULT_SCHEDULE_RUN_TYPE = "DAILY"


def schedule_run_type(row: Mapping[str, Any]) -> str:
    """The schedule's role, normalised, defaulting to the pre-M5 DAILY role."""
    return str(row.get("run_type") or DEFAULT_SCHEDULE_RUN_TYPE).strip().upper()


def workflow_a_subject_key(row: Mapping[str, Any]) -> str:
    """Watchdog subject identity for one Workflow A schedule role.

    Since migration 062 the canonical schedule identity is
    `uq_client_dataset_schedule UNIQUE (client_id, dataset_name, run_type)`, so
    the role is part of what makes a subject one subject. Keying only on
    (client, dataset) folded a DAILY schedule and its WEEKLY_RECONCILIATION
    sibling into a single key, and within one scan that is not a cosmetic
    collision: each sibling overwrote the other's verdict, read the other's
    verdict as `previous`, incremented one shared `observation_count`, and could
    close an incident it never opened — a WEEKLY `OK` silently resolving the
    DAILY `MISSING` incident raised moments earlier in the same pass.

    DAILY deliberately keeps the legacy two-segment key. Every DAILY subject in
    production predates 062, and its observation history, eligibility epoch and
    open incident fingerprints are addressed by that exact string — an incident
    fingerprint hashes `subject_key` — so qualifying it would orphan live state
    and re-open every currently open incident under a new identity. Non-DAILY
    roles are new subjects with no history to preserve, so they carry the
    qualifier:

        workflow_a:ALPHA00001:trips_sync
        workflow_a:ALPHA00001:trips_sync:WEEKLY_RECONCILIATION
        workflow_a:ALPHA00001:trips_sync:MONTHLY_RECONCILIATION

    The two shapes cannot be confused. `dataset_name` is constrained to
    `^[a-z][a-z0-9_]*$` (migration 011) and every `run_type` in
    `ck_client_dataset_schedule_run_type` is upper-case, so no unqualified key
    can ever spell a qualified one.

    The client segment falls back to `client_id` because `client_code` is
    *nullable by design* — migration 017 states that a client may intentionally
    have none, and a unique index does not constrain repeated NULLs, so two
    code-less clients sharing a dataset would otherwise collide exactly the way
    two roles did. Every schedule that has a code keeps that code, which is why
    no existing key moves: the fallback is only reachable for a client that
    never had an identity in this key to begin with.

    That is why the fallback tests for *absence*, not for falsiness. `client_code`
    is unconstrained TEXT, so `''` is a present value, and it already addressed a
    live subject as `workflow_a::{dataset}` before this fix; substituting
    `client_id` for it would move an existing DAILY key exactly the way
    qualifying DAILY would. A blank code that two clients share is a collision
    like any other, and `report_subject_key_collisions` names it rather than
    guessing an identity — the same rule this module applies everywhere else.
    """
    client_code = row.get("client_code")
    client = row.get("client_id") if client_code is None else client_code
    key = f"workflow_a:{client}:{row.get('dataset_name')}"
    run_type = schedule_run_type(row)
    return key if run_type == DEFAULT_SCHEDULE_RUN_TYPE else f"{key}:{run_type}"


def _stderr_event(event: str, **fields: Any) -> None:
    payload = {
        "level": "ERROR",
        "classification": "operational_error",
        "component": "ops.execution_watchdog",
        "event": event,
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    try:
        print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)
    except Exception:
        pass


def report_subject_key_collisions(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Make a shared subject key loud. Do not silently re-key around it.

    The schema makes `(client_id, dataset_name, run_type)` unique and that is
    what the key encodes, but it encodes the client as `client_code`, which is
    unconstrained TEXT: nothing stops an operator from coding one client as
    another client's UUID, which would defeat the code-less fallback. That
    configuration is pathological rather than reachable by accident, and the
    fleet has no code-less client at all.

    Detection, not repair, is deliberately the whole of this function. Appending
    `schedule_id` to a colliding group looks like a fix and is worse: the key
    would then depend on *which other rows exist*, so correcting or deleting one
    schedule would silently move the survivor's identity and orphan the
    observation history and open incident fingerprints it accumulated under the
    disambiguated key. A subject key must be a fact about one schedule. An
    operational_error naming both schedules is the honest signal, and the
    remedy — give the clients distinct codes — is an operator action.
    """
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(workflow_a_subject_key(row), []).append(str(row.get("schedule_id")))
    collisions = sorted(key for key, ids in grouped.items() if len(ids) > 1)
    for key in collisions:
        _stderr_event(
            "watchdog_subject_key_collision",
            subject_key=key,
            schedule_ids=sorted(grouped[key]),
            remediation=(
                "Two schedules resolve to one watchdog subject, so they share one "
                "observation row, one counter and one incident. Give the clients "
                "distinct client_code values; the watchdog will not guess an identity."
            ),
        )
    return collisions


# ------------------------------------------------------- pure evaluation


class _FireSpec:
    """Minimal duck type accepted by the dispatcher's fire arithmetic."""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.frequency = row["frequency"]
        self.run_time = row["run_time"]
        self.day_of_week = row.get("day_of_week")
        self.day_of_month = row.get("day_of_month")
        self.day_of_month_last = bool(row.get("day_of_month_last"))


def expected_fire_utc(row: Mapping[str, Any], *, now_utc: datetime) -> datetime | None:
    """Latest fire at or before `now_utc`, using the dispatcher's own arithmetic."""
    try:
        tz = ZoneInfo(str(row.get("timezone") or "UTC"))
    except ZoneInfoNotFoundError:
        return None
    fire_local = latest_scheduled_fire_local(
        now_local=now_utc.astimezone(tz), sched=_FireSpec(row)
    )
    if fire_local is None:
        return None
    fire_utc = fire_local.astimezone(timezone.utc)
    return fire_utc if fire_utc <= now_utc else None


def evaluate_schedule_subject(
    *,
    row: Mapping[str, Any],
    history: Mapping[str, Any] | None,
    now_utc: datetime,
    config: WatchdogConfig,
    eligible_since_ts: datetime | None = None,
) -> Observation:
    """Classify one Workflow A schedule, stamping its eligibility epoch.

    The epoch is attached to whatever verdict `_classify_schedule_subject`
    returns so `record_observation` persists it, and the *next* scan can tell an
    eligibility transition from a subject it has merely seen before.
    """
    from dataclasses import replace as _replace

    eligible, _ = schedule_eligibility(row)
    observation = _classify_schedule_subject(
        row=row, history=history, now_utc=now_utc, config=config,
        eligible_since_ts=eligible_since_ts,
    )
    return _replace(
        observation,
        eligible=eligible,
        eligible_since=eligible_since_ts if eligible else None,
    )


def _classify_schedule_subject(
    *,
    row: Mapping[str, Any],
    history: Mapping[str, Any] | None,
    now_utc: datetime,
    config: WatchdogConfig,
    eligible_since_ts: datetime | None = None,
) -> Observation:
    """Classify one Workflow A schedule against its claim record.

    `eligible_since_ts` is the eligibility epoch from
    `eligibility_epoch()` — when the subject most recently became eligible, not
    when it was first seen. Fires older than it were never expected.
    """
    dataset = row.get("dataset_name")
    client_code = row.get("client_code")
    run_type = schedule_run_type(row)
    subject_key = workflow_a_subject_key(row)
    component = f"workflow_a.schedule.{dataset}"
    base = {
        "watchdog": WATCHDOG_NAME,
        "client_code": client_code,
        "dataset_name": dataset,
        # The schedule role is part of the subject's identity, so it belongs in
        # the persisted detail: without it two sibling roles over one dataset are
        # indistinguishable in an observation row or an incident payload.
        "run_type": run_type,
        "schedule_id": str(row.get("schedule_id")),
    }

    eligible, reason = schedule_eligibility(row)
    if not eligible:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_DISABLED,
            title="Schedule not eligible for dispatch",
            summary=(
                f"The dispatcher would not select this schedule ({reason}), so no "
                f"execution is expected."
            ),
            component=component, detail={**base, "ineligible_reason": reason},
            client_code=client_code,
            client_id=str(row.get("client_id")) if row.get("client_id") else None,
            dataset_name=dataset,
        )

    fire_utc = expected_fire_utc(row, now_utc=now_utc)
    if fire_utc is not None and eligible_since_ts is not None and fire_utc < eligible_since_ts:
        # Newly enabled schedule, or newly enabled client. The fire happened
        # while nothing was expected, so calling it missing would be a false
        # alarm on every activation.
        return Observation(
            subject_key=subject_key, verdict=VERDICT_NOT_YET_EXPECTED,
            title="Fire predates eligibility",
            summary=(
                f"The most recent fire ({fire_utc.isoformat()}) is earlier than this "
                f"subject became eligible ({eligible_since_ts.isoformat()}); nothing "
                f"was expected for it."
            ),
            component=component,
            detail={
                **base,
                "scheduled_fire_ts": fire_utc.isoformat(),
                "eligible_since": eligible_since_ts.isoformat(),
            },
            client_code=client_code,
            client_id=str(row.get("client_id")) if row.get("client_id") else None,
            dataset_name=dataset,
        )
    if fire_utc is None:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
            title="No fire due yet", summary="No scheduled fire has come due.",
            component=component, detail=base, client_code=client_code,
            client_id=str(row.get("client_id")) if row.get("client_id") else None,
            dataset_name=dataset,
        )

    grace = timedelta(minutes=config.grace_for(dataset))
    deadline = fire_utc + grace
    detail = {
        **base,
        "scheduled_fire_ts": fire_utc.isoformat(),
        "deadline_ts": deadline.isoformat(),
        "completion_grace_minutes": config.grace_for(dataset),
    }
    client_id = str(row.get("client_id")) if row.get("client_id") else None

    if history is None:
        if now_utc < deadline:
            return Observation(
                subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
                title="Execution still within grace",
                summary="The fire is due but the grace window has not expired.",
                component=component, detail=detail, client_code=client_code,
                client_id=client_id, dataset_name=dataset,
            )
        return Observation(
            subject_key=subject_key, verdict=VERDICT_MISSING,
            title=f"Scheduled execution never started: {client_code}/{dataset}",
            summary=(
                f"The dispatcher was expected to claim a fire at {fire_utc.isoformat()} "
                f"for {client_code}/{dataset}, but no row exists in "
                f"workflow_a_control.client_schedule_run_history for that fire and the "
                f"{config.grace_for(dataset)}-minute grace window has expired. The "
                f"timer, the dispatcher or the schedule itself is not running."
            ),
            component=component, detail=detail, client_code=client_code,
            client_id=client_id, dataset_name=dataset,
            incident_code=INCIDENT_SCHEDULED_RUN_MISSING,
        )

    status = str(history.get("status") or "").upper()
    detail = {**detail, "run_history_id": str(history.get("run_history_id")), "status": status}

    if status == "SUCCESS":
        return Observation(
            subject_key=subject_key, verdict=VERDICT_OK,
            title="Execution completed", summary="The expected fire reached SUCCESS.",
            component=component, detail=detail, client_code=client_code,
            client_id=client_id, dataset_name=dataset,
        )
    if status == "FAILED":
        # The job itself already raised a JOB_TERMINAL_FAILURE incident through
        # ops/runner.py. Re-alerting here would double-notify one root cause.
        return Observation(
            subject_key=subject_key, verdict=VERDICT_EXPECTED_FAILED,
            title="Execution failed",
            summary="The expected fire reached FAILED and is alerted by the job path.",
            component=component, detail=detail, client_code=client_code,
            client_id=client_id, dataset_name=dataset,
        )

    started = _as_utc(history.get("started_at") or history.get("created_at"))
    stale_minutes = config.stale_for(dataset)
    stale_deadline = (started or fire_utc) + timedelta(minutes=stale_minutes)
    detail = {**detail, "stale_grace_minutes": stale_minutes,
              "stale_deadline_ts": stale_deadline.isoformat()}
    if now_utc < stale_deadline:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
            title="Execution in progress",
            summary="The execution is RUNNING within its allowed window.",
            component=component, detail=detail, client_code=client_code,
            client_id=client_id, dataset_name=dataset,
        )
    return Observation(
        subject_key=subject_key, verdict=VERDICT_STALE,
        title=f"Scheduled execution stuck: {client_code}/{dataset}",
        summary=(
            f"The fire at {fire_utc.isoformat()} for {client_code}/{dataset} is still "
            f"RUNNING after {stale_minutes} minutes. A wedged claim blocks every other "
            f"Workflow A dataset until the dispatcher's own stale reaper releases it."
        ),
        component=component, detail=detail, client_code=client_code,
        client_id=client_id, dataset_name=dataset,
        incident_code=INCIDENT_SCHEDULED_RUN_STALE,
    )


def expected_systemd_fires(
    expectation: SystemdExpectation, *, now_utc: datetime, lookback_hours: int = 48
) -> list[datetime]:
    """Every fire of `expectation` inside the lookback window, oldest first."""
    try:
        tz = ZoneInfo(expectation.timezone_name)
    except ZoneInfoNotFoundError:
        return []
    earliest = now_utc - timedelta(hours=lookback_hours)
    fires: list[datetime] = []
    now_local = now_utc.astimezone(tz)
    for day_offset in range(0, (lookback_hours // 24) + 2):
        day = (now_local - timedelta(days=day_offset)).date()
        for run_time in expectation.parsed_times():
            local = datetime.combine(day, run_time, tzinfo=tz)
            if expectation.days_of_week and local.weekday() not in expectation.days_of_week:
                continue
            fire = local.astimezone(timezone.utc)
            if earliest <= fire <= now_utc:
                fires.append(fire)
    return sorted(fires)


def evaluate_systemd_subject(
    *,
    expectation: SystemdExpectation,
    fire_utc: datetime,
    run: Mapping[str, Any] | None,
    now_utc: datetime,
) -> Observation:
    """Classify one systemd-driven fire against `public.runs`.

    This is an **occurrence**, not an incident. `fold_systemd_occurrences()`
    reduces every fire in the horizon to one root observation; only that root
    reaches the incident layer. See the module docstring on identity.
    """
    subject_key = f"systemd:{expectation.subject}:{fire_utc.isoformat()}"
    deadline = fire_utc + timedelta(minutes=expectation.completion_grace_minutes)
    detail = {
        "watchdog": WATCHDOG_NAME,
        "unit_subject": expectation.subject,
        "run_source": expectation.run_source,
        "scheduled_fire_ts": fire_utc.isoformat(),
        "deadline_ts": deadline.isoformat(),
        "completion_grace_minutes": expectation.completion_grace_minutes,
    }

    if not expectation.enabled:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_DISABLED,
            title="Expectation disabled", summary="No execution is expected.",
            component=expectation.component, detail=detail,
        )

    if run is None:
        if now_utc < deadline:
            return Observation(
                subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
                title="Execution still within grace",
                summary="The fire is due but the grace window has not expired.",
                component=expectation.component, detail=detail,
            )
        return Observation(
            subject_key=subject_key, verdict=VERDICT_MISSING,
            title=f"Scheduled execution never started: {expectation.subject}",
            summary=(
                f"{expectation.run_source} was expected to run at {fire_utc.isoformat()} "
                f"but produced no row in public.runs within "
                f"{expectation.completion_grace_minutes} minutes. The unit may be "
                f"disabled, the timer may not be firing, or the process may be dying "
                f"before it can register a run."
            ),
            component=expectation.component, detail=detail,
            incident_code=INCIDENT_SCHEDULED_RUN_MISSING,
        )

    status = str(run.get("status") or "").upper()
    detail = {**detail, "run_id": str(run.get("run_id")), "status": status}
    if status == "SUCCESS":
        return Observation(
            subject_key=subject_key, verdict=VERDICT_OK, title="Execution completed",
            summary="The expected fire reached SUCCESS.",
            component=expectation.component, detail=detail,
        )
    if status in TERMINAL_RUN_STATUSES:
        # FAILED and CANCELED are both settled outcomes, so neither may age into
        # STALE. They are reported apart because only FAILED is already alerted
        # by the job path; a CANCELED run was terminalized deliberately and has
        # no failure to re-notify.
        canceled = status == "CANCELED"
        return Observation(
            subject_key=subject_key, verdict=VERDICT_EXPECTED_FAILED,
            title="Execution canceled" if canceled else "Execution failed",
            summary=(
                "The expected fire reached CANCELED and is terminal; it is not stuck."
                if canceled else
                "The expected fire reached FAILED and is alerted by the job path."
            ),
            component=expectation.component, detail=detail,
        )

    started = _as_utc(run.get("started_at")) or fire_utc
    stale_deadline = started + timedelta(minutes=expectation.stale_grace_minutes)
    detail = {**detail, "stale_grace_minutes": expectation.stale_grace_minutes,
              "stale_deadline_ts": stale_deadline.isoformat()}
    if now_utc < stale_deadline:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
            title="Execution in progress",
            summary="The execution is non-terminal within its allowed window.",
            component=expectation.component, detail=detail,
        )
    return Observation(
        subject_key=subject_key, verdict=VERDICT_STALE,
        title=f"Scheduled execution stuck: {expectation.subject}",
        summary=(
            f"The {expectation.run_source} run for the fire at {fire_utc.isoformat()} is "
            f"still non-terminal after {expectation.stale_grace_minutes} minutes. Nothing "
            f"reaps stale rows in public.runs, so it will stay this way until an operator "
            f"intervenes."
        ),
        component=expectation.component, detail=detail,
        incident_code=INCIDENT_SCHEDULED_RUN_STALE,
    )


def evaluate_heartbeat_subject(
    *,
    expectation: HeartbeatExpectation,
    last_beat_at: datetime | None,
    now_utc: datetime,
) -> Observation:
    """Classify scheduler liveness where idle ticks persist no run row."""
    subject_key = f"heartbeat:{expectation.subject}"
    detail = {
        "watchdog": WATCHDOG_NAME,
        "heartbeat_component": expectation.heartbeat_component,
        "grace_minutes": expectation.grace_minutes,
        "last_beat_at": last_beat_at.isoformat() if last_beat_at else None,
    }
    if not expectation.enabled:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_DISABLED,
            title="Heartbeat expectation disabled", summary="No heartbeat is expected.",
            component=expectation.component, detail=detail,
        )

    deadline_age = timedelta(minutes=expectation.grace_minutes)
    if last_beat_at is not None and now_utc - last_beat_at <= deadline_age:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_OK, title="Scheduler alive",
            summary="The scheduler stamped a recent heartbeat.",
            component=expectation.component, detail=detail,
        )

    age_minutes = (
        int((now_utc - last_beat_at).total_seconds() // 60) if last_beat_at else None
    )
    return Observation(
        subject_key=subject_key, verdict=VERDICT_HEARTBEAT_LOST,
        title=f"Scheduler heartbeat lost: {expectation.subject}",
        summary=(
            f"{expectation.heartbeat_component} has not reached the platform database for "
            + (f"{age_minutes} minutes" if age_minutes is not None else "any recorded time")
            + f" (grace {expectation.grace_minutes} minutes). Its timer or service is not "
            f"running. {expectation.consequence}"
        ),
        component=expectation.component, detail={**detail, "age_minutes": age_minutes},
        incident_code=INCIDENT_SCHEDULER_HEARTBEAT_LOST,
    )


def evaluate_alert_delivery_subject(
    *,
    expectation: AlertDeliveryExpectation,
    state: Mapping[str, Any],
    now_utc: datetime,
) -> Observation:
    """Classify the health of the alert outbox itself.

    Two distinct conditions, one subject, because the operator action is the same
    (fix the mail path, then requeue) and splitting them would produce two emails
    for one broken channel:

      * `dead_letter` rows — alerts that exhausted their retries and will never
        be delivered. Terminal; only an operator clears them.
      * queued rows overdue past the backlog grace — the worker is not draining,
        which usually means its timer is off or every attempt is failing.

    Not reported here: transient `retry` rows inside their backoff. That is the
    retry mechanism working, and alerting on it would fire on every brief SMTP
    hiccup.
    """
    subject_key = f"alert_delivery:{expectation.subject}"
    dead_letter = int(state.get("dead_letter_count") or 0)
    overdue = int(state.get("overdue_queued_count") or 0)
    detail = {
        "watchdog": WATCHDOG_NAME,
        "dead_letter_count": dead_letter,
        "queued_count": int(state.get("queued_count") or 0),
        "overdue_queued_count": overdue,
        "backlog_grace_minutes": expectation.backlog_grace_minutes,
        "oldest_queued_at": _iso_or_none(state.get("oldest_queued_at")),
        "oldest_dead_letter_at": _iso_or_none(state.get("oldest_dead_letter_at")),
    }

    if not expectation.enabled:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_DISABLED,
            title="Alert delivery expectation disabled",
            summary="Alert outbox health is not being asserted.",
            component=expectation.component, detail=detail,
        )

    if not dead_letter and not overdue:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_OK,
            title="Alert delivery healthy",
            summary="No dead-lettered alerts and no overdue queued alerts.",
            component=expectation.component, detail=detail,
        )

    causes = []
    if dead_letter:
        causes.append(f"{dead_letter} alert email(s) reached dead_letter")
    if overdue:
        causes.append(
            f"{overdue} queued alert email(s) have not been delivered within "
            f"{expectation.backlog_grace_minutes} minutes"
        )
    return Observation(
        subject_key=subject_key, verdict=VERDICT_STALE,
        title="Operator alert email is not being delivered",
        summary=(
            "; ".join(causes)
            + ". Incidents are still being recorded, but the operator is not being "
              "told about them. The alert worker cannot report this by email, so "
              "this observation and its unit state are the signal."
        ),
        component=expectation.component, detail=detail,
        incident_code=INCIDENT_ALERT_DELIVERY_FAILED,
    )


def load_alert_delivery_state(
    conn, *, expectation: AlertDeliveryExpectation, now_utc: datetime
) -> dict[str, Any]:
    """Outbox health counters. Read-only."""
    cutoff = now_utc - timedelta(minutes=expectation.backlog_grace_minutes)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              count(*) FILTER (WHERE status = 'dead_letter')            AS dead_letter_count,
              min(created_at) FILTER (WHERE status = 'dead_letter')     AS oldest_dead_letter_at,
              count(*) FILTER (WHERE status IN ('pending', 'retry'))    AS queued_count,
              count(*) FILTER (
                WHERE status IN ('pending', 'retry') AND created_at < %s
              )                                                          AS overdue_queued_count,
              min(created_at) FILTER (WHERE status IN ('pending', 'retry'))
                                                                         AS oldest_queued_at
            FROM suspected_bug_email_outbox
            """,
            (cutoff,),
        )
        row = dict(cur.fetchone() or {})
    conn.rollback()
    return row


def _iso_or_none(value: Any) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ------------------------------------------------------------- persistence


# Eligibility must mean exactly what the dispatcher means by it. The dispatcher
# selects with:
#
#     FROM client_dataset_schedule cds
#     JOIN client_account   ca ON ca.client_id    = cds.client_id
#     JOIN dataset_registry dr ON dr.dataset_name = cds.dataset_name
#     WHERE cds.enabled = true AND ca.enabled = true
#
# Both JOINs are inner, so an unregistered dataset is as ineligible as a disabled
# one. The watchdog previously filtered on `s.enabled` alone and therefore
# expected executions the dispatcher would never perform — an enabled schedule
# belonging to a disabled client was reported MISSING forever.
# `ops/tests_manual/test_execution_watchdog.py` compares both paths against one
# fixture and guards the dispatcher source against silent drift.
ELIGIBILITY_INELIGIBLE_SCHEDULE = "schedule_disabled"
ELIGIBILITY_INELIGIBLE_CLIENT = "client_account_disabled"
ELIGIBILITY_UNREGISTERED_DATASET = "dataset_not_registered"


def schedule_eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Mirror of the dispatcher's eligibility predicate. Returns (eligible, reason)."""
    if not row.get("enabled"):
        return False, ELIGIBILITY_INELIGIBLE_SCHEDULE
    if not row.get("client_enabled"):
        return False, ELIGIBILITY_INELIGIBLE_CLIENT
    if not row.get("dataset_registered"):
        return False, ELIGIBILITY_UNREGISTERED_DATASET
    return True, None


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def eligibility_epoch(
    row: Mapping[str, Any],
    *,
    eligible_now: bool,
    previous: Mapping[str, Any] | None,
    now_utc: datetime,
) -> datetime | None:
    """When this subject most recently *became* eligible.

    Not "when it was first observed". Those differ exactly where it matters: a
    schedule observed while its client was disabled has an old `first_observed_at`
    and, when the client is re-enabled, would retroactively expect fires from the
    disabled interval. `client_account` has no timestamps, so the only way to know
    the transition happened is to remember what the last scan saw.

    * ineligible now → no epoch; nothing is expected;
    * previously ineligible (or previously unknown-but-recorded) → the epoch is
      **now**: this scan is the transition, so earlier fires stay unexpected;
    * previously eligible → keep the stored epoch, so repeated scans never push
      it forward and a long-running schedule keeps normal missing detection;
    * never observed → fall back to the schedule row's own
      `updated_at`/`created_at`. That is real evidence (enabling a schedule writes
      `updated_at`) and it keeps a long-established schedule under normal
      detection from the very first scan instead of granting it a free pass.
    """
    if not eligible_now:
        return None
    if previous is not None:
        was_eligible = previous.get("eligible")
        stored = _aware(previous.get("eligible_since"))
        if was_eligible and stored is not None:
            return stored
        # False, or NULL from a row written before eligibility was tracked:
        # treat this scan as the transition into eligibility.
        return now_utc
    candidates = [
        value for value in (_aware(row.get("updated_at")), _aware(row.get("created_at")))
        if value is not None
    ]
    return max(candidates) if candidates else now_utc


def load_observation_state(conn) -> dict[str, dict[str, Any]]:
    """Previous eligibility state per subject, for transition detection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT subject_key, first_observed_at, eligible, eligible_since "
            "FROM ops_control.watchdog_observation"
        )
        return {str(row["subject_key"]): dict(row) for row in cur.fetchall()}


def load_workflow_a_subjects(conn, *, now_utc: datetime) -> list[dict[str, Any]]:
    """Every Workflow A schedule, with the eligibility inputs the dispatcher uses.

    All schedules are selected, not just enabled ones, so a disabled schedule can
    be reported as `DISABLED` instead of silently vanishing — the previous query
    filtered them out, which is why the documented `DISABLED` verdict was never
    actually produced.

    `run_type` is projected because it is half of the subject's identity (see
    `workflow_a_subject_key`); without it this loader looked up the *sibling's*
    eligibility epoch. The ordering carries the full scheduling identity down to
    `schedule_id` so enumeration is a total order rather than one that leaves
    sibling roles in whatever order the plan happened to emit. Ordering is
    debuggability, not correctness: once the keys are distinct, no order of the
    same rows can change the persisted outcome.
    """
    previous_state = load_observation_state(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.schedule_id, s.client_id, s.client_code, s.dataset_name, s.enabled,
                   s.run_type,
                   s.frequency, s.day_of_week, s.day_of_month, s.day_of_month_last,
                   s.run_time, s.timezone, s.lookback_days,
                   s.created_at, s.updated_at,
                   COALESCE(ca.enabled, false)         AS client_enabled,
                   (dr.dataset_name IS NOT NULL)       AS dataset_registered
              FROM workflow_a_control.client_dataset_schedule s
              LEFT JOIN workflow_a_control.client_account ca
                     ON ca.client_id = s.client_id
              LEFT JOIN workflow_a_control.dataset_registry dr
                     ON dr.dataset_name = s.dataset_name
             ORDER BY s.client_code, s.dataset_name, s.run_type, s.schedule_id
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    report_subject_key_collisions(rows)

    out: list[dict[str, Any]] = []
    for row in rows:
        subject_key = workflow_a_subject_key(row)
        eligible, _ = schedule_eligibility(row)
        fire_utc = expected_fire_utc(row, now_utc=now_utc) if eligible else None
        history: dict[str, Any] | None = None
        if fire_utc is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_history_id, status, started_at, created_at, finished_at
                      FROM workflow_a_control.client_schedule_run_history
                     WHERE schedule_id = %s::uuid AND scheduled_fire_ts = %s
                     LIMIT 1
                    """,
                    (str(row["schedule_id"]), fire_utc),
                )
                found = cur.fetchone()
                history = dict(found) if found else None
        out.append(
            {
                "row": row,
                "history": history,
                "eligible": eligible,
                "eligible_since": eligibility_epoch(
                    row, eligible_now=eligible,
                    previous=previous_state.get(subject_key), now_utc=now_utc,
                ),
            }
        )
    return out


def load_systemd_run(conn, *, run_source: str, fire_utc: datetime,
                     grace_minutes: int) -> dict[str, Any] | None:
    """The run attributable to one systemd fire.

    Matched on `source` and start time rather than `trigger`, because timer-driven
    Workflow B runs are currently recorded with `trigger='MANUAL'` (the unit passes
    no trigger). Start-time attribution is therefore the only reliable link.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, status, started_at, ended_at
              FROM runs
             WHERE source = %s
               AND started_at >= %s
               AND started_at < %s
             ORDER BY started_at ASC
             LIMIT 1
            """,
            (run_source, fire_utc - timedelta(minutes=5),
             fire_utc + timedelta(minutes=max(grace_minutes, 1))),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def load_heartbeat(conn, *, component: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_beat_at FROM ops_control.scheduler_heartbeat WHERE component = %s",
            (component,),
        )
        row = cur.fetchone()
    return _as_utc(row["last_beat_at"]) if row else None


def fold_systemd_occurrences(
    *, expectation: SystemdExpectation, occurrences: Sequence[Observation],
    now_utc: datetime,
) -> Observation:
    """Reduce every evaluated fire to one root observation for the schedule.

    The incident this produces answers "is this scheduled workflow currently
    producing its expected runs?", not "did the 06:00 fire on the 3rd happen?".
    That distinction is the fix for unbounded incident growth: keying identity per
    fire meant a continuing Workflow B outage opened a new incident at 06:00 and
    another at 20:00, every day, and none of them could ever resolve — a later
    successful fire had a different key, so it closed nothing and the old
    incidents stayed open forever once they fell out of the scan horizon.

    Root health is the verdict of the **newest decided fire**. A fire still inside
    its grace window is not yet decided and cannot make the subject healthy or
    unhealthy. Individual missed fires are preserved as evidence in `detail`.
    """
    subject_key = f"systemd:{expectation.subject}"
    component = expectation.component
    ordered = sorted(occurrences, key=lambda item: str(item.detail.get("scheduled_fire_ts") or ""))
    decided = [item for item in ordered if item.verdict != VERDICT_IN_WINDOW]
    missed = [
        str(item.detail.get("scheduled_fire_ts"))
        for item in ordered if item.verdict in (VERDICT_MISSING, VERDICT_STALE)
    ]
    detail = {
        "watchdog": WATCHDOG_NAME,
        "unit_subject": expectation.subject,
        "run_source": expectation.run_source,
        "completion_grace_minutes": expectation.completion_grace_minutes,
        "evaluated_fires": [str(item.detail.get("scheduled_fire_ts")) for item in ordered],
        "missed_fires": missed,
        "missed_fire_count": len(missed),
        "latest_decided_fire": (
            str(decided[-1].detail.get("scheduled_fire_ts")) if decided else None
        ),
    }

    if not expectation.enabled:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_DISABLED,
            title="Expectation disabled", summary="No execution is expected.",
            component=component, detail=detail,
        )
    if not decided:
        return Observation(
            subject_key=subject_key, verdict=VERDICT_IN_WINDOW,
            title="No decided fire in the horizon",
            summary="Every evaluated fire is still inside its grace window.",
            component=component, detail=detail,
        )

    latest = decided[-1]
    detail = {**detail, **{k: v for k, v in latest.detail.items() if k in ("run_id", "status")}}
    if latest.verdict in (VERDICT_MISSING, VERDICT_STALE):
        wording = "never started" if latest.verdict == VERDICT_MISSING else "is wedged"
        return Observation(
            subject_key=subject_key, verdict=latest.verdict,
            title=f"Scheduled workflow is not producing runs: {expectation.subject}",
            summary=(
                f"The most recent decided fire of {expectation.run_source} "
                f"({detail['latest_decided_fire']}) {wording}. "
                f"{len(missed)} of {len(ordered)} evaluated fires in the horizon are "
                f"unaccounted for: {', '.join(missed) or 'none'}. This incident tracks "
                f"the current health of the schedule and closes when a later fire "
                f"succeeds."
            ),
            component=component, detail=detail,
            incident_code=(
                INCIDENT_SCHEDULED_RUN_MISSING if latest.verdict == VERDICT_MISSING
                else INCIDENT_SCHEDULED_RUN_STALE
            ),
        )
    return Observation(
        subject_key=subject_key, verdict=latest.verdict,
        title=latest.title, summary=latest.summary,
        component=component, detail=detail,
    )


def load_open_fingerprints(conn, subject_key: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_incident_fingerprints FROM ops_control.watchdog_observation "
            "WHERE subject_key = %s",
            (subject_key,),
        )
        row = cur.fetchone()
    if not row:
        return []
    value = row["open_incident_fingerprints"]
    return [str(item) for item in (value or []) if item]


def record_observation(conn, observation: Observation, *, now_utc: datetime,
                       alerted: bool, fingerprint: str | None = None) -> str | None:
    """Upsert the latest verdict. Returns the previous verdict, if any.

    `fingerprint` is the incident this observation just opened or refreshed. It
    accumulates on the subject so recovery can later resolve exactly those
    incidents and nothing else.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, open_incident_fingerprints FROM ops_control.watchdog_observation "
            "WHERE subject_key = %s",
            (observation.subject_key,),
        )
        row = cur.fetchone()
        previous = str(row["verdict"]) if row else None
        existing = [str(item) for item in ((row or {}).get("open_incident_fingerprints") or [])]
        merged = existing + ([fingerprint] if fingerprint and fingerprint not in existing else [])
        cur.execute(
            """
            INSERT INTO ops_control.watchdog_observation AS w
                (subject_key, watchdog_name, verdict, detail, open_incident_fingerprints,
                 eligible, eligible_since,
                 first_observed_at, last_observed_at, last_alerted_at,
                 observation_count, updated_at)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, 1, now())
            ON CONFLICT (subject_key) DO UPDATE
               SET watchdog_name = EXCLUDED.watchdog_name,
                   verdict = EXCLUDED.verdict,
                   detail = EXCLUDED.detail,
                   open_incident_fingerprints = EXCLUDED.open_incident_fingerprints,
                   eligible = EXCLUDED.eligible,
                   eligible_since = EXCLUDED.eligible_since,
                   last_observed_at = EXCLUDED.last_observed_at,
                   last_alerted_at = COALESCE(EXCLUDED.last_alerted_at, w.last_alerted_at),
                   observation_count = w.observation_count + 1,
                   updated_at = now()
            """,
            (
                observation.subject_key, observation.watchdog_name, observation.verdict,
                json.dumps(dict(observation.detail), default=str),
                json.dumps(merged),
                observation.eligible, observation.eligible_since,
                now_utc, now_utc, now_utc if alerted else None,
            ),
        )
    conn.commit()
    return previous


def resolve_subject_incidents(
    conn, *, subject_key: str, incident_codes: Sequence[str], now_utc: datetime,
    keep: Sequence[str] = (),
) -> int:
    """Close the incidents this exact subject has open. Nothing else.

    Resolving by `(component, incident_code)` was wrong in a way that mattered:
    `suspected_bug_incidents` has no subject column, so every mountpoint shares a
    component, and `/` recovering closed the still-critical incident for
    `/var/lib/docker`. Identity is the fingerprint, so the fingerprints this
    subject opened are what it may close.

    `keep` is the escalation case: when a filesystem goes WARNING → CRITICAL the
    critical incident stays open while the superseded warning is resolved.
    """
    fingerprints = [item for item in load_open_fingerprints(conn, subject_key) if item not in keep]
    if not fingerprints:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE suspected_bug_incidents
               SET state = %s, resolved_at = %s, updated_at = now()
             WHERE fingerprint = ANY(%s)
               AND incident_code = ANY(%s)
               AND state = %s
            """,
            (STATE_RESOLVED, now_utc, fingerprints, list(incident_codes), STATE_OPEN),
        )
        closed = cur.rowcount
        cur.execute(
            "UPDATE ops_control.watchdog_observation "
            "   SET open_incident_fingerprints = %s::jsonb, updated_at = now() "
            " WHERE subject_key = %s",
            (json.dumps(list(keep)), subject_key),
        )
    conn.commit()
    return int(closed)


# ------------------------------------------------------------------- scan


def scan(*, conn=None, config: WatchdogConfig | None = None,
         now_utc: datetime | None = None, alert: bool = True) -> dict[str, Any]:
    """One full watchdog pass. Idempotent: repeated scans re-observe, never re-storm."""
    config = config or load_config()
    now_utc = now_utc or utcnow()
    owns_conn = conn is None
    conn = conn or platform_db_conn()
    observations: list[Observation] = []
    alerts: list[dict[str, Any]] = []
    recoveries: list[str] = []

    try:
        for item in load_workflow_a_subjects(conn, now_utc=now_utc):
            observations.append(
                evaluate_schedule_subject(
                    row=item["row"], history=item["history"], now_utc=now_utc, config=config,
                    eligible_since_ts=item.get("eligible_since"),
                )
            )

        for expectation in config.systemd_expectations:
            # Every fire in the horizon is evaluated, then folded into one root
            # subject. Only the root reaches the incident layer.
            occurrences = []
            for fire_utc in expected_systemd_fires(expectation, now_utc=now_utc):
                run = load_systemd_run(
                    conn, run_source=expectation.run_source, fire_utc=fire_utc,
                    grace_minutes=expectation.completion_grace_minutes,
                )
                occurrences.append(
                    evaluate_systemd_subject(
                        expectation=expectation, fire_utc=fire_utc, run=run, now_utc=now_utc
                    )
                )
            observations.append(
                fold_systemd_occurrences(
                    expectation=expectation, occurrences=occurrences, now_utc=now_utc
                )
            )

        for expectation in config.heartbeats:
            observations.append(
                evaluate_heartbeat_subject(
                    expectation=expectation,
                    last_beat_at=load_heartbeat(conn, component=expectation.heartbeat_component),
                    now_utc=now_utc,
                )
            )

        if config.alert_delivery is not None:
            observations.append(
                evaluate_alert_delivery_subject(
                    expectation=config.alert_delivery,
                    state=load_alert_delivery_state(
                        conn, expectation=config.alert_delivery, now_utc=now_utc
                    ),
                    now_utc=now_utc,
                )
            )

        for observation in observations:
            should_alert = alert and observation.alerting
            if not alert:
                # `--dry-run` must be exactly that. Persisting observations would
                # mutate production state and would also silently rewrite the
                # recovery baseline that the next real scan compares against.
                continue
            if should_alert:
                result = report_operational_failure(
                    incident_code=observation.incident_code or INCIDENT_SCHEDULED_RUN_MISSING,
                    title=observation.title,
                    summary=observation.summary,
                    component=observation.component,
                    severity="error",
                    client_code=observation.client_code,
                    client_id=observation.client_id,
                    dataset_name=observation.dataset_name,
                    subject_type="watchdog_subject",
                    subject_key=observation.subject_key,
                    suggested_action=(
                        "Confirm the owning systemd timer/service is enabled and firing, "
                        "then recover the missed or wedged execution. This incident closes "
                        "automatically once the watchdog observes a healthy execution."
                    ),
                    details=dict(observation.detail),
                    extra_identity={"verdict": observation.verdict},
                    occurred_at=now_utc,
                    now=now_utc,
                )
                fingerprint = getattr(result, "fingerprint", None)
                # A MISSING subject that escalates to STALE (or back) changes
                # fingerprint. Close the superseded one; keep the current.
                resolve_subject_incidents(
                    conn, subject_key=observation.subject_key,
                    incident_codes=WATCHDOG_INCIDENT_CODES,
                    now_utc=now_utc, keep=(fingerprint,) if fingerprint else (),
                )
                record_observation(
                    conn, observation, now_utc=now_utc, alerted=True, fingerprint=fingerprint,
                )
                alerts.append(
                    {
                        "subject_key": observation.subject_key,
                        "verdict": observation.verdict,
                        "email_enqueued": bool(getattr(result, "email_enqueued", False)),
                        "suppression_reason": getattr(result, "suppression_reason", None),
                        "incident_id": getattr(result, "incident_id", None),
                    }
                )
                continue

            previous = record_observation(
                conn, observation, now_utc=now_utc, alerted=False
            )
            # Only a genuine healthy *execution* resolves. Disabling a broken
            # schedule, or a fire that predates eligibility, must not silently
            # close the incident it left open.
            if (
                observation.verdict not in NON_EXPECTING_VERDICTS
                and observation.verdict in HEALTHY_VERDICTS
                and previous is not None
                and previous not in HEALTHY_VERDICTS
            ):
                closed = resolve_subject_incidents(
                    conn, subject_key=observation.subject_key,
                    incident_codes=WATCHDOG_INCIDENT_CODES, now_utc=now_utc,
                )
                if closed:
                    recoveries.append(observation.subject_key)
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass

    readiness = alerting_readiness()
    counts: dict[str, int] = {}
    for observation in observations:
        counts[observation.verdict] = counts.get(observation.verdict, 0) + 1
    return {
        "schema": "log-platform-execution-watchdog/v1",
        "scanned_at": now_utc.isoformat(),
        "subjects": len(observations),
        "verdicts": counts,
        "alerts": alerts,
        "recovered": recoveries,
        "alerting_ready": readiness.ready,
        "alerting_problems": readiness.problems,
        "operator_action_required": bool(alerts) or not readiness.ready,
        "observations": [observation.as_dict() for observation in observations],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent missing-run / stuck-run watchdog")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Evaluate and print verdicts without creating incidents (default: alert)",
    )
    parser.add_argument("--expectations", type=Path, default=None)
    parser.add_argument(
        "--fail-on-alert", action="store_true",
        help="Exit non-zero when the scan produced an alerting verdict",
    )
    args = parser.parse_args(argv)

    report = scan(config=load_config(args.expectations), alert=not args.dry_run)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.fail_on_alert and report["operator_action_required"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
