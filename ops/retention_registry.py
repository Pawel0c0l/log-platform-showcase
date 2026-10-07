#!/usr/bin/env python3
"""THE authoritative retention policy registry for the whole platform.

WHY THIS MODULE EXISTS.
    Before it, "how long do we keep X?" had as many answers as there were
    cleanup implementations: `--days 60` in a systemd unit, `180` in
    `api/platform_prune.py`, `3 days` in `ops/database_export_worker.py`, `14`
    in `ops/backup_retention.py`, per-client rows in
    `workflow_a_control.client_table_retention`, `10/60 days` in the Worker's
    `capability_ttl.js` — and, for most persisted relations, no answer at all.
    An operator could not read the effective configuration from one place, and
    nothing could tell that a new migration had introduced a store nobody
    governs.

    This module is that one place. Every governed store has exactly one entry
    here; every entry names its own age basis, deletion mechanism, responsible
    cleanup job and lifecycle mode. Cleanup implementations read their cutoff
    from here rather than carrying a constant.

THE OWNER RULE.
    Persisted platform data must not remain stored beyond **13 calendar
    months** unless a shorter lifecycle already removes it earlier, or unless an
    explicit owner-approved override is recorded on the entry.

    13 CALENDAR MONTHS, not 395 days. The ceiling is computed with calendar
    arithmetic (`subtract_calendar_months`), so the cutoff for 2026-03-31 is
    2025-02-28 and not "whatever 395 days happens to land on". Day-based
    policies are still allowed — they are just validated against the SHORTEST
    span 13 calendar months can have, so no day count can ever silently exceed
    the ceiling.

    The ceiling is a MAXIMUM. Nothing here lengthens an existing shorter
    lifetime to match it: the Eco weekly capability still lives 10 days, the
    monthly one 60, a Database Explorer export 3, a browser session minutes.

NO SILENT EXCEPTIONS.
    A store that cannot follow the ceiling is not quietly dropped from the
    registry. It gets an entry with `Status.BLOCKED_OWNER_DECISION` and a
    `blocker` string, and `validate()` reports it every time. The three modes
    that legitimately have no age-based cutoff — `Mode.LIFECYCLE_BOUND` (the row
    dies with the entity it configures), `Mode.NOT_APPLICABLE` (nothing here has
    a meaningful age) and `Mode.OWNER_EXEMPT` (data with a real age that the
    owner has explicitly decided not to age out) — must each carry a written
    rationale, and validation refuses an entry that omits it.

THE ONE APPROVED EXEMPTION.
    `Mode.OWNER_EXEMPT` is the only way a store holding real, age-bearing
    business data may sit outside the ceiling, and it is deliberately expensive
    to declare: it requires `Retention.none()`, a rationale, and an attributed
    `OwnerExemption` (who approved it, when, and why). Exactly one entry carries
    it today — the Workflow B GPS assignment log — and `validate()` refuses an
    exemption that is unattributed, as well as an `OwnerExemption` attached to
    any other mode. An exemption is therefore visible, attributable and
    countable; it is never the absence of an entry.

    An exempt store is GOVERNED, not unmanaged. It keeps its registry entry, it
    keeps its relation mapping, it appears in the operator view with an explicit
    status, and the coverage check still fails for any relation that has no
    entry at all. What it does not have is an age-based deletion anchor, and no
    sweep may invent one for it.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# THE global policy. Stated exactly once, in calendar months.
# ---------------------------------------------------------------------------

#: The owner-approved global maximum retention for platform-controlled data.
#: Every other number in this repository that expresses "the ceiling" must be
#: derived from this constant, never re-typed.
HARD_RETENTION_MONTHS = 13

#: The identifier the global default is referenced by in logs, metrics and
#: operator output. Cleanup implementations emit it so a retention decision can
#: always be traced back to the policy that produced it.
GLOBAL_POLICY_ID = "platform.global_hard_retention"


class Backend(str, Enum):
    """Where the bytes physically live."""

    PLATFORM_POSTGRES = "platform_postgres"
    CLIENT_BUSINESS_POSTGRES = "client_business_postgres"
    MINIO = "minio"
    CLOUDFLARE_D1 = "cloudflare_d1"
    CLOUDFLARE_R2 = "cloudflare_r2"
    FILESYSTEM = "filesystem"
    HOST_MANAGED = "host_managed"


class Mode(str, Enum):
    """What the lifecycle actually does when the cutoff is reached."""

    #: Rows/objects are physically removed.
    HARD_DELETE = "hard_delete"
    #: Rows are removed, but only as dead state that already authorises nothing
    #: (expired sessions). Distinguished from HARD_DELETE because the trigger is
    #: the record's own expiry, not the retention horizon.
    COMPACTION = "compaction"
    #: Secret material is destroyed early; non-secret audit identity survives
    #: until the hard ceiling.
    SECRET_MINIMISATION = "secret_minimisation"
    #: The record stops being usable and keeps only enough identity to answer
    #: "this existed and expired", until the hard ceiling removes it.
    TOMBSTONE_RETENTION = "tombstone_retention"
    #: Access is withdrawn first (availability/expiry flag), physical deletion
    #: follows on the ordinary horizon.
    LOGICAL_EXPIRY_THEN_HARD_DELETE = "logical_expiry_then_hard_delete"
    #: No age-based retention: the row exists exactly as long as the entity it
    #: configures, and dies with it (usually ON DELETE CASCADE). Requires a
    #: `rationale`.
    LIFECYCLE_BOUND = "lifecycle_bound"
    #: The store holds no persisted platform/business data whose age is
    #: meaningful (a singleton identity row, a migration ledger). Requires a
    #: `rationale`.
    NOT_APPLICABLE = "not_applicable"
    #: The data DOES have a meaningful age, and the owner has explicitly decided
    #: it is not to be aged out. Distinct from NOT_APPLICABLE, which claims the
    #: age is meaningless, and from BLOCKED_OWNER_DECISION, which means nobody
    #: has decided yet. Requires a `rationale` AND an attributed
    #: `OwnerExemption`; nothing may be deleted from such a store because of age.
    OWNER_EXEMPT = "owner_exempt"


class Status(str, Enum):
    ACTIVE = "active"
    #: Still present, no longer written, scheduled to disappear.
    DEPRECATED = "deprecated"
    #: Declared in the repository but never created/used in production.
    UNSUPPORTED = "unsupported"
    #: The ceiling cannot be enforced with the present architecture. An owner
    #: decision is required; `blocker` says what for.
    BLOCKED_OWNER_DECISION = "blocked_owner_decision"


class Unit(str, Enum):
    MONTHS = "months"
    DAYS = "days"
    HOURS = "hours"
    #: Sub-hour lifetimes. Present because at least one governed store really is
    #: measured in minutes — a browser session authorises for 30 of them — and
    #: rounding that up to "12 hours" made the central surface say something the
    #: Worker does not do.
    MINUTES = "minutes"
    NONE = "none"


@dataclass(frozen=True)
class Retention:
    """How long this store may keep data.

    `Retention.default()` carries NO number: it *is* the global ceiling, and
    resolves through `HARD_RETENTION_MONTHS` at use time. That is what keeps
    "13" from being copied into 40 entries.
    """

    unit: Unit
    value: int | None = None
    #: True only for `default()`. Machine-detectable so an operator can see at a
    #: glance which stores inherit the global rule and which state their own.
    is_global_default: bool = False

    @staticmethod
    def default() -> "Retention":
        return Retention(unit=Unit.MONTHS, value=None, is_global_default=True)

    @staticmethod
    def months(value: int) -> "Retention":
        return Retention(unit=Unit.MONTHS, value=int(value))

    @staticmethod
    def days(value: int) -> "Retention":
        return Retention(unit=Unit.DAYS, value=int(value))

    @staticmethod
    def hours(value: int) -> "Retention":
        return Retention(unit=Unit.HOURS, value=int(value))

    @staticmethod
    def minutes(value: int) -> "Retention":
        return Retention(unit=Unit.MINUTES, value=int(value))

    @staticmethod
    def none() -> "Retention":
        """No age-based retention.

        Only legal with LIFECYCLE_BOUND, NOT_APPLICABLE or OWNER_EXEMPT — and
        the mode, not this value, is what says WHY there is no number.
        """
        return Retention(unit=Unit.NONE, value=None)

    @property
    def effective_months(self) -> int | None:
        if self.unit is not Unit.MONTHS:
            return None
        return HARD_RETENTION_MONTHS if self.is_global_default else self.value

    def describe(self) -> str:
        if self.unit is Unit.NONE:
            # Deliberately mode-agnostic: the entry's mode says whether "none"
            # means lifecycle-bound, not-applicable or owner-exempt, and this
            # string must not assert one of them for the other two.
            return "none (no age-based retention)"
        if self.is_global_default:
            return f"{HARD_RETENTION_MONTHS} calendar months (global default)"
        return f"{self.value} {self.unit.value}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit.value,
            "value": self.effective_months if self.unit is Unit.MONTHS else self.value,
            "is_global_default": self.is_global_default,
            "description": self.describe(),
        }


@dataclass(frozen=True)
class OwnerOverride:
    """An explicit, attributed decision to keep something LONGER than the ceiling.

    An override that is merely *shorter* is not an override — it is an ordinary
    shorter policy and needs nothing recorded here. This type exists only so a
    longer-than-ceiling lifetime is impossible to introduce anonymously:
    validation refuses one without approver, date and reason.
    """

    months: int
    approved_by: str
    approved_on: str  # ISO date
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "months": self.months,
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OwnerExemption:
    """An explicit, attributed decision that a store has NO age-based retention.

    The counterpart of `OwnerOverride`. An override says "keep it longer than
    the ceiling, for N months"; an exemption says "do not delete it because of
    age at all". Both exist for the same reason: a store that escapes the
    ceiling must never be able to do so anonymously. Validation refuses an
    exemption without approver, date and reason, and refuses one attached to any
    mode other than `Mode.OWNER_EXEMPT`, so a second exemption cannot appear by
    accident or by copy-paste.
    """

    approved_by: str
    approved_on: str  # ISO date
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EffectiveMechanism:
    """What ACTUALLY removes (or fails to remove) this store's data, in practice.

    WHY A POLICY IS NOT ALWAYS THE WHOLE TRUTH. Two honest gaps kept appearing
    between what an entry above declares and what a host really does:

      * something OUTSIDE this repository removes the data sooner. The stage-2
        scratch directory lives in `/tmp`, which `systemd-tmpfiles-clean.timer`
        empties after 30 days — well inside the ceiling, but the registry said
        only "13 calendar months" and the operator had no way to learn where the
        real, shorter lifetime came from;

      * a mechanism the catalogue lists as retention work is configured NOT to
        delete. The per-client Workflow A purge is scheduled and enabled, and
        its `ExecStart` passes `dry_run:true`, so a shorter per-client policy
        can be *configured* while nothing shortens anything physically.

    Both are recorded here rather than by editing the policy's own retention.
    Neither is a second retention policy: `enforcing=False` states that this
    mechanism deletes nothing, and `approximate_max` is only legal on an
    enforcing mechanism whose effect is genuinely SHORTER than the entry's own
    horizon. Nothing here may ever lengthen a lifetime — that is what
    `OwnerOverride` is for, and it needs an owner.
    """

    #: What performs it, named the way an operator would find it.
    mechanism: str
    #: Schedule id in `ops/schedule_catalog.py`, when a recurring mechanism runs
    #: it. `ops.schedule_catalog.validate()` fails if it names no such schedule,
    #: which is what keeps this from becoming an uncheckable string.
    schedule_id: str | None
    #: True when this mechanism physically removes data today. False means it is
    #: configured, scheduled and deleting nothing.
    enforcing: bool
    #: The effective maximum age this mechanism imposes, when it is genuinely
    #: shorter than the policy's own horizon. `None` when it imposes none.
    approximate_max: timedelta | None = None
    #: True when the mechanism is owned by the host/OS rather than by this
    #: repository, so an operator knows the lever is not in `ops/systemd/`.
    host_managed: bool = False
    #: Required. An effective mechanism nobody explained is a rumour.
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "schedule_id": self.schedule_id,
            "enforcing": self.enforcing,
            "approximate_max_seconds": (
                int(self.approximate_max.total_seconds())
                if self.approximate_max is not None else None
            ),
            "approximate_max": (
                _humanise(self.approximate_max)
                if self.approximate_max is not None else None
            ),
            "host_managed": self.host_managed,
            "note": self.note,
        }


@dataclass(frozen=True)
class RetentionPolicy:
    policy_id: str
    #: Human name of the governed store, exactly as an operator would name it.
    store: str
    backend: Backend
    owner_domain: str
    retention: Retention
    #: The column / object attribute the age is measured from. `None` only for
    #: LIFECYCLE_BOUND / NOT_APPLICABLE.
    age_basis: str | None
    mode: Mode
    #: The command or module that actually performs the cleanup. `None` only for
    #: LIFECYCLE_BOUND / NOT_APPLICABLE / BLOCKED_OWNER_DECISION.
    cleanup_job: str | None
    #: Logical schedule id in `ops/schedule_catalog.py` that runs `cleanup_job`.
    schedule_id: str | None = None
    status: Status = Status.ACTIVE
    #: A lifecycle that already removes (or withdraws) the data before the
    #: ceiling is reached. Free text, e.g. "capability expiry 10 days".
    shorter_ttl: str | None = None
    contains_personal_data: bool = False
    #: Required for LIFECYCLE_BOUND / NOT_APPLICABLE.
    rationale: str | None = None
    #: Required for BLOCKED_OWNER_DECISION.
    blocker: str | None = None
    override: OwnerOverride | None = None
    #: Required for, and legal only on, `Mode.OWNER_EXEMPT`.
    exemption: OwnerExemption | None = None
    #: The store enforces its own retention continuously and this repository
    #: runs no sweep for it (journald). Such a policy still needs a lead — for
    #: the store's own rotation granularity — declared as `self_enforcing_lead`.
    self_enforcing: bool = False
    self_enforcing_lead: timedelta | None = None
    #: Set only to override the backup-topology answer for one policy. `None`
    #: means "ask the topology", which is what every entry should normally do.
    in_backup_set_override: bool | None = None
    #: What actually happens on the host, where that differs from the entry
    #: above: a shorter OS-owned cleanup, or a scheduled mechanism configured
    #: not to delete. See `EffectiveMechanism`.
    effective_mechanisms: tuple["EffectiveMechanism", ...] = ()
    #: Rows/objects whose parents govern their lifetime (informational).
    depends_on: tuple[str, ...] = ()
    notes: str | None = None

    # -- derived -----------------------------------------------------------
    #: The modes that legitimately have no age-based cutoff. A sweep must never
    #: plan a deletion for a policy in one of them.
    NO_AGE_MODES = (Mode.LIFECYCLE_BOUND, Mode.NOT_APPLICABLE, Mode.OWNER_EXEMPT)

    @property
    def is_age_based(self) -> bool:
        return self.mode not in RetentionPolicy.NO_AGE_MODES

    @property
    def is_owner_exempt(self) -> bool:
        """Explicitly, attributably exempt from age-based retention."""
        return self.mode is Mode.OWNER_EXEMPT

    def governance_class(self) -> str:
        """WHICH KIND of governed this entry is — the operator's first question.

        Four answers are possible for any persisted store, and only the fourth
        is a coverage failure:

          * `CEILING`        — governed at the 13-calendar-month maximum;
          * `SHORTER`        — governed by its own, shorter lifetime;
          * `OWNER_EXEMPT`   — explicitly, attributably exempt from age-based
                               retention by owner decision;
          * `LIFECYCLE`      — no age of its own: the row dies with its parent,
                               or nothing here has a meaningful age.

        A store with NO entry at all is the fourth kind — UNREGISTERED — and it
        is not representable here precisely because it has no policy object.
        `ungoverned()` is what reports it, and it is the only one that fails
        coverage.
        """
        if self.mode is Mode.OWNER_EXEMPT:
            return "OWNER_EXEMPT"
        if not self.is_age_based:
            return "LIFECYCLE"
        if self.override is not None:
            return "OWNER_OVERRIDE"
        return "CEILING" if self.is_ceiling_horizon else "SHORTER"

    @property
    def horizon_months(self) -> int | None:
        """The policy's own horizon in months, or `None` if it is day/hour based."""
        if self.override is not None:
            return self.override.months
        if self.retention.unit is Unit.MONTHS:
            return self.retention.effective_months
        return None

    @property
    def is_ceiling_horizon(self) -> bool:
        """True when this policy's horizon IS the hard deadline.

        Only these need a deadline look-ahead. A policy with a genuinely shorter
        horizon — 60-day logs, 3-day exports — already deletes long before the
        ceiling, and shortening it further would be lengthening nothing and
        losing data early for no reason. `validate()` proves each shorter policy
        still clears the ceiling once its own lead is added.
        """
        return self.is_age_based and self.horizon_months is not None

    @property
    def in_backup_set(self) -> bool:
        if self.in_backup_set_override is not None:
            return self.in_backup_set_override
        return any(topology.covers(self.backend) for topology in BACKUP_SETS)

    def maintenance_cycle(self) -> MaintenanceCycle | None:
        if self.schedule_id is None:
            return None
        return MAINTENANCE_CYCLES.get(self.schedule_id)

    def enforcement_lead(self) -> timedelta:
        """How far AHEAD of the deadline this policy must delete.

        Composed from declared descriptors, never from a constant at a call
        site: the responsible maintenance cycle, plus the backup shadow when the
        store is actually inside a backup set.
        """
        lead = timedelta(0)
        if self.self_enforcing:
            lead += self.self_enforcing_lead or timedelta(0)
        else:
            cycle = self.maintenance_cycle()
            if cycle is not None:
                lead += cycle.guaranteed_interval
        if self.in_backup_set:
            for topology in BACKUP_SETS:
                if topology.covers(self.backend):
                    lead += topology.shadow
        return lead

    def enforcement_cutoff(self, now: datetime | None = None) -> datetime | None:
        """THE cutoff a cleanup implementation must use.

        For a ceiling-horizon policy this is `now + lead - 13 calendar months`,
        so that everything whose deadline falls before the next guaranteed
        cleanup opportunity — and before the last backup copy could expire — is
        removed on THIS pass. For an explicitly shorter policy it is the policy's
        own cutoff: it already deletes far inside the ceiling, and moving it
        earlier would destroy data the owner did not ask to lose.
        """
        moment = _utc(now)
        if not self.is_age_based:
            return None
        if not self.is_ceiling_horizon:
            return self.cutoff(moment)
        months = self.horizon_months or HARD_RETENTION_MONTHS
        return subtract_calendar_months(moment + self.enforcement_lead(), months)

    def deadline_of(self, created: datetime, ) -> datetime:
        """The hard deadline of one record: creation plus the policy horizon."""
        months = self.horizon_months
        if months is None:
            raise ValueError(f"{self.policy_id} has no month-based horizon")
        return add_calendar_months(created, months)

    def cutoff(self, now: datetime | None = None) -> datetime | None:
        """The NOMINAL horizon: the instant a record's deadline falls on.

        This is what the policy says; `enforcement_cutoff` is what a sweep must
        actually use. `None` for policies with no age-based retention.
        """
        now = _utc(now)
        if self.override is not None:
            return subtract_calendar_months(now, self.override.months)
        unit = self.retention.unit
        if unit is Unit.NONE:
            return None
        if unit is Unit.MONTHS:
            return subtract_calendar_months(now, self.retention.effective_months)
        if unit is Unit.DAYS:
            return now - timedelta(days=int(self.retention.value or 0))
        if unit is Unit.HOURS:
            return now - timedelta(hours=int(self.retention.value or 0))
        if unit is Unit.MINUTES:
            return now - timedelta(minutes=int(self.retention.value or 0))
        raise AssertionError(f"unhandled unit {unit!r}")

    @property
    def effective_max(self) -> timedelta | None:
        """The SHORTEST maximum age any ENFORCING effective mechanism imposes.

        `None` when nothing outside this policy shortens the lifetime, which is
        the ordinary case. A value here is the number an operator should read as
        "how long the data really lives", and it is always shorter than the
        entry's own horizon — `validate()` refuses a longer one.
        """
        spans = [
            item.approximate_max for item in self.effective_mechanisms
            if item.enforcing and item.approximate_max is not None
        ]
        return min(spans) if spans else None

    @property
    def non_enforcing_mechanisms(self) -> tuple["EffectiveMechanism", ...]:
        """Declared mechanisms that are scheduled but delete nothing today.

        The operator distinction the owner asked for: a shorter policy that is
        CONFIGURED is not a shorter physical retention that is ENFORCED, and the
        registry must never let the first read as the second.
        """
        return tuple(item for item in self.effective_mechanisms if not item.enforcing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "store": self.store,
            "backend": self.backend.value,
            "owner_domain": self.owner_domain,
            "retention": self.retention.as_dict(),
            "age_basis": self.age_basis,
            "mode": self.mode.value,
            "cleanup_job": self.cleanup_job,
            "schedule_id": self.schedule_id,
            "status": self.status.value,
            "shorter_ttl": self.shorter_ttl,
            "contains_personal_data": self.contains_personal_data,
            "rationale": self.rationale,
            "blocker": self.blocker,
            "override": self.override.as_dict() if self.override else None,
            "exemption": self.exemption.as_dict() if self.exemption else None,
            "governance": self.governance_class(),
            "is_age_based": self.is_age_based,
            "self_enforcing": self.self_enforcing,
            "maintenance_cycle": (
                self.maintenance_cycle().as_dict() if self.maintenance_cycle() else None
            ),
            "in_backup_set": self.in_backup_set,
            "backup_shadow_seconds": (
                int(PLATFORM_BACKUP_SET.shadow.total_seconds())
                if self.in_backup_set else 0
            ),
            "enforcement_lead_seconds": int(self.enforcement_lead().total_seconds()),
            "enforcement_lead": _humanise(self.enforcement_lead()),
            "is_ceiling_horizon": self.is_ceiling_horizon,
            "effective_mechanisms": [
                item.as_dict() for item in self.effective_mechanisms
            ],
            "effective_max_seconds": (
                int(self.effective_max.total_seconds())
                if self.effective_max is not None else None
            ),
            "effective_max": (
                _humanise(self.effective_max) if self.effective_max is not None else None
            ),
            "has_non_enforcing_mechanism": bool(self.non_enforcing_mechanisms),
            "depends_on": list(self.depends_on),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Calendar-month arithmetic
# ---------------------------------------------------------------------------

def subtract_calendar_months(moment: datetime, months: int) -> datetime:
    """`moment` minus `months` CALENDAR months, clamped at the month end.

    Deterministic boundary behaviour, which is the whole reason this exists
    instead of a day count:

      * 2026-03-31 − 13 → 2025-02-28   (February has no 31st, and 2025 is not a
        leap year, so the clamp lands on the 28th);
      * 2028-03-29 − 13 → 2027-02-28;
      * 2027-03-29 − 13 → 2026-02-28;
      * 2025-03-29 − 13 → 2024-02-29   (2024 IS a leap year, so no clamp);
      * 2026-01-31 − 13 → 2024-12-31   (no clamp needed).

    Time of day, microseconds and tzinfo are preserved exactly; only the
    calendar date moves. The result of clamping is never "rolled over" into the
    next month — 31 April is 30 April, not 1 May.
    """
    if not isinstance(months, int) or months < 0:
        raise ValueError("months must be a non-negative int")
    total = (moment.year * 12 + (moment.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def add_calendar_months(moment: datetime, months: int) -> datetime:
    """`moment` PLUS `months` calendar months, clamped at the month end.

    The exact mirror of `subtract_calendar_months`, and the operation a test
    needs to state "this record's hard deadline is X". Clamping keeps it a
    proper inverse at month ends in the only direction that matters for
    retention: 31 January plus one month is 28 February, and 28 February minus
    one month is 28 January — the round trip is never LATER than where it
    started, so a deadline computed this way is never optimistic.
    """
    if not isinstance(months, int) or months < 0:
        raise ValueError("months must be a non-negative int")
    total = (moment.year * 12 + (moment.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def hard_retention_cutoff(now: datetime | None = None) -> datetime:
    """THE global cutoff: everything strictly older than this is eligible."""
    return subtract_calendar_months(_utc(now), HARD_RETENTION_MONTHS)


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("naive datetimes are refused; pass an aware UTC datetime")
    return now.astimezone(timezone.utc)


def minimum_span_days(months: int = HARD_RETENTION_MONTHS) -> int:
    """The FEWEST days `months` calendar months can span.

    Used to decide whether a day-based policy is provably within the ceiling.
    A day count is compliant only if it is <= this number, because a longer one
    would exceed the ceiling for at least one start date in the calendar — and
    "compliant most of the year" is not compliance.

    Computed by sweeping every (month, day) start across a full leap cycle, so
    the answer is derived rather than asserted.
    """
    shortest = None
    for year in (2024, 2025, 2026, 2027):  # one complete leap cycle
        for month in range(1, 13):
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                end = datetime(year, month, day, tzinfo=timezone.utc)
                start = subtract_calendar_months(end, months)
                span = (end - start).days
                if shortest is None or span < shortest:
                    shortest = span
    assert shortest is not None
    return shortest


# ---------------------------------------------------------------------------
# Shared mechanism descriptors — why a periodic sweep still meets a hard deadline
# ---------------------------------------------------------------------------
#
# THE PROBLEM A CUTOFF ALONE DOES NOT SOLVE. A sweep that deletes "everything
# older than 13 months" and runs weekly leaves a record alive for up to another
# week past its deadline. Thirteen months plus the next maintenance run is not
# thirteen months, and the owner rule is a MAXIMUM AGE, not a maximum age plus
# operational latency.
#
# THE FIX. Delete EARLY, never late. At a sweep running at `now`, remove every
# record whose deadline falls before the NEXT guaranteed sweep — that is, use
#
#     cutoff = now + lead - 13 calendar months
#
# where `lead` is the longest a record could otherwise wait. Deleting a record a
# few days before its deadline is permitted by the policy (shorter retention is
# always allowed); deleting it a few days after is not.
#
# The lead is composed, never hard-coded at a call site. It is the sum of:
#
#   * the MAINTENANCE CYCLE of the schedule responsible for the policy — the
#     maximum interval between two guaranteed cleanup opportunities;
#   * the BACKUP SHADOW, for stores that appear inside a backup set: how long a
#     copy of an already-deleted record can still exist inside retained
#     archives.
#
# Both are declared here, once, as descriptors that policies reference by id.
# `ops/schedule_catalog.py` validates each maintenance cycle against the cadence
# actually written in the unit file, so a timer that is slowed down without
# updating its cycle is a drift failure rather than a silent retention breach.

@dataclass(frozen=True)
class MaintenanceCycle:
    """The longest a governed record can wait for its next cleanup opportunity.

    `guaranteed_interval` is the WORST case, not the nominal cadence: a timer
    firing every Sunday guarantees an opportunity every 7 days, and that is the
    number retention must reason with.
    """

    schedule_id: str
    guaranteed_interval: timedelta
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "guaranteed_interval_seconds": int(self.guaranteed_interval.total_seconds()),
            "guaranteed_interval": _humanise(self.guaranteed_interval),
            "rationale": self.rationale,
        }


#: Keyed by the schedule id in `ops/schedule_catalog.py`.
MAINTENANCE_CYCLES: Mapping[str, MaintenanceCycle] = {
    "platform-hard-retention": MaintenanceCycle(
        schedule_id="platform-hard-retention",
        guaranteed_interval=timedelta(days=7),
        rationale=(
            "`OnCalendar=Sun *-*-* 05:00:00`. Weekly is deliberate — the sweep "
            "touches every client database and the largest tables on the "
            "platform — and the deadline look-ahead is what makes a weekly "
            "cadence compatible with a strict maximum age."
        ),
    ),
    "log-platform-prune": MaintenanceCycle(
        schedule_id="log-platform-prune",
        guaranteed_interval=timedelta(days=1),
        rationale="`OnCalendar=*-*-* 03:30:00`, daily.",
    ),
    "backup-retention": MaintenanceCycle(
        schedule_id="backup-retention",
        guaranteed_interval=timedelta(days=1),
        rationale="`OnCalendar=*-*-* 04:15:00`, daily.",
    ),
    "log-backup": MaintenanceCycle(
        schedule_id="log-backup",
        guaranteed_interval=timedelta(days=1),
        rationale="`OnCalendar=*-*-* 03:00:00`, daily.",
    ),
    "database-export-cleanup": MaintenanceCycle(
        schedule_id="database-export-cleanup",
        guaranteed_interval=timedelta(hours=1),
        rationale="`OnCalendar=hourly`.",
    ),
    "database-export-worker": MaintenanceCycle(
        schedule_id="database-export-worker",
        guaranteed_interval=timedelta(hours=1),
        rationale=(
            "`--cleanup-interval-seconds 3600` on the continuous worker's "
            "ExecStart. A Type=simple service paces itself, so the interval is "
            "read from the flag rather than from a timer; "
            "`database-export-cleanup.timer` provides the same hourly guarantee "
            "independently, which is why the worker being down does not widen "
            "the horizon."
        ),
    ),
    "log-job@retention-purge": MaintenanceCycle(
        schedule_id="log-job@retention-purge",
        guaranteed_interval=timedelta(days=7),
        rationale="`OnCalendar=Sun *-*-* 03:30:00 UTC`, weekly.",
    ),
    "journald-retention-vacuum": MaintenanceCycle(
        schedule_id="journald-retention-vacuum",
        guaranteed_interval=timedelta(days=1),
        rationale=(
            "`OnCalendar=*-*-* 05:45:00`, daily. journald applies "
            "`MaxRetentionSec=` when it rotates or vacuums, which log traffic "
            "drives; a quiet host would otherwise stop enforcing, so the vacuum "
            "is scheduled rather than assumed."
        ),
    ),
    "systemd-tmpfiles-clean": MaintenanceCycle(
        schedule_id="systemd-tmpfiles-clean",
        guaranteed_interval=timedelta(days=1),
        rationale=(
            "`OnBootSec=15min, OnUnitActiveSec=1d` on the HOST's own "
            "`systemd-tmpfiles-clean.timer`, which this repository neither "
            "ships nor configures. It is declared because it is the mechanism "
            "that really bounds `/tmp/log-platform-stage2/cleaned` — see that "
            "policy's `effective_mechanisms` — and an effective lifecycle "
            "nobody can name a cadence for is not centrally visible."
        ),
    ),
    "eco-dashboard-maintenance": MaintenanceCycle(
        schedule_id="eco-dashboard-maintenance",
        guaranteed_interval=timedelta(days=7),
        rationale=(
            "Driven by `platform-hard-retention`, NOT by Eco mailing traffic. "
            "The Eco schedules are disabled in production for four of five "
            "clients, so business activity cannot be a retention scheduler; the "
            "weekly platform sweep calls the publisher-authenticated Worker "
            "maintenance route itself. A mailing run may additionally call it, "
            "which only ever makes the interval shorter."
        ),
    ),
}


@dataclass(frozen=True)
class BackupTopology:
    """Which stores a backup set actually contains, and for how long.

    AUDITED, NOT ASSUMED. `ops/backup.sh` runs exactly one
    `pg_dump -d $POSTGRES_DB` (the PLATFORM database) and tars the MinIO data
    directory. It does not touch the client business databases, Cloudflare D1 or
    R2, `REPORTS_DATA_DIR`, or the stage-2 scratch directory — so those stores
    carry no backup shadow at all, and pretending otherwise would shorten their
    live retention for no reason.
    """

    name: str
    covered_backends: frozenset[Backend]
    #: THE backup-set lifetime, stated once, here. `ops/backup_retention.py`
    #: READS this number rather than owning one of its own — the direction of
    #: that dependency is the whole point. It used to carry its own
    #: `DEFAULT_RETENTION_DAYS = 14` with env/CLI overrides layered on top,
    #: which meant a unit, an environment file or a typed flag could establish a
    #: backup lifetime longer than the shadow every hard-retention proof in this
    #: module is computed from. See `validate_backup_retention_days()`.
    retention_days: int
    creation_cycle: str
    expiry_cycle: str
    note: str

    @property
    def shadow(self) -> timedelta:
        """How long a copy of an already-deleted record can still exist.

        A set is eligible once it is `retention_days` old, and the sweep that
        removes it runs on its own cycle, so the worst case is the retention
        window plus one expiry cycle. The CREATION cadence deliberately does not
        appear: the newest backup that can contain a record is one taken before
        that record was deleted, whenever that happened to be.
        """
        cycle = MAINTENANCE_CYCLES[self.expiry_cycle].guaranteed_interval
        return timedelta(days=self.retention_days) + cycle

    def covers(self, backend: Backend) -> bool:
        return backend in self.covered_backends

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "covered_backends": sorted(item.value for item in self.covered_backends),
            "retention_days": self.retention_days,
            "creation_cycle": self.creation_cycle,
            "expiry_cycle": self.expiry_cycle,
            "shadow_seconds": int(self.shadow.total_seconds()),
            "shadow": _humanise(self.shadow),
            "note": self.note,
        }


PLATFORM_BACKUP_SET = BackupTopology(
    name="platform nightly set (postgres dump + MinIO tarball)",
    covered_backends=frozenset({Backend.PLATFORM_POSTGRES, Backend.MINIO}),
    retention_days=14,
    creation_cycle="log-backup",
    expiry_cycle="backup-retention",
    note=(
        "`ops/backup.sh` dumps the platform database and tars MinIO `/data`. "
        "Client business databases are NOT in any repository-controlled backup "
        "set — a separate disaster-recovery observation, and the reason their "
        "retention needs no backup lead."
    ),
)

BACKUP_SETS: tuple[BackupTopology, ...] = (PLATFORM_BACKUP_SET,)


class BackupRetentionPolicyConflict(ValueError):
    """An operational backup lifetime contradicts the central policy."""


def validate_backup_retention_days(
    value: object, *, source: str = "configuration",
    topology: BackupTopology = PLATFORM_BACKUP_SET,
) -> int:
    """THE gate every configured backup lifetime must pass. Fails closed.

    `ops/backup_retention.py` still accepts an operational override — a shorter
    window is a legitimate thing to want on a small disk — but it may no longer
    ESTABLISH a lifetime. The rule is one-directional and follows directly from
    what the backup shadow is used for:

      * SHORTER than the declared window is fine. It deletes archives earlier,
        so the shadow every enforcement lead is computed from stays an upper
        bound, and the survivor floor (three independently verified restorable
        sets, re-verified immediately before deletion) is what protects
        restorability from an over-aggressive window.

      * LONGER is refused. A 30-day window against a 14-day declared shadow
        would leave deleted records inside retained archives for sixteen days
        after the sweep believed the last copy was gone — silently invalidating
        the end-to-end proof for every store in the backup set, with nothing
        anywhere reporting a contradiction.

    `source` names where the value came from, so the refusal tells an operator
    which lever to move.
    """
    if isinstance(value, bool):
        raise BackupRetentionPolicyConflict(
            f"backup retention days from {source} must be an integer, not a boolean"
        )
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BackupRetentionPolicyConflict(
            f"backup retention days from {source} is not an integer: {value!r}"
        ) from exc
    if days < 1:
        raise BackupRetentionPolicyConflict(
            f"backup retention days from {source} must be >= 1, got {days}"
        )
    if days > topology.retention_days:
        raise BackupRetentionPolicyConflict(
            f"backup retention of {days} days from {source} exceeds the central "
            f"policy of {topology.retention_days} days declared by "
            f"{topology.name!r} in ops/retention_registry.py. The backup shadow "
            f"({_humanise(topology.shadow)}) that every hard-retention "
            f"enforcement lead is derived from would no longer bound how long a "
            f"deleted record survives inside a retained archive. Change "
            f"BackupTopology.retention_days if the policy itself is to change."
        )
    return days


#: How long journald may keep writing into ONE journal file before rotating it.
#: It matters to retention because vacuuming acts on whole files: an entry can
#: survive for as long as its file stays writable plus the retention window.
JOURNALD_MAX_FILE_SEC = timedelta(days=7)

#: How often the repository-managed vacuum unit runs, so a host quiet enough to
#: never rotate a journal still enforces the ceiling.
JOURNALD_VACUUM_CYCLE = timedelta(days=1)


def journald_max_retention(
    *, max_file_sec: timedelta = JOURNALD_MAX_FILE_SEC,
    vacuum_cycle: timedelta = JOURNALD_VACUUM_CYCLE,
) -> timedelta:
    """The `MaxRetentionSec=` journald must be configured with.

    journald cannot express calendar months — `MaxRetentionSec=` is a fixed
    duration — so a conservative fixed duration is derived from the SHORTEST
    span 13 calendar months can have. Using the shortest is what makes the
    result safe for every calendar start date; using 13*30 or "about 395 days"
    would exceed the ceiling for some of them.

    Two further deductions, because vacuuming is file-granular rather than
    entry-granular:

      * `max_file_sec` — the active file is not vacuumed, so an entry can be
        that much older than the retention window suggests;
      * `vacuum_cycle` — the enforcement itself is periodic, exactly as it is
        for every other store here.

    The result is therefore a genuine maximum age, not an average one.
    """
    ceiling = timedelta(days=minimum_span_days(HARD_RETENTION_MONTHS))
    value = ceiling - max_file_sec - vacuum_cycle
    if value <= timedelta(0):
        raise ValueError("journald retention derivation produced a non-positive duration")
    return value


def _humanise(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# THE registry
# ---------------------------------------------------------------------------

_D = Retention.default


def _p(**kwargs: Any) -> RetentionPolicy:
    return RetentionPolicy(**kwargs)


POLICIES: tuple[RetentionPolicy, ...] = (
    # -- Platform PostgreSQL: run/log/artifact core -------------------------
    _p(
        policy_id="platform_db.public.runs",
        store="logdb public.runs",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.days(60),
        age_basis="started_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (--execute --days 60)",
        schedule_id="log-platform-prune",
        shorter_ttl="platform prune horizon, 60 days",
        notes=(
            "Only terminal runs are eligible; a run still referenced by "
            "ops_control.run_reconciliation (ON DELETE RESTRICT) is retained "
            "until that evidence row itself ages out at the ceiling."
        ),
    ),
    _p(
        policy_id="platform_db.public.logs",
        store="logdb public.logs",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.days(60),
        age_basis="ts",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (--execute --days 60)",
        schedule_id="log-platform-prune",
        shorter_ttl="platform prune horizon, 60 days",
    ),
    _p(
        policy_id="platform_db.public.artifacts",
        store="logdb public.artifacts (+ the MinIO object each row owns)",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.days(60),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (--execute --days 60)",
        schedule_id="log-platform-prune",
        shorter_ttl="platform prune horizon, 60 days",
        contains_personal_data=True,
        notes=(
            "The ordinary 60-day pass excludes several reference classes "
            "(Workflow B lineage, user curation). Those exclusions are what "
            "platform_db.public.artifacts_reference_excluded governs at the "
            "ceiling; without it they were unbounded."
        ),
    ),
    _p(
        policy_id="platform_db.public.artifacts_reference_excluded",
        store=(
            "logdb public.artifacts still protected at 60 days by "
            "artifact_workflow_b / artifact_retained_reference"
        ),
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (--execute --hard-ceiling)",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "Same planner, same reference contract, same object-before-row "
            "ordering as the 60-day pass; only the cutoff and the exclusion set "
            "differ. An artifact whose bytes are still being OFFERED (available "
            "generated-report member) or whose run is RUNNING stays excluded "
            "even here — those are correctness guards, not retention."
        ),
    ),
    _p(
        policy_id="object_store.minio.platform_artifacts",
        store="MinIO bucket holding artifact objects",
        backend=Backend.MINIO,
        owner_domain="platform-core",
        retention=_D(),
        age_basis="owning artifacts.created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (objects deleted before their rows)",
        # The 60-day pass claims most objects; the ceiling pass claims the ones
        # it deliberately excludes. The WORST case is the ceiling pass, and the
        # enforcement lead has to be computed from the worst case.
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        depends_on=("platform_db.public.artifacts",),
        notes=(
            "The object has no independent age: it is deleted by whichever "
            "artifact pass claims its row, so its effective retention is the "
            "shorter of the 60-day pass and the ceiling pass."
        ),
    ),
    # -- Platform PostgreSQL: previously unbounded operational history ------
    _p(
        policy_id="platform_db.public.portal_audit_events",
        store="logdb public.portal_audit_events",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="portal",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes="Login/logout, denied access, admin change, export and row-browse audit.",
    ),
    _p(
        policy_id="platform_db.public.suspected_bug_incidents",
        store="logdb public.suspected_bug_incidents (cascades occurrences + outbox)",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=_D(),
        age_basis="last_seen_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes=(
            "Anchored on LAST seen, not first: an incident still recurring keeps "
            "its fingerprint (and therefore its duplicate-alert suppression) "
            "however old it is. Only a fingerprint silent for the whole ceiling "
            "is removed, and occurrences and outbox rows cascade with it."
        ),
    ),
    _p(
        policy_id="platform_db.public.suspected_bug_occurrences",
        store="logdb public.suspected_bug_occurrences",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=_D(),
        age_basis="occurred_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        depends_on=("platform_db.public.suspected_bug_incidents",),
        notes=(
            "Swept in its own right as well as by cascade, so a long-lived "
            "incident cannot accumulate occurrences older than the ceiling."
        ),
    ),
    _p(
        policy_id="platform_db.public.suspected_bug_email_outbox",
        store="logdb public.suspected_bug_email_outbox",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        depends_on=("platform_db.public.suspected_bug_incidents",),
        notes="Alert delivery history; carries operator recipient addresses.",
    ),
    _p(
        policy_id="platform_db.public.database_export_jobs",
        store="logdb public.database_export_jobs",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="database-explorer",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.LOGICAL_EXPIRY_THEN_HARD_DELETE,
        cleanup_job="ops.database_export_worker --cleanup-only (bytes); ops.hard_retention (row)",
        schedule_id="platform-hard-retention",
        shorter_ttl="downloadable bytes expire 3 calendar days after completion",
        contains_personal_data=True,
        notes=(
            "The 3-day policy destroys the export object and marks the row "
            "expired; the row itself was then kept forever as lifecycle history. "
            "It now ages out at the ceiling, cascading its attempt-object ledger."
        ),
    ),
    _p(
        policy_id="platform_db.public.database_export_attempt_objects",
        store="logdb public.database_export_attempt_objects",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="database-explorer",
        retention=_D(),
        age_basis="created_at (parent job_id ON DELETE CASCADE)",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        depends_on=("platform_db.public.database_export_jobs",),
        notes=(
            "A row in state 'cleanup_pending' is never removed by age: the "
            "unpublished object it names would be orphaned in MinIO. Those rows "
            "are reported as protected, not deleted."
        ),
    ),
    _p(
        policy_id="platform_db.public.portal_generated_report_instances",
        store="logdb public.portal_generated_report_instances (cascades files)",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="portal",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.LOGICAL_EXPIRY_THEN_HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        shorter_ttl="member availability withdrawn at portal_generated_report_files.expires_at",
        contains_personal_data=True,
        depends_on=("platform_db.public.artifacts",),
        notes=(
            "Zero rows in production today. Member rows cascade; the artifact a "
            "member pointed at is released by the ordinary artifact horizon "
            "once availability has been withdrawn."
        ),
    ),
    _p(
        policy_id="platform_db.ingest.imap_message",
        store="logdb ingest.imap_message (cascades ingest.raw_file)",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="fetched_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "Holds sender address and subject of every fetched report mail. "
            "raw_file has no timestamp of its own and its FK is NOT NULL "
            "ON DELETE CASCADE, so the parent message IS the retention anchor "
            "for the whole ingest record family."
        ),
    ),
    _p(
        policy_id="platform_db.ingest.raw_file",
        store="logdb ingest.raw_file",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="parent ingest.imap_message.fetched_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention (by cascade from imap_message)",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        depends_on=("platform_db.ingest.imap_message",),
        notes=(
            "raw_file.duplicate_of_id is NO ACTION, so an old original still "
            "named by a younger duplicate is skipped and reported rather than "
            "failing the batch."
        ),
    ),
    _p(
        policy_id="platform_db.ops_control.environment_identity_promotion",
        store="logdb ops_control.environment_identity_promotion",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="operations",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes="Promotion journal; append-only and previously unbounded.",
    ),
    _p(
        policy_id="platform_db.ops_control.run_reconciliation",
        store="logdb ops_control.run_reconciliation",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="operations",
        retention=_D(),
        age_basis="reconciled_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        depends_on=("platform_db.public.runs",),
        notes=(
            "Its FK to runs is ON DELETE RESTRICT, so this row is exactly what "
            "keeps a reconciled run alive past 60 days. Removing it at the "
            "ceiling is what lets the run become prunable on a later pass — the "
            "order is evidence first, subject second, never the reverse."
        ),
    ),
    _p(
        policy_id="platform_db.ops_control.watchdog_observation",
        store="logdb ops_control.watchdog_observation",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="operations",
        retention=_D(),
        age_basis="last_observed_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes=(
            "One row per watched subject, refreshed in place. A subject that "
            "stops existing leaves its last row behind forever; the ceiling "
            "removes it. A live subject is refreshed and never eligible."
        ),
    ),
    _p(
        policy_id="platform_db.workflow_a_control.client_schedule_run_history",
        store="logdb workflow_a_control.client_schedule_run_history",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="scheduled_fire_ts",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes=(
            "Cascades workflow_a_control.provider_request_log rows bound to the "
            "fire. A RUNNING row is never eligible however old — the dispatcher's "
            "single-job gate reads it."
        ),
    ),
    _p(
        policy_id="platform_db.workflow_a_control.provider_request_log",
        store="logdb workflow_a_control.provider_request_log",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-a",
        retention=Retention.days(180),
        age_basis="recorded_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="api.platform_prune (fixed 180-day horizon)",
        schedule_id="log-platform-prune",
        shorter_ttl="provider request evidence horizon, 180 days",
        notes=(
            "Deliberately longer than the 60-day log horizon and deliberately "
            "shorter than the ceiling: 180 days spans two consecutive monthly "
            "reconciliations."
        ),
    ),
    _p(
        policy_id="platform_db.workflow_a_control.client_dataset_recovery_run",
        store="logdb workflow_a_control.client_dataset_recovery_run",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
    ),
    _p(
        policy_id="platform_db.workflow_a_control.trip_delivery_lag_daily",
        store="logdb workflow_a_control.trip_delivery_lag_daily",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="trip_end_date",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes="Daily delivery-lag rollup; anchored on the business day it describes.",
    ),
    # -- Platform PostgreSQL: configuration and current-state ---------------
    _p(
        policy_id="platform_db.configuration_and_access_control",
        store=(
            "logdb configuration/authorization relations: artifact_users, "
            "artifact_roles, artifact_user_roles, artifact_role_permissions, "
            "artifact_virtual_folders, portal_clients, portal_user_clients, "
            "portal_groups, portal_group_users, portal_group_clients, "
            "portal_report_folders, portal_report_folder_users, "
            "portal_report_folder_groups, portal_database_datasets, "
            "portal_database_dataset_columns, portal_database_dataset_users, "
            "portal_database_dataset_groups, portal_database_column_sets, "
            "portal_database_saved_views, portal_user_preferences, "
            "portal_generated_report_definitions, database_export_system_folders, "
            "workflow_a_control.client_account, "
            "workflow_a_control.client_dataset_schedule, "
            "workflow_a_control.dataset_registry, "
            "workflow_a_control.table_registry, "
            "workflow_a_control.client_table_retention, "
            "workflow_b_control.report_type_registry, "
            "workflow_b_control.report_type_client_load_policy"
        ),
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.LIFECYCLE_BOUND,
        cleanup_job=None,
        contains_personal_data=True,
        rationale=(
            "These rows are the platform's CONFIGURATION, not a record of "
            "anything that happened. A user, a client, a folder, a dataset "
            "grant and a schedule exist exactly as long as the operator wants "
            "them to, and are removed by the administrative action that removes "
            "the entity — every one of them is ON DELETE CASCADE from its owning "
            "entity. Ageing them out on a clock would silently delete a live "
            "customer's access while they are still using it. They are in scope "
            "of the ceiling only in the sense that deleting the entity deletes "
            "the row: a user removed today leaves nothing behind tomorrow."
        ),
    ),
    _p(
        policy_id="platform_db.current_state_singletons",
        store=(
            "logdb current-state relations: ops_control.environment_identity, "
            "ops_control.scheduler_heartbeat, "
            "workflow_a_control.client_sync_state, "
            "workflow_a_control.client_dataset_coverage"
        ),
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="operations",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.LIFECYCLE_BOUND,
        cleanup_job=None,
        rationale=(
            "One row per live subject, rewritten in place; the row IS the "
            "current state, never a history of it. Row count is bounded by the "
            "number of components/clients, and deleting a row would destroy the "
            "platform's own identity attestation or reset a client's coverage "
            "watermark. Removing the client or component removes the row."
        ),
    ),
    _p(
        policy_id="platform_db.public.schema_migrations",
        store="logdb public.schema_migrations",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.NOT_APPLICABLE,
        cleanup_job=None,
        rationale=(
            "A ledger of applied migration filenames and checksums. It holds no "
            "business, customer or personal data, and deleting an entry would "
            "make the migration runner re-apply an applied file."
        ),
    ),
    _p(
        policy_id="platform_db.workflow_a_control.client_schedule_legacy",
        store="logdb workflow_a_control.client_schedule_legacy",
        backend=Backend.PLATFORM_POSTGRES,
        owner_domain="workflow-a",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.LIFECYCLE_BOUND,
        cleanup_job=None,
        status=Status.DEPRECATED,
        rationale=(
            "Renamed out of the way by migration 012 and never written since. It "
            "carries NO timestamp of any kind — schedule_id, client_id, enabled, "
            "timezone, cron_expression, window_preset, schedule_kind — so there is "
            "no anchor to age it by, and inventing one would be exactly the guess "
            "this registry refuses elsewhere. It is pre-dispatcher CONFIGURATION "
            "and cascades from workflow_a_control.client_account, so removing a "
            "client removes it. Dropping the relation outright is an operator "
            "decision, not a retention one."
        ),
    ),
    # -- Client business PostgreSQL ----------------------------------------
    _p(
        policy_id="client_db.workflow_a_registered_tables",
        store=(
            "every enabled client business database, for each table in "
            "jobs.api.telematics.registry.TABLES (client_trips, "
            "client_speeding_notifications, client_vehicle_daily_fuel, "
            "client_vehicle_driver_daily_fuel, eco_trip_assignments, "
            "eco_driver_weekly_stats, eco_driver_monthly_stats, "
            "eco_person_people, eco_person_driver_mappings, "
            "eco_person_trip_assignments, eco_person_weekly_stats, "
            "eco_person_monthly_stats, eco_person_weekly_email_send_log, "
            "eco_person_monthly_email_send_log)"
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="the table's registered retention_key_column",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention (ceiling); jobs.api.telematics.retention_purge (shorter per-client policy)",
        schedule_id="platform-hard-retention",
        shorter_ttl=(
            "workflow_a_control.client_table_retention.retention_days where the "
            "policy row is enabled (365 days for most, 65 for "
            "DELTA00001/client_trips) — CONFIGURED, not currently enforced; see "
            "effective_mechanisms"
        ),
        contains_personal_data=True,
        effective_mechanisms=(
            EffectiveMechanism(
                mechanism=(
                    "jobs.api.telematics.retention_purge, via "
                    "log-job@retention-purge.timer, whose ExecStart passes "
                    '{"dry_run":true}'
                ),
                schedule_id="log-job@retention-purge",
                enforcing=False,
                # Deliberately no approximate_max: a mechanism that deletes
                # nothing shortens nothing, and `validate()` refuses the
                # combination so a simulated policy can never be read as a
                # shorter physical retention.
                note=(
                    "CONFIGURED SHORTER, SIMULATED ONLY. The weekly per-client "
                    "purge is installed and enabled on the host, and it runs "
                    "dry — so `client_table_retention` rows describe a policy "
                    "nothing currently applies. In production one row is "
                    "enabled (DELTA00001 / client_trips, 65 days) and its "
                    "`last_purge_run_at` is NULL: nothing has ever been purged "
                    "under it. Read this entry's own horizon, the ceiling, as "
                    "the retention actually enforced today; the shorter number "
                    "is a configured intention. Enabling destructive per-client "
                    "purge is a separate, owner-authorized change."
                ),
            ),
        ),
        notes=(
            "The ceiling applies to EVERY registered table of every enabled "
            "client, independently of client_table_retention.enabled. That flag "
            "used to be the only thing standing between these tables and "
            "unbounded growth, and 68 of the 69 policy rows in production are "
            "disabled. The one enabled row is not enforced either — the "
            "recurring job is dry-run; see effective_mechanisms."
        ),
    ),
    _p(
        policy_id="client_db.eco_driving_email_send_log",
        store=(
            "client business public.eco_driving_weekly_email_send_log and "
            "public.eco_driving_monthly_email_send_log"
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="eco-driving",
        retention=_D(),
        age_basis="attempted_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "Driver e-mail addresses and per-driver send history. Absent from "
            "jobs.api.telematics.registry.TABLES and therefore from every "
            "retention mechanism that existed before this registry."
        ),
    ),
    _p(
        policy_id="client_db.eco_drivers_id_chart",
        store="client business public.eco_drivers_id_chart",
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="eco-driving",
        retention=_D(),
        age_basis="updated_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "Driver roster: name and e-mail per driver id. Anchored on "
            "updated_at, so a roster row still being refreshed by imports is "
            "never eligible and only an abandoned entry ages out."
        ),
    ),
    _p(
        policy_id="client_db.eco_dashboard_delivery_operation",
        store="client business public.eco_dashboard_delivery_operation",
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="eco-dashboard",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.SECRET_MINIMISATION,
        cleanup_job="jobs.ecodriving_dashboard.delivery_ledger (secret); ops.hard_retention (row)",
        schedule_id="platform-hard-retention",
        shorter_ttl=(
            "raw capability bearer destroyed at grant expiry — 10 days weekly, "
            "60 days monthly — by retire_expired_capabilities (state CAPABILITY_RETIRED)"
        ),
        contains_personal_data=True,
        notes=(
            "Two separate lifetimes on one row, and that is the point. The "
            "SECRET dies at the grant's own expiry, long before the ceiling; "
            "the non-secret audit identity (which grant, which generation, when "
            "it expired) survives as a tombstone and is removed at the ceiling. "
            "A row still holding a live bearer is never eligible."
        ),
    ),
    _p(
        policy_id="client_db.workflow_b_stage3_report_tables",
        store=(
            "client business telematics_reports.report_207 and "
            "telematics_reports.report_d105_2_ecodriving"
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="_loaded_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "The largest customer datasets on the platform (7.2M + 0.5M rows in "
            "alpha_main alone): registration plates, speeds, locations, driver "
            "names. Loaded by Workflow B Stage 3 and previously governed by "
            "nothing at all. Anchored on _loaded_at, the only timestamp the "
            "loader writes — the business columns are text."
        ),
    ),
    _p(
        policy_id="client_db.workflow_b_gps_assignment_log",
        store=(
            'client business telematics_reports."Alpha_GPS_Baza_LOG" '
            '(and its pre-rename twin "Alpha_GPS_Baza_LOG")'
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-b",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.OWNER_EXEMPT,
        cleanup_job=None,
        contains_personal_data=True,
        exemption=OwnerExemption(
            approved_by="platform owner",
            approved_on="2026-08-29",
            reason=(
                "The GPS assignment log is the business history of which "
                "vehicle carried which unit, and it is consulted for "
                "assignments far older than thirteen months. The owner decided "
                "explicitly that this relation has NO age-based retention: "
                "records are not deleted, and not classified as expired, "
                "because of their age."
            ),
        ),
        rationale=(
            "OWNER DECISION, 2026-08-29: an explicit exception to the "
            "13-calendar-month ceiling. This store keeps its history for as "
            "long as the owner wants it kept, so it has no deletion anchor at "
            "all — neither the semantic `assignment_date` nor the ingestion "
            "`imported_at`. The rows a read-only inventory measured as older "
            "than the former cutoff are EXPECTED RETAINED DATA, not overdue "
            "cleanup. Consequently the sweep plans no DELETE here, and neither "
            "GPS writer filters at ingestion: the full-replace import restores "
            "the whole workbook, historical assignments included, exactly as it "
            "did before retention governance existed. The exemption covers the "
            "assignment log only; its import history "
            "(`alpha_gps_baza_log_import_runs`) stays under the ordinary "
            "ceiling as `client_db.workflow_b_gps_assignment_import_runs`."
        ),
    ),
    _p(
        policy_id="client_db.workflow_b_gps_assignment_import_runs",
        store="client business telematics_reports.alpha_gps_baza_log_import_runs",
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="started_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        notes=(
            "Import-run history for the GPS assignment log: one row per import "
            "attempt, with its checksum, status and counts. It is operational "
            "provenance rather than business history, it is not what the owner "
            "exempted, and it ages out on its own `started_at` under the global "
            "ceiling. Splitting it out of the assignment-log policy is what "
            "keeps the exemption exactly as wide as the decision was."
        ),
    ),
    _p(
        policy_id="client_db.legacy_backup_tables",
        store=(
            "client business public.client_trips_legacy_backup_020, "
            "public.client_trips_legacy_backup_021, and the ad-hoc "
            "telematics_reports.backup_* write-test copies in alpha_main"
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="start_timestamp (trip copies) / _loaded_at (report copies)",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        status=Status.DEPRECATED,
        contains_personal_data=True,
        notes=(
            "Full copies of customer trip rows left behind by migrations 020/021 "
            "and by a June write test: 13 339 rows in foxtrot_main, 4 092 in "
            "delta_main. They are governed here rather than dropped by this "
            "task, because dropping a relation in a production client database "
            "is an operator decision, not a retention one. "
            "The two 2026-06-19 write-test snapshots in alpha_main are "
            "registered by EXACT NAME in GOVERNED_CLIENT_RELATIONS and swept by "
            "two declared TableSweeps — never by a `backup_*` wildcard, which "
            "would also claim operator and forensic relations nobody decided to "
            "delete. They hold 0 rows today; coverage is about which policy "
            "owns a store, not about whether it currently has anything in it."
        ),
    ),
    _p(
        policy_id="client_db.v2_staging_tables",
        store=(
            "client business public.source_trips, public.source_notifications, "
            "public.source_fuel_observations"
        ),
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="workflow-a",
        retention=_D(),
        age_basis="not established — no V2 loader exists",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        status=Status.UNSUPPORTED,
        notes=(
            "Declared by db/client_business/017_v2_staging_tables.sql; migration "
            "016 removed the matching registry rows because no V2 job exists. "
            "Empty in every client database. Registered so that a future V2 "
            "loader cannot start writing into an ungoverned store: the coverage "
            "check would otherwise pass silently."
        ),
    ),
    _p(
        policy_id="client_db.configuration_and_identity",
        store="client business public.schema_migrations, ops_control.environment_identity",
        backend=Backend.CLIENT_BUSINESS_POSTGRES,
        owner_domain="platform-core",
        retention=Retention.none(),
        age_basis=None,
        mode=Mode.NOT_APPLICABLE,
        cleanup_job=None,
        rationale=(
            "Per-client migration ledger and the client database's own identity "
            "attestation row. No business, customer or personal data; deleting "
            "either breaks schema management or identity attestation."
        ),
    ),
    # -- Cloudflare D1 / R2 -------------------------------------------------
    _p(
        policy_id="cloudflare_d1.eco_session",
        store="D1 driver-eco-authorization, eco_session",
        backend=Backend.CLOUDFLARE_D1,
        owner_domain="eco-dashboard",
        # 30 MINUTES, not 12 hours. The value is
        # `delivery/driver_eco_dashboard/worker/lib/session.js`
        # `SESSION_TTL_SECONDS = 30 * 60` — what the Worker actually mints — and
        # `ops/tests_manual/test_retention_registry.py` fails if the two drift.
        # The entry previously said 12 hours, which was neither the
        # authorization lifetime nor the compaction horizon: it was a number
        # nothing produced.
        retention=Retention.minutes(30),
        age_basis="expires_at",
        mode=Mode.COMPACTION,
        cleanup_job="POST /api/publish/maintenance (worker), driven by the host mailing run",
        schedule_id="eco-dashboard-maintenance",
        shorter_ttl="browser session lifetime, capped at its grant's own expiry",
        effective_mechanisms=(
            EffectiveMechanism(
                mechanism=(
                    "eco_session row compaction — "
                    "`DELETE FROM eco_session WHERE expires_at <= now` in "
                    "delivery/driver_eco_dashboard/worker/lib/store.js"
                    "::deleteExpiredSessions"
                ),
                schedule_id="eco-dashboard-maintenance",
                enforcing=True,
                # No approximate_max: compaction is LONGER than the 30-minute
                # authorization lifetime, so it shortens nothing and must not be
                # reported as if it did. It is declared because the two horizons
                # are different questions and the registry previously conflated
                # them into one wrong number.
                note=(
                    "TWO DIFFERENT HORIZONS, and the registry states the "
                    "shorter one. AUTHORIZATION lifetime is 30 minutes: past "
                    "`expires_at` the row authorises nothing — `classifySession` "
                    "refuses it — and it is additionally capped at its grant's "
                    "own expiry. PHYSICAL presence lasts until the next "
                    "maintenance call compacts it, which is the weekly "
                    "platform sweep, so a dead session row can sit in D1 for up "
                    "to one maintenance cycle after it stops authorising. That "
                    "residue is dead state, not a longer session."
                ),
            ),
        ),
        notes=(
            "The value here is the session AUTHORIZATION lifetime, not a "
            "retention horizon: a session row authorises nothing the moment it "
            "expires and is compacted on the next maintenance call — see "
            "`effective_mechanisms` for the compaction horizon, which is longer "
            "and is a different question. It is registered so the shortest "
            "lifetime on the platform is visible next to the longest."
        ),
    ),
    _p(
        policy_id="cloudflare_d1.eco_capability",
        store="D1 driver-eco-authorization, eco_capability",
        backend=Backend.CLOUDFLARE_D1,
        owner_domain="eco-dashboard",
        retention=_D(),
        age_basis="issued_at (a live grant is additionally never eligible)",
        mode=Mode.TOMBSTONE_RETENTION,
        cleanup_job="POST /api/publish/maintenance (worker)",
        schedule_id="eco-dashboard-maintenance",
        shorter_ttl="grant expiry: 10 days weekly, 60 days monthly",
        notes=(
            "The grant stops authorising at its own TTL and the row then "
            "survives ON PURPOSE, so an expired link answers 410 LINK_EXPIRED "
            "rather than looking like a link that never existed. 'Survives' now "
            "means 'until the ceiling', not 'forever'. The row holds a digest, "
            "an opaque subject_ref and an opaque object key — never a bearer."
        ),
    ),
    _p(
        policy_id="cloudflare_d1.eco_publication_operation",
        store="D1 driver-eco-authorization, eco_publication_operation",
        backend=Backend.CLOUDFLARE_D1,
        owner_domain="eco-dashboard",
        retention=_D(),
        age_basis="created_at",
        mode=Mode.HARD_DELETE,
        cleanup_job="POST /api/publish/maintenance (worker)",
        schedule_id="eco-dashboard-maintenance",
        depends_on=("cloudflare_d1.eco_capability",),
        notes=(
            "The publication ledger references the grant it minted, so it is "
            "removed before the grant it names — child first, parent second."
        ),
    ),
    _p(
        policy_id="cloudflare_r2.driver_eco_snapshots",
        store="R2 bucket driver-eco-snapshots (EU jurisdiction)",
        backend=Backend.CLOUDFLARE_R2,
        owner_domain="eco-dashboard",
        retention=_D(),
        age_basis="R2 object uploaded timestamp",
        mode=Mode.HARD_DELETE,
        cleanup_job="POST /api/publish/maintenance (worker)",
        schedule_id="eco-dashboard-maintenance",
        shorter_ttl=None,
        contains_personal_data=True,
        notes=(
            "The historical snapshot deliberately OUTLIVES the capability that "
            "pointed at it — a 10-day link does not delete a driver's report — "
            "but 'outlives' is no longer 'indefinitely'. Each snapshot holds one "
            "driver's Eco figures for one period and is deleted at the ceiling."
        ),
    ),
    # -- Filesystem ---------------------------------------------------------
    _p(
        policy_id="filesystem.platform_backup_sets",
        store="backups/ (postgres_*.sql.gz, minio_*.tar.gz, backup_*.manifest.json)",
        backend=Backend.FILESYSTEM,
        owner_domain="operations",
        # NOT a typed 14. The number comes from the topology above, which is the
        # same object `ops/backup_retention.py` reads its window from, so the
        # policy an operator sees here and the window the executor applies
        # cannot be different numbers.
        retention=Retention.days(PLATFORM_BACKUP_SET.retention_days),
        age_basis="backup set timestamp",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.backup_retention --execute",
        schedule_id="backup-retention",
        shorter_ttl=(
            f"{PLATFORM_BACKUP_SET.retention_days} days, floored at 3 "
            f"independently verified restorable sets"
        ),
        contains_personal_data=True,
        notes=(
            "A backup FILE is comfortably inside the ceiling. A backup's "
            "CONTENTS are the open question — see BACKUP_SHADOW_NOTE and "
            "backup_shadow_days(). "
            "The window applies to every set the naming contract recognises, "
            "not only to manifest-bearing ones: `ops.backup_retention` "
            "classifies a set as a manifest set, a recognised pre-manifest "
            "LEGACY PAIR (postgres + MinIO, no manifest — the shape "
            "`ops/backup.sh` produced before manifests existed) or an "
            "incomplete remnant, and expires all three at this horizon. Only a "
            "`.partial`/`.failed` remnant still needs `--purge-invalid`, "
            "because that suffix marks a run's own in-flight state. Before "
            "that, an absent manifest meant an UNBOUNDED lifetime: 11 legacy "
            "sets were retained as `invalid_remnant_retained` while this entry "
            "claimed 14 days."
        ),
    ),
    _p(
        policy_id="filesystem.workflow_b_report_files",
        store="REPORTS_DATA_DIR raw/ and normalized/ (default /home/logplatform/data/reports)",
        backend=Backend.FILESYSTEM,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="file mtime",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention (filesystem backend)",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        notes=(
            "Stage 1 artifact reconciliation depends on raw_path and "
            "normalized_csv_path staying durable. The ceiling is more than an "
            "order of magnitude beyond any reconciliation window, and the sweep "
            "runs after the ingest rows that reference these paths have "
            "themselves aged out, so no reachable reconciliation is broken. "
            "Empty in production today."
        ),
    ),
    _p(
        policy_id="filesystem.workflow_b_stage2_cleaned",
        store="/tmp/log-platform-stage2/cleaned",
        backend=Backend.FILESYSTEM,
        owner_domain="workflow-b",
        retention=_D(),
        age_basis="file mtime",
        mode=Mode.HARD_DELETE,
        cleanup_job="ops.hard_retention (filesystem backend)",
        schedule_id="platform-hard-retention",
        contains_personal_data=True,
        effective_mechanisms=(
            EffectiveMechanism(
                mechanism=(
                    "systemd-tmpfiles-clean.timer applying `D /tmp 1777 root "
                    "root 30d` from the host's /usr/lib/tmpfiles.d/tmp.conf"
                ),
                schedule_id="systemd-tmpfiles-clean",
                enforcing=True,
                approximate_max=timedelta(days=30),
                host_managed=True,
                note=(
                    "THE MECHANISM THAT ACTUALLY BOUNDS THIS PATH, and it is "
                    "not the hard-retention sweep. The directory is under "
                    "/tmp, which the host cleans on an age of 30 days; the `D` "
                    "type additionally empties /tmp at boot, so a rebooted host "
                    "removes it sooner still. The ceiling above remains the "
                    "declared policy and the backstop for a host that is never "
                    "rebooted and whose tmpfiles configuration is changed, but "
                    "an operator reading '13 calendar months' here without this "
                    "line would be reading a lifetime nothing on this host "
                    "produces. The OS policy is host-managed: this repository "
                    "neither installs nor configures it, and this task did not "
                    "change it."
                ),
            ),
        ),
        notes=(
            "Stage 2 cleaned CSVs. Transient by intent; the ceiling is the "
            "backstop for the case where the host is never rebooted. The real, "
            "shorter lifecycle is host tmpfiles cleanup — see "
            "`effective_mechanisms`, and `systemd-tmpfiles-clean` in "
            "ops/schedule_catalog.py for the recurring mechanism itself."
        ),
    ),
    _p(
        policy_id="host.journald",
        store="systemd journal (all platform unit stdout/stderr)",
        backend=Backend.HOST_MANAGED,
        owner_domain="operations",
        retention=_D(),
        age_basis="journal entry timestamp",
        mode=Mode.HARD_DELETE,
        cleanup_job="systemd-journald MaxRetentionSec + journald-retention-vacuum",
        schedule_id="journald-retention-vacuum",
        self_enforcing=True,
        self_enforcing_lead=JOURNALD_MAX_FILE_SEC + JOURNALD_VACUUM_CYCLE,
        contains_personal_data=True,
        notes=(
            "Disk size is not a time-retention policy, and this host had no "
            "MaxRetentionSec at all. The repository now owns the configuration: "
            "`ops/systemd/proposed/journald-retention.conf` is a drop-in for "
            "/etc/systemd/journald.conf.d/, and its numbers are DERIVED from "
            "`journald_max_retention()` rather than typed. journald cannot "
            "express calendar months, so the derivation starts from the shortest "
            "span 13 calendar months can have and deducts the rotation and "
            "vacuum granularity — a conservative fixed duration that can never "
            "exceed the ceiling for any calendar start date. "
            "`journald-retention-vacuum.timer` runs `journalctl --vacuum-time` "
            "daily so a host quiet enough never to rotate a journal still "
            "enforces it. Installing the drop-in and enabling the timer are "
            "separately authorized host mutations; neither is performed here."
        ),
    ),
)


#: The one place a caller can ask "what is the ceiling?" without importing the
#: whole registry.
GLOBAL_DEFAULT = Retention.default()


# ---------------------------------------------------------------------------
# Relation coverage — how an ungoverned store becomes DETECTABLE
# ---------------------------------------------------------------------------
#
# The policies above are written for operators: one entry per data family, with
# the store described in prose. That reads well and proves nothing. These two
# maps are the machine-checkable half — every relation the repository can
# create, named exactly, pointing at the policy that governs it.
#
# `ops/tests_manual/test_retention_registry.py` parses every `CREATE TABLE` in
# `db/migrations/`, `db/client_business/` and `api/main.py` and fails if any
# relation is missing here. A migration that introduces a table therefore cannot
# merge without someone deciding how long its rows may live — which is the whole
# point of "unknown persistent stores should be detectable by validation rather
# than silently operating outside governance".
#
# Relations not created by any repository DDL are listed anyway, with a comment
# saying where they come from: the Stage 3 loader builds its destination tables
# at runtime, and two migrations rename their rebuild table into a backup.

GOVERNED_RELATIONS: Mapping[str, str] = {
    # -- run/log/artifact core -------------------------------------------
    "public.runs": "platform_db.public.runs",
    "public.logs": "platform_db.public.logs",
    "public.artifacts": "platform_db.public.artifacts",
    "public.artifact_metadata_overrides": "platform_db.configuration_and_access_control",
    "public.artifact_tags": "platform_db.configuration_and_access_control",
    "public.artifact_virtual_folders": "platform_db.configuration_and_access_control",
    "public.artifact_virtual_folder_items": "platform_db.configuration_and_access_control",
    "public.artifact_users": "platform_db.configuration_and_access_control",
    "public.artifact_roles": "platform_db.configuration_and_access_control",
    "public.artifact_user_roles": "platform_db.configuration_and_access_control",
    "public.artifact_role_permissions": "platform_db.configuration_and_access_control",
    # -- portal ------------------------------------------------------------
    "public.portal_audit_events": "platform_db.public.portal_audit_events",
    "public.portal_clients": "platform_db.configuration_and_access_control",
    "public.portal_user_clients": "platform_db.configuration_and_access_control",
    "public.portal_groups": "platform_db.configuration_and_access_control",
    "public.portal_group_users": "platform_db.configuration_and_access_control",
    "public.portal_group_clients": "platform_db.configuration_and_access_control",
    "public.portal_report_folders": "platform_db.configuration_and_access_control",
    "public.portal_report_folder_users": "platform_db.configuration_and_access_control",
    "public.portal_report_folder_groups": "platform_db.configuration_and_access_control",
    "public.portal_database_datasets": "platform_db.configuration_and_access_control",
    "public.portal_database_dataset_columns": "platform_db.configuration_and_access_control",
    "public.portal_database_dataset_users": "platform_db.configuration_and_access_control",
    "public.portal_database_dataset_groups": "platform_db.configuration_and_access_control",
    "public.portal_database_column_sets": "platform_db.configuration_and_access_control",
    "public.portal_database_saved_views": "platform_db.configuration_and_access_control",
    "public.portal_user_preferences": "platform_db.configuration_and_access_control",
    "public.portal_generated_report_definitions": "platform_db.configuration_and_access_control",
    "public.portal_generated_report_instances": "platform_db.public.portal_generated_report_instances",
    # Cascades from its instance; it has no independent lifetime.
    "public.portal_generated_report_files": "platform_db.public.portal_generated_report_instances",
    # -- database explorer ---------------------------------------------------
    "public.database_export_jobs": "platform_db.public.database_export_jobs",
    "public.database_export_attempt_objects": "platform_db.public.database_export_attempt_objects",
    "public.database_export_system_folders": "platform_db.configuration_and_access_control",
    # -- incidents -----------------------------------------------------------
    "public.suspected_bug_incidents": "platform_db.public.suspected_bug_incidents",
    "public.suspected_bug_occurrences": "platform_db.public.suspected_bug_occurrences",
    "public.suspected_bug_email_outbox": "platform_db.public.suspected_bug_email_outbox",
    # -- ledgers and identity ------------------------------------------------
    # Created by `ops/db_migrate.sh` itself rather than by a migration file.
    "public.schema_migrations": "platform_db.public.schema_migrations",
    "ingest.imap_message": "platform_db.ingest.imap_message",
    "ingest.raw_file": "platform_db.ingest.raw_file",
    "ops_control.environment_identity": "platform_db.current_state_singletons",
    "ops_control.environment_identity_promotion": "platform_db.ops_control.environment_identity_promotion",
    "ops_control.run_reconciliation": "platform_db.ops_control.run_reconciliation",
    "ops_control.scheduler_heartbeat": "platform_db.current_state_singletons",
    "ops_control.watchdog_observation": "platform_db.ops_control.watchdog_observation",
    # The retention ledger itself: current state, one row per
    # (policy, scope, swept target) since migration 071.
    "ops_control.retention_execution": "platform_db.current_state_singletons",
    # -- Workflow A control plane -------------------------------------------
    "workflow_a_control.client_account": "platform_db.configuration_and_access_control",
    "workflow_a_control.client_dataset_schedule": "platform_db.configuration_and_access_control",
    "workflow_a_control.dataset_registry": "platform_db.configuration_and_access_control",
    "workflow_a_control.table_registry": "platform_db.configuration_and_access_control",
    "workflow_a_control.client_table_retention": "platform_db.configuration_and_access_control",
    "workflow_a_control.client_sync_state": "platform_db.current_state_singletons",
    "workflow_a_control.client_dataset_coverage": "platform_db.current_state_singletons",
    "workflow_a_control.client_schedule_run_history": "platform_db.workflow_a_control.client_schedule_run_history",
    "workflow_a_control.provider_request_log": "platform_db.workflow_a_control.provider_request_log",
    "workflow_a_control.client_dataset_recovery_run": "platform_db.workflow_a_control.client_dataset_recovery_run",
    "workflow_a_control.trip_delivery_lag_daily": "platform_db.workflow_a_control.trip_delivery_lag_daily",
    # Migration 008 creates `client_schedule`; migration 012 renames it to
    # `client_schedule_legacy`. Both names map to the same deprecated entry so
    # neither the pre-012 nor the post-012 database reads as ungoverned.
    "workflow_a_control.client_schedule": "platform_db.workflow_a_control.client_schedule_legacy",
    "workflow_a_control.client_schedule_legacy": "platform_db.workflow_a_control.client_schedule_legacy",
    # -- Workflow B control plane -------------------------------------------
    "workflow_b_control.report_type_registry": "platform_db.configuration_and_access_control",
    "workflow_b_control.report_type_client_load_policy": "platform_db.configuration_and_access_control",
}

GOVERNED_CLIENT_RELATIONS: Mapping[str, str] = {
    "public.client_trips": "client_db.workflow_a_registered_tables",
    "public.client_speeding_notifications": "client_db.workflow_a_registered_tables",
    "public.client_vehicle_daily_fuel": "client_db.workflow_a_registered_tables",
    "public.client_vehicle_driver_daily_fuel": "client_db.workflow_a_registered_tables",
    "public.eco_trip_assignments": "client_db.workflow_a_registered_tables",
    "public.eco_driver_weekly_stats": "client_db.workflow_a_registered_tables",
    "public.eco_driver_monthly_stats": "client_db.workflow_a_registered_tables",
    "public.eco_person_people": "client_db.workflow_a_registered_tables",
    "public.eco_person_driver_mappings": "client_db.workflow_a_registered_tables",
    "public.eco_person_trip_assignments": "client_db.workflow_a_registered_tables",
    "public.eco_person_weekly_stats": "client_db.workflow_a_registered_tables",
    "public.eco_person_monthly_stats": "client_db.workflow_a_registered_tables",
    "public.eco_person_weekly_email_send_log": "client_db.workflow_a_registered_tables",
    "public.eco_person_monthly_email_send_log": "client_db.workflow_a_registered_tables",
    "public.eco_driving_weekly_email_send_log": "client_db.eco_driving_email_send_log",
    "public.eco_driving_monthly_email_send_log": "client_db.eco_driving_email_send_log",
    "public.eco_drivers_id_chart": "client_db.eco_drivers_id_chart",
    "public.eco_dashboard_delivery_operation": "client_db.eco_dashboard_delivery_operation",
    "public.source_trips": "client_db.v2_staging_tables",
    "public.source_notifications": "client_db.v2_staging_tables",
    "public.source_fuel_observations": "client_db.v2_staging_tables",
    # Migrations 020 and 021 build `client_trips_rebuilt_0NN` and rename the old
    # table to `client_trips_legacy_backup_0NN`. Both names are governed.
    "public.client_trips_rebuilt_020": "client_db.legacy_backup_tables",
    "public.client_trips_rebuilt_021": "client_db.legacy_backup_tables",
    "public.client_trips_legacy_backup_020": "client_db.legacy_backup_tables",
    "public.client_trips_legacy_backup_021": "client_db.legacy_backup_tables",
    # The 2026-06-19 D105.2 write-test snapshots in alpha_main. Created by an
    # operator before a write test — `ops/reports/d105_2_ecodriving_alpha00001_
    # local_write_test_rollback_20260619_100539.sql` is the rollback that reads
    # the first of them — so no DDL in this repository creates them and the
    # migration-parsing coverage check could never have seen them. Live coverage
    # (`--coverage --clients`) did.
    #
    # REGISTERED BY EXACT NAME, DELIBERATELY. They are the same KIND of store as
    # the migration 020/021 leftovers above — a full copy of customer rows from
    # a governed table, carrying that table's own anchor — so they belong to the
    # same policy and need no new one. What they must NOT have is a `backup_*`
    # wildcard: `telematics_reports` also holds operator and forensic material, and
    # a pattern that swept "anything called backup" would be a deletion rule
    # nobody wrote. Two names, two anchors, two declared sweeps.
    "telematics_reports.backup_client_trips_d105_2_write_test_20260619_100539":
        "client_db.legacy_backup_tables",
    "telematics_reports.backup_d105_2_ecodriving_write_test_20260619_100539":
        "client_db.legacy_backup_tables",
    "public.schema_migrations": "client_db.configuration_and_identity",
    "ops_control.environment_identity": "client_db.configuration_and_identity",
    # Workflow B Stage 3 destinations. Created by the loader at runtime from the
    # report-type registry, not by any DDL file in this repository — which is
    # exactly why they were invisible to every previous retention discussion.
    "telematics_reports.report_207": "client_db.workflow_b_stage3_report_tables",
    "telematics_reports.report_d105_2_ecodriving": "client_db.workflow_b_stage3_report_tables",
    "telematics_reports.Alpha_GPS_Baza_LOG": "client_db.workflow_b_gps_assignment_log",
    "telematics_reports.Alpha_GPS_Baza_LOG": "client_db.workflow_b_gps_assignment_log",
    "telematics_reports.alpha_gps_baza_log_import_runs": "client_db.workflow_b_gps_assignment_import_runs",
}


def relation_policy(relation: str, *, client_business: bool = False) -> RetentionPolicy | None:
    """The policy governing `schema.table`, or `None` if the store is unknown.

    `None` is the answer that matters: it is what "this store is operating
    outside governance" looks like to a caller.
    """
    table = GOVERNED_CLIENT_RELATIONS if client_business else GOVERNED_RELATIONS
    policy_id = table.get(relation)
    return BY_ID.get(policy_id) if policy_id else None


def ungoverned(relations: Iterable[str], *, client_business: bool = False) -> list[str]:
    """Which of `relations` no policy claims. Sorted, for a stable report."""
    table = GOVERNED_CLIENT_RELATIONS if client_business else GOVERNED_RELATIONS
    return sorted(name for name in set(relations) if name not in table)


# ---------------------------------------------------------------------------
# Backup shadow — the honest part
# ---------------------------------------------------------------------------

BACKUP_SHADOW_NOTE = (
    "A hard delete at the source does not reach the copies of that row already "
    "inside retained backup archives. `ops/backup.sh` takes a full `pg_dump` of "
    "the PLATFORM database plus a full MinIO tarball, and "
    "`ops/backup_retention.py` expires a whole set after 14 days — there is no "
    "incremental chain, no per-record expiry and no cryptographic-erasure key "
    "lifecycle, so a backup copy cannot be deleted individually. "
    "That is not solved by shortening the archive's life and hoping: it is "
    "solved by deleting at the SOURCE early enough that the last archive "
    "containing the record expires on or before the record's own deadline. The "
    "source lead is therefore the backup shadow — set retention plus one expiry "
    "cycle — added to the ordinary maintenance-cycle lead, and it is derived "
    "from `PLATFORM_BACKUP_SET` rather than typed at a call site. "
    "Client business databases, Cloudflare D1/R2 and the filesystem roots are "
    "in NO repository-controlled backup set, so they carry no shadow at all."
)


#: The policies `api/platform_prune.py`'s ordinary day-count pass governs. All
#: three must declare the same horizon: they are deleted by one command with one
#: `--days`, so a disagreement here would be a policy the executor cannot honour.
PLATFORM_PRUNE_POLICY_IDS = (
    "platform_db.public.runs",
    "platform_db.public.logs",
    "platform_db.public.artifacts",
)


class PrunePolicyConflict(ValueError):
    """A configured prune horizon contradicts the central policy."""


def platform_prune_retention_days() -> int:
    """THE day count `api/platform_prune.py --days` is allowed to express.

    The 60 in `ops/systemd/log-platform-prune.service` and the 60 in three
    registry entries were independent numbers that happened to agree. This is
    the one an operator, the unit and the executor now all resolve through, and
    `validate_platform_prune_days()` is what stops a longer one being passed.
    """
    horizons = set()
    for policy_id in PLATFORM_PRUNE_POLICY_IDS:
        policy = BY_ID[policy_id]
        if policy.retention.unit is not Unit.DAYS or not policy.retention.value:
            raise PrunePolicyConflict(
                f"{policy_id} does not declare a day-count horizon; "
                f"api.platform_prune has no number to derive"
            )
        horizons.add(int(policy.retention.value))
    if len(horizons) != 1:
        raise PrunePolicyConflict(
            f"the platform-prune policies declare disagreeing horizons "
            f"{sorted(horizons)}; one command with one --days cannot honour both"
        )
    return horizons.pop()


def validate_platform_prune_days(value: object, *, source: str = "--days") -> int:
    """Accept a prune horizon only if the central policy admits it. Fails closed.

    Shorter is allowed — it deletes earlier and can only reduce how long a
    record lives. Longer is refused: `--days 90` against a declared 60-day
    policy would keep run/log/artifact history a month past what this registry
    tells an operator it keeps, with nothing reporting the contradiction.
    """
    declared = platform_prune_retention_days()
    if isinstance(value, bool):
        raise PrunePolicyConflict(f"prune horizon from {source} must not be a boolean")
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PrunePolicyConflict(
            f"prune horizon from {source} is not an integer: {value!r}"
        ) from exc
    if days < 1:
        raise PrunePolicyConflict(f"prune horizon from {source} must be >= 1, got {days}")
    if days > declared:
        raise PrunePolicyConflict(
            f"prune horizon of {days} days from {source} exceeds the "
            f"{declared}-day policy declared by "
            f"{', '.join(PLATFORM_PRUNE_POLICY_IDS)} in ops/retention_registry.py"
        )
    return days


def backup_shadow_days(
    *, backup_retention_days: int | None = None, cleanup_interval_days: int | None = None
) -> int:
    """Worst-case days a deleted record can still exist inside retained backups.

    Defaults come from the audited topology, so the number an operator reads is
    the number the executor uses. The parameters exist for what-if analysis and
    for tests that need to vary the levers.
    """
    if backup_retention_days is None:
        backup_retention_days = PLATFORM_BACKUP_SET.retention_days
    if cleanup_interval_days is None:
        cleanup_interval_days = int(
            MAINTENANCE_CYCLES[PLATFORM_BACKUP_SET.expiry_cycle]
            .guaranteed_interval.total_seconds() // 86400
        )
    if backup_retention_days < 0 or cleanup_interval_days < 0:
        raise ValueError("day counts must be non-negative")
    return int(backup_retention_days) + int(cleanup_interval_days)


def final_surviving_copy(
    policy: RetentionPolicy, *, created: datetime, sweep_times: Iterable[datetime],
) -> dict[str, Any]:
    """Model one datum end to end and report when its LAST copy disappears.

    The whole point of the backup-shadow argument is that it must be checkable
    rather than asserted, so this function walks the actual lifecycle:

      1. the datum is created;
      2. full backups are taken on their own cadence and each contains the datum
         if it was still in the source at the time;
      3. a sweep deletes it once its deadline falls inside that sweep's
         enforcement cutoff;
      4. the newest backup that still contains it expires;
      5. the later of (3) and (4) is when the last copy is gone.

    Returns the deadline, the source deletion, and the final surviving-copy
    instant, so a caller can assert `final <= deadline` directly.
    """
    deadline = policy.deadline_of(created)
    source_deleted: datetime | None = None
    for sweep in sorted(sweep_times):
        cutoff = policy.enforcement_cutoff(sweep)
        if cutoff is not None and created < cutoff:
            source_deleted = sweep
            break

    final = source_deleted
    if source_deleted is not None and policy.in_backup_set:
        # The newest backup that can contain the datum is one taken strictly
        # before the source deletion; it expires `shadow` later.
        for topology in BACKUP_SETS:
            if not topology.covers(policy.backend):
                continue
            final = max(final, source_deleted + topology.shadow)
    return {
        "created": created,
        "deadline": deadline,
        "source_deleted": source_deleted,
        "final_surviving_copy": final,
        "compliant": bool(final is not None and final <= deadline),
        "enforcement_lead_seconds": int(policy.enforcement_lead().total_seconds()),
        "in_backup_set": policy.in_backup_set,
    }


# ---------------------------------------------------------------------------
# Ingestion-time enforcement
# ---------------------------------------------------------------------------

#: The GPS assignment log — the platform's ONE owner-approved exemption from
#: age-based retention. Named here so the executor, the operator view and the
#: tests can reference the exempt policy by constant rather than by string, and
#: so that anything asking this policy for a cutoff gets the same answer from
#: one place: there is none.
GPS_ASSIGNMENT_POLICY_ID = "client_db.workflow_b_gps_assignment_log"


def expired_by_semantic_date(
    policy_id: str, value: date | datetime | None, *, now: datetime | None = None,
) -> bool:
    """Is this SEMANTIC business date already past the policy's enforcement cutoff?

    `None` is never expired. A record whose semantic age cannot be established
    is not deleted and not dropped at ingestion — it is kept and counted, so an
    unparseable date surfaces as a defect rather than as a silent deletion or a
    silent immortality.

    A policy with no age-based retention — lifecycle-bound, not-applicable or
    owner-exempt — has no cutoff, so NOTHING is expired under it. That is the
    single place the exemption has to hold for every caller: an exempt store
    cannot be made to answer "yes, delete this" by asking differently.
    """
    if value is None:
        return False
    policy = get(policy_id)
    cutoff = policy.enforcement_cutoff(now)
    if cutoff is None:
        return False
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment < cutoff
    return value < cutoff.date()


def partition_by_semantic_date(
    policy_id: str, records: Iterable[Any], *, key: Any, now: datetime | None = None,
) -> tuple[list[Any], list[Any], list[Any]]:
    """Split records into `(retained, expired, unanchored)` for an IMPORTER.

    WHY AN IMPORTER WOULD NEED THIS. A retention sweep is sufficient for a store
    that only ever accumulates. It is not sufficient for one whose loader
    performs a full replace from an upstream file AND is governed by an
    age-based policy: the sweep deletes the over-age rows and the next import
    writes them straight back. Such a loader filters here, with the same cutoff
    from the same policy as the sweep, so the two cannot disagree.

    NO WRITER APPLIES THIS TODAY. The only full-replace store on the platform is
    the GPS assignment log, and the owner has exempted it from age-based
    retention, so its importers deliberately load every historical row. The
    helper stays because the mechanism is policy-driven rather than store-
    specific — and because it is safe by construction for an exempt policy:
    `expired_by_semantic_date` returns False for every record when the policy
    has no cutoff, so applying it to an exempt store retains everything.

    `key` is a callable extracting the semantic date from one record.
    """
    retained: list[Any] = []
    expired: list[Any] = []
    unanchored: list[Any] = []
    for record in records:
        value = key(record)
        if value is None:
            unanchored.append(record)
            retained.append(record)
        elif expired_by_semantic_date(policy_id, value, now=now):
            expired.append(record)
        else:
            retained.append(record)
    return retained, expired, unanchored


# ---------------------------------------------------------------------------
# Lookup + validation
# ---------------------------------------------------------------------------

BY_ID: Mapping[str, RetentionPolicy] = {policy.policy_id: policy for policy in POLICIES}


def get(policy_id: str) -> RetentionPolicy:
    try:
        return BY_ID[policy_id]
    except KeyError:
        raise KeyError(
            f"unknown retention policy {policy_id!r}; known: {sorted(BY_ID)}"
        ) from None


def policies_for_job(cleanup_job_prefix: str) -> tuple[RetentionPolicy, ...]:
    return tuple(
        policy for policy in POLICIES
        if policy.cleanup_job and policy.cleanup_job.startswith(cleanup_job_prefix)
    )


@dataclass(frozen=True)
class Violation:
    policy_id: str
    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.code} [{self.policy_id}]: {self.detail}"


def validate(policies: Sequence[RetentionPolicy] | None = None) -> list[Violation]:
    """Return every structural or policy violation. Empty list means compliant.

    This is the machine-detectable half of the owner's rule. It is deliberately
    exhaustive rather than fail-fast: an operator wants the whole list.
    """
    entries = tuple(policies if policies is not None else POLICIES)
    problems: list[Violation] = []
    ceiling_days = minimum_span_days(HARD_RETENTION_MONTHS)

    seen: set[str] = set()
    for policy in entries:
        pid = policy.policy_id
        if pid in seen:
            problems.append(Violation(pid, "DUPLICATE_POLICY_ID", "declared more than once"))
        seen.add(pid)

        if not policy.store.strip():
            problems.append(Violation(pid, "EMPTY_STORE", "store must name the governed data"))
        if not policy.owner_domain.strip():
            problems.append(Violation(pid, "EMPTY_OWNER", "owner_domain is required"))

        # -- the ceiling itself --------------------------------------------
        if policy.override is not None:
            over = policy.override
            if not (over.approved_by.strip() and over.approved_on.strip() and over.reason.strip()):
                problems.append(Violation(
                    pid, "UNATTRIBUTED_OVERRIDE",
                    "an override must record approved_by, approved_on and reason",
                ))
            if over.months <= HARD_RETENTION_MONTHS:
                problems.append(Violation(
                    pid, "POINTLESS_OVERRIDE",
                    f"override of {over.months} months is not longer than the "
                    f"{HARD_RETENTION_MONTHS}-month ceiling; express it as an "
                    f"ordinary shorter retention instead",
                ))
        else:
            unit = policy.retention.unit
            if unit is Unit.MONTHS:
                months = policy.retention.effective_months or 0
                if months > HARD_RETENTION_MONTHS:
                    problems.append(Violation(
                        pid, "EXCEEDS_CEILING",
                        f"{months} months exceeds the {HARD_RETENTION_MONTHS}-month "
                        f"ceiling without an owner-approved override",
                    ))
            elif unit in (Unit.DAYS, Unit.HOURS):
                # A shorter horizon still has to clear the ceiling ONCE ITS OWN
                # LEAD IS ADDED: the sweep is periodic and the store may be
                # inside a backup set, so the real worst-case age of a record is
                # the configured horizon plus that latency. Comparing the bare
                # number against the ceiling would have missed exactly the class
                # of defect this whole model exists to prevent.
                configured = (
                    timedelta(days=int(policy.retention.value or 0))
                    if unit is Unit.DAYS
                    else timedelta(hours=int(policy.retention.value or 0))
                )
                worst_case = configured + policy.enforcement_lead()
                if worst_case > timedelta(days=ceiling_days):
                    problems.append(Violation(
                        pid, "EXCEEDS_CEILING",
                        f"{policy.retention.describe()} plus a "
                        f"{_humanise(policy.enforcement_lead())} enforcement lead is "
                        f"{_humanise(worst_case)}, which can exceed "
                        f"{HARD_RETENTION_MONTHS} calendar months "
                        f"(shortest span {ceiling_days} days)",
                    ))

        # -- the one approved way out of the ceiling -------------------------
        # An exemption is legal ONLY on Mode.OWNER_EXEMPT, and only when it is
        # attributed. Both halves matter: the first stops an exemption being
        # pasted onto an ordinary age-based policy where nothing would read it,
        # the second stops one appearing without an owner behind it.
        if policy.exemption is not None:
            ex = policy.exemption
            if policy.mode is not Mode.OWNER_EXEMPT:
                problems.append(Violation(
                    pid, "STRAY_EXEMPTION",
                    f"an OwnerExemption is only meaningful on "
                    f"{Mode.OWNER_EXEMPT.value}, not on {policy.mode.value}",
                ))
            if not (ex.approved_by.strip() and ex.approved_on.strip() and ex.reason.strip()):
                problems.append(Violation(
                    pid, "UNATTRIBUTED_EXEMPTION",
                    "an exemption must record approved_by, approved_on and reason",
                ))
        elif policy.mode is Mode.OWNER_EXEMPT:
            problems.append(Violation(
                pid, "MISSING_EXEMPTION",
                f"{Mode.OWNER_EXEMPT.value} requires an attributed OwnerExemption; "
                f"an unattributed exception to the ceiling is exactly what the "
                f"owner rule forbids",
            ))
        if policy.mode is Mode.OWNER_EXEMPT and policy.override is not None:
            problems.append(Violation(
                pid, "EXEMPTION_WITH_OVERRIDE",
                "a store is either exempt from age-based retention or kept for a "
                "longer number of months, never both",
            ))

        # -- mode/field coherence -------------------------------------------
        if policy.mode in RetentionPolicy.NO_AGE_MODES:
            if policy.mode is Mode.OWNER_EXEMPT and policy.age_basis:
                # An exempt store must not carry a deletion anchor: an anchor is
                # what a future sweep would reach for, and the whole point of
                # the decision is that there is nothing to reach for.
                problems.append(Violation(
                    pid, "EXEMPT_WITH_AGE_BASIS",
                    f"{policy.age_basis!r} is a deletion anchor on a store that "
                    f"has no age-based retention",
                ))
            if policy.mode is Mode.OWNER_EXEMPT and policy.cleanup_job:
                problems.append(Violation(
                    pid, "EXEMPT_WITH_CLEANUP_JOB",
                    "an owner-exempt store has nothing to clean up by age",
                ))
            if policy.retention.unit is not Unit.NONE:
                problems.append(Violation(
                    pid, "MODE_RETENTION_MISMATCH",
                    f"{policy.mode.value} must declare Retention.none()",
                ))
            if not (policy.rationale or "").strip():
                problems.append(Violation(
                    pid, "MISSING_RATIONALE",
                    f"{policy.mode.value} is only acceptable with a written rationale; "
                    f"an unexplained exemption is exactly what the owner rule forbids",
                ))
        else:
            if policy.retention.unit is Unit.NONE:
                problems.append(Violation(
                    pid, "MODE_RETENTION_MISMATCH",
                    "an age-based mode requires an actual retention",
                ))
            if not (policy.age_basis or "").strip():
                problems.append(Violation(
                    pid, "MISSING_AGE_BASIS", "an age-based policy must name its timestamp",
                ))
            if policy.status is not Status.BLOCKED_OWNER_DECISION and not (policy.cleanup_job or "").strip():
                problems.append(Violation(
                    pid, "MISSING_CLEANUP_JOB",
                    "configuration without an execution path is not governance",
                ))

            # -- the deadline look-ahead must be derivable ------------------
            if policy.self_enforcing:
                if not policy.self_enforcing_lead or policy.self_enforcing_lead <= timedelta(0):
                    problems.append(Violation(
                        pid, "MISSING_SELF_ENFORCING_LEAD",
                        "a store that enforces its own retention must still declare "
                        "the granularity of that enforcement, or its deadline is "
                        "unprovable",
                    ))
            elif policy.status is not Status.BLOCKED_OWNER_DECISION:
                if policy.schedule_id is None:
                    problems.append(Violation(
                        pid, "MISSING_SCHEDULE",
                        "an age-based policy must name the schedule that executes it",
                    ))
                elif policy.maintenance_cycle() is None:
                    problems.append(Violation(
                        pid, "MISSING_MAINTENANCE_CYCLE",
                        f"schedule {policy.schedule_id!r} declares no MaintenanceCycle, "
                        f"so the deadline look-ahead would silently be zero and a "
                        f"periodic sweep could delete after the deadline",
                    ))

            if (policy.is_ceiling_horizon
                    and policy.status is not Status.BLOCKED_OWNER_DECISION
                    and policy.enforcement_lead() <= timedelta(0)):
                problems.append(Violation(
                    pid, "ZERO_ENFORCEMENT_LEAD",
                    "a ceiling-horizon policy with no lead deletes at the deadline "
                    "at the earliest, which means after it in practice",
                ))

        # -- blockers --------------------------------------------------------
        if policy.status is Status.BLOCKED_OWNER_DECISION and not (policy.blocker or "").strip():
            problems.append(Violation(
                pid, "MISSING_BLOCKER", "a blocked policy must say what is blocked",
            ))
        if policy.status is not Status.BLOCKED_OWNER_DECISION and (policy.blocker or "").strip():
            problems.append(Violation(
                pid, "STRAY_BLOCKER", "blocker text on a policy that is not blocked",
            ))

        # -- effective mechanisms -------------------------------------------
        # The point of the descriptor is HONESTY, so the two ways it could be
        # used dishonestly are refused: claiming a mechanism shortens a lifetime
        # when it deletes nothing, and claiming an "effective" lifetime that is
        # not actually shorter than the declared one. Neither may become a way
        # to state a longer retention without an owner.
        for mechanism in policy.effective_mechanisms:
            if not mechanism.mechanism.strip():
                problems.append(Violation(
                    pid, "EMPTY_EFFECTIVE_MECHANISM",
                    "an effective mechanism must name what performs it",
                ))
            if not mechanism.note.strip():
                problems.append(Violation(
                    pid, "MISSING_EFFECTIVE_NOTE",
                    "an effective mechanism must explain itself; an unexplained "
                    "one is a rumour, not central visibility",
                ))
            if mechanism.approximate_max is not None:
                if not mechanism.enforcing:
                    problems.append(Violation(
                        pid, "NON_ENFORCING_WITH_MAX",
                        f"{mechanism.mechanism!r} deletes nothing, so it cannot "
                        f"impose an effective maximum age; a configured shorter "
                        f"policy is not an enforced one",
                    ))
                if mechanism.approximate_max <= timedelta(0):
                    problems.append(Violation(
                        pid, "NON_POSITIVE_EFFECTIVE_MAX",
                        "an effective maximum age must be positive",
                    ))
                else:
                    declared_cutoff = policy.cutoff()
                    if declared_cutoff is not None:
                        declared_span = _utc(None) - declared_cutoff
                        if mechanism.approximate_max >= declared_span:
                            problems.append(Violation(
                                pid, "EFFECTIVE_MAX_NOT_SHORTER",
                                f"an effective maximum of "
                                f"{_humanise(mechanism.approximate_max)} is not "
                                f"shorter than the declared "
                                f"{policy.retention.describe()}; this descriptor "
                                f"records a SHORTER real lifecycle and may never "
                                f"lengthen one",
                            ))
                    elif not policy.is_age_based:
                        problems.append(Violation(
                            pid, "EFFECTIVE_MAX_ON_NO_AGE_POLICY",
                            "a store with no age-based retention cannot have an "
                            "effective maximum age; declare the mechanism "
                            "without one, or the policy is not what it says",
                        ))

        for dependency in policy.depends_on:
            if dependency not in BY_ID:
                problems.append(Violation(
                    pid, "UNKNOWN_DEPENDENCY", f"depends_on names unknown policy {dependency!r}",
                ))

    for schedule_id, cycle in MAINTENANCE_CYCLES.items():
        if cycle.schedule_id != schedule_id:
            problems.append(Violation(
                schedule_id, "MAINTENANCE_CYCLE_KEY_MISMATCH",
                f"declared under {schedule_id!r} but names {cycle.schedule_id!r}",
            ))
        if cycle.guaranteed_interval <= timedelta(0):
            problems.append(Violation(
                schedule_id, "NON_POSITIVE_CYCLE", "a guaranteed interval must be positive",
            ))
        if not cycle.rationale.strip():
            problems.append(Violation(
                schedule_id, "MISSING_CYCLE_RATIONALE",
                "a maintenance cycle must say which cadence it is derived from",
            ))

    for topology in BACKUP_SETS:
        if topology.retention_days < 1:
            problems.append(Violation(
                topology.name, "INVALID_BACKUP_RETENTION", "retention_days must be >= 1",
            ))
        for field_name in ("creation_cycle", "expiry_cycle"):
            schedule_id = getattr(topology, field_name)
            if schedule_id not in MAINTENANCE_CYCLES:
                problems.append(Violation(
                    topology.name, "UNKNOWN_BACKUP_CYCLE",
                    f"{field_name}={schedule_id!r} declares no MaintenanceCycle",
                ))

    for relation, policy_id in {**GOVERNED_RELATIONS}.items():
        if policy_id not in BY_ID:
            problems.append(Violation(
                policy_id, "UNKNOWN_RELATION_POLICY",
                f"GOVERNED_RELATIONS[{relation!r}] names no declared policy",
            ))
    for relation, policy_id in {**GOVERNED_CLIENT_RELATIONS}.items():
        if policy_id not in BY_ID:
            problems.append(Violation(
                policy_id, "UNKNOWN_RELATION_POLICY",
                f"GOVERNED_CLIENT_RELATIONS[{relation!r}] names no declared policy",
            ))

    return problems


def open_owner_decisions() -> tuple[RetentionPolicy, ...]:
    """Policies that need an owner decision before they can be executed."""
    return tuple(p for p in POLICIES if p.status is Status.BLOCKED_OWNER_DECISION)


def owner_exemptions() -> tuple[RetentionPolicy, ...]:
    """Stores the owner has explicitly exempted from age-based retention.

    Countable on purpose. "How many exceptions to the ceiling exist, and who
    approved each one?" must be answerable in one call, and the answer today is
    one: the Workflow B GPS assignment log.
    """
    return tuple(p for p in POLICIES if p.is_owner_exempt)


def effective_mechanisms() -> tuple[tuple[RetentionPolicy, EffectiveMechanism], ...]:
    """Every place the host's real behaviour differs from the declared entry.

    The answer to "does anything runtime contradict what this registry says?",
    in one call and without a database. Two shapes appear: a store the OS
    empties sooner than the ceiling, and a scheduled mechanism configured not to
    delete at all.
    """
    return tuple(
        (policy, mechanism)
        for policy in POLICIES
        for mechanism in policy.effective_mechanisms
    )


def shorter_effective_lifecycles() -> tuple[RetentionPolicy, ...]:
    """Policies whose data really disappears sooner than the entry declares."""
    return tuple(p for p in POLICIES if p.effective_max is not None)


def simulated_mechanisms() -> tuple[RetentionPolicy, ...]:
    """Policies with a scheduled cleanup mechanism that currently deletes nothing.

    The distinction the owner asked for: a shorter policy that is CONFIGURED is
    not a shorter physical retention that is ENFORCED.
    """
    return tuple(p for p in POLICIES if p.non_enforcing_mechanisms)


def governance_summary() -> dict[str, list[str]]:
    """Every policy id grouped by WHICH KIND of governance it has.

    The operator distinction the owner asked for. Three of the four kinds are
    fine; the fourth — a store with no entry at all — is not representable here
    and is reported by `ungoverned()` instead.
    """
    grouped: dict[str, list[str]] = {}
    for policy in POLICIES:
        grouped.setdefault(policy.governance_class(), []).append(policy.policy_id)
    return {key: sorted(value) for key, value in sorted(grouped.items())}


# ---------------------------------------------------------------------------
# Coverage against a live database — READ ONLY
# ---------------------------------------------------------------------------
#
# `ungoverned()` has always been able to answer "which of these relations does
# no policy claim?", but the caller had to supply the relation list, so proving
# coverage against a real database meant writing an ad-hoc script every time.
# This section supplies the list from the database itself, with one SELECT
# against the system catalogue and nothing else.
#
# READ-ONLY IS A CONTRACT, NOT AN INTENTION. The only statement issued is the
# `pg_class` census below; the connection is opened, read and rolled back. No
# ledger row, no incident, no advisory lock, no DDL. An operator must be able to
# run this against production without thinking about it — which is exactly the
# property the audit found missing when a "dry run" persisted an incident.

#: Schemas that never hold governed platform data. `pg_catalog` and
#: `information_schema` are PostgreSQL's own; the rest are extension-owned.
_SYSTEM_SCHEMAS = ["pg_catalog", "information_schema", "pg_toast"]

#: `<> ALL(%s)` rather than `NOT IN %s`: psycopg3 binds a single parameter and
#: does not expand a Python tuple into a SQL list, so `IN %s` is a syntax error
#: at the server rather than a filter.
_RELATION_CENSUS_SQL = """
    SELECT n.nspname || '.' || c.relname AS relation
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p')
       AND n.nspname <> ALL(%s)
       AND n.nspname NOT LIKE 'pg_temp%%'
       AND n.nspname NOT LIKE 'pg_toast%%'
     ORDER BY 1
"""


def database_relations(dsn: str, *, timeout: int = 5) -> list[str]:
    """Every ordinary/partitioned table in one database. One read-only SELECT."""
    import psycopg  # noqa: PLC0415 - optional dependency

    with psycopg.connect(dsn, connect_timeout=timeout) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(_RELATION_CENSUS_SQL, (list(_SYSTEM_SCHEMAS),))
                rows = [str(row[0]) for row in cur.fetchall()]
        finally:
            conn.rollback()
    return rows


def _platform_dsn_from_env() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', '')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _client_accounts(dsn: str, *, timeout: int = 5) -> list[dict[str, Any]]:
    """Enabled client business databases, read from the platform control plane."""
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    with psycopg.connect(dsn, connect_timeout=timeout) as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT client_code, client_db_host, client_db_port,
                           client_db_name, client_db_user,
                           client_db_password_secret_ref
                      FROM workflow_a_control.client_account
                     WHERE enabled = true
                     ORDER BY client_code
                    """
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.rollback()


def coverage_report(
    *, platform_dsn: str | None = None, include_clients: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prove — or fail to prove — that every persisted relation has a policy.

    The half of governance that repository code alone cannot establish. The
    declared relation maps above are checked against `db/migrations/` by
    `ops/tests_manual/test_retention_registry.py`, which proves no MIGRATION can
    introduce an ungoverned table. It cannot prove anything about a table that
    exists in a real database without one — a Stage 3 loader destination built
    at runtime, a hand-created scratch table, a relation left by an operator.
    That is what this asks the database itself.

    Unreachable is reported, never guessed: a database that cannot be read
    yields `reachable: false` and `ungoverned: null`, so an absent answer can
    never be mistaken for a clean one.
    """
    moment = _utc(now)
    report: dict[str, Any] = {
        "schema": "log-platform-retention-coverage/v1",
        "generated_at": moment.isoformat(),
        "read_only": True,
        "global_default_months": HARD_RETENTION_MONTHS,
        "declared": {
            "policies": len(POLICIES),
            "platform_relations": len(GOVERNED_RELATIONS),
            "client_relations": len(GOVERNED_CLIENT_RELATIONS),
        },
        "governance_summary": governance_summary(),
        "owner_exemptions": [p.policy_id for p in owner_exemptions()],
        "open_owner_decisions": [p.policy_id for p in open_owner_decisions()],
        "violations": [
            {"policy_id": v.policy_id, "code": v.code, "detail": v.detail}
            for v in validate()
        ],
        # The registry's own answer to "does the runtime contradict the
        # declaration?", available with no database at all.
        "effective_mechanisms": [
            {"policy_id": policy.policy_id, **mechanism.as_dict()}
            for policy, mechanism in effective_mechanisms()
        ],
        "shorter_effective_lifecycles": [
            {"policy_id": p.policy_id, "declared": p.retention.describe(),
             "effective_max": _humanise(p.effective_max)}  # type: ignore[arg-type]
            for p in shorter_effective_lifecycles()
        ],
        "configured_but_not_enforced": [
            {"policy_id": p.policy_id,
             "mechanisms": [m.mechanism for m in p.non_enforcing_mechanisms]}
            for p in simulated_mechanisms()
        ],
        "databases": [],
    }

    dsn = platform_dsn or _platform_dsn_from_env()
    platform_entry: dict[str, Any] = {
        "scope": "platform", "database": os.getenv("POSTGRES_DB", "logdb"),
        "reachable": False, "relations": None, "governed": None,
        "ungoverned": None, "error": None,
    }
    accounts: list[dict[str, Any]] = []
    try:
        relations = database_relations(dsn)
        missing = ungoverned(relations)
        platform_entry.update({
            "reachable": True,
            "relations": len(relations),
            "governed": len(relations) - len(missing),
            "ungoverned": missing,
        })
        if include_clients:
            accounts = _client_accounts(dsn)
    except Exception as exc:
        platform_entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
    report["databases"].append(platform_entry)

    for account in accounts:
        code = str(account.get("client_code") or account.get("client_db_name") or "?")
        entry: dict[str, Any] = {
            "scope": code, "database": str(account.get("client_db_name")),
            "reachable": False, "relations": None, "governed": None,
            "ungoverned": None, "error": None,
        }
        try:
            from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: PLC0415

            password = resolve_secret(str(account["client_db_password_secret_ref"]))
            client_dsn = (
                f"host={account['client_db_host']} "
                f"port={int(account['client_db_port'])} "
                f"dbname={account['client_db_name']} "
                f"user={account['client_db_user']} password={password}"
            )
            relations = database_relations(client_dsn)
            missing = ungoverned(relations, client_business=True)
            entry.update({
                "reachable": True,
                "relations": len(relations),
                "governed": len(relations) - len(missing),
                "ungoverned": missing,
            })
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        report["databases"].append(entry)

    proven = [item for item in report["databases"] if item["reachable"]]
    unproven = [item for item in report["databases"] if not item["reachable"]]
    uncovered = sorted(
        f"{item['scope']}:{relation}"
        for item in proven for relation in (item["ungoverned"] or [])
    )
    report["summary"] = {
        "databases_inspected": len(proven),
        "databases_unreachable": len(unproven),
        "ungoverned_relations": len(uncovered),
        "ungoverned": uncovered,
        "violations": len(report["violations"]),
    }
    # An unreachable database is NOT coverage: it is an unanswered question, and
    # answering "clean" to an unanswered question is the failure mode this whole
    # registry exists to prevent.
    report["ok"] = bool(
        proven and not unproven and not uncovered and not report["violations"]
    )
    return report


def render_coverage(report: Mapping[str, Any]) -> str:
    lines = [
        f"Retention coverage — READ-ONLY, generated {report['generated_at']}",
        f"Global default: {report['global_default_months']} calendar months",
        "",
    ]
    rows: list[tuple[str, ...]] = [
        ("SCOPE", "DATABASE", "REACHABLE", "RELATIONS", "GOVERNED", "UNGOVERNED")
    ]
    for item in report["databases"]:
        rows.append((
            str(item["scope"]), str(item["database"]),
            "yes" if item["reachable"] else "NO",
            str(item["relations"] if item["relations"] is not None else "-"),
            str(item["governed"] if item["governed"] is not None else "-"),
            str(len(item["ungoverned"]) if item["ungoverned"] is not None else "?"),
        ))
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for index, row in enumerate(rows):
        lines.append("  ".join(
            cell.ljust(widths[position]) for position, cell in enumerate(row)
        ).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))

    for item in report["databases"]:
        if item["error"]:
            lines.append(f"  {item['scope']}: NOT INSPECTED — {item['error']}")
        for relation in (item["ungoverned"] or []):
            lines.append(f"  UNGOVERNED {item['scope']}: {relation}")

    lines.append("")
    lines.append("Governance classes: " + ", ".join(
        f"{key}={len(value)}" for key, value in report["governance_summary"].items()
    ))
    lines.append(f"Owner-approved exemptions: {len(report['owner_exemptions'])} "
                 f"({', '.join(report['owner_exemptions']) or 'none'})")
    lines.append(f"Open owner decisions: {len(report['open_owner_decisions'])}")
    lines.append(f"Registry violations: {len(report['violations'])}")
    for problem in report["violations"]:
        lines.append(f"  {problem['code']} [{problem['policy_id']}]: {problem['detail']}")

    lines.append("")
    lines.append("Effective mechanisms — where the host differs from the declaration:")
    if not report["effective_mechanisms"]:
        lines.append("  (none declared)")
    for item in report["effective_mechanisms"]:
        marker = "ENFORCING" if item["enforcing"] else "NOT ENFORCING"
        extent = f", effective max {item['approximate_max']}" if item["approximate_max"] else ""
        host = ", host-managed" if item["host_managed"] else ""
        lines.append(f"  {item['policy_id']}: {marker}{extent}{host}")
        lines.append(f"    {item['mechanism']}")
        if item["schedule_id"]:
            lines.append(f"    recurring mechanism: {item['schedule_id']} "
                         f"(ops/schedule_catalog.py)")
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"Databases inspected: {summary['databases_inspected']}; "
        f"unreachable: {summary['databases_unreachable']}; "
        f"ungoverned relations: {summary['ungoverned_relations']}"
    )
    lines.append(f"Coverage proven: {'yes' if report['ok'] else 'NO'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def as_dict(now: datetime | None = None) -> dict[str, Any]:
    moment = _utc(now)
    return {
        "schema": "log-platform-retention-registry/v1",
        "generated_at": moment.isoformat(),
        "global_default": {
            "policy_id": GLOBAL_POLICY_ID,
            "months": HARD_RETENTION_MONTHS,
            "unit": Unit.MONTHS.value,
            "description": f"{HARD_RETENTION_MONTHS} calendar months",
            "cutoff": hard_retention_cutoff(moment).isoformat(),
            "minimum_span_days": minimum_span_days(),
        },
        "backup_topology": [topology.as_dict() for topology in BACKUP_SETS],
        "maintenance_cycles": [
            cycle.as_dict() for cycle in
            sorted(MAINTENANCE_CYCLES.values(), key=lambda item: item.schedule_id)
        ],
        "backup_shadow": {
            "note": BACKUP_SHADOW_NOTE,
            "worst_case_extra_days": backup_shadow_days(),
        },
        "journald": {
            "max_retention_seconds": int(journald_max_retention().total_seconds()),
            "max_file_sec": int(JOURNALD_MAX_FILE_SEC.total_seconds()),
            "vacuum_cycle_seconds": int(JOURNALD_VACUUM_CYCLE.total_seconds()),
        },
        "policies": [
            {**policy.as_dict(),
             "cutoff": (policy.cutoff(moment).isoformat() if policy.cutoff(moment) else None),
             "enforcement_cutoff": (
                 policy.enforcement_cutoff(moment).isoformat()
                 if policy.enforcement_cutoff(moment) else None
             )}
            for policy in POLICIES
        ],
        "violations": [
            {"policy_id": v.policy_id, "code": v.code, "detail": v.detail}
            for v in validate()
        ],
        "open_owner_decisions": [p.policy_id for p in open_owner_decisions()],
        "governance_summary": governance_summary(),
        "owner_exemptions": [
            {"policy_id": p.policy_id, "store": p.store,
             **(p.exemption.as_dict() if p.exemption else {})}
            for p in owner_exemptions()
        ],
        # Where the host's real behaviour differs from the declaration above:
        # an OS-owned cleanup that is shorter, or a scheduled mechanism that
        # deletes nothing. Present at the top level so an operator does not have
        # to scan every policy to find them.
        "effective_mechanisms": [
            {"policy_id": policy.policy_id, **mechanism.as_dict()}
            for policy, mechanism in effective_mechanisms()
        ],
        "shorter_effective_lifecycles": [
            p.policy_id for p in shorter_effective_lifecycles()
        ],
        "configured_but_not_enforced": [
            p.policy_id for p in simulated_mechanisms()
        ],
    }


def render_table(now: datetime | None = None) -> str:
    moment = _utc(now)
    header = ("POLICY", "BACKEND", "RETENTION", "MODE", "STATUS", "AGE BASIS",
              "CYCLE", "BACKUP", "LEAD", "CLEANUP")
    rows = [header]
    for policy in POLICIES:
        rows.append((
            policy.policy_id,
            policy.backend.value,
            (f"{policy.override.months} months (owner override)"
             if policy.override is not None else
             ("none (OWNER-APPROVED EXEMPTION)" if policy.is_owner_exempt
              else policy.retention.describe())),
            policy.mode.value,
            policy.status.value,
            policy.age_basis or "-",
            (_humanise(policy.maintenance_cycle().guaranteed_interval)
             if policy.maintenance_cycle() else
             (_humanise(policy.self_enforcing_lead) if policy.self_enforcing_lead else "-")),
            _humanise(PLATFORM_BACKUP_SET.shadow) if policy.in_backup_set else "-",
            _humanise(policy.enforcement_lead()) if policy.is_age_based else "-",
            policy.cleanup_job or "-",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = [
        f"Global default: {HARD_RETENTION_MONTHS} calendar months "
        f"(deadline cutoff {hard_retention_cutoff(moment).isoformat()})",
        f"Backup topology: {PLATFORM_BACKUP_SET.name}; covers "
        f"{', '.join(sorted(item.value for item in PLATFORM_BACKUP_SET.covered_backends))}; "
        f"shadow {_humanise(PLATFORM_BACKUP_SET.shadow)}",
        "LEAD is how far ahead of the deadline a sweep must delete, so that no "
        "copy — live or backup — outlives it.",
        "",
    ]
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    problems = validate()
    lines.append("")
    lines.append(f"Violations: {len(problems)}")
    for problem in problems:
        lines.append(f"  {problem}")
    blocked = open_owner_decisions()
    lines.append(f"Open owner decisions: {len(blocked)}")
    for policy in blocked:
        lines.append(f"  {policy.policy_id}: {policy.blocker}")
    exempt = owner_exemptions()
    lines.append(
        f"Owner-approved exemptions from age-based retention: {len(exempt)} "
        f"(explicitly approved and centrally registered — NOT unmanaged, NOT "
        f"blocked, NOT missing an anchor)"
    )
    for policy in exempt:
        ex = policy.exemption
        lines.append(
            f"  {policy.policy_id}: no age-based retention; approved by "
            f"{ex.approved_by if ex else '?'} on {ex.approved_on if ex else '?'}"
        )
        lines.append(f"    store: {policy.store}")
    summary = governance_summary()
    lines.append("Governance classes: " + ", ".join(
        f"{key}={len(value)}" for key, value in summary.items()
    ))

    # The declared table above is what the platform PROMISES. This block is
    # where the host disagrees with it, and the owner asked for both to be
    # readable from one surface.
    mechanisms = effective_mechanisms()
    lines.append("")
    lines.append(
        f"Effective mechanisms (host behaviour differing from the declaration "
        f"above): {len(mechanisms)}"
    )
    for policy, mechanism in mechanisms:
        if mechanism.enforcing and mechanism.approximate_max is not None:
            headline = (
                f"EFFECTIVE MAX {_humanise(mechanism.approximate_max)} — shorter "
                f"than the declared {policy.retention.describe()}"
            )
        elif mechanism.enforcing:
            headline = "enforcing; imposes no shorter maximum"
        else:
            headline = "CONFIGURED BUT NOT ENFORCING — deletes nothing today"
        lines.append(f"  {policy.policy_id}: {headline}")
        lines.append(f"    store:     {policy.store}")
        lines.append(
            f"    mechanism: {mechanism.mechanism}"
            f"{' (host-managed)' if mechanism.host_managed else ''}"
        )
        lines.append(
            f"    recurring: {mechanism.schedule_id or '-'} "
            f"(see ops/schedule_catalog.py)"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the authoritative platform retention registry",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Print nothing but violations; exit non-zero if any exist",
    )
    parser.add_argument(
        "--coverage", action="store_true",
        help=(
            "Prove persistent-store coverage against a live database. "
            "READ-ONLY: one pg_class census per database, rolled back; no "
            "ledger row, no incident, no lock, no DDL. Exits non-zero unless "
            "every inspected database is reachable and fully governed."
        ),
    )
    parser.add_argument(
        "--clients", action="store_true",
        help=(
            "With --coverage, also inspect every enabled client business "
            "database from workflow_a_control.client_account"
        ),
    )
    args = parser.parse_args(argv)

    problems = validate()
    if args.validate_only:
        for problem in problems:
            print(problem)
        print(f"{len(problems)} violation(s)")
        return 1 if problems else 0

    if args.coverage:
        try:
            from dotenv import load_dotenv  # noqa: PLC0415

            load_dotenv(REPO_ROOT / ".env", override=False)
        except ImportError:
            pass
        report = coverage_report(include_clients=bool(args.clients))
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            print(render_coverage(report))
        return 0 if report["ok"] else 1

    if args.format == "json":
        print(json.dumps(as_dict(), indent=2, sort_keys=True))
    else:
        print(render_table())
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
