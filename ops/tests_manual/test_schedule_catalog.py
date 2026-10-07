#!/usr/bin/env python3
"""The central recurring-schedule catalogue, and its drift detection.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_schedule_catalog.py

Pure by default: unit files are read from the repository, not from the host.
The runtime section is skipped with a NOTE where `systemctl` is unavailable, so
the suite is meaningful in CI and stronger on the host.

WHAT THIS PROVES

  * every recurring definition in the repository is represented, and a new
    `.timer` with no entry is a FAILURE rather than an omission nobody notices;
  * cadence and timezone are PARSED from the unit files, so the catalogue
    cannot state a schedule the installed unit does not have — including the
    two-`OnCalendar` case a naive ini parser silently halves;
  * every retention job appears and is flagged as retention work, and every
    retention policy's named schedule exists;
  * enabled/disabled state is derived from the host, never asserted here.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import retention_registry as rr  # noqa: E402
from ops import schedule_catalog as sc  # noqa: E402

PASSED: list[str] = []
NOTES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def test_the_catalogue_has_no_drift() -> None:
    problems = sc.validate()
    check("no repository drift", not problems, "; ".join(str(p) for p in problems))
    PASSED.append("the_catalogue_has_no_drift")


def test_every_repository_timer_is_registered() -> None:
    units = set(sc.repository_timer_units())
    registered = {entry.unit for entry in sc.SCHEDULES if entry.unit}
    check("every .timer in ops/systemd/ has a catalogue entry",
          not (units - registered), str(sorted(units - registered)))
    check("and the repository really does contain timers to check",
          len(units) >= 10, str(len(units)))
    PASSED.append("every_repository_timer_is_registered")


def test_an_unregistered_timer_is_detected() -> None:
    """The check has to bite. Hide one entry and confirm the drift appears."""
    original = sc.SCHEDULES
    try:
        sc.SCHEDULES = tuple(
            entry for entry in original if entry.schedule_id != "log-workflow-b"
        )
        codes = {(problem.code, problem.subject) for problem in sc.validate()}
        check("removing an entry surfaces its timer as unregistered",
              ("UNREGISTERED_TIMER", "log-workflow-b") in codes, str(sorted(codes)))
    finally:
        sc.SCHEDULES = original
    check("and the catalogue is intact again", not sc.validate())
    PASSED.append("an_unregistered_timer_is_detected")


def test_a_missing_unit_file_is_detected() -> None:
    original = sc.SCHEDULES
    try:
        sc.SCHEDULES = original + (
            sc.ScheduleEntry(
                schedule_id="synthetic-ghost",
                title="A schedule whose unit file does not exist",
                owner_domain="test",
                mechanism=sc.Mechanism.SYSTEMD_TIMER,
                lifecycle=sc.Lifecycle.PROPOSED,
                unit="synthetic-ghost",
                source_of_truth="nowhere",
            ),
        )
        codes = {problem.code for problem in sc.validate()}
        check("an entry naming a nonexistent unit is drift",
              "MISSING_UNIT_FILE" in codes, str(sorted(codes)))
    finally:
        sc.SCHEDULES = original
    PASSED.append("a_missing_unit_file_is_detected")


def test_cadence_and_timezone_are_parsed_not_transcribed() -> None:
    facts = {entry.schedule_id: sc.repository_facts(entry) for entry in sc.SCHEDULES}

    prune = facts["log-platform-prune"]
    check("the prune cadence comes from its unit file",
          prune["cadence"] == "*-*-* 03:30:00", str(prune["cadence"]))
    check("and the unit file it came from is named",
          prune["timer_file"] == "ops/systemd/log-platform-prune.timer")
    check("its ExecStart is read, not restated",
          "--days 60" in (prune["command"] or ""), str(prune["command"]))
    check("and Persistent is carried through", prune["persistent"] is True)

    # A unit with TWO OnCalendar lines. `configparser` would keep only the last
    # one and the catalogue would silently claim a single daily fire.
    workflow_b = facts["log-workflow-b"]
    check("both Workflow B fires are represented",
          workflow_b["cadence"].count("OnCalendar") == 0
          and "06:00:00" in workflow_b["cadence"]
          and "20:00:00" in workflow_b["cadence"], str(workflow_b["cadence"]))
    check("and its timezone is read from the expression",
          workflow_b["timezone"] == "Europe/Warsaw", str(workflow_b["timezone"]))

    purge = facts["log-job@retention-purge"]
    check("the Workflow A purge is UTC", purge["timezone"] == "UTC", str(purge["timezone"]))
    check("a unit with no explicit zone says so rather than guessing",
          facts["log-backup"]["timezone"] == "host local time")

    monotonic = facts["execution-watchdog"]
    check("a monotonic timer states its interval, not a calendar",
          "OnUnitActiveSec=15min" in monotonic["cadence"], str(monotonic["cadence"]))

    hard = facts["platform-hard-retention"]
    check("the new hard-retention sweep is weekly",
          hard["cadence"] == "Sun *-*-* 05:00:00", str(hard["cadence"]))
    check("and its command carries no retention number of its own",
          "--execute" in (hard["command"] or "")
          and "13" not in (hard["command"] or ""), str(hard["command"]))
    PASSED.append("cadence_and_timezone_are_parsed_not_transcribed")


def test_the_guaranteed_interval_is_derived_from_the_unit_file() -> None:
    """Retention's deadline look-ahead is only sound if this number is real.

    The registry declares a guaranteed interval per schedule; here it is derived
    from the cadence the unit actually carries, and the two must agree. A timer
    slowed down without updating its cycle would otherwise shrink every
    dependent policy's lead and start deleting after the deadline.
    """
    expected = {
        "log-backup": timedelta(days=1),
        "log-platform-prune": timedelta(days=1),
        "backup-retention": timedelta(days=1),
        "platform-hard-retention": timedelta(days=7),
        "database-export-cleanup": timedelta(hours=1),
        "log-job@retention-purge": timedelta(days=7),
        "log-job@dispatcher": timedelta(minutes=5),
        "execution-watchdog": timedelta(minutes=15),
        "disk-space-monitor": timedelta(hours=1),
        "journald-retention-vacuum": timedelta(days=1),
        # Two OnCalendar lines, 06:00 and 20:00: the WORST gap is 14 hours, not
        # 12 and not "twice daily". An ini parser that kept only the last line
        # would answer 24 hours and overstate the guarantee.
        "log-workflow-b": timedelta(hours=14),
        # A Type=simple service paces itself; its cadence is an ExecStart flag.
        "database-export-worker": timedelta(hours=1),
    }
    for schedule_id, interval in expected.items():
        derived = sc.derive_guaranteed_interval(sc.BY_ID[schedule_id])
        check(f"{schedule_id} derives {interval}", derived == interval, str(derived))

    for schedule_id, cycle in rr.MAINTENANCE_CYCLES.items():
        derived = sc.derive_guaranteed_interval(sc.BY_ID[schedule_id])
        if derived is None:
            continue
        check(f"{schedule_id}: declared cycle equals the unit's real cadence",
              cycle.guaranteed_interval == derived,
              f"{cycle.guaranteed_interval} vs {derived}")

    # An unmodelled cadence must answer None, never a wrong small number: a
    # too-short derived interval would shorten the lead and delete late.
    check("an entry with no unit derives nothing",
          sc.derive_guaranteed_interval(sc.BY_ID["eco-dashboard-maintenance"]) is None)
    PASSED.append("the_guaranteed_interval_is_derived_from_the_unit_file")


def test_a_cycle_that_disagrees_with_its_timer_is_drift() -> None:
    original = dict(rr.MAINTENANCE_CYCLES)
    try:
        rr.MAINTENANCE_CYCLES["log-platform-prune"] = rr.MaintenanceCycle(
            schedule_id="log-platform-prune",
            guaranteed_interval=timedelta(days=30),
            rationale="synthetic drift",
        )
        codes = {(problem.code, problem.subject) for problem in sc.validate()}
        check("the disagreement is reported",
              ("MAINTENANCE_CYCLE_DRIFT", "log-platform-prune") in codes,
              str(sorted(codes)))
    finally:
        rr.MAINTENANCE_CYCLES.clear()
        rr.MAINTENANCE_CYCLES.update(original)
    check("and the catalogue is clean again", not sc.validate())

    # A retention schedule with no declared cycle at all is equally a failure.
    removed = rr.MAINTENANCE_CYCLES.pop("platform-hard-retention")
    try:
        codes = {(problem.code, problem.subject) for problem in sc.validate()}
        check("a retention schedule with no declared cycle is drift",
              ("MISSING_MAINTENANCE_CYCLE", "platform-hard-retention") in codes,
              str(sorted(codes)))
    finally:
        rr.MAINTENANCE_CYCLES["platform-hard-retention"] = removed
    PASSED.append("a_cycle_that_disagrees_with_its_timer_is_drift")


def test_a_driven_schedule_names_its_driver() -> None:
    """A schedule with no timer of its own must say what actually runs it."""
    eco = sc.BY_ID["eco-dashboard-maintenance"]
    check("the Cloudflare maintenance has no unit", eco.unit is None)
    check("so it names its driver", eco.driven_by == "platform-hard-retention")
    check("which is a real schedule", eco.driven_by in sc.BY_ID)

    original = sc.SCHEDULES
    try:
        sc.SCHEDULES = tuple(
            entry if entry.schedule_id != "eco-dashboard-maintenance"
            else sc.ScheduleEntry(
                **{**entry.__dict__, "driven_by": "a-schedule-that-does-not-exist"})
            for entry in original
        )
        codes = {problem.code for problem in sc.validate()}
        check("an unknown driver is drift", "UNKNOWN_DRIVER" in codes, str(sorted(codes)))
    finally:
        sc.SCHEDULES = original
    PASSED.append("a_driven_schedule_names_its_driver")


def test_retention_work_is_visible_as_retention_work() -> None:
    retention = {entry.schedule_id for entry in sc.SCHEDULES if entry.retention_related}
    for expected in ("log-backup", "log-platform-prune", "backup-retention",
                     "platform-hard-retention", "database-export-cleanup",
                     "log-job@retention-purge", "eco-dashboard-maintenance",
                     "journald-retention-vacuum"):
        check(f"{expected} is flagged as retention/maintenance work",
              expected in retention, str(sorted(retention)))
    PASSED.append("retention_work_is_visible_as_retention_work")


def test_every_retention_policy_points_at_a_real_schedule() -> None:
    for policy in rr.POLICIES:
        if policy.schedule_id is None:
            continue
        check(f"{policy.policy_id} names a catalogued schedule",
              policy.schedule_id in sc.BY_ID, policy.schedule_id)
    # And the reverse: a schedule cannot claim to run a policy that is not there.
    for entry in sc.SCHEDULES:
        for policy_id in entry.retention_policies:
            check(f"{entry.schedule_id} runs a declared policy",
                  policy_id in rr.BY_ID, policy_id)
    # Every ops.hard_retention policy must be claimed by the sweep.
    swept = set(sc.BY_ID["platform-hard-retention"].retention_policies)
    for policy in rr.POLICIES:
        if policy.cleanup_job and policy.cleanup_job.startswith("ops.hard_retention"):
            check(f"{policy.policy_id} is claimed by the sweep",
                  policy.policy_id in swept, policy.policy_id)
    PASSED.append("every_retention_policy_points_at_a_real_schedule")


def test_the_database_driven_schedules_are_expandable() -> None:
    """The dispatcher's fires live in a table, and the catalogue says so."""
    entry = sc.BY_ID["log-job@dispatcher"]
    check("the dispatcher is not modelled as a plain timer",
          entry.mechanism is sc.Mechanism.DATABASE_SCHEDULER)
    check("and its real source of truth is named",
          "client_dataset_schedule" in entry.source_of_truth)

    rows = sc.database_schedules()
    if rows is None:
        NOTES.append("per-client expansion skipped: no platform database reachable")
        PASSED.append("the_database_driven_schedules_are_expandable")
        return
    check("the expansion returns per-client rows", isinstance(rows, list))
    if rows:
        first = rows[0]
        check("each row carries the facts an operator needs",
              {"client_code", "dataset_name", "enabled", "frequency", "timezone",
               "job_module"} <= set(first), str(sorted(first)))
        rendered = sc.render_table(include_database=True)
        check("and the rendered table includes them",
              "workflow_a_control.client_dataset_schedule" in rendered)
    PASSED.append("the_database_driven_schedules_are_expandable")


def test_runtime_state_is_read_not_asserted() -> None:
    facts = sc.runtime_facts(sc.BY_ID["log-platform-prune"])
    if facts["installed"] is None:
        NOTES.append("runtime state skipped: systemctl unavailable")
        PASSED.append("runtime_state_is_read_not_asserted")
        return
    check("the installed prune timer is loaded", facts["installed"] is True)
    check("its enabled state comes from systemd",
          facts["enabled"] in {"enabled", "disabled", "static", "generated",
                               "masked", "indirect", "enabled-runtime"},
          str(facts["enabled"]))
    payload = sc.as_dict(include_runtime=True)
    check("every entry carries a runtime block",
          all("runtime" in entry for entry in payload["schedules"]))
    drift = [item for item in payload["drift"]
             if item["code"] == "UNREGISTERED_HOST_TIMER"]
    check("no platform timer on this host is missing from the catalogue",
          not drift, str(drift))
    PASSED.append("runtime_state_is_read_not_asserted")


def test_one_command_renders_the_whole_inventory() -> None:
    rendered = sc.render_table()
    for entry in sc.SCHEDULES:
        check(f"{entry.schedule_id} appears in the rendered inventory",
              entry.schedule_id in rendered)
    check("the header names the operator's columns",
          all(column in rendered.splitlines()[0]
              for column in ("SCHEDULE", "MECHANISM", "CADENCE", "RETENTION", "SOURCE")))
    check("and drift is reported in the same output", "Schedule drift:" in rendered)
    payload = sc.as_dict()
    check("the JSON form carries the same entries",
          len(payload["schedules"]) == len(sc.SCHEDULES))
    PASSED.append("one_command_renders_the_whole_inventory")


def test_discovery_does_not_depend_on_a_name_pattern() -> None:
    """A repository timer cannot escape runtime drift detection by being named badly.

    `journald-retention-vacuum.timer` is shipped here, installed and enabled on
    production, and executes a governed retention policy — and it matched none
    of `PLATFORM_UNIT_PREFIXES`, so the scan whose whole job is to prove no
    platform timer escapes this catalogue could not see it. Discovery is now
    derived from what the repository CONTROLS.
    """
    names = sc.platform_timer_names()
    for unit in sc.repository_timer_units():
        check(f"{unit} is discoverable regardless of its name", unit in names)
    check("journald-retention-vacuum specifically",
          "journald-retention-vacuum" in names)
    for entry in sc.SCHEDULES:
        if entry.unit:
            check(f"{entry.schedule_id}'s unit is discoverable", entry.unit in names)
    # A unit that matches no prefix and is not in SCHEDULES is still found when
    # the repository ships it, which is the property the prefix list lacked.
    unprefixed = [
        unit for unit in sc.repository_timer_units()
        if not f"{unit}.timer".startswith(sc.PLATFORM_UNIT_PREFIXES)
    ]
    NOTES.append(
        f"repository timers matching no name prefix: {unprefixed or 'none today'} "
        f"— all discoverable by inventory"
    )
    PASSED.append("discovery_does_not_depend_on_a_name_pattern")


def test_a_timer_that_runs_its_service_on_start_is_drift() -> None:
    """The [Unit] semantics the catalogue could not previously express.

    `Requires=<own service>` in a timer's [Unit] runs the job whenever the
    TIMER is started — `enable --now`, a daemon-reload restart, every boot —
    entirely outside the OnCalendar this catalogue reports. That is a schedule
    fact, so the schedule surface has to be able to state it.
    """
    for entry in sc.SCHEDULES:
        semantics = sc.unit_semantics(entry)
        check(f"{entry.schedule_id} does not activate its service on start",
              not semantics["activates_paired_service_on_start"],
              str(semantics["activating_dependencies"]))

    # And the check bites: a synthetic entry pointed at a timer that does carry
    # the dependency must be reported.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "probe.timer").write_text(
            "[Unit]\nDescription=probe\nRequires=probe.service\n\n"
            "[Timer]\nOnCalendar=daily\nUnit=probe.service\n",
            encoding="utf-8",
        )
        (root / "probe.service").write_text(
            "[Unit]\nDescription=probe\n\n[Service]\nExecStart=/bin/true\n",
            encoding="utf-8",
        )
        original = sc.PROPOSED_DIR
        sc.PROPOSED_DIR = root
        try:
            probe = sc.ScheduleEntry(
                schedule_id="probe", title="probe", owner_domain="test",
                mechanism=sc.Mechanism.SYSTEMD_TIMER,
                lifecycle=sc.Lifecycle.PROPOSED, unit="probe",
                source_of_truth="probe.timer",
            )
            semantics = sc.unit_semantics(probe)
        finally:
            sc.PROPOSED_DIR = original
    check("a timer that activates its own service is detected",
          semantics["activates_paired_service_on_start"], str(semantics))
    check("and the offending directive is named",
          semantics["activating_dependencies"] == {"Requires": ["probe.service"]},
          str(semantics["activating_dependencies"]))
    PASSED.append("a_timer_that_runs_its_service_on_start_is_drift")


def test_a_scheduled_retention_job_that_deletes_nothing_says_so() -> None:
    """Installed, enabled and firing is not the same answer as enforcing."""
    purge = sc.BY_ID["log-job@retention-purge"]
    check("the per-client purge is derived as simulated",
          sc.derive_enforcement(purge) == "simulated_dry_run",
          str(sc.derive_enforcement(purge)))
    check("the value is READ from the unit, not declared here",
          '"dry_run":true' in str(sc.repository_facts(purge)["command"]))
    for schedule_id in ("platform-hard-retention", "backup-retention",
                        "log-platform-prune", "journald-retention-vacuum"):
        check(f"{schedule_id} is derived as enforcing",
              sc.derive_enforcement(sc.BY_ID[schedule_id]) == "enforcing",
              str(sc.derive_enforcement(sc.BY_ID[schedule_id])))
    check("a non-retention schedule is not asked the question",
          sc.derive_enforcement(sc.BY_ID["log-workflow-b"]) is None)

    rendered = sc.render_table()
    check("the operator table says DRY RUN rather than yes", "DRY RUN" in rendered)
    check("and explains what that means for a shorter policy",
          "CONFIGURED, not" in rendered)
    payload = sc.as_dict()
    entry = next(item for item in payload["schedules"]
                 if item["schedule_id"] == "log-job@retention-purge")
    check("and the machine-readable form carries it",
          entry["enforcement"] == "simulated_dry_run")
    PASSED.append("a_scheduled_retention_job_that_deletes_nothing_says_so")


def test_the_host_tmpfiles_cleanup_is_centrally_visible() -> None:
    """The mechanism that really bounds a governed path is in the catalogue."""
    entry = sc.BY_ID["systemd-tmpfiles-clean"]
    check("it is declared as an OS mechanism, not a repository timer",
          entry.mechanism is sc.Mechanism.HOST_OS)
    check("it has no unit file here, and claims none", entry.unit is None)
    check("it is marked as retention work", entry.retention_related)
    check("it names the governed policy it bounds",
          entry.retention_policies == ("filesystem.workflow_b_stage2_cleaned",))
    check("its source of truth is the host's own files",
          "tmpfiles.d/tmp.conf" in entry.source_of_truth)
    check("its cadence is stated, since nothing here can parse it",
          entry.declared_cadence and "OnUnitActiveSec=1d" in entry.declared_cadence)
    check("and it declares a guaranteed interval to the registry",
          "systemd-tmpfiles-clean" in rr.MAINTENANCE_CYCLES)

    # The link holds in both directions.
    policy = rr.get("filesystem.workflow_b_stage2_cleaned")
    check("the registry's effective mechanism resolves to this entry",
          all(item.schedule_id in sc.BY_ID for item in policy.effective_mechanisms),
          str([item.schedule_id for item in policy.effective_mechanisms]))
    PASSED.append("the_host_tmpfiles_cleanup_is_centrally_visible")


def test_an_effective_mechanism_naming_no_schedule_is_drift() -> None:
    """Central visibility must be a property, not a claim."""
    from dataclasses import replace as dc_replace

    policy = rr.get("filesystem.workflow_b_stage2_cleaned")
    broken = dc_replace(policy, effective_mechanisms=(
        dc_replace(policy.effective_mechanisms[0], schedule_id="not-a-schedule"),
    ))
    original = rr.POLICIES
    rr.POLICIES = tuple(
        broken if item.policy_id == policy.policy_id else item for item in original
    )
    try:
        problems = sc.validate()
    finally:
        rr.POLICIES = original
    check("an unknown effective-mechanism schedule is reported",
          any(p.code == "EFFECTIVE_MECHANISM_SCHEDULE_MISSING" for p in problems),
          "; ".join(str(p) for p in problems))
    check("and the real catalogue is still clean", not sc.validate())
    PASSED.append("an_effective_mechanism_naming_no_schedule_is_drift")


def main() -> int:
    test_the_catalogue_has_no_drift()
    test_every_repository_timer_is_registered()
    test_an_unregistered_timer_is_detected()
    test_a_missing_unit_file_is_detected()
    test_cadence_and_timezone_are_parsed_not_transcribed()
    test_the_guaranteed_interval_is_derived_from_the_unit_file()
    test_a_cycle_that_disagrees_with_its_timer_is_drift()
    test_a_driven_schedule_names_its_driver()
    test_retention_work_is_visible_as_retention_work()
    test_every_retention_policy_points_at_a_real_schedule()
    test_the_database_driven_schedules_are_expandable()
    test_runtime_state_is_read_not_asserted()
    test_one_command_renders_the_whole_inventory()
    test_discovery_does_not_depend_on_a_name_pattern()
    test_a_timer_that_runs_its_service_on_start_is_drift()
    test_a_scheduled_retention_job_that_deletes_nothing_says_so()
    test_the_host_tmpfiles_cleanup_is_centrally_visible()
    test_an_effective_mechanism_naming_no_schedule_is_drift()
    for name in PASSED:
        print(f"PASS {name}")
    for note in NOTES:
        print(f"NOTE {note}")
    print(f"\n{len(PASSED)} checks passed — central schedule catalogue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
