#!/usr/bin/env python3
"""THE authoritative catalogue of every recurring platform execution.

WHY THIS MODULE EXISTS.
    Recurring work on this platform is started by four different mechanisms —
    installed systemd timers, systemd timers that still live under
    `ops/systemd/proposed/`, the database-driven Workflow A dispatcher reading
    `workflow_a_control.client_dataset_schedule`, and housekeeping the Worker
    performs when a host job calls it. An operator asking "what runs, when, and
    is it on?" had to read three directories and one table, and no single
    artefact could be trusted to be complete.

    This module answers that question in one place.

WHY IT IS NOT A SECOND LIST.
    A hand-maintained inventory drifts the moment somebody adds a timer, and a
    drifted inventory is worse than none because it reads as authoritative. So
    the registry below holds only the facts that CANNOT be derived — the
    logical identity of a schedule, who owns it, whether it is retention work —
    and every schedule fact that *can* be derived is read at call time from the
    source that actually decides it:

      * cadence, timezone, persistence, command: parsed from the unit files in
        `ops/systemd/`, which is what an operator installs;
      * installed/enabled state and next fire: read from `systemctl` when this
        runs on the host (read-only; never required);
      * per-client Workflow A schedules: read from the platform database.

    `validate()` then closes the loop in the other direction: a `.timer` in the
    repository with no registry entry, or a registry entry naming a unit that
    does not exist, is a failure — so a new recurring job cannot quietly come
    into existence outside this catalogue.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SYSTEMD_DIR = REPO_ROOT / "ops" / "systemd"
PROPOSED_DIR = SYSTEMD_DIR / "proposed"


class Mechanism(str, Enum):
    SYSTEMD_TIMER = "systemd_timer"
    #: The Workflow A dispatcher: a systemd timer ticks, but WHAT runs and WHEN
    #: is decided by rows in the platform database.
    DATABASE_SCHEDULER = "database_scheduler"
    #: A long-running service that paces itself internally.
    SERVICE_INTERNAL_LOOP = "service_internal_loop"
    #: Housekeeping performed by the Cloudflare Worker, triggered by a host job
    #: rather than by a scheduler of its own.
    HOST_DRIVEN_REMOTE = "host_driven_remote"
    #: A recurring mechanism the OPERATING SYSTEM owns. This repository neither
    #: ships nor configures it, and there is no unit file here to parse — but it
    #: touches a governed path, so leaving it out made the effective lifecycle of
    #: that path invisible from the one place recurring execution is supposed to
    #: be readable.
    HOST_OS = "host_os"


class Lifecycle(str, Enum):
    #: Installed on the production host and firing.
    PRODUCTION_ACTIVE = "production_active"
    #: Unit exists in the repository under `proposed/`, not (yet) the installed
    #: contract. May still be installed on the host — the runtime column says.
    PROPOSED = "proposed"
    #: Deliberately not scheduled; operators run it by hand.
    MANUAL_ONLY = "manual_only"
    #: Existed once, retained for reference.
    HISTORICAL = "historical"


@dataclass(frozen=True)
class ScheduleEntry:
    """The facts that cannot be derived. Everything else is read at call time."""

    schedule_id: str
    title: str
    owner_domain: str
    mechanism: Mechanism
    lifecycle: Lifecycle
    #: Unit basename (without extension) under `ops/systemd/` or
    #: `ops/systemd/proposed/`. `None` for non-systemd mechanisms.
    unit: str | None = None
    #: Where the effective schedule is actually decided, in words.
    source_of_truth: str = ""
    #: Environment/client scope.
    scope: str = "platform"
    #: True when this schedule exists to enforce retention or run maintenance.
    retention_related: bool = False
    #: Retention policy ids in `ops/retention_registry.py` this schedule executes.
    retention_policies: tuple[str, ...] = ()
    #: The schedule that actually causes this one to run, when it is not its own
    #: timer. `eco-dashboard-maintenance` has no timer: the platform sweep calls
    #: it. Naming the driver is what stops "it runs when somebody sends e-mail"
    #: from being an unexamined assumption.
    driven_by: str | None = None
    #: True when the unit's `.service` is a host-installed TEMPLATE
    #: (`log-job@.service`) rather than a file in this repository. Declared, not
    #: inferred, so a genuinely missing service file is still a drift finding.
    service_from_host_template: bool = False
    #: For mechanisms with no unit file: the command an operator would run.
    command: str | None = None
    #: Cadence for non-systemd mechanisms, where nothing can be parsed.
    declared_cadence: str | None = None
    declared_timezone: str | None = None
    notes: str | None = None


SCHEDULES: tuple[ScheduleEntry, ...] = (
    ScheduleEntry(
        schedule_id="log-backup",
        title="Nightly platform backup (PostgreSQL dump + MinIO tarball)",
        owner_domain="operations",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PRODUCTION_ACTIVE,
        unit="log-backup",
        source_of_truth="ops/systemd/log-backup.timer",
        retention_related=True,
    ),
    ScheduleEntry(
        schedule_id="log-platform-prune",
        title="Platform retention prune (runs, logs, artifacts, MinIO objects)",
        owner_domain="platform-core",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PRODUCTION_ACTIVE,
        unit="log-platform-prune",
        source_of_truth="ops/systemd/log-platform-prune.timer",
        retention_related=True,
        retention_policies=(
            "platform_db.public.runs",
            "platform_db.public.logs",
            "platform_db.public.artifacts",
            "object_store.minio.platform_artifacts",
            "platform_db.workflow_a_control.provider_request_log",
        ),
    ),
    ScheduleEntry(
        schedule_id="backup-retention",
        title="Backup set expiry (14 days, floored at 3 verified sets)",
        owner_domain="operations",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="backup-retention",
        source_of_truth="ops/systemd/proposed/backup-retention.timer",
        retention_related=True,
        retention_policies=("filesystem.platform_backup_sets",),
        notes="Installed and enabled on the production host despite living under proposed/.",
    ),
    ScheduleEntry(
        schedule_id="platform-hard-retention",
        title="Global 13-calendar-month hard-retention sweep",
        owner_domain="platform-core",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="platform-hard-retention",
        source_of_truth="ops/systemd/proposed/platform-hard-retention.timer",
        retention_related=True,
        retention_policies=(
            "platform_db.public.artifacts_reference_excluded",
            "platform_db.public.portal_audit_events",
            "platform_db.public.suspected_bug_incidents",
            "platform_db.public.suspected_bug_occurrences",
            "platform_db.public.suspected_bug_email_outbox",
            "platform_db.public.database_export_jobs",
            "platform_db.public.database_export_attempt_objects",
            "platform_db.public.portal_generated_report_instances",
            "platform_db.ingest.imap_message",
            "platform_db.ingest.raw_file",
            "platform_db.ops_control.environment_identity_promotion",
            "platform_db.ops_control.run_reconciliation",
            "platform_db.ops_control.watchdog_observation",
            "platform_db.workflow_a_control.client_schedule_run_history",
            "platform_db.workflow_a_control.client_dataset_recovery_run",
            "platform_db.workflow_a_control.trip_delivery_lag_daily",
            "client_db.workflow_a_registered_tables",
            "client_db.eco_driving_email_send_log",
            "client_db.eco_drivers_id_chart",
            "client_db.eco_dashboard_delivery_operation",
            "client_db.workflow_b_stage3_report_tables",
            "client_db.workflow_b_gps_assignment_import_runs",
            "client_db.legacy_backup_tables",
            "client_db.v2_staging_tables",
            "filesystem.workflow_b_report_files",
            "filesystem.workflow_b_stage2_cleaned",
        ),
        notes=(
            "Added by the retention-governance task. Dry-run by default; the "
            "unit ships with --execute and must be rehearsed before enabling."
        ),
    ),
    ScheduleEntry(
        schedule_id="database-export-cleanup",
        title="Database Explorer async export expiry (3 calendar days)",
        owner_domain="database-explorer",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="database-export-cleanup",
        source_of_truth="ops/systemd/proposed/database-export-cleanup.timer",
        retention_related=True,
        retention_policies=("platform_db.public.database_export_jobs",),
    ),
    ScheduleEntry(
        schedule_id="log-job@retention-purge",
        title="Workflow A per-client, per-table retention purge",
        owner_domain="workflow-a",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="log-job@retention-purge",
        source_of_truth=(
            "ops/systemd/proposed/log-job@retention-purge.timer for the cadence; "
            "workflow_a_control.client_table_retention for what it deletes"
        ),
        retention_related=True,
        retention_policies=("client_db.workflow_a_registered_tables",),
        notes=(
            "Installed and enabled on the host, but its ExecStart passes "
            "dry_run:true — so it currently deletes nothing anywhere."
        ),
    ),
    ScheduleEntry(
        schedule_id="log-job@dispatcher",
        title="Workflow A dispatcher tick (claims at most one due fire)",
        owner_domain="workflow-a",
        mechanism=Mechanism.DATABASE_SCHEDULER,
        lifecycle=Lifecycle.PROPOSED,
        unit="log-job@dispatcher",
        source_of_truth=(
            "ops/systemd/proposed/log-job@dispatcher.timer for the tick; "
            "workflow_a_control.client_dataset_schedule for every actual schedule"
        ),
        scope="per client, per dataset",
        notes="Expand the per-client fires with --database.",
    ),
    ScheduleEntry(
        schedule_id="log-workflow-b",
        title="Workflow B orchestrator (mail fetch through Stage 3 load)",
        owner_domain="workflow-b",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="log-workflow-b",
        source_of_truth="ops/systemd/proposed/log-workflow-b.timer",
    ),
    ScheduleEntry(
        schedule_id="log-job@jobs.mail.fetch_reports",
        title="Standalone Workflow B mail fetch",
        owner_domain="workflow-b",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="log-job@jobs.mail.fetch_reports",
        service_from_host_template=True,
        source_of_truth="ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer",
        notes="Superseded on the host by log-workflow-b; installed but disabled.",
    ),
    ScheduleEntry(
        schedule_id="execution-watchdog",
        title="Missing-run and stuck-run watchdog",
        owner_domain="operations",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="execution-watchdog",
        source_of_truth="ops/systemd/proposed/execution-watchdog.timer",
    ),
    ScheduleEntry(
        schedule_id="disk-space-monitor",
        title="Filesystem headroom guard",
        owner_domain="operations",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="disk-space-monitor",
        source_of_truth="ops/systemd/proposed/disk-space-monitor.timer",
    ),
    ScheduleEntry(
        schedule_id="suspected-bug-email-worker",
        title="suspected_bug alert outbox delivery",
        owner_domain="platform-core",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="suspected-bug-email-worker",
        source_of_truth="ops/systemd/proposed/suspected-bug-email-worker.timer",
    ),
    ScheduleEntry(
        schedule_id="database-export-worker",
        title="Database Explorer async export worker (continuous)",
        owner_domain="database-explorer",
        mechanism=Mechanism.SERVICE_INTERNAL_LOOP,
        lifecycle=Lifecycle.PROPOSED,
        unit="database-export-worker",
        source_of_truth="ops/systemd/proposed/database-export-worker.service ExecStart flags",
        retention_related=True,
        retention_policies=("platform_db.public.database_export_jobs",),
        declared_cadence="--poll-seconds 30, --cleanup-interval-seconds 3600",
        notes="A Type=simple service, not a timer; it paces itself.",
    ),
    ScheduleEntry(
        schedule_id="eco-dashboard-maintenance",
        title="Driver Eco Dashboard authorization-state maintenance (D1 + R2)",
        owner_domain="eco-dashboard",
        mechanism=Mechanism.HOST_DRIVEN_REMOTE,
        lifecycle=Lifecycle.PRODUCTION_ACTIVE,
        driven_by="platform-hard-retention",
        source_of_truth=(
            "ops/hard_retention.py::sweep_eco_dashboard_maintenance — driven by "
            "platform-hard-retention.timer. jobs/ecodriving_dashboard/"
            "eco_mailing_integration.py additionally calls the same route at the "
            "end of a mailing run, which can only make the interval shorter"
        ),
        scope="Cloudflare Worker driver-eco-dashboard",
        retention_related=True,
        retention_policies=(
            "cloudflare_d1.eco_session",
            "cloudflare_d1.eco_capability",
            "cloudflare_d1.eco_publication_operation",
            "cloudflare_r2.driver_eco_snapshots",
        ),
        command="POST /api/publish/maintenance (publisher-authenticated)",
        declared_cadence="weekly, with the platform hard-retention sweep",
        notes=(
            "The Worker has no cron trigger of its own by design, and Eco "
            "MAILING is not a retention scheduler: four of five production "
            "clients have every Eco schedule disabled, so D1 and R2 would have "
            "been governed only while somebody kept sending e-mail. The platform "
            "sweep now calls the same publisher-authenticated route itself, over "
            "the same validated endpoint and credential — no new route, no cron "
            "trigger, no unauthenticated surface."
        ),
    ),
    ScheduleEntry(
        schedule_id="journald-retention-vacuum",
        title="journald retention vacuum (13-calendar-month ceiling)",
        owner_domain="operations",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.PROPOSED,
        unit="journald-retention-vacuum",
        source_of_truth=(
            "ops/systemd/proposed/journald-retention-vacuum.timer for the "
            "cadence; ops/systemd/proposed/journald-retention.conf for the "
            "retention value, itself derived from "
            "ops.retention_registry.journald_max_retention()"
        ),
        retention_related=True,
        retention_policies=("host.journald",),
        notes=(
            "journald applies MaxRetentionSec on rotation, which log traffic "
            "drives; a quiet host would stop enforcing, so the vacuum is "
            "scheduled rather than assumed."
        ),
    ),
    ScheduleEntry(
        schedule_id="systemd-tmpfiles-clean",
        title="Host /tmp cleanup — the real lifecycle of the Stage 2 scratch root",
        owner_domain="operations",
        mechanism=Mechanism.HOST_OS,
        lifecycle=Lifecycle.PRODUCTION_ACTIVE,
        unit=None,
        source_of_truth=(
            "/usr/lib/systemd/system/systemd-tmpfiles-clean.timer for the "
            "cadence and /usr/lib/tmpfiles.d/tmp.conf (`D /tmp 1777 root root "
            "30d`) for the age — both HOST-managed; this repository ships "
            "neither and changed neither"
        ),
        scope="host /tmp, including /tmp/log-platform-stage2",
        retention_related=True,
        retention_policies=("filesystem.workflow_b_stage2_cleaned",),
        command="/usr/bin/systemd-tmpfiles --clean (systemd-tmpfiles-clean.service)",
        declared_cadence="OnBootSec=15min; OnUnitActiveSec=1d",
        declared_timezone="host local time",
        notes=(
            "CATALOGUED BECAUSE IT GOVERNS A GOVERNED PATH. "
            "`/tmp/log-platform-stage2/cleaned` is a registered retention root "
            "whose declared policy is the 13-month ceiling, but nothing on this "
            "host lets a file there live that long: tmpfiles cleans /tmp on an "
            "age of 30 days, and the `D` type additionally empties it at boot. "
            "The effective lifecycle is therefore an OS mechanism, and an "
            "operator reading only the repository's own timers could not learn "
            "that. Declared in the registry as the policy's `effective_"
            "mechanisms`; this entry is the recurring half. "
            "It is deliberately NOT a repository timer: there is no unit file to "
            "parse, the lever is not in ops/systemd/, and reconfiguring the "
            "host's tmpfiles policy is not something this repository does."
        ),
    ),
    ScheduleEntry(
        schedule_id="log-job@jobs.reports.demo",
        title="Demo report job",
        owner_domain="platform-core",
        mechanism=Mechanism.SYSTEMD_TIMER,
        lifecycle=Lifecycle.HISTORICAL,
        unit=None,
        source_of_truth="host-installed only; no unit file in this repository",
        notes=(
            "Installed on the host and disabled. Recorded so runtime drift "
            "detection does not report it as an unknown platform timer."
        ),
    ),
)

BY_ID: Mapping[str, ScheduleEntry] = {entry.schedule_id: entry for entry in SCHEDULES}


# ---------------------------------------------------------------------------
# Derivation: repository unit files
# ---------------------------------------------------------------------------

def _parse_unit_sections(path: Path) -> dict[str, dict[str, list[str]]]:
    """Parse a systemd unit into `SECTION -> KEY -> [values]`.

    Hand-rolled rather than `configparser` because systemd allows a key to
    repeat (two `OnCalendar=` lines mean two fires) and `configparser` would
    silently keep only the last one — which is exactly the fact an operator
    reading this catalogue must not lose.

    The SECTION matters as much as the key, and flattening it away is what let a
    whole class of behaviour stay invisible here: `Requires=` under `[Unit]` on
    a timer activates the paired service when the TIMER STARTS, which is not a
    calendar event at all. A catalogue that reads only `[Timer]` cannot see it.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        sections.setdefault(current, {}).setdefault(
            key.strip(), []
        ).append(value.strip())
    return sections


def _parse_unit(path: Path) -> dict[str, list[str]]:
    """The section-flattened view, for callers that only need a key's values."""
    flat: dict[str, list[str]] = {}
    for keys in _parse_unit_sections(path).values():
        for key, values in keys.items():
            flat.setdefault(key, []).extend(values)
    return flat


#: `[Unit]` directives that PULL IN another unit when this one is started. They
#: are start-time dependencies, not triggers, so on a timer they cause execution
#: that no `OnCalendar=` in the file accounts for.
ACTIVATING_DEPENDENCIES = (
    "Requires", "Wants", "BindsTo", "Requisite", "PartOf", "Upholds",
)


def unit_semantics(entry: "ScheduleEntry") -> dict[str, Any]:
    """Material NON-CALENDAR execution semantics from a timer's `[Unit]` section.

    Deliberately narrow. This is not a systemd reimplementation and must not
    become one: it reports the activating dependencies a timer declares, and
    whether any of them names the very service the timer triggers — the one
    property that turns `systemctl start <timer>` (and `enable --now`, and every
    boot) into an unscheduled run of the job.

    That is not a hypothetical. On 2026-08-29 `enable --now
    platform-hard-retention.timer` ran a full production sweep one second after
    the timer started, with systemd recording no trigger at all, because the
    timer carried `Requires=` on its own service. The catalogue described the
    calendar correctly and had no way to say that.
    """
    facts: dict[str, Any] = {
        "activating_dependencies": {},
        "activates_paired_service_on_start": False,
        "after": [],
        "condition_directives": {},
    }
    if entry.unit is None:
        return facts
    timer = unit_path(entry.unit, ".timer")
    if timer is None:
        return facts
    unit_section = _parse_unit_sections(timer).get("Unit", {})
    triggered = set(_parse_unit_sections(timer).get("Timer", {}).get("Unit", []))
    for key in ACTIVATING_DEPENDENCIES:
        values = [value for value in unit_section.get(key, []) if value]
        if values:
            facts["activating_dependencies"][key] = values
            if triggered & set(values):
                facts["activates_paired_service_on_start"] = True
    facts["after"] = list(unit_section.get("After", []))
    facts["condition_directives"] = {
        key: values for key, values in unit_section.items()
        if key.startswith(("Condition", "Assert"))
    }
    return facts


def unit_path(unit: str, suffix: str) -> Path | None:
    """Locate `unit.suffix`, preferring the installed contract over proposed."""
    for directory in (SYSTEMD_DIR, PROPOSED_DIR):
        candidate = directory / f"{unit}{suffix}"
        if candidate.is_file():
            return candidate
    # Templated instance units (`log-job@dispatcher`) share `log-job@.service`.
    if "@" in unit:
        base = unit.split("@", 1)[0]
        for directory in (SYSTEMD_DIR, PROPOSED_DIR):
            candidate = directory / f"{base}@{suffix}"
            if candidate.is_file():
                return candidate
    return None


def repository_facts(entry: ScheduleEntry) -> dict[str, Any]:
    """Cadence, timezone and command, read from the unit files themselves."""
    facts: dict[str, Any] = {
        "timer_file": None,
        "service_file": None,
        "cadence": entry.declared_cadence,
        "timezone": entry.declared_timezone,
        "persistent": None,
        "command": entry.command,
    }
    if entry.unit is None:
        return facts

    timer = unit_path(entry.unit, ".timer")
    if timer is not None:
        facts["timer_file"] = str(timer.relative_to(REPO_ROOT))
        parsed = _parse_unit(timer)
        cadence: list[str] = []
        cadence.extend(parsed.get("OnCalendar", []))
        for key in ("OnBootSec", "OnUnitActiveSec", "OnUnitInactiveSec"):
            cadence.extend(f"{key}={value}" for value in parsed.get(key, []))
        if cadence:
            facts["cadence"] = "; ".join(cadence)
        # systemd puts the zone at the end of an OnCalendar expression; anything
        # else is the host's local time, and saying so beats leaving it blank.
        zones = {
            value.rsplit(" ", 1)[-1]
            for value in parsed.get("OnCalendar", [])
            if " " in value and not value.rsplit(" ", 1)[-1][:1].isdigit()
        }
        facts["timezone"] = ", ".join(sorted(zones)) if zones else "host local time"
        persistent = parsed.get("Persistent")
        if persistent:
            facts["persistent"] = persistent[-1].lower() in ("1", "true", "yes", "on")

    service = unit_path(entry.unit, ".service")
    if service is not None:
        facts["service_file"] = str(service.relative_to(REPO_ROOT))
        execs = [value for value in _parse_unit(service).get("ExecStart", []) if value]
        if execs:
            facts["command"] = execs[-1]
    return facts


# ---------------------------------------------------------------------------
# Derivation: does a retention schedule actually delete anything?
# ---------------------------------------------------------------------------
#
# WHY THIS IS DERIVED AND NOT DECLARED. `log-job@retention-purge.timer` is
# installed, enabled and firing weekly, and its service passes
# `{"dry_run":true}` — so the catalogue could truthfully say "per-client
# retention purge, weekly, enabled" while the job deletes nothing anywhere. An
# operator reading that alongside a 65-day per-client policy would reasonably
# conclude a shorter physical retention is being enforced. It is not.
#
# A declared flag would drift the first time somebody changed the unit. Reading
# it out of the ExecStart the unit actually carries cannot.

#: Substrings in an ExecStart that mean "plan, do not delete". Matched
#: case-insensitively against the whole command line.
_SIMULATION_MARKERS = ('"dry_run":true', "'dry_run':true", "--dry-run", "--plan-only")


def derive_enforcement(entry: "ScheduleEntry") -> str | None:
    """`enforcing` / `simulated_dry_run` / `unknown` for a retention schedule.

    `None` for a schedule that is not retention work, where the question does
    not arise.
    """
    if not entry.retention_related:
        return None
    command = repository_facts(entry).get("command")
    if not command:
        return "unknown"
    lowered = str(command).lower().replace(" ", "")
    if any(marker.replace(" ", "") in lowered for marker in _SIMULATION_MARKERS):
        return "simulated_dry_run"
    return "enforcing"


# ---------------------------------------------------------------------------
# Derivation: the guaranteed maintenance interval
# ---------------------------------------------------------------------------
#
# WHY RETENTION CARES. A cleanup that deletes "older than 13 months" and runs
# weekly leaves records alive for up to another week past their deadline. The
# fix is to delete early by one guaranteed interval — which means retention
# needs a number for "how long until the next certain cleanup opportunity", and
# that number must come from the cadence the unit actually has, not from a
# comment. `ops.retention_registry.MAINTENANCE_CYCLES` declares it; this
# function derives it from the unit file; `validate()` refuses a disagreement.

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_SPAN_UNITS = {
    "us": 1e-6, "ms": 1e-3, "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def _parse_time_span(text: str) -> float | None:
    """systemd time span → seconds. Returns `None` for anything unrecognised."""
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*([a-zA-Z]*)", text.strip()):
        factor = _SPAN_UNITS.get(unit.lower() or "s")
        if factor is None:
            return None
        total += int(amount) * factor
        matched = True
    return total if matched else None


def _daily_times(expressions: Sequence[str]) -> list[int] | None:
    """Seconds-of-day for a set of plain `*-*-* HH:MM:SS` expressions."""
    seconds: list[int] = []
    for raw in expressions:
        parts = raw.split()
        if len(parts) >= 2 and parts[-1][:1].isalpha():
            parts = parts[:-1]  # drop a trailing timezone
        if len(parts) != 2 or parts[0] != "*-*-*":
            return None
        clock = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", parts[1])
        if clock is None:
            return None
        seconds.append(
            int(clock.group(1)) * 3600 + int(clock.group(2)) * 60
            + int(clock.group(3) or 0)
        )
    return sorted(seconds) if seconds else None


def derive_guaranteed_interval(entry: ScheduleEntry) -> timedelta | None:
    """The WORST-case gap between two fires, read from the unit file.

    Returns `None` when the cadence uses a form this function does not model —
    which `validate()` treats as "the declared cycle stands", never as zero.
    Deriving a wrong small number would be worse than deriving none: it would
    shorten the retention lead and reintroduce exactly the late-deletion defect.
    """
    if entry.unit is None:
        return None

    if entry.mechanism is Mechanism.SERVICE_INTERNAL_LOOP:
        # A Type=simple service has no timer; its cadence is an ExecStart flag.
        service = unit_path(entry.unit, ".service")
        if service is None:
            return None
        for value in _parse_unit(service).get("ExecStart", []):
            flag = re.search(r"--cleanup-interval-seconds\s+(\d+)", value)
            if flag:
                return timedelta(seconds=int(flag.group(1)))
        return None

    timer = unit_path(entry.unit, ".timer")
    if timer is None:
        return None
    parsed = _parse_unit(timer)

    monotonic = parsed.get("OnUnitActiveSec") or parsed.get("OnUnitInactiveSec")
    if monotonic:
        seconds = _parse_time_span(monotonic[-1])
        return timedelta(seconds=seconds) if seconds else None

    calendars = [value.strip() for value in parsed.get("OnCalendar", []) if value.strip()]
    if not calendars:
        return None

    if len(calendars) == 1:
        single = calendars[0]
        if single == "hourly":
            return timedelta(hours=1)
        if single == "daily":
            return timedelta(days=1)
        if single == "weekly":
            return timedelta(days=7)
        # `*:0/5` — every 5 minutes past every hour.
        every = re.fullmatch(r"\*:0/(\d+)", single)
        if every:
            return timedelta(minutes=int(every.group(1)))
        # `Sun *-*-* HH:MM:SS [TZ]` — one fire a week.
        head = single.split()[0]
        if head in _WEEKDAYS:
            return timedelta(days=7)

    times = _daily_times(calendars)
    if times is None:
        return None
    if len(times) == 1:
        return timedelta(days=1)
    # Largest gap between consecutive fires, wrapping midnight.
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    gaps.append(86400 - times[-1] + times[0])
    return timedelta(seconds=max(gaps))


# ---------------------------------------------------------------------------
# Derivation: host runtime (read-only, optional)
# ---------------------------------------------------------------------------

#: A HEURISTIC, and no longer the whole answer. It catches a platform-looking
#: timer nobody catalogued and no repository file explains — which is the only
#: thing a name pattern can honestly do. It is deliberately not extended for
#: every new unit: see `platform_timer_names()`.
PLATFORM_UNIT_PREFIXES = (
    "log-", "backup-retention", "database-export-", "disk-space-monitor",
    "execution-watchdog", "suspected-bug-", "platform-hard-retention",
    "journald-retention-",
)


def _systemctl(*args: str) -> str | None:
    if shutil.which("systemctl") is None:
        return None
    try:
        done = subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "").strip() or None


def runtime_facts(entry: ScheduleEntry) -> dict[str, Any]:
    """Read-only host state. Absent runtime is `None`, never a guess."""
    facts: dict[str, Any] = {
        "installed": None, "enabled": None, "next_run": None, "last_run": None,
    }
    if entry.unit is None:
        return facts
    target = f"{entry.unit}.timer"
    shown = _systemctl("show", target, "--property=LoadState,UnitFileState,"
                       "NextElapseUSecRealtime,LastTriggerUSec")
    if shown is None:
        return facts
    properties = dict(
        line.split("=", 1) for line in shown.splitlines() if "=" in line
    )
    load_state = properties.get("LoadState")
    facts["installed"] = load_state == "loaded"
    facts["enabled"] = properties.get("UnitFileState") or None
    for key, target_key in (
        ("NextElapseUSecRealtime", "next_run"), ("LastTriggerUSec", "last_run"),
    ):
        raw = properties.get(key, "")
        if raw and raw not in ("0", "n/a", "infinity"):
            try:
                facts[target_key] = datetime.fromtimestamp(
                    int(raw) / 1_000_000, tz=timezone.utc
                ).isoformat()
            except (ValueError, OverflowError, OSError):
                facts[target_key] = raw
    return facts


def platform_timer_names() -> frozenset[str]:
    """Every timer name this repository considers its own, derived not listed.

    WHY THE PREFIX LIST WAS NOT ENOUGH. Runtime drift detection asked
    "which installed timers look like ours?" using `PLATFORM_UNIT_PREFIXES`
    alone, and `journald-retention-vacuum.timer` — shipped by this repository,
    installed and enabled on the production host, executing a governed retention
    policy — matched none of them. It was therefore invisible to the scan that
    exists to prove no platform timer escapes this catalogue. Only its own
    presence in `SCHEDULES` kept it visible at all, which is precisely the
    guarantee the scan is supposed to provide independently.

    The fix is to derive the inventory from what the repository CONTROLS rather
    than from what its names happen to start with:

      * every `.timer` file in `ops/systemd/` and `ops/systemd/proposed/` — the
        set an operator installs, which grows automatically with the repository;
      * every unit named by an entry here, including host-template instances
        that have no `.timer` file of their own;
      * the prefix heuristic, kept for the case a name pattern is genuinely the
        only available signal: a platform-looking timer installed on the host
        that no repository file and no entry explains.

    The first two mean a new repository timer can never again escape discovery
    for want of a matching prefix, and nobody has to remember to extend a list.
    """
    names = set(repository_timer_units())
    names |= {entry.unit for entry in SCHEDULES if entry.unit}
    return frozenset(names)


def host_platform_timers() -> tuple[str, ...] | None:
    """Every timer unit on the host this repository claims or that looks like ours.

    Returns `None` when `systemctl` cannot answer, so an absent runtime is never
    mistaken for an empty one.
    """
    listing = _systemctl("list-unit-files", "--type=timer", "--no-legend", "--no-pager")
    if listing is None:
        return None
    known = platform_timer_names()
    names = []
    for line in listing.splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.endswith(".timer"):
            continue
        bare = name[: -len(".timer")]
        if bare in known or name.startswith(PLATFORM_UNIT_PREFIXES):
            names.append(bare)
    return tuple(sorted(set(names)))


# ---------------------------------------------------------------------------
# Derivation: database-driven Workflow A schedules
# ---------------------------------------------------------------------------

def database_schedules(dsn: str | None = None) -> list[dict[str, Any]] | None:
    """Per-client Workflow A fires, read from the table that decides them.

    Returns `None` when no database is reachable — the catalogue still renders,
    it just says so instead of inventing rows.
    """
    try:
        import psycopg  # noqa: PLC0415 - optional dependency
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError:
        return None
    if dsn is None:
        host = os.getenv("POSTGRES_HOST", "127.0.0.1")
        dsn = (
            f"host={host} port={os.getenv('POSTGRES_PORT', '5432')} "
            f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
            f"user={os.getenv('POSTGRES_USER', '')} "
            f"password={os.getenv('POSTGRES_PASSWORD', '')}"
        )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT s.client_code, s.dataset_name, s.enabled, s.frequency,
                           s.run_time::text AS run_time, s.timezone,
                           s.day_of_week, s.day_of_month, s.lookback_days,
                           d.job_module
                      FROM workflow_a_control.client_dataset_schedule s
                      JOIN workflow_a_control.dataset_registry d
                        ON d.dataset_name = s.dataset_name
                     ORDER BY s.client_code, s.dataset_name, s.enabled DESC
                    """
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        return None


def _cadence_of(row: Mapping[str, Any]) -> str:
    frequency = row.get("frequency")
    run_time = row.get("run_time") or "?"
    if frequency == "weekly":
        return f"weekly, day_of_week={row.get('day_of_week')} at {run_time}"
    if frequency == "monthly":
        return f"monthly, day_of_month={row.get('day_of_month')} at {run_time}"
    return f"{frequency} at {run_time}"


# ---------------------------------------------------------------------------
# Drift validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Drift:
    code: str
    subject: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.code} [{self.subject}]: {self.detail}"


def repository_timer_units() -> tuple[str, ...]:
    units: set[str] = set()
    for directory in (SYSTEMD_DIR, PROPOSED_DIR):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.timer"):
            units.add(path.name[: -len(".timer")])
    return tuple(sorted(units))


def validate(*, include_runtime: bool = False) -> list[Drift]:
    """Every way the catalogue and reality can disagree.

    Repository checks always run and are deterministic. Runtime checks run only
    when asked for and only when `systemctl` answers, so this is usable in CI
    and on the host alike.
    """
    problems: list[Drift] = []

    registered_units = {
        entry.unit for entry in SCHEDULES
        if entry.unit and entry.mechanism is not Mechanism.SERVICE_INTERNAL_LOOP
    }
    for unit in repository_timer_units():
        if unit not in registered_units:
            problems.append(Drift(
                "UNREGISTERED_TIMER", unit,
                "a .timer exists in ops/systemd/ with no entry in SCHEDULES; "
                "every recurring job must be catalogued",
            ))

    seen: set[str] = set()
    for entry in SCHEDULES:
        if entry.schedule_id in seen:
            problems.append(Drift("DUPLICATE_ID", entry.schedule_id, "declared twice"))
        seen.add(entry.schedule_id)

        if entry.unit and entry.lifecycle is not Lifecycle.HISTORICAL:
            if entry.mechanism is Mechanism.SERVICE_INTERNAL_LOOP:
                if unit_path(entry.unit, ".service") is None:
                    problems.append(Drift(
                        "MISSING_UNIT_FILE", entry.schedule_id,
                        f"no {entry.unit}.service in ops/systemd/",
                    ))
            else:
                if unit_path(entry.unit, ".timer") is None:
                    problems.append(Drift(
                        "MISSING_UNIT_FILE", entry.schedule_id,
                        f"no {entry.unit}.timer in ops/systemd/",
                    ))
                if (not entry.service_from_host_template
                        and unit_path(entry.unit, ".service") is None):
                    problems.append(Drift(
                        "MISSING_UNIT_FILE", entry.schedule_id,
                        f"no {entry.unit}.service in ops/systemd/",
                    ))
        if not entry.source_of_truth.strip():
            problems.append(Drift(
                "MISSING_SOURCE_OF_TRUTH", entry.schedule_id,
                "an entry must say where its schedule is actually decided",
            ))

        # A timer whose [Unit] pulls in the very service its [Timer] triggers
        # runs that service on every `systemctl start` and every boot, outside
        # the calendar this catalogue reports. That is a schedule fact, so it
        # belongs here and not only in the systemd unit-contract test.
        semantics = unit_semantics(entry)
        if semantics["activates_paired_service_on_start"]:
            problems.append(Drift(
                "TIMER_ACTIVATES_ITS_SERVICE", entry.schedule_id,
                f"[Unit] "
                f"{sorted(semantics['activating_dependencies'])} names the "
                f"service this timer triggers, so starting the timer — and "
                f"every boot — runs the job outside its OnCalendar. "
                f"`Unit=` in [Timer] is the only binding a timer needs",
            ))

    # Every retention policy that names a schedule must find it here, and every
    # schedule that claims to run a policy must name a real one.
    try:
        from ops import retention_registry
    except Exception:  # pragma: no cover - import guard only
        retention_registry = None  # type: ignore[assignment]
    if retention_registry is not None:
        for policy in retention_registry.POLICIES:
            if policy.schedule_id and policy.schedule_id not in BY_ID:
                problems.append(Drift(
                    "RETENTION_SCHEDULE_MISSING", policy.policy_id,
                    f"retention policy names schedule {policy.schedule_id!r}, "
                    f"which this catalogue does not define",
                ))
            # The registry may declare that something OTHER than its own
            # cleanup job is what really bounds a store — host tmpfiles, a
            # Worker compaction pass, a dry-run purge. Whatever it names has to
            # be a schedule an operator can look up here, or "centrally
            # visible" is a claim rather than a property.
            for mechanism in getattr(policy, "effective_mechanisms", ()):
                if mechanism.schedule_id and mechanism.schedule_id not in BY_ID:
                    problems.append(Drift(
                        "EFFECTIVE_MECHANISM_SCHEDULE_MISSING", policy.policy_id,
                        f"an effective mechanism names schedule "
                        f"{mechanism.schedule_id!r}, which this catalogue does "
                        f"not define; the recurring mechanism behind a "
                        f"governed store's real lifecycle must be readable here",
                    ))
        for entry in SCHEDULES:
            for policy_id in entry.retention_policies:
                if policy_id not in retention_registry.BY_ID:
                    problems.append(Drift(
                        "UNKNOWN_RETENTION_POLICY", entry.schedule_id,
                        f"claims to execute unknown policy {policy_id!r}",
                    ))

            # THE check that keeps the retention lead honest. A timer slowed
            # from daily to weekly without updating its declared cycle would
            # silently shrink every dependent policy's lead and start deleting
            # after the deadline.
            declared = retention_registry.MAINTENANCE_CYCLES.get(entry.schedule_id)
            derived = derive_guaranteed_interval(entry)
            if declared is not None and derived is not None:
                if declared.guaranteed_interval != derived:
                    problems.append(Drift(
                        "MAINTENANCE_CYCLE_DRIFT", entry.schedule_id,
                        f"unit cadence implies a guaranteed interval of {derived}, "
                        f"but the retention registry declares "
                        f"{declared.guaranteed_interval}; the retention lead "
                        f"derived from it would be wrong",
                    ))
            if (entry.retention_related and declared is None
                    and entry.driven_by is None
                    and entry.lifecycle is not Lifecycle.HISTORICAL):
                problems.append(Drift(
                    "MISSING_MAINTENANCE_CYCLE", entry.schedule_id,
                    "a retention schedule must declare a guaranteed interval in "
                    "ops.retention_registry.MAINTENANCE_CYCLES",
                ))
            if entry.driven_by is not None and entry.driven_by not in BY_ID:
                problems.append(Drift(
                    "UNKNOWN_DRIVER", entry.schedule_id,
                    f"driven_by names unknown schedule {entry.driven_by!r}",
                ))

    if include_runtime:
        host_units = host_platform_timers()
        if host_units is not None:
            known = {entry.unit for entry in SCHEDULES if entry.unit}
            known |= {"log-job@jobs.reports.demo"}
            for unit in host_units:
                if unit not in known:
                    problems.append(Drift(
                        "UNREGISTERED_HOST_TIMER", unit,
                        "a platform-looking timer is installed on the host but is "
                        "not in this catalogue",
                    ))
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def as_dict(*, include_runtime: bool = False, include_database: bool = False) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for entry in SCHEDULES:
        record: dict[str, Any] = {
            "schedule_id": entry.schedule_id,
            "title": entry.title,
            "owner_domain": entry.owner_domain,
            "mechanism": entry.mechanism.value,
            "lifecycle": entry.lifecycle.value,
            "scope": entry.scope,
            "unit": entry.unit,
            "source_of_truth": entry.source_of_truth,
            "driven_by": entry.driven_by,
            "guaranteed_interval_seconds": (
                int(derive_guaranteed_interval(entry).total_seconds())
                if derive_guaranteed_interval(entry) else None
            ),
            "retention_related": entry.retention_related,
            "retention_policies": list(entry.retention_policies),
            # Whether this retention schedule actually deletes, read from the
            # ExecStart the unit carries rather than declared here.
            "enforcement": derive_enforcement(entry),
            "notes": entry.notes,
            "repository": repository_facts(entry),
            # Material non-calendar execution semantics from [Unit].
            "unit_semantics": unit_semantics(entry),
        }
        if include_runtime:
            record["runtime"] = runtime_facts(entry)
        entries.append(record)

    payload: dict[str, Any] = {
        "schema": "log-platform-schedule-catalog/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schedules": entries,
        "drift": [
            {"code": d.code, "subject": d.subject, "detail": d.detail}
            for d in validate(include_runtime=include_runtime)
        ],
    }
    if include_database:
        rows = database_schedules()
        payload["workflow_a_client_schedules"] = (
            None if rows is None else [
                {
                    "client_code": row["client_code"],
                    "dataset_name": row["dataset_name"],
                    "enabled": bool(row["enabled"]),
                    "cadence": _cadence_of(row),
                    "timezone": row["timezone"],
                    "lookback_days": row["lookback_days"],
                    "job_module": row["job_module"],
                    "mechanism": Mechanism.DATABASE_SCHEDULER.value,
                    "source_of_truth": "workflow_a_control.client_dataset_schedule",
                }
                for row in rows
            ]
        )
    return payload


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = []
    for index, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            out.append("  ".join("-" * width for width in widths))
    return out


def render_table(*, include_runtime: bool = False, include_database: bool = False) -> str:
    header = ("SCHEDULE", "MECHANISM", "LIFECYCLE", "ENABLED", "CADENCE", "TZ",
              "RETENTION", "ENFORCES", "SOURCE", "COMMAND")
    rows: list[tuple[str, ...]] = [header]
    for entry in SCHEDULES:
        facts = repository_facts(entry)
        runtime = runtime_facts(entry) if include_runtime else {}
        enabled = (
            str(runtime.get("enabled") or "-") if include_runtime
            else ("installed" if entry.lifecycle is Lifecycle.PRODUCTION_ACTIVE else "-")
        )
        enforcement = derive_enforcement(entry)
        rows.append((
            entry.schedule_id,
            entry.mechanism.value,
            entry.lifecycle.value,
            enabled,
            str(facts["cadence"] or "-"),
            str(facts["timezone"] or "-"),
            "yes" if entry.retention_related else "no",
            # "yes" is not the same answer as "it is scheduled". A schedule can
            # be installed, enabled and firing and still delete nothing.
            {"enforcing": "yes", "simulated_dry_run": "DRY RUN",
             "unknown": "?", None: "-"}[enforcement],
            facts["timer_file"] or entry.source_of_truth or "-",
            (str(facts["command"] or "-"))[:96],
        ))
    lines = _table(rows)

    simulated = [
        entry for entry in SCHEDULES
        if derive_enforcement(entry) == "simulated_dry_run"
    ]
    if simulated:
        lines.append("")
        lines.append(
            "Retention schedules that run but DELETE NOTHING (ExecStart is "
            "dry-run):"
        )
        for entry in simulated:
            lines.append(
                f"  {entry.schedule_id}: {entry.title} — "
                f"policies {', '.join(entry.retention_policies) or '-'}"
            )
        lines.append(
            "  A shorter policy configured for one of these is CONFIGURED, not "
            "enforced; the global hard-retention sweep remains the ceiling that "
            "is actually applied."
        )

    activating = [
        entry for entry in SCHEDULES
        if unit_semantics(entry)["activates_paired_service_on_start"]
    ]
    if activating:
        lines.append("")
        lines.append(
            "Timers that ALSO run their service when the timer is started "
            "(and therefore at every boot), outside the cadence above:"
        )
        for entry in activating:
            deps = unit_semantics(entry)["activating_dependencies"]
            lines.append(f"  {entry.schedule_id}: [Unit] {deps}")

    if include_database:
        rows_db = database_schedules()
        lines.append("")
        if rows_db is None:
            lines.append(
                "Workflow A per-client schedules: database not reachable "
                "(workflow_a_control.client_dataset_schedule is the source of truth)"
            )
        else:
            lines.append(
                f"Workflow A per-client schedules "
                f"(workflow_a_control.client_dataset_schedule, {len(rows_db)} rows)"
            )
            db_rows: list[tuple[str, ...]] = [
                ("CLIENT", "DATASET", "ENABLED", "CADENCE", "TZ", "LOOKBACK", "JOB")
            ]
            for row in rows_db:
                db_rows.append((
                    str(row["client_code"] or "-"),
                    str(row["dataset_name"]),
                    "yes" if row["enabled"] else "no",
                    _cadence_of(row),
                    str(row["timezone"]),
                    str(row["lookback_days"]),
                    str(row["job_module"]),
                ))
            lines.extend(_table(db_rows))

    problems = validate(include_runtime=include_runtime)
    lines.append("")
    lines.append(f"Schedule drift: {len(problems)}")
    for problem in problems:
        lines.append(f"  {problem}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect every recurring platform schedule from one place",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument(
        "--runtime", action="store_true",
        help="Also read installed/enabled state and next fire from systemctl (read-only)",
    )
    parser.add_argument(
        "--database", action="store_true",
        help="Also expand the per-client Workflow A schedules from the platform database",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Print nothing but drift; exit non-zero if any exists",
    )
    args = parser.parse_args(argv)

    problems = validate(include_runtime=args.runtime)
    if args.validate_only:
        for problem in problems:
            print(problem)
        print(f"{len(problems)} drift finding(s)")
        return 1 if problems else 0

    if args.format == "json":
        print(json.dumps(
            as_dict(include_runtime=args.runtime, include_database=args.database),
            indent=2, sort_keys=True, default=str,
        ))
    else:
        print(render_table(include_runtime=args.runtime, include_database=args.database))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
