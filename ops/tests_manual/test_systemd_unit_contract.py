#!/usr/bin/env python3
"""Semantic contract tests for the P0 systemd units (Codex BLOCKER 2 / HIGH 1).

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_systemd_unit_contract.py

These assert *behaviour implied by systemd's own rules*, not the presence of
strings. The previous generation of this file checked that
`OnFailure=log-platform-unit-failure@%n.service` appeared somewhere in a drop-in;
it passed while the directive sat under `[Service]`, where systemd ignores it.
Every check here is written so that the defect it guards against would fail it.

`systemd-analyze verify` is the real authority and is *not* run here: it requires
a writable working directory that this environment does not provide. The host
command is recorded in `docs/11_operational_readiness.md` and must be run before
installation. Where systemd's own binaries can answer a question offline —
`systemd-escape` for specifier expansion — they are used rather than reimplemented.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROPOSED = REPO_ROOT / "ops/systemd/proposed"

FAILURE_HANDLER = "log-platform-unit-failure@.service"
ONFAILURE_TARGET = "log-platform-unit-failure@%n.service"

# Timer-driven oneshot jobs. The timer is the only thing that may schedule them.
#
# HISTORICAL, AND NO LONGER THE SUBJECT OF THE INVARIANT. This hand-maintained
# tuple was the whole coverage of "a timer-driven job must not also be
# boot-enabled", and nobody extended it when platform-hard-retention.service was
# written — so the newest and by far the most destructive timer-driven service
# on the platform, one whose ExecStart carries `--execute` against every client
# business database, was the one service the invariant did not cover. It shipped
# with `[Install] WantedBy=multi-user.target`, which would have run a full
# retention sweep at every boot the moment anyone enabled the service.
#
# `timer_driven_services()` below now derives the subject from the timers on
# disk, so a new one is covered the day its timer file appears. This tuple is
# kept only as the assertion that the derivation still finds what was already
# known — a hand-maintained list may check a derivation, never replace it.
TIMER_DRIVEN_SERVICES = (
    "backup-retention.service",
    "execution-watchdog.service",
    "disk-space-monitor.service",
    "suspected-bug-email-worker.service",
    "platform-hard-retention.service",
)

# Timer-paired services that still carry [Install]. Same escape-hatch shape as
# TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY: the invariant covers disk MINUS this
# list, so a new unit is guarded by construction, and each name here has to keep
# being true.
#
# These five pre-date the invariant and belong to other workstreams. None is a
# destructive retention job: the log-job templates, the Workflow B orchestrator
# and the export cleanup are idempotent working jobs, and
# journald-retention-vacuum removes only archived journal files already past a
# 385-day horizon — a boot-time run of any of them repeats work rather than
# destroying data that a schedule was protecting. Removing [Install] from them
# is a change to those workstreams' installation procedure, which this task is
# not authorized to make.
SERVICES_WITH_KNOWN_INSTALL_SECTION = (
    "database-export-cleanup.service",
    "journald-retention-vacuum.service",
    "log-job@dispatcher.service",
    "log-job@retention-purge.service",
    "log-workflow-b.service",
)
NEW_TIMERS = (
    "backup-retention.timer",
    "execution-watchdog.timer",
    "disk-space-monitor.timer",
    "suspected-bug-email-worker.timer",
)
# The retention-governance timers. They are a separate tuple because their
# paired services are not in TIMER_DRIVEN_SERVICES — platform-hard-retention
# carries its own [Install] — but the start-invariant below applies to them
# identically, and did not cover them when they were written: both shipped with
# `Requires=<their service>` in [Unit] and both ran their service during
# `enable --now` on 2026-08-29. See test_no_timer_is_outside_the_start_invariant.
RETENTION_TIMERS = (
    "platform-hard-retention.timer",
    "journald-retention-vacuum.timer",
)
# Timers whose [Unit] still names their own service, so starting them starts it.
#
# EMPTY, AND THAT IS THE RESULT. It held three entries — database-export-cleanup,
# log-job@dispatcher and log-job@retention-purge — which the retention/schedule
# governance audit re-examined rather than grandfathered. Two were idempotent and
# one was not: with `dry_run:true` flipped off, a timer start or a boot would have
# turned log-job@retention-purge into an unscheduled DELETE against client
# business data. Correct behaviour is not "harmless today", so all three lost
# their `Requires=` and the guard below now covers every proposed timer.
#
# The tuple stays as the escape hatch it was designed to be: the start-invariant
# derives its subject from disk MINUS this list, so anything added to
# ops/systemd/proposed/ is guarded by default and escaping the guard takes a
# deliberate, reviewable edit here.
TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY: tuple[str, ...] = ()
# The declared cadence of each retention timer. Pinned so that a schedule change
# is a conscious edit in two places, not a drive-by in one.
RETENTION_TIMER_CADENCE = {
    "platform-hard-retention.timer": "Sun *-*-* 05:00:00",
    "journald-retention-vacuum.timer": "*-*-* 05:45:00",
}
# Units that must never route their own failure: they are the alert path.
ALERT_PATH_UNITS = (FAILURE_HANDLER, "suspected-bug-email-worker.service")

# --- B1-R: alerting runtime execution-surface isolation ---------------------
#
# The development checkout. It is a legitimate string in documentation, in
# host-bound units and in the launcher's own comments, so nothing below bans it
# textually — the assertions are about the *executable source contract* of
# specific units.
DEV_TREE = "/opt/log-platform"
RELEASE_ROOT = "/opt/log-platform-release"
OPS_RUNNER = "/usr/local/bin/log-ops-runner.sh"
OPS_RUNNER_SOURCE = PROPOSED / "log-ops-runner.sh"

# Operational sidecars whose Python source root must be the active release, so
# that activating a release is sufficient to change the code they run. Both are
# B1 components: the worker stamps the heartbeat, the watchdog asserts it.
RELEASE_BOUND_SERVICES = {
    "suspected-bug-email-worker.service": "ops.suspected_bug_email_worker",
    "execution-watchdog.service": "ops.execution_watchdog",
    # The two long-running services. Until this slice they were the last
    # production processes whose code came from the development checkout: the
    # release pointer moved and they kept serving whatever the worktree happened
    # to contain at their next restart. `uvicorn` is a third-party module rather
    # than an ops.* one, which is the point — the launcher binds the *tree* the
    # process imports from, and `api.main:app` is then resolved inside it.
    "log-platform-api.service": "uvicorn",
    "database-export-worker.service": "ops.database_export_worker",
    # Same module as the worker, from the same release: a cleanup pass running a
    # different build would expire artifacts under one version's rules.
    "database-export-cleanup.service": "ops.database_export_worker",
}
# Units whose release must contain these paths before the launcher will exec.
# Declared per unit rather than inferred: the API needs the `.env` runtime-link
# that api/platform_prune.py reads in-process, the export worker needs the API
# module it imports, and neither requirement is derivable from the module name.
REQUIRED_RELEASE_FILES = {
    "log-platform-api.service": {"api/main.py", ".env"},
    "database-export-worker.service": {"ops/database_export_worker.py", "api/main.py"},
    "database-export-cleanup.service": {"ops/database_export_worker.py", "api/main.py"},
}
# The pre-release-boundary bootstrap pair. They deliberately still name a
# checkout — a fresh host has no release root — so they are pinned here rather
# than silently drifting into the release-bound set or being "fixed" by someone
# who reads only the diff.
DEVELOPMENT_BOOTSTRAP_SOURCES = (
    REPO_ROOT / "ops/systemd/log-platform-api.service.example",
    REPO_ROOT / "ops/systemd/install_log_platform_api_service.sh",
)
RELEASE_ROOT_GUARD = "RELEASE_BOUNDARY_PRESENT"
# Deliberately host-bound; see the rationale block in the unit itself. Pinned
# here so that repointing it becomes a conscious edit rather than a drive-by.
HOST_BOUND_ALERT_SERVICES = {
    FAILURE_HANDLER: "ops.systemd_failure_adapter",
}
# ops/suspected_bug_email_worker.py owns exit code 3 (dead letter). A launcher
# that reused it would make a broken release look like a dead mail channel.
WORKER_RESERVED_EXIT_CODES = {3}
# Real unit names that must survive the OnFailure= round trip unchanged.
MONITORED_UNITS = (
    "log-workflow-b.service",
    "log-backup.service",
    "log-platform-prune.service",
    "log-job@dispatcher.service",
    "execution-watchdog.service",
    "disk-space-monitor.service",
    "backup-retention.service",
)


def directives(path: Path) -> list[tuple[str, str, str]]:
    """(section, key, value) for every real directive.

    A hand-rolled parser rather than configparser: systemd allows repeated keys
    and repeated sections, and both carry meaning. Collapsing them the way
    configparser does would hide exactly the kind of defect this file exists to
    catch.
    """
    out: list[tuple[str, str, str]] = []
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            out.append((section, key.strip(), value.strip()))
    return out


def guarded_timers() -> list[str]:
    """Every proposed timer the start-invariant covers: disk minus the exceptions."""
    return sorted(
        path.name for path in PROPOSED.glob("*.timer")
        if path.name not in TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY
    )


def timer_driven_services() -> dict[str, str]:
    """`service file -> the timer that triggers it`, derived from disk.

    Every `Unit=` a proposed timer names, kept only when that service is a file
    in this repository — a host-installed template (`log-job@.service`) has no
    [Install] of ours to assert about. Derived rather than listed so a service
    added with its timer is covered on the day it appears, which is exactly what
    did not happen for platform-hard-retention.service.
    """
    found: dict[str, str] = {}
    for timer in sorted(PROPOSED.glob("*.timer")):
        for target in values(timer, "Unit", section="Timer"):
            if (PROPOSED / target).is_file():
                found[target] = timer.name
    return found


def sections(path: Path) -> set[str]:
    return {section for section, _, _ in directives(path)}


def values(path: Path, key: str, *, section: str | None = None) -> list[str]:
    return [
        value for sec, name, value in directives(path)
        if name == key and (section is None or sec == section)
    ]


def systemd_unescape(text: str) -> str:
    """Real systemd escaping semantics, from systemd's own binary."""
    return subprocess.run(
        ["systemd-escape", "--unescape", "--", text],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_onfailure_is_a_unit_directive_everywhere() -> None:
    """`OnFailure=` under `[Service]` is ignored with only a log warning."""
    paths = [PROPOSED / name for name in ("execution-watchdog.service",
                                          "disk-space-monitor.service",
                                          "backup-retention.service")]
    paths += sorted(PROPOSED.glob("*.service.d/95-onfailure.conf"))
    assert len(paths) >= 7, f"expected the full monitored set, found {len(paths)}"
    for path in paths:
        placements = [sec for sec, key, _ in directives(path) if key == "OnFailure"]
        assert placements, f"{path.name}: no OnFailure= at all"
        assert set(placements) == {"Unit"}, (
            f"{path}: OnFailure= in {placements}; systemd honours it only in [Unit]"
        )
        assert ONFAILURE_TARGET in values(path, "OnFailure"), path.name
    print("PASS: every monitored unit routes failure from [Unit], not [Service]")


def test_starting_a_timer_does_not_start_its_service() -> None:
    """A timer's [Unit] dependencies activate when the TIMER starts.

    `Requires=backup-retention.service` therefore makes `systemctl start
    backup-retention.timer` — and `enable --now` — run destructive retention
    immediately. `Unit=` is the only link a timer needs.
    """
    activating = ("Requires", "Wants", "BindsTo", "Requisite", "PartOf", "Upholds")
    guarded = guarded_timers()
    assert set(NEW_TIMERS) | set(RETENTION_TIMERS) <= set(guarded), (
        "a timer this invariant is named for dropped off disk"
    )
    for name in guarded:
        path = PROPOSED / name
        target = values(path, "Unit", section="Timer")
        assert target == [name.replace(".timer", ".service")], f"{name}: Unit= {target}"
        for key in activating:
            offenders = [v for v in values(path, key) if v in target]
            assert not offenders, (
                f"{name}: {key}={offenders} activates the service when the timer starts"
            )
    print("PASS: starting a timer cannot activate its own service")


def test_the_start_invariant_exception_list_stays_truthful() -> None:
    """The escape hatch may not be a dumping ground, in either direction.

    The invariant above existed before the retention timers were written and
    still did not catch them, because its subject was a hand-maintained tuple
    that nobody extended. It now reads the directory, so the only way to be
    unguarded is to be named here — and being named here has to keep being true.
    """
    on_disk = {path.name for path in PROPOSED.glob("*.timer")}
    missing = set(TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY) - on_disk
    assert not missing, f"exception list names timers that do not exist: {sorted(missing)}"
    activating = ("Requires", "Wants", "BindsTo", "Requisite", "PartOf", "Upholds")
    for name in TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY:
        path = PROPOSED / name
        target = values(path, "Unit", section="Timer")
        still_offends = any(v in target for key in activating for v in values(path, key))
        assert still_offends, (
            f"{name}: no longer activates its service on timer start; delete it "
            "from TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY so the guard covers it"
        )
    print(f"PASS: {len(guarded_timers())} of {len(on_disk)} proposed timers guarded; "
          f"{len(TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY)} named exceptions, all still true")


def test_retention_timers_keep_their_cadence_and_catch_up() -> None:
    """Removing the [Unit] dependency must not touch the schedule contract.

    `Persistent=true` is the property that survives a host being off over a
    Sunday 05:00; it is unrelated to the start-time pull-in that was removed,
    and the two are easy to conflate. `Unit=` in [Timer] must remain the
    authoritative binding.
    """
    for name, cadence in RETENTION_TIMER_CADENCE.items():
        path = PROPOSED / name
        assert values(path, "OnCalendar", section="Timer") == [cadence], (
            f"{name}: cadence changed"
        )
        assert values(path, "Persistent", section="Timer") == ["true"], (
            f"{name}: Persistent must stay true — a missed calendar event still "
            "has to execute after the host comes back"
        )
        assert values(path, "Unit", section="Timer") == [
            name.replace(".timer", ".service")], f"{name}: Unit= binding changed"
        assert values(path, "AccuracySec", section="Timer") == ["1min"], name
        assert values(path, "WantedBy", section="Install") == ["timers.target"], name
        assert "Unit" in sections(path), f"{name}: lost its [Unit] section"
        assert values(path, "Description", section="Unit"), f"{name}: no Description"
    assert values(PROPOSED / "platform-hard-retention.timer",
                  "RandomizedDelaySec", section="Timer") == ["0"], (
        "the weekly sweep is ordered against the nightly window; a randomized "
        "delay would blur that ordering"
    )
    print("PASS: retention cadence, catch-up and Unit= binding are unchanged")


def test_timer_driven_services_cannot_be_enabled_standalone() -> None:
    """`WantedBy=multi-user.target` on a oneshot job means it also runs at boot.

    For backup-retention.service that is destructive deletion on every boot; for
    platform-hard-retention.service it is a full `--execute` sweep of every
    client business database, every governed filesystem root and the Cloudflare
    stores. With no [Install] section, `systemctl enable` fails loudly instead.

    THE SUBJECT IS DERIVED FROM DISK. The previous version iterated a
    hand-maintained tuple, and platform-hard-retention.service was written later
    and never added to it — so the invariant existed and the most destructive
    unit on the platform was outside it. Every timer-paired service in this
    repository is now covered by construction; escaping takes a named,
    reviewable entry in SERVICES_WITH_KNOWN_INSTALL_SECTION.
    """
    derived = timer_driven_services()
    assert set(TIMER_DRIVEN_SERVICES) <= set(derived), (
        f"a known timer-driven service dropped off disk: "
        f"{sorted(set(TIMER_DRIVEN_SERVICES) - set(derived))}"
    )
    covered = [
        name for name in sorted(derived)
        if name not in SERVICES_WITH_KNOWN_INSTALL_SECTION
    ]
    assert set(TIMER_DRIVEN_SERVICES) <= set(covered), (
        "a known timer-driven service is being excused by the exception list"
    )
    for name in covered:
        path = PROPOSED / name
        assert "Install" not in sections(path), (
            f"{name}: has [Install]; enabling it wires a boot activation the "
            f"timer ({derived[name]}) already owns"
        )
    for name in NEW_TIMERS:
        assert values(PROPOSED / name, "WantedBy", section="Install") == ["timers.target"], name
    print(f"PASS: {len(covered)} of {len(derived)} timer-paired services are not "
          f"independently installable; the timers are")


def test_the_install_exception_list_stays_truthful() -> None:
    """The escape hatch may not be a dumping ground, in either direction.

    Exactly the shape that already guards the timer-start invariant: a name here
    must still be a timer-paired service that really does carry [Install], so
    removing one cannot be forgotten and the exception cannot outlive the fact.
    """
    derived = timer_driven_services()
    for name in SERVICES_WITH_KNOWN_INSTALL_SECTION:
        assert name in derived, (
            f"{name} is excused from the [Install] invariant but is no longer a "
            f"timer-paired service in this repository; delete it from "
            f"SERVICES_WITH_KNOWN_INSTALL_SECTION"
        )
        assert "Install" in sections(PROPOSED / name), (
            f"{name}: no longer has [Install]; delete it from "
            f"SERVICES_WITH_KNOWN_INSTALL_SECTION so the guard covers it"
        )
    assert "platform-hard-retention.service" not in SERVICES_WITH_KNOWN_INSTALL_SECTION, (
        "the retention sweep is the reason this invariant was widened; it may "
        "not be excused from it"
    )
    print(f"PASS: {len(SERVICES_WITH_KNOWN_INSTALL_SECTION)} named [Install] "
          f"exceptions, all still true")


def test_the_retention_sweep_has_no_boot_activation_path() -> None:
    """The specific unit the audit found, asserted by name and end to end.

    Its timer must be the only installable half, and the service must keep the
    ExecStart and cadence it already had — the correction removes a boot
    activation, not a schedule or a command.
    """
    service = PROPOSED / "platform-hard-retention.service"
    timer = PROPOSED / "platform-hard-retention.timer"
    assert "Install" not in sections(service), (
        "platform-hard-retention.service must not be independently enableable: "
        "with WantedBy=multi-user.target a full --execute retention sweep runs "
        "at every boot, outside its Sunday 05:00 calendar"
    )
    assert values(timer, "WantedBy", section="Install") == ["timers.target"]
    assert values(timer, "OnCalendar", section="Timer") == ["Sun *-*-* 05:00:00"]
    assert values(timer, "Persistent", section="Timer") == ["true"]
    execs = values(service, "ExecStart", section="Service")
    assert len(execs) == 1 and execs[0].endswith("-m ops.hard_retention --execute"), execs
    print("PASS: the retention sweep runs on its timer and on nothing else")


def test_destructive_timer_does_not_replay_missed_fires() -> None:
    """Persistent=true replays a calendar event missed while the host was off."""
    assert values(PROPOSED / "backup-retention.timer", "Persistent") == ["false"], (
        "backup-retention.service deletes backups; a catch-up fire would run it "
        "unattended immediately after a boot"
    )
    # Non-destructive monitors legitimately want the catch-up.
    for name in ("execution-watchdog.timer", "disk-space-monitor.timer",
                 "suspected-bug-email-worker.timer"):
        assert values(PROPOSED / name, "Persistent") == ["true"], name
    print("PASS: only the destructive timer refuses catch-up fires")


def test_failed_unit_name_survives_the_onfailure_round_trip() -> None:
    """`%I` unescapes, and systemd's escaping maps '-' to '/'.

    With `OnFailure=log-platform-unit-failure@%n.service` the instance is the
    failed unit name, so the handler must read `%i` (verbatim). `%I` turns
    log-workflow-b.service into log/workflow/b.service — a unit that does not
    exist. Checked against systemd's own escaping binary, not a reimplementation.
    """
    handler = PROPOSED / FAILURE_HANDLER
    exec_start = values(handler, "ExecStart", section="Service")
    assert len(exec_start) == 1, exec_start
    command = exec_start[0]
    assert command.endswith(" %i"), f"handler must pass %i verbatim, got: {command}"
    assert "%I" not in command, "%I unescapes the instance and corrupts the unit name"

    corrupted = []
    for unit in MONITORED_UNITS:
        # %n == the failed unit name == the instance systemd creates.
        assert systemd_unescape(unit) != unit or "-" not in unit, unit
        if systemd_unescape(unit) != unit:
            corrupted.append((unit, systemd_unescape(unit)))
    assert ("log-workflow-b.service", "log/workflow/b.service") in corrupted, (
        "the %I hazard must be real, or this test proves nothing"
    )
    print(f"PASS: handler reads %i; %I would corrupt {len(corrupted)}/{len(MONITORED_UNITS)} units")


def test_retention_bootstraps_the_platform_identity() -> None:
    """Retention must run behind the identity wrapper, not as a bare module.

    `ops.backup_retention` attests the platform identity, and
    `load_runtime_identity()` needs LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID
    plus the four expected PostgreSQL values. Those live in the repository .env;
    the unit's EnvironmentFiles carry runtime configuration and the canonical
    target environment only. Invoked directly the unit failed closed on every
    fire with EXPECTED_PLATFORM_IDENTITY_MISSING — never deleting anything, but
    never retaining anything either, while OnFailure= raised a nightly incident.

    Parsed semantically rather than compared as a line, so path or flag changes
    that preserve the contract do not break this, and the inert form does.
    """
    path = PROPOSED / "backup-retention.service"
    exec_start = values(path, "ExecStart", section="Service")
    assert len(exec_start) == 1, exec_start
    argv = exec_start[0].split()

    assert "--" in argv, "the wrapper takes the real command after a `--` separator"
    separator = argv.index("--")
    wrapper, command = argv[:separator], argv[separator + 1:]

    assert wrapper[-1].endswith("ops/run_with_environment_identity.py"), (
        f"retention must bootstrap identity through the wrapper, got: {wrapper}"
    )
    assert wrapper[0].endswith("/python"), f"the wrapper runs under the venv: {wrapper}"

    # The retention module must be BEHIND the separator. Before it, the wrapper
    # would receive it as its own argument and identity would never be applied.
    assert "ops.backup_retention" not in " ".join(wrapper), (
        "retention appears before the separator; the wrapper would not wrap it"
    )
    assert command[-2:] == ["ops.backup_retention", "--execute"], (
        f"expected `-m ops.backup_retention --execute` behind the wrapper, got: {command}"
    )
    assert "-m" in command, command

    # The unit must not smuggle identity in as literal values instead.
    unit_text = path.read_text(encoding="utf-8")
    for banned in ("LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID=",
                   "LOG_PLATFORM_TARGET_ENVIRONMENT="):
        assert banned not in unit_text, (
            f"{banned} hard-coded in the unit; identity must come from the wrapper"
        )

    # --execute belongs to the scheduled destructive jobs and nothing else. Two
    # of them are: backup-retention expires backup sets, platform-hard-retention
    # enforces the 13-calendar-month ceiling. Both are timer-driven, neither is
    # boot-enableable, and no other timer-driven service may carry the flag.
    DESTRUCTIVE_BY_DESIGN = ("backup-retention.service", "platform-hard-retention.service")
    for name in TIMER_DRIVEN_SERVICES:
        if name in DESTRUCTIVE_BY_DESIGN:
            continue
        assert "--execute" not in " ".join(values(PROPOSED / name, "ExecStart")), name
    for name in DESTRUCTIVE_BY_DESIGN:
        assert "--execute" in " ".join(values(PROPOSED / name, "ExecStart")), (
            f"{name}: lost its --execute; the scheduled job would silently stop "
            f"enforcing while still reporting success"
        )
        assert "Install" not in sections(PROPOSED / name), (
            f"{name}: a destructive scheduled job must not be boot-enableable"
        )
    print("PASS: retention runs behind the identity bootstrap wrapper")


def test_the_alert_path_never_routes_its_own_failure() -> None:
    for name in ALERT_PATH_UNITS:
        onfailure = values(PROPOSED / name, "OnFailure")
        assert onfailure == [], f"{name}: OnFailure={onfailure} is a loop"
    # The handler must also always succeed: a failing failure-handler re-triggers.
    handler = PROPOSED / FAILURE_HANDLER
    assert values(handler, "Type", section="Service") == ["oneshot"], "handler must be oneshot"
    print("PASS: the failure handler and mail worker cannot trigger themselves")


def under_dev_tree(path: str) -> bool:
    """Is `path` inside the development checkout?

    Prefix matching alone is wrong here and would silently pass every assertion
    below: the release root is `<DEV_TREE>-release`, so `startswith(DEV_TREE)`
    is true for the very path these tests demand. Only an exact match or a
    genuine path component counts.
    """
    return path == DEV_TREE or path.startswith(DEV_TREE + "/")


def exec_start_argv(path: Path) -> list[str]:
    starts = values(path, "ExecStart", section="Service")
    assert len(starts) == 1, f"{path.name}: expected one ExecStart, got {len(starts)}"
    return starts[0].split()


def test_b1_operational_sidecars_are_release_bound() -> None:
    """The worker and the watchdog must run the active release, not a checkout.

    This is the whole point of B1-R. Before it, activating a release changed the
    orchestrator (which runs behind log-job-runner.sh) but not the worker or the
    watchdog, so `ops/watchdog_expectations.json` and the heartbeat the worker
    stamps could only be deployed by mutating the Workflow A worktree.
    """
    for name, module in RELEASE_BOUND_SERVICES.items():
        argv = exec_start_argv(PROPOSED / name)
        assert argv[0] == OPS_RUNNER, f"{name}: ExecStart runs {argv[0]}, not the release launcher"
        assert argv[1] == module, f"{name}: launches {argv[1]}, expected {module}"
    print("PASS: the alert worker and the watchdog resolve the active release")


def test_release_bound_units_never_name_the_development_tree() -> None:
    """Regression guard for the defect B1-R closes.

    Asserted against the executable source contract — the interpreter path, the
    working directory and any declared PYTHONPATH — not against the presence of
    the string, which is legitimate elsewhere in this repository.
    """
    for name in RELEASE_BOUND_SERVICES:
        path = PROPOSED / name
        for token in exec_start_argv(path):
            assert not under_dev_tree(token), f"{name}: ExecStart names {token}"
        for workdir in values(path, "WorkingDirectory", section="Service"):
            assert not under_dev_tree(workdir), f"{name}: WorkingDirectory={workdir}"
        # `Environment=PYTHONPATH=` cannot be correct here even if it named the
        # release: the active release is a symlink target that changes without
        # this file changing. It must be resolved per invocation instead.
        inline = [v for v in values(path, "Environment", section="Service")
                  if v.startswith("PYTHONPATH=")]
        assert not inline, f"{name}: Environment={inline} pins a source root statically"
    print("PASS: no release-bound unit names the development tree as its source root")


def test_failure_adapter_is_deliberately_host_bound() -> None:
    """The last-resort reporter must not depend on the pointer it must report on.

    A broken `current` fails every release-bound unit at once. A release-bound
    failure handler would fail with them, and the operator would hear nothing
    about the one fault that stopped everything. Pinned so that repointing it is
    a conscious decision; the version-compatibility half of that decision is
    proven in test_release_runtime_isolation.py.
    """
    for name, module in HOST_BOUND_ALERT_SERVICES.items():
        argv = exec_start_argv(PROPOSED / name)
        assert argv[0] != OPS_RUNNER, f"{name}: must not resolve the release launcher"
        assert under_dev_tree(argv[0]), f"{name}: interpreter {argv[0]} is not host-bound"
        assert module in argv, f"{name}: does not launch {module}"
    print("PASS: the failure handler stays host-bound, by decision and by test")


def test_ops_runner_fails_closed_on_an_unusable_release() -> None:
    """The launcher's own safety properties, read from the script it installs."""
    text = OPS_RUNNER_SOURCE.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash"), "launcher needs a shebang"
    assert "set -euo pipefail" in text, "launcher must abort on error"
    assert f'RELEASE_ROOT="{RELEASE_ROOT}"' in text, "launcher must pin the production release root"
    # SET, not prepend: `ops` is a namespace package, so a PYTHONPATH spanning
    # both trees would let a module missing from one resolve out of the other.
    assert 'export PYTHONPATH="${BASE_DIR}"' in text, "launcher must replace PYTHONPATH"
    # A dev tree symlinked in as `current` must be refused.
    assert "releases/[0-9a-f]{12}" in text, "launcher must require a real release directory"
    # The observability plane must not wait on the job-execution barrier.
    assert "flock" not in text, "the launcher must not gate alerting on the job barrier"
    print("PASS: the release launcher fails closed and does not gate on the job barrier")


def test_ops_runner_exit_codes_cannot_be_confused_with_module_codes() -> None:
    """Exit 3 from the worker means 'dead letter', not 'broken release'."""
    codes = {
        int(value)
        for line in OPS_RUNNER_SOURCE.read_text(encoding="utf-8").splitlines()
        if line.startswith("EXIT_") and "=" in line
        for value in [line.partition("=")[2].strip()]
        if value.isdigit()
    }
    assert codes, "launcher declares no named exit codes"
    collisions = codes & WORKER_RESERVED_EXIT_CODES
    assert not collisions, f"launcher reuses module-owned exit code(s) {collisions}"
    print("PASS: launcher exit codes stay distinct from the modules it launches")


def test_long_running_services_declare_their_release_prerequisites() -> None:
    """A release that cannot serve the unit must be refused, not restarted into.

    `Restart=on-failure` is what makes this more than tidiness. Without the
    declaration a structurally incomplete release makes the API and the export
    worker exit 1 every RestartSec forever, with the same status an ordinary
    crash produces; with it they exit 92 once and stay failed, which is a
    different operator action.
    """
    for name, expected in REQUIRED_RELEASE_FILES.items():
        declared = [
            value.partition("=")[2]
            for value in values(PROPOSED / name, "Environment", section="Service")
            if value.startswith("OPS_RUNNER_REQUIRE_RELEASE_FILE=")
        ]
        assert len(declared) == 1, f"{name}: expected one requirement declaration, got {declared}"
        paths = {part for part in declared[0].split(":") if part}
        assert paths == expected, f"{name}: declares {sorted(paths)}, expected {sorted(expected)}"
        for path in paths:
            assert not path.startswith("/"), f"{name}: {path} is absolute"
            assert ".." not in path, f"{name}: {path} escapes the release"
    print("PASS: the long-running services declare what the active release must contain")


def test_ops_runner_enforces_declared_release_prerequisites() -> None:
    """The declaration above is worthless unless the launcher acts on it."""
    text = OPS_RUNNER_SOURCE.read_text(encoding="utf-8")
    assert "OPS_RUNNER_REQUIRE_RELEASE_FILE" in text, "launcher ignores the requirement declaration"
    assert "RELEASE_ENTRYPOINT_MISSING" in text, "launcher has no refusal for a missing entrypoint"
    # A requirement that could be satisfied from outside the release would defeat
    # the boundary it is supposed to reinforce.
    assert 'OPS_RUNNER_REQUIREMENT_INVALID' in text, "launcher accepts absolute/escaping requirements"
    codes = {
        int(value)
        for line in text.splitlines()
        if line.startswith("EXIT_") and "=" in line
        for value in [line.partition("=")[2].strip()]
        if value.isdigit()
    }
    assert 92 in codes, "launcher does not name a distinct entrypoint-missing exit code"
    print("PASS: the launcher enforces each unit's declared release prerequisites")


def test_development_bootstrap_cannot_silently_reinstall_a_checkout_bound_api() -> None:
    """The old installer still exists; it must not be a quiet path back.

    The defect this slice closes was produced by exactly this generator, so a
    comment is not sufficient — the helper has to refuse on a host that keeps an
    active release, and the refusal has to be the default.
    """
    example, installer = DEVELOPMENT_BOOTSTRAP_SOURCES
    for path in DEVELOPMENT_BOOTSTRAP_SOURCES:
        head = path.read_text(encoding="utf-8")[:1200]
        assert "PRE-RELEASE-BOUNDARY" in head, f"{path.name}: no provenance warning"
        assert "ops/systemd/proposed/log-platform-api.service" in head, (
            f"{path.name}: does not name the authoritative release-bound unit")
    text = installer.read_text(encoding="utf-8")
    assert RELEASE_ROOT_GUARD in text, "installer has no release-boundary refusal"
    assert 'allow_dev_tree_binding=false' in text, "the refusal is not the default"
    assert '-L "${release_root}/current"' in text, (
        "the refusal does not key on an actual active release pointer")
    # `--dry-run` must stay usable for inspection, so the guard has to sit after
    # the dry-run exit rather than before it.
    assert text.index('if [[ "${dry_run}" == "true" ]]; then') < text.index(RELEASE_ROOT_GUARD), (
        "the guard blocks --dry-run inspection")
    # And it must be unreachable-by-default only through the explicit override.
    assert "--allow-development-tree-binding" in text
    print("PASS: the development bootstrap installer refuses a release-boundary host")


def test_export_cleanup_shares_the_worker_release(  ) -> None:
    """Both database-export units must run the same module from the same tree.

    The worker publishes an artifact and the cleanup pass expires it. Splitting
    their code provenance means one version's retention rule can delete what
    another version's publication rule promised.
    """
    worker = exec_start_argv(PROPOSED / "database-export-worker.service")
    cleanup = exec_start_argv(PROPOSED / "database-export-cleanup.service")
    assert worker[0] == cleanup[0] == OPS_RUNNER, (worker[0], cleanup[0])
    assert worker[1] == cleanup[1] == "ops.database_export_worker", (worker[1], cleanup[1])
    assert "--loop" in worker and "--cleanup-only" in cleanup, (worker, cleanup)
    print("PASS: the export worker and its cleanup pass share one release-bound module")


def main() -> None:
    test_onfailure_is_a_unit_directive_everywhere()
    test_starting_a_timer_does_not_start_its_service()
    test_the_start_invariant_exception_list_stays_truthful()
    test_retention_timers_keep_their_cadence_and_catch_up()
    test_timer_driven_services_cannot_be_enabled_standalone()
    test_the_install_exception_list_stays_truthful()
    test_the_retention_sweep_has_no_boot_activation_path()
    test_destructive_timer_does_not_replay_missed_fires()
    test_failed_unit_name_survives_the_onfailure_round_trip()
    test_retention_bootstraps_the_platform_identity()
    test_the_alert_path_never_routes_its_own_failure()
    test_b1_operational_sidecars_are_release_bound()
    test_release_bound_units_never_name_the_development_tree()
    test_failure_adapter_is_deliberately_host_bound()
    test_ops_runner_fails_closed_on_an_unusable_release()
    test_ops_runner_exit_codes_cannot_be_confused_with_module_codes()
    test_long_running_services_declare_their_release_prerequisites()
    test_ops_runner_enforces_declared_release_prerequisites()
    test_development_bootstrap_cannot_silently_reinstall_a_checkout_bound_api()
    test_export_cleanup_shares_the_worker_release()
    print("OK - systemd unit contract tests passed")


if __name__ == "__main__":
    sys.exit(main())
