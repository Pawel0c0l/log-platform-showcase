#!/usr/bin/env python3
"""Deterministic tests for the missing-run / stuck-run watchdog (P0-3).

The pure-evaluation tests need nothing but stdlib. The persistence tests need a
throwaway database and refuse to touch `logdb`:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_execution_watchdog.py

    WATCHDOG_TEST_DSN='postgresql://user:pw@127.0.0.1:5432/disposable' \
        .venv/bin/python ops/tests_manual/test_execution_watchdog.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ops.execution_watchdog as wd  # noqa: E402
import ops.suspected_bug_email_worker as email_worker  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def schedule(**overrides):
    row = {
        "schedule_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "client_code": "ALPHA00001",
        "dataset_name": "trips_sync",
        "enabled": True,
        "frequency": "daily",
        "run_type": "DAILY",
        "day_of_week": None,
        "day_of_month": None,
        "day_of_month_last": False,
        "run_time": time(2, 0),
        "timezone": "UTC",
        "lookback_days": 1,
        # Eligibility inputs the dispatcher joins on. Defaults describe a schedule
        # the dispatcher would actually select.
        "client_enabled": True,
        "dataset_registered": True,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


CONFIG = wd.WatchdogConfig(completion_grace_minutes=180, stale_grace_minutes=240)


def verdict(row, history, *, now=NOW, config=CONFIG) -> str:
    return wd.evaluate_schedule_subject(
        row=row, history=history, now_utc=now, config=config
    ).verdict


# ----------------------------------------------------------- workflow A


def test_expected_fire_with_success_is_ok() -> None:
    history = {"run_history_id": "r1", "status": "SUCCESS",
               "started_at": NOW - timedelta(hours=9), "finished_at": NOW - timedelta(hours=8)}
    assert verdict(schedule(), history) == wd.VERDICT_OK
    print("PASS: expected fire + SUCCESS produces no incident")


def test_expected_fire_with_failure_defers_to_the_job_alert_path() -> None:
    """A FAILED run already raised JOB_TERMINAL_FAILURE via ops/runner.py.

    Raising a second, missing-run incident for the same fire would double-notify
    one root cause — exactly the alert storm this milestone must avoid.
    """
    history = {"run_history_id": "r1", "status": "FAILED",
               "started_at": NOW - timedelta(hours=9)}
    observation = wd.evaluate_schedule_subject(
        row=schedule(), history=history, now_utc=NOW, config=CONFIG
    )
    assert observation.verdict == wd.VERDICT_EXPECTED_FAILED
    assert not observation.alerting
    print("PASS: expected fire + FAILED does not duplicate the job failure alert")


def test_missing_execution_raises_only_after_the_grace_window() -> None:
    # Fire was 02:00 UTC; at 03:00 the 180-minute grace has not expired.
    early = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
    assert verdict(schedule(), None, now=early) == wd.VERDICT_IN_WINDOW

    observation = wd.evaluate_schedule_subject(
        row=schedule(), history=None, now_utc=NOW, config=CONFIG
    )
    assert observation.verdict == wd.VERDICT_MISSING
    assert observation.alerting
    assert observation.incident_code == wd.INCIDENT_SCHEDULED_RUN_MISSING
    assert observation.detail["scheduled_fire_ts"] == "2026-08-09T02:00:00+00:00"
    print("PASS: a missing execution alerts only once its grace window expires")


def test_running_past_stale_grace_is_stuck() -> None:
    fresh = {"run_history_id": "r1", "status": "RUNNING", "started_at": NOW - timedelta(hours=1)}
    assert verdict(schedule(), fresh) == wd.VERDICT_IN_WINDOW

    wedged = {"run_history_id": "r1", "status": "RUNNING", "started_at": NOW - timedelta(hours=9)}
    observation = wd.evaluate_schedule_subject(
        row=schedule(), history=wedged, now_utc=NOW, config=CONFIG
    )
    assert observation.verdict == wd.VERDICT_STALE
    assert observation.incident_code == wd.INCIDENT_SCHEDULED_RUN_STALE
    # Must fire well before the dispatcher's own 720-minute stale reaper.
    assert CONFIG.stale_for("trips_sync") < 720
    print("PASS: a run stuck past its grace is detected before the 720-minute reaper")


def test_disabled_schedule_expects_nothing() -> None:
    observation = wd.evaluate_schedule_subject(
        row=schedule(enabled=False), history=None, now_utc=NOW, config=CONFIG
    )
    assert observation.verdict == wd.VERDICT_DISABLED and not observation.alerting
    print("PASS: a disabled schedule never produces a missing-run incident")


def test_weekly_and_monthly_fires_use_dispatcher_arithmetic() -> None:
    # 2026-08-09 is a Sunday (weekday 6).
    weekly = schedule(frequency="weekly", day_of_week=6, run_time=time(2, 0))
    assert wd.expected_fire_utc(weekly, now_utc=NOW) == datetime(2026, 8, 9, 2, 0, tzinfo=UTC)

    monthly = schedule(frequency="monthly", day_of_month=1, run_time=time(3, 30))
    assert wd.expected_fire_utc(monthly, now_utc=NOW) == datetime(2026, 8, 1, 3, 30, tzinfo=UTC)

    # A schedule whose fire has not yet arrived today falls back to yesterday.
    later = schedule(run_time=time(23, 0))
    assert wd.expected_fire_utc(later, now_utc=NOW) == datetime(2026, 8, 8, 23, 0, tzinfo=UTC)

    assert wd.expected_fire_utc(schedule(timezone="Not/AZone"), now_utc=NOW) is None
    print("PASS: fire arithmetic matches the dispatcher for daily, weekly and monthly")


def test_dataset_override_widens_only_the_named_dataset() -> None:
    config = wd.WatchdogConfig(
        completion_grace_minutes=60,
        dataset_overrides={"trips_sync": {"completion_grace_minutes": 900}},
    )
    assert config.grace_for("trips_sync") == 900
    assert config.grace_for("fuel_daily_aggregation") == 60
    # Same 02:00 fire, same 12:00 scan: the widened dataset is still in window,
    # the dataset on the default grace is already overdue.
    assert verdict(schedule(), None, config=config) == wd.VERDICT_IN_WINDOW
    assert verdict(
        schedule(dataset_name="fuel_daily_aggregation"), None, config=config
    ) == wd.VERDICT_MISSING
    print("PASS: per-dataset grace overrides apply only to the named dataset")


# --------------------------------------------------------- systemd path


def systemd_expectation(**overrides) -> wd.SystemdExpectation:
    values = {
        "subject": "workflow_b_orchestrator",
        "component": "workflow_b.orchestrator",
        "run_source": "jobs.reports.workflow_b.orchestrator",
        "times": ("06:00", "20:00"),
        "timezone_name": "Europe/Warsaw",
        "completion_grace_minutes": 180,
        "stale_grace_minutes": 240,
    }
    values.update(overrides)
    return wd.SystemdExpectation(**values)


def test_eligibility_mirrors_the_dispatcher() -> None:
    """The watchdog must never expect what the dispatcher would never select."""
    assert wd.schedule_eligibility(schedule()) == (True, None)
    assert wd.schedule_eligibility(schedule(enabled=False)) == (
        False, wd.ELIGIBILITY_INELIGIBLE_SCHEDULE,
    )
    # The reported defect: enabled schedule, disabled client. The dispatcher's
    # `JOIN client_account ... WHERE ca.enabled = true` drops it; the watchdog
    # used to report it MISSING forever.
    assert wd.schedule_eligibility(schedule(client_enabled=False)) == (
        False, wd.ELIGIBILITY_INELIGIBLE_CLIENT,
    )
    # The dispatcher's JOIN to dataset_registry is inner too.
    assert wd.schedule_eligibility(schedule(dataset_registered=False)) == (
        False, wd.ELIGIBILITY_UNREGISTERED_DATASET,
    )

    for row, reason in (
        (schedule(enabled=False), wd.ELIGIBILITY_INELIGIBLE_SCHEDULE),
        (schedule(client_enabled=False), wd.ELIGIBILITY_INELIGIBLE_CLIENT),
        (schedule(dataset_registered=False), wd.ELIGIBILITY_UNREGISTERED_DATASET),
    ):
        observation = wd.evaluate_schedule_subject(
            row=row, history=None, now_utc=NOW, config=CONFIG
        )
        assert observation.verdict == wd.VERDICT_DISABLED, reason
        assert not observation.alerting
        assert observation.detail["ineligible_reason"] == reason
    print("PASS: disabled clients and unregistered datasets are never expected")


def test_dispatcher_eligibility_contract_has_not_drifted() -> None:
    """Static guard on the source the mirror is derived from.

    A live fixture comparison runs in the persistence tests. This catches the
    other half of the risk: someone changing the dispatcher's own predicate
    without touching the watchdog.
    """
    source = (REPO_ROOT / "jobs/api/telematics/dispatcher.py").read_text(encoding="utf-8")
    for required in (
        "JOIN workflow_a_control.client_account ca",
        "JOIN workflow_a_control.dataset_registry dr",
        "WHERE cds.enabled = true",
        "AND ca.enabled  = true",
    ):
        assert required in source, (
            f"dispatcher eligibility changed ({required!r} missing); "
            f"ops/execution_watchdog.schedule_eligibility must be updated with it"
        )
    print("PASS: the dispatcher eligibility predicate the mirror copies is unchanged")


def epoch(row, *, eligible_now=None, previous=None, now=NOW):
    if eligible_now is None:
        eligible_now = wd.schedule_eligibility(row)[0]
    return wd.eligibility_epoch(
        row, eligible_now=eligible_now, previous=previous, now_utc=now
    )


def observed(*, eligible, eligible_since, first_observed_at=None):
    """A previously persisted watchdog_observation row."""
    return {
        "eligible": eligible,
        "eligible_since": eligible_since,
        "first_observed_at": first_observed_at or datetime(2026, 1, 1, tzinfo=UTC),
    }


def test_codex_disabled_client_re_enable_does_not_backdate_expectation() -> None:
    """Named reproduction of the second Codex review's HIGH finding.

    Client disabled at 00:00; fire at 02:00; watchdog scans at 03:00 and records
    DISABLED. Client re-enabled at 10:00. `client_account` has no timestamps and
    `client_dataset_schedule.updated_at` never moved, so a first-observed-based
    epoch would leave the 02:00 fire retroactively expected and report MISSING.
    """
    day = datetime(2026, 8, 9, tzinfo=UTC)
    fire = day.replace(hour=2)                      # run_time 02:00 UTC
    scan_disabled = day.replace(hour=3)
    re_enabled = day.replace(hour=10)
    scan_after = day.replace(hour=10, minute=5)

    # Long-established schedule row: enabling the *client* does not touch it.
    disabled_row = schedule(client_enabled=False,
                            created_at=datetime(2026, 1, 1, tzinfo=UTC),
                            updated_at=datetime(2026, 1, 1, tzinfo=UTC))

    # 03:00 — ineligible, recorded as DISABLED with no epoch.
    first = wd.evaluate_schedule_subject(
        row=disabled_row, history=None, now_utc=scan_disabled, config=CONFIG,
        eligible_since_ts=epoch(disabled_row, previous=None, now=scan_disabled),
    )
    assert first.verdict == wd.VERDICT_DISABLED
    assert first.eligible is False and first.eligible_since is None
    stored = observed(eligible=first.eligible, eligible_since=first.eligible_since)

    # 10:05 — client re-enabled. This scan is the transition.
    enabled_row = schedule(client_enabled=True,
                           created_at=datetime(2026, 1, 1, tzinfo=UTC),
                           updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    new_epoch = epoch(enabled_row, previous=stored, now=scan_after)
    assert new_epoch == scan_after, "the epoch must be the transition, not the old row"

    after = wd.evaluate_schedule_subject(
        row=enabled_row, history=None, now_utc=scan_after, config=CONFIG,
        eligible_since_ts=new_epoch,
    )
    assert after.verdict == wd.VERDICT_NOT_YET_EXPECTED, after.verdict
    assert not after.alerting
    assert after.detail["scheduled_fire_ts"] == fire.isoformat()
    print("PASS: a fire during a disabled interval never becomes expected on re-enable")


def test_first_fire_after_re_enable_is_monitored_normally() -> None:
    day = datetime(2026, 8, 9, tzinfo=UTC)
    transition = day.replace(hour=10)
    row = schedule(created_at=datetime(2026, 1, 1, tzinfo=UTC),
                   updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    stored = observed(eligible=True, eligible_since=transition)

    # Next day's 02:00 fire is after the epoch and is held to account.
    next_day = day + timedelta(days=1)
    now = next_day.replace(hour=8)
    kept = epoch(row, previous=stored, now=now)
    assert kept == transition, "a continuing eligible subject keeps its epoch"

    observation = wd.evaluate_schedule_subject(
        row=row, history=None, now_utc=now, config=CONFIG, eligible_since_ts=kept,
    )
    assert observation.verdict == wd.VERDICT_MISSING
    assert observation.detail["scheduled_fire_ts"] == next_day.replace(hour=2).isoformat()
    print("PASS: the first legitimate fire after re-enable is monitored normally")


def test_repeated_eligibility_cycles_start_fresh_epochs() -> None:
    t0 = datetime(2026, 8, 1, 12, tzinfo=UTC)
    row = schedule(created_at=datetime(2026, 1, 1, tzinfo=UTC),
                   updated_at=datetime(2026, 1, 1, tzinfo=UTC))

    # eligible -> disabled clears the epoch entirely.
    disabled = epoch(row, eligible_now=False,
                     previous=observed(eligible=True, eligible_since=t0), now=t0)
    assert disabled is None

    # disabled -> eligible again is a new epoch, not the first one.
    t1 = t0 + timedelta(days=3)
    second = epoch(row, eligible_now=True,
                   previous=observed(eligible=False, eligible_since=None), now=t1)
    assert second == t1

    # and it does not drift forward on subsequent scans.
    for later in (t1 + timedelta(hours=1), t1 + timedelta(days=2)):
        assert epoch(row, previous=observed(eligible=True, eligible_since=second),
                     now=later) == second
    print("PASS: repeated disable/enable cycles start fresh epochs that never drift")


def test_unregistered_dataset_registered_later_does_not_backdate() -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    unregistered = schedule(dataset_registered=False)
    assert epoch(unregistered, previous=None, now=now) is None

    stored = observed(eligible=False, eligible_since=None)
    registered = schedule(dataset_registered=True,
                          created_at=datetime(2026, 1, 1, tzinfo=UTC),
                          updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert epoch(registered, previous=stored, now=now) == now

    observation = wd.evaluate_schedule_subject(
        row=registered, history=None, now_utc=now, config=CONFIG,
        eligible_since_ts=epoch(registered, previous=stored, now=now),
    )
    assert observation.verdict == wd.VERDICT_NOT_YET_EXPECTED
    print("PASS: registering a dataset does not retroactively expect earlier fires")


def test_never_seen_subjects_use_row_evidence_not_a_free_pass() -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    # Never observed, eligible, and unchanged for months: normal expectation.
    established = schedule(created_at=datetime(2026, 1, 1, tzinfo=UTC),
                           updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    first_epoch = epoch(established, previous=None, now=now)
    assert first_epoch == datetime(2026, 1, 1, tzinfo=UTC)
    assert wd.evaluate_schedule_subject(
        row=established, history=None, now_utc=now, config=CONFIG,
        eligible_since_ts=first_epoch,
    ).verdict == wd.VERDICT_MISSING

    # Never observed and ineligible: DISABLED, no epoch.
    never_eligible = schedule(client_enabled=False)
    assert epoch(never_eligible, previous=None, now=now) is None
    assert wd.evaluate_schedule_subject(
        row=never_eligible, history=None, now_utc=now, config=CONFIG,
        eligible_since_ts=None,
    ).verdict == wd.VERDICT_DISABLED
    print("PASS: an unseen but long-established schedule is monitored from the first scan")


def test_newly_eligible_subjects_have_no_retroactive_missing_fires() -> None:
    """Enabling a schedule this morning must not report last night as missed."""
    fire = wd.expected_fire_utc(schedule(), now_utc=NOW)
    assert fire is not None and fire < NOW

    # Schedule row itself was just touched (enabling writes updated_at).
    just_enabled = schedule(updated_at=NOW - timedelta(minutes=5))
    observation = wd.evaluate_schedule_subject(
        row=just_enabled, history=None, now_utc=NOW, config=CONFIG,
        eligible_since_ts=epoch(just_enabled, previous=None, now=NOW),
    )
    assert observation.verdict == wd.VERDICT_NOT_YET_EXPECTED
    assert not observation.alerting

    # Newly enabled *client*: client_account carries no timestamps, so the
    # watchdog's own first observation of the subject is the baseline.
    old_row = schedule()
    observation = wd.evaluate_schedule_subject(
        row=old_row, history=None, now_utc=NOW, config=CONFIG,
        eligible_since_ts=epoch(old_row, previous=observed(eligible=False, eligible_since=None), now=NOW),
    )
    assert observation.verdict == wd.VERDICT_NOT_YET_EXPECTED

    # A long-established subject is still held to account.
    established = wd.evaluate_schedule_subject(
        row=old_row, history=None, now_utc=NOW, config=CONFIG,
        eligible_since_ts=epoch(old_row, previous=observed(eligible=True, eligible_since=datetime(2026, 2, 1, tzinfo=UTC)), now=NOW),
    )
    assert established.verdict == wd.VERDICT_MISSING
    print("PASS: newly enabled schedules and clients produce no retroactive MISSING")


def test_systemd_fires_are_enumerated_in_the_lookback_window() -> None:
    fires = wd.expected_systemd_fires(systemd_expectation(), now_utc=NOW, lookback_hours=48)
    assert fires == sorted(fires)
    assert len(fires) == 4  # 08-07 20:00, 08-08 06:00, 08-08 20:00, 08-09 06:00 (Warsaw)
    assert all(fire <= NOW for fire in fires)
    print("PASS: systemd fires are enumerated deterministically inside the lookback window")


def test_workflow_b_run_that_never_registered_is_missing() -> None:
    """The exact 2026-07-31 20:00 / 2026-08-01 06:00 production failure class.

    The unit failed with a non-zero exit before `POST /runs`, so `public.runs`
    holds nothing at all. Only an outside-in assertion can see this.
    """
    fire = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)  # 06:00 Warsaw
    observation = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire, run=None, now_utc=NOW
    )
    assert observation.verdict == wd.VERDICT_MISSING
    assert observation.incident_code == wd.INCIDENT_SCHEDULED_RUN_MISSING

    within_grace = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire, run=None,
        now_utc=fire + timedelta(minutes=30),
    )
    assert within_grace.verdict == wd.VERDICT_IN_WINDOW
    print("PASS: a unit that died before registering a run is detected as MISSING")


def test_workflow_b_non_terminal_run_becomes_stale() -> None:
    fire = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    run = {"run_id": "abc", "status": "RUNNING", "started_at": fire}
    assert wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire, run=run,
        now_utc=fire + timedelta(hours=1),
    ).verdict == wd.VERDICT_IN_WINDOW
    stuck = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire, run=run,
        now_utc=fire + timedelta(hours=9),
    )
    assert stuck.verdict == wd.VERDICT_STALE
    print("PASS: a non-terminal Workflow B run past its grace is detected as STALE")


def test_every_terminal_run_status_settles_the_fire() -> None:
    """CANCELED is terminal, and an aged CANCELED run is not "stuck".

    The watchdog previously recognized only SUCCESS and FAILED, so any other
    settled status aged into a STALE incident claiming an execution was still
    running. Nothing wrote CANCELED before, which is why the gap never fired;
    `ops/reconcile_historical_run.py` makes it a real writer.
    """
    fire = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    # Far past every grace window, so only terminality can prevent STALE.
    long_after = fire + timedelta(days=40)

    verdicts = {}
    for status in ("SUCCESS", "FAILED", "CANCELED"):
        observation = wd.evaluate_systemd_subject(
            expectation=systemd_expectation(), fire_utc=fire,
            run={"run_id": "abc", "status": status, "started_at": fire},
            now_utc=long_after,
        )
        verdicts[status] = observation
        assert observation.verdict != wd.VERDICT_STALE, status
        assert observation.incident_code is None, status
        assert not observation.alerting, status

    assert verdicts["SUCCESS"].verdict == wd.VERDICT_OK
    assert verdicts["FAILED"].verdict == wd.VERDICT_EXPECTED_FAILED
    assert verdicts["CANCELED"].verdict == wd.VERDICT_EXPECTED_FAILED
    # FAILED and CANCELED share a verdict but must stay readable apart: only
    # FAILED is re-alerted by the job path.
    assert "canceled" in verdicts["CANCELED"].title.lower()
    assert "CANCELED" in verdicts["CANCELED"].summary
    assert verdicts["CANCELED"].detail["status"] == "CANCELED"

    # RUNNING remains the only non-terminal status, and still ages into STALE.
    aged_running = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire,
        run={"run_id": "abc", "status": "RUNNING", "started_at": fire},
        now_utc=long_after,
    )
    assert aged_running.verdict == wd.VERDICT_STALE
    assert aged_running.incident_code == wd.INCIDENT_SCHEDULED_RUN_STALE

    # An unknown status must NOT be silently treated as settled.
    unknown = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire,
        run={"run_id": "abc", "status": "WEDGED", "started_at": fire},
        now_utc=long_after,
    )
    assert unknown.verdict == wd.VERDICT_STALE

    # Grace semantics are untouched: a CANCELED run inside its window is also
    # decided immediately rather than waiting the window out.
    early = wd.evaluate_systemd_subject(
        expectation=systemd_expectation(), fire_utc=fire,
        run={"run_id": "abc", "status": "CANCELED", "started_at": fire},
        now_utc=fire + timedelta(minutes=5),
    )
    assert early.verdict == wd.VERDICT_EXPECTED_FAILED
    print("PASS: SUCCESS/FAILED/CANCELED settle a fire; only RUNNING can go STALE")


def test_terminal_status_vocabulary_matches_the_platform() -> None:
    """The watchdog's local copy may never drift from the authoritative sets.

    It is a local constant on purpose — the authoritative definitions live in
    `api/main.py` (FastAPI) and `api/platform_prune.py` (boto3), and this
    watchdog runs as its own minimal unit that must not acquire either
    dependency. This test is what makes the duplication safe.
    """
    repo = Path(__file__).resolve().parents[2]

    def literal_set(source: str, name: str) -> set[str]:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if name not in targets:
                continue
            value = node.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "frozenset" and value.args):
                value = value.args[0]
            if isinstance(value, ast.Set):
                return {e.value for e in value.elts if isinstance(e, ast.Constant)}
        raise AssertionError(f"{name} not found")

    api_terminal = literal_set((repo / "api/main.py").read_text(), "TERMINAL_RUN_STATUSES")
    api_allowed = literal_set((repo / "api/main.py").read_text(), "ALLOWED_RUN_STATUSES")
    prune_terminal = literal_set(
        (repo / "api/platform_prune.py").read_text(), "TERMINAL_RUN_STATUSES"
    )

    assert set(wd.TERMINAL_RUN_STATUSES) == api_terminal, (
        wd.TERMINAL_RUN_STATUSES, api_terminal)
    assert set(wd.TERMINAL_RUN_STATUSES) == prune_terminal, (
        wd.TERMINAL_RUN_STATUSES, prune_terminal)
    # RUNNING is the only non-terminal status the platform allows, which is what
    # makes "not terminal -> may go stale" a safe rule.
    assert api_allowed - api_terminal == {"RUNNING"}
    print("PASS: the watchdog's terminal vocabulary matches the API and prune")


def fold(fires_and_runs, *, now, expectation=None):
    expectation = expectation or systemd_expectation()
    occurrences = [
        wd.evaluate_systemd_subject(
            expectation=expectation, fire_utc=fire, run=run, now_utc=now
        )
        for fire, run in fires_and_runs
    ]
    return wd.fold_systemd_occurrences(
        expectation=expectation, occurrences=occurrences, now_utc=now
    )


def test_a_continuing_outage_is_one_root_subject_not_one_per_fire() -> None:
    """Codex HIGH 4: three missed Workflow B fires must be one incident.

    Per-fire identity meant 06:00 and 20:00 each opened their own incident every
    day, and none could ever resolve because a later successful fire carried a
    different key.
    """
    fires = [
        datetime(2026, 8, 7, 18, 0, tzinfo=UTC),
        datetime(2026, 8, 8, 4, 0, tzinfo=UTC),
        datetime(2026, 8, 8, 18, 0, tzinfo=UTC),
    ]
    root = fold([(fire, None) for fire in fires], now=NOW)
    assert root.subject_key == "systemd:workflow_b_orchestrator", root.subject_key
    assert ":" not in root.subject_key.removeprefix("systemd:"), "no fire timestamp in identity"
    assert root.verdict == wd.VERDICT_MISSING
    assert root.incident_code == wd.INCIDENT_SCHEDULED_RUN_MISSING
    # The individual missed fires survive as evidence.
    assert root.detail["missed_fire_count"] == 3
    assert [fire.isoformat() for fire in fires] == root.detail["missed_fires"]

    # A fourth missed fire keeps the same identity: same incident, more evidence.
    fires.append(datetime(2026, 8, 9, 4, 0, tzinfo=UTC))
    later = fold([(fire, None) for fire in fires], now=NOW)
    assert later.subject_key == root.subject_key
    assert later.detail["missed_fire_count"] == 4
    print("PASS: a continuing outage stays one root subject however many fires it spans")


def test_a_later_successful_fire_makes_the_root_healthy() -> None:
    missed = [
        (datetime(2026, 8, 8, 4, 0, tzinfo=UTC), None),
        (datetime(2026, 8, 8, 18, 0, tzinfo=UTC), None),
    ]
    success = (
        datetime(2026, 8, 9, 4, 0, tzinfo=UTC),
        {"run_id": "r1", "status": "SUCCESS"},
    )
    root = fold(missed + [success], now=NOW)
    assert root.verdict == wd.VERDICT_OK, "the newest decided fire succeeded"
    assert not root.alerting
    # Evidence of the outage is retained even though the subject is healthy now.
    assert root.detail["missed_fire_count"] == 2

    # A new outage after recovery is unhealthy again under the same identity,
    # which is what lets the incident layer open a fresh lifecycle.
    relapse = fold(
        missed + [success, (datetime(2026, 8, 9, 18, 0, tzinfo=UTC), None)],
        now=datetime(2026, 8, 9, 23, 0, tzinfo=UTC),
    )
    assert relapse.verdict == wd.VERDICT_MISSING
    assert relapse.subject_key == root.subject_key
    print("PASS: a later successful fire makes the root healthy; a relapse re-opens it")


def test_a_fire_inside_its_grace_window_decides_nothing() -> None:
    fire = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    just_fired = fold([(fire, None)], now=fire + timedelta(minutes=10))
    assert just_fired.verdict == wd.VERDICT_IN_WINDOW
    assert not just_fired.alerting
    assert just_fired.detail["latest_decided_fire"] is None

    # An undecided newest fire must not mask an already-decided failure.
    mixed = fold(
        [(datetime(2026, 8, 8, 18, 0, tzinfo=UTC), None), (fire, None)],
        now=fire + timedelta(minutes=10),
    )
    assert mixed.verdict == wd.VERDICT_MISSING
    print("PASS: fires still inside grace decide nothing and hide nothing")


def test_a_disabled_expectation_expects_nothing() -> None:
    root = fold(
        [(datetime(2026, 8, 8, 4, 0, tzinfo=UTC), None)],
        now=NOW, expectation=systemd_expectation(enabled=False),
    )
    assert root.verdict == wd.VERDICT_DISABLED and not root.alerting
    print("PASS: a disabled systemd expectation never alerts")


# ------------------------------------------------------------ heartbeat


def test_heartbeat_absence_is_scheduler_death() -> None:
    expectation = wd.HeartbeatExpectation(
        subject="workflow_a_dispatcher", component="workflow_a.dispatcher",
        heartbeat_component="workflow_a.dispatcher", grace_minutes=30,
    )
    alive = wd.evaluate_heartbeat_subject(
        expectation=expectation, last_beat_at=NOW - timedelta(minutes=6), now_utc=NOW
    )
    assert alive.verdict == wd.VERDICT_OK and not alive.alerting

    dead = wd.evaluate_heartbeat_subject(
        expectation=expectation, last_beat_at=NOW - timedelta(hours=4), now_utc=NOW
    )
    assert dead.verdict == wd.VERDICT_HEARTBEAT_LOST and dead.alerting
    assert dead.detail["age_minutes"] == 240

    never = wd.evaluate_heartbeat_subject(
        expectation=expectation, last_beat_at=None, now_utc=NOW
    )
    assert never.verdict == wd.VERDICT_HEARTBEAT_LOST
    print("PASS: a silent dispatcher is detected even though idle ticks write no run row")


def test_alert_worker_liveness_is_observed_independently() -> None:
    """A stopped alert worker must be non-OK; a running one must stay OK."""
    expectation = wd.HeartbeatExpectation(
        subject="alerting_email_worker",
        component="alerting.email_worker",
        heartbeat_component=email_worker.HEARTBEAT_COMPONENT,
        grace_minutes=30,
        consequence="No queued operator alert can be delivered while it is down.",
    )

    # The worker fires every 5 minutes; a recent beat is the healthy case and
    # must not alert, or the subject would page on every ordinary scan.
    for age in (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=29)):
        healthy = wd.evaluate_heartbeat_subject(
            expectation=expectation, last_beat_at=NOW - age, now_utc=NOW
        )
        assert healthy.verdict == wd.VERDICT_OK, age
        assert not healthy.alerting, age

    stopped = wd.evaluate_heartbeat_subject(
        expectation=expectation, last_beat_at=NOW - timedelta(hours=2), now_utc=NOW
    )
    assert stopped.verdict == wd.VERDICT_HEARTBEAT_LOST
    assert stopped.alerting
    assert stopped.incident_code == wd.INCIDENT_SCHEDULER_HEARTBEAT_LOST
    # The consequence must be the worker's, not the dispatcher's: an alert that
    # says "no schedule can fire" sends the operator to the wrong subsystem.
    assert "alert" in stopped.summary.lower()
    assert "no schedule it owns can fire" not in stopped.summary.lower()

    never = wd.evaluate_heartbeat_subject(
        expectation=expectation, last_beat_at=None, now_utc=NOW
    )
    assert never.verdict == wd.VERDICT_HEARTBEAT_LOST
    print("PASS: a stopped alert worker is detected; a running one stays healthy")


def test_dead_letter_and_backlog_are_durably_observable() -> None:
    """A dead-lettered alert cannot end as an unnoticed row."""
    expectation = wd.AlertDeliveryExpectation()

    healthy = wd.evaluate_alert_delivery_subject(
        expectation=expectation,
        state={"dead_letter_count": 0, "queued_count": 3, "overdue_queued_count": 0},
        now_utc=NOW,
    )
    assert healthy.verdict == wd.VERDICT_OK
    assert not healthy.alerting

    # Queued rows inside the backlog grace are healthy backoff, not an incident.
    backing_off = wd.evaluate_alert_delivery_subject(
        expectation=expectation,
        state={"dead_letter_count": 0, "queued_count": 1, "overdue_queued_count": 0,
               "oldest_queued_at": NOW - timedelta(minutes=45)},
        now_utc=NOW,
    )
    assert backing_off.verdict == wd.VERDICT_OK

    dead = wd.evaluate_alert_delivery_subject(
        expectation=expectation,
        state={"dead_letter_count": 2, "queued_count": 0, "overdue_queued_count": 0,
               "oldest_dead_letter_at": NOW - timedelta(hours=6)},
        now_utc=NOW,
    )
    assert dead.verdict == wd.VERDICT_STALE
    assert dead.alerting
    assert dead.incident_code == wd.INCIDENT_ALERT_DELIVERY_FAILED
    assert dead.detail["dead_letter_count"] == 2
    assert "dead_letter" in dead.summary

    stuck = wd.evaluate_alert_delivery_subject(
        expectation=expectation,
        state={"dead_letter_count": 0, "queued_count": 5, "overdue_queued_count": 5},
        now_utc=NOW,
    )
    assert stuck.verdict == wd.VERDICT_STALE
    assert stuck.alerting

    disabled = wd.evaluate_alert_delivery_subject(
        expectation=wd.AlertDeliveryExpectation(enabled=False),
        state={"dead_letter_count": 9, "overdue_queued_count": 9},
        now_utc=NOW,
    )
    assert disabled.verdict == wd.VERDICT_DISABLED and not disabled.alerting
    print("PASS: dead-lettered and stuck alert email is a durable non-OK observation")


def test_alert_delivery_incident_is_resolvable_and_not_self_alerting() -> None:
    """The observer may speak about the mail path; the mail path may not."""
    from ops import operational_alert

    # Recovery must be able to close what this subject opened, or a fixed mail
    # path would leave its incident open forever.
    assert operational_alert.INCIDENT_ALERT_DELIVERY_FAILED in wd.WATCHDOG_INCIDENT_CODES

    # The worker reporting itself is the loop and stays refused...
    assert operational_alert.is_self_alerting("ops.suspected_bug_email_worker")
    assert operational_alert.is_self_alerting("suspected-bug-email-worker.service")
    # ...while the independent observer's subjects are deliberately reportable,
    # otherwise a dead mail path would have no reporter at all.
    assert not operational_alert.is_self_alerting(wd.AlertDeliveryExpectation().component)
    assert not operational_alert.is_self_alerting("alerting.email_worker")
    print("PASS: the alert path cannot alert about itself; the watchdog still can")


def test_repository_expectations_file_loads() -> None:
    config = wd.load_config(REPO_ROOT / "ops" / "watchdog_expectations.json")
    subjects = {item.subject for item in config.systemd_expectations}
    assert "workflow_b_orchestrator" in subjects
    # Exact, not a superset: an expectation silently disappearing from the
    # shipped file is the failure this assertion exists to catch.
    assert {item.subject for item in config.heartbeats} == {
        "workflow_a_dispatcher",
        "alerting_email_worker",
    }
    worker = next(
        item for item in config.heartbeats if item.subject == "alerting_email_worker"
    )
    # The declared liveness key must match what the worker actually stamps, or
    # the watchdog silently monitors a component nothing ever writes.
    assert worker.heartbeat_component == email_worker.HEARTBEAT_COMPONENT
    assert worker.grace_minutes == 30
    assert "alert" in worker.consequence.lower()
    # The outbox subject is the durable half of the dead-letter contract.
    assert config.alert_delivery is not None
    assert config.alert_delivery.enabled
    assert config.alert_delivery.backlog_grace_minutes == 180
    assert config.grace_for("trips_sync") == 240
    # Backup and prune persist no run row, so asserting against `runs` would be
    # meaningless; they are covered by systemd OnFailure= routing instead.
    assert not any("backup" in item.subject for item in config.systemd_expectations)
    print("PASS: the shipped expectations file loads and scopes subjects correctly")


# ---------------------------------------------------------- persistence


def test_persistence_and_recovery(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS ops_control")
        conn.commit()
        migration = (REPO_ROOT / "db/migrations/059_operational_watchdog_state.sql").read_text()
        with conn.cursor() as cur:
            cur.execute(migration)
        conn.commit()
        # Re-running an additive migration must be safe.
        with conn.cursor() as cur:
            cur.execute(migration)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("DELETE FROM ops_control.watchdog_observation")
        conn.commit()

        missing = wd.Observation(
            subject_key="workflow_a:ALPHA00001:trips_sync", verdict=wd.VERDICT_MISSING,
            title="t", summary="s", component="workflow_a.schedule.trips_sync",
            detail={"scheduled_fire_ts": NOW.isoformat()},
        )
        assert wd.record_observation(conn, missing, now_utc=NOW, alerted=True) is None
        assert wd.record_observation(conn, missing, now_utc=NOW, alerted=True) == wd.VERDICT_MISSING

        with conn.cursor() as cur:
            cur.execute(
                "SELECT observation_count, verdict, last_alerted_at "
                "FROM ops_control.watchdog_observation WHERE subject_key = %s",
                (missing.subject_key,),
            )
            row = cur.fetchone()
        # Repeated scans re-observe one row; they never accumulate subjects.
        assert row["observation_count"] == 2 and row["verdict"] == wd.VERDICT_MISSING
        assert row["last_alerted_at"] is not None

        recovered = wd.Observation(
            subject_key=missing.subject_key, verdict=wd.VERDICT_OK, title="t", summary="s",
            component=missing.component,
        )
        assert wd.record_observation(conn, recovered, now_utc=NOW, alerted=False) == wd.VERDICT_MISSING
        print("PASS: repeated scans are idempotent and expose the previous verdict")

        # A dry-run scan is read-only: it must not persist observations, because
        # doing so would mutate production and rewrite the recovery baseline that
        # the next real scan compares against. Only the columns the watchdog
        # reads are needed here.
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE SCHEMA IF NOT EXISTS workflow_a_control;
                CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_schedule (
                  schedule_id uuid PRIMARY KEY, client_id uuid, client_code text,
                  dataset_name text, enabled boolean, frequency text,
                  run_type text NOT NULL DEFAULT 'DAILY',
                  day_of_week smallint, day_of_month smallint,
                  day_of_month_last boolean, run_time time, timezone text,
                  lookback_days integer,
                  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
                  -- Migration 062's identity and cadence invariants, verbatim, so a
                  -- fixture the real schema would reject cannot pass here either.
                  CONSTRAINT uq_client_dataset_schedule
                    UNIQUE (client_id, dataset_name, run_type),
                  CONSTRAINT ck_client_dataset_schedule_run_type
                    CHECK (run_type IN ('DAILY', 'WEEKLY_RECONCILIATION',
                                        'MONTHLY_RECONCILIATION')),
                  CONSTRAINT ck_client_dataset_schedule_run_type_cadence
                    CHECK (run_type = 'DAILY'
                        OR (run_type = 'WEEKLY_RECONCILIATION'  AND frequency = 'weekly')
                        OR (run_type = 'MONTHLY_RECONCILIATION' AND frequency = 'monthly'))
                );
                ALTER TABLE workflow_a_control.client_dataset_schedule
                  ADD COLUMN IF NOT EXISTS run_type text NOT NULL DEFAULT 'DAILY';
                CREATE TABLE IF NOT EXISTS workflow_a_control.client_account (
                  client_id uuid PRIMARY KEY, client_code text, client_name text,
                  enabled boolean
                );
                CREATE TABLE IF NOT EXISTS workflow_a_control.dataset_registry (
                  dataset_name text PRIMARY KEY, job_module text
                );
                CREATE TABLE IF NOT EXISTS workflow_a_control.client_schedule_run_history (
                  run_history_id uuid PRIMARY KEY, schedule_id uuid,
                  scheduled_fire_ts timestamptz, status text,
                  started_at timestamptz, created_at timestamptz, finished_at timestamptz
                );
                """
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n, max(updated_at) AS u "
                        "FROM ops_control.watchdog_observation")
            before = cur.fetchone()
        conn.rollback()
        wd.scan(conn=conn, config=wd.WatchdogConfig(), now_utc=NOW, alert=False)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n, max(updated_at) AS u "
                        "FROM ops_control.watchdog_observation")
            after = cur.fetchone()
        conn.rollback()
        assert before == after, f"dry run mutated observations: {before} -> {after}"
        print("PASS: a dry-run scan persists nothing")

        # Recovery closes only incident codes this watchdog owns.
        # Mirrors the runs/logs bootstrap in api/main.py SCHEMA_SQL, which
        # migration 052 depends on.
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE EXTENSION IF NOT EXISTS pgcrypto;
                CREATE TABLE IF NOT EXISTS runs (
                  run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,
                  ended_at TIMESTAMPTZ, status TEXT NOT NULL, trigger TEXT NOT NULL,
                  source TEXT NOT NULL, actor TEXT,
                  params JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                CREATE TABLE IF NOT EXISTS logs (
                  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL,
                  type TEXT NOT NULL, source TEXT NOT NULL,
                  run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
                  message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
                );
                """
            )
            cur.execute(
                (REPO_ROOT / "db/migrations/052_suspected_bug_incidents_and_email_outbox.sql")
                .read_text()
            )
            cur.execute("DELETE FROM suspected_bug_incidents")
        conn.commit()
        with conn.cursor() as cur:
            for code in (wd.INCIDENT_SCHEDULED_RUN_MISSING, "ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"):
                cur.execute(
                    """
                    INSERT INTO suspected_bug_incidents
                        (fingerprint, classification, incident_code, title, severity, state,
                         environment, component, first_seen_at, last_seen_at, occurrence_count,
                         material_signature)
                    VALUES (%s, 'suspected_bug', %s, 't', 'error', 'open', 'test',
                            %s, now(), now(), 1, 'sig')
                    """,
                    (f"fp-{code}", code, missing.component),
                )
        conn.commit()

        # The subject must own the fingerprint before it may close it.
        wd.record_observation(
            conn, missing, now_utc=NOW, alerted=True,
            fingerprint=f"fp-{wd.INCIDENT_SCHEDULED_RUN_MISSING}",
        )
        closed = wd.resolve_subject_incidents(
            conn, subject_key=missing.subject_key,
            incident_codes=wd.WATCHDOG_INCIDENT_CODES, now_utc=NOW,
        )
        assert closed == 1, f"expected exactly one watchdog incident closed, got {closed}"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT incident_code, state FROM suspected_bug_incidents ORDER BY incident_code"
            )
            states = {row["incident_code"]: row["state"] for row in cur.fetchall()}
        assert states[wd.INCIDENT_SCHEDULED_RUN_MISSING] == "resolved"
        # A business incident must never be closed by watchdog recovery.
        assert states["ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"] == "open"
        assert wd.load_open_fingerprints(conn, missing.subject_key) == []
        print("PASS: recovery resolves watchdog incidents only, never business incidents")

        _assert_recovery_is_scoped_to_one_subject(conn)
        _assert_eligibility_matches_the_dispatcher_query(conn)
        _assert_eligibility_epoch_round_trips(conn)
        _assert_existing_daily_state_continues_across_the_fix(conn)
        _assert_sibling_roles_persist_as_independent_subjects(conn)
        _assert_incident_lifecycle_is_scoped_to_one_role(conn)
        _assert_scan_order_cannot_change_the_outcome(conn)
    finally:
        conn.close()


def _assert_eligibility_epoch_round_trips(conn) -> None:
    """The epoch must survive in the database, not in process memory.

    Each watchdog scan is a separate short-lived process, so a transition
    remembered only in RAM would be forgotten before the next one.
    """
    subject = "workflow_a:ALPHA00001:trips_sync"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ops_control.watchdog_observation")
    conn.commit()

    disabled_at = datetime(2026, 8, 9, 3, tzinfo=UTC)
    enabled_at = datetime(2026, 8, 9, 10, 5, tzinfo=UTC)

    # Scan 1: ineligible.
    wd.record_observation(
        conn,
        wd.Observation(subject_key=subject, verdict=wd.VERDICT_DISABLED, title="t",
                       summary="s", component="workflow_a.schedule.trips_sync",
                       eligible=False, eligible_since=None),
        now_utc=disabled_at, alerted=False,
    )
    state = wd.load_observation_state(conn)[subject]
    assert state["eligible"] is False and state["eligible_since"] is None

    # Scan 2 reads that state and computes the transition.
    row = schedule(created_at=datetime(2026, 1, 1, tzinfo=UTC),
                   updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    transition = wd.eligibility_epoch(
        row, eligible_now=True, previous=state, now_utc=enabled_at
    )
    assert transition == enabled_at
    wd.record_observation(
        conn,
        wd.Observation(subject_key=subject, verdict=wd.VERDICT_NOT_YET_EXPECTED, title="t",
                       summary="s", component="workflow_a.schedule.trips_sync",
                       eligible=True, eligible_since=transition),
        now_utc=enabled_at, alerted=False,
    )

    # Scan 3 keeps it rather than advancing to "now".
    state = wd.load_observation_state(conn)[subject]
    assert state["eligible"] is True
    later = enabled_at + timedelta(hours=6)
    assert wd.eligibility_epoch(row, eligible_now=True, previous=state, now_utc=later) == enabled_at
    print("PASS: the eligibility epoch persists across scans and does not drift forward")


def _assert_recovery_is_scoped_to_one_subject(conn) -> None:
    """Two subjects, both failing. One recovers. The other must stay open."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM suspected_bug_incidents")
        cur.execute("DELETE FROM ops_control.watchdog_observation")
        for index, subject in enumerate(("systemd:workflow_b_orchestrator",
                                         "workflow_a:ALPHA00001:trips_sync")):
            cur.execute(
                """
                INSERT INTO suspected_bug_incidents
                    (fingerprint, classification, incident_code, title, severity, state,
                     environment, component, first_seen_at, last_seen_at, occurrence_count,
                     material_signature)
                VALUES (%s, 'suspected_bug', %s, 't', 'error', 'open', 'test',
                        %s, now(), now(), 1, 'sig')
                """,
                (f"fp-subject-{index}", wd.INCIDENT_SCHEDULED_RUN_MISSING, "shared.component"),
            )
    conn.commit()

    for index, subject in enumerate(("systemd:workflow_b_orchestrator",
                                     "workflow_a:ALPHA00001:trips_sync")):
        wd.record_observation(
            conn,
            wd.Observation(subject_key=subject, verdict=wd.VERDICT_MISSING, title="t",
                           summary="s", component="shared.component"),
            now_utc=NOW, alerted=True, fingerprint=f"fp-subject-{index}",
        )

    closed = wd.resolve_subject_incidents(
        conn, subject_key="systemd:workflow_b_orchestrator",
        incident_codes=wd.WATCHDOG_INCIDENT_CODES, now_utc=NOW,
    )
    assert closed == 1, closed
    with conn.cursor() as cur:
        cur.execute("SELECT fingerprint, state FROM suspected_bug_incidents ORDER BY fingerprint")
        states = {row["fingerprint"]: row["state"] for row in cur.fetchall()}
    assert states["fp-subject-0"] == "resolved"
    assert states["fp-subject-1"] == "open", (
        "recovery of one subject closed another subject's incident"
    )
    print("PASS: recovery is scoped to the exact subject, never the shared component")


def _assert_eligibility_matches_the_dispatcher_query(conn) -> None:
    """One fixture, both selection paths, compared directly.

    The dispatcher's own predicate is executed here verbatim; the watchdog's
    mirror runs over the rows its loader returns. Any divergence — a disabled
    client, an unregistered dataset — shows up as a set difference.
    """
    client_a = "aaaaaaaa-0000-0000-0000-000000000001"
    client_b = "bbbbbbbb-0000-0000-0000-000000000002"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
        cur.execute("DELETE FROM workflow_a_control.dataset_registry")
        cur.execute("DELETE FROM ops_control.watchdog_observation")
        cur.execute(
            "INSERT INTO workflow_a_control.client_account (client_id, client_code, "
            "client_name, enabled) VALUES (%s,'ALPHA00001','Alpha',true), "
            "(%s,'DEAD00001','Dead',false)",
            (client_a, client_b),
        )
        cur.execute(
            "INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module) "
            "VALUES ('trips_sync','jobs.api.telematics.sync_trips_and_speeding')"
        )
        rows = [
            # (schedule_id, client_id, code, dataset, enabled, run_type) -> eligible?
            ("11111111-0000-0000-0000-000000000001", client_a, "ALPHA00001", "trips_sync",
             True, "DAILY"),
            # A disabled schedule over the SAME dataset. Since migration 062 that
            # can only be a different role: (client, dataset, run_type) is unique.
            ("11111111-0000-0000-0000-000000000002", client_a, "ALPHA00001", "trips_sync",
             False, "WEEKLY_RECONCILIATION"),
            # enabled schedule, DISABLED client: the reported defect.
            ("11111111-0000-0000-0000-000000000003", client_b, "DEAD00001", "trips_sync",
             True, "DAILY"),
            # enabled schedule, enabled client, UNREGISTERED dataset.
            ("11111111-0000-0000-0000-000000000004", client_a, "ALPHA00001", "ghost_sync",
             True, "DAILY"),
        ]
        for schedule_id, client_id, code, dataset, enabled, run_type in rows:
            weekly = run_type == "WEEKLY_RECONCILIATION"
            cur.execute(
                "INSERT INTO workflow_a_control.client_dataset_schedule "
                "(schedule_id, client_id, client_code, dataset_name, enabled, frequency, "
                " run_type, day_of_week, run_time, timezone, lookback_days, day_of_month_last) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'02:00','UTC',1,false)",
                (schedule_id, client_id, code, dataset, enabled,
                 "weekly" if weekly else "daily", run_type, 6 if weekly else None),
            )
    conn.commit()

    # The dispatcher's eligibility predicate, verbatim.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cds.schedule_id
              FROM workflow_a_control.client_dataset_schedule cds
              JOIN workflow_a_control.client_account ca
                ON ca.client_id = cds.client_id
              JOIN workflow_a_control.dataset_registry dr
                ON dr.dataset_name = cds.dataset_name
             WHERE cds.enabled = true
               AND ca.enabled  = true
            """
        )
        dispatcher_selected = {str(row["schedule_id"]) for row in cur.fetchall()}

    watchdog_expected = {
        str(item["row"]["schedule_id"])
        for item in wd.load_workflow_a_subjects(conn, now_utc=NOW)
        if wd.schedule_eligibility(item["row"])[0]
    }
    assert dispatcher_selected == {"11111111-0000-0000-0000-000000000001"}, dispatcher_selected
    assert watchdog_expected == dispatcher_selected, (
        f"watchdog expects {watchdog_expected - dispatcher_selected} the dispatcher "
        f"never runs, and ignores {dispatcher_selected - watchdog_expected}"
    )

    # And the ineligible ones are reported, not silently dropped.
    verdicts = {
        item["row"]["schedule_id"]: wd.evaluate_schedule_subject(
            row=item["row"], history=item["history"], now_utc=NOW, config=wd.WatchdogConfig(),
            eligible_since_ts=item.get("eligible_since"),
        ).verdict
        for item in wd.load_workflow_a_subjects(conn, now_utc=NOW)
    }
    assert sum(1 for value in verdicts.values() if value == wd.VERDICT_DISABLED) == 3
    assert not any(value == wd.VERDICT_MISSING for value in verdicts.values()), (
        "an ineligible schedule was reported MISSING"
    )
    print("PASS: watchdog eligibility and dispatcher selection agree on one fixture")


# ------------------------------------------------- schedule-role identity


def _migration_062() -> str:
    return (
        REPO_ROOT / "db/migrations/062_workflow_a_multi_cadence_schedule_identity.sql"
    ).read_text()


_RUN_TYPE_VOCABULARY = re.compile(
    r"ADD CONSTRAINT ck_client_dataset_schedule_run_type\s*\n\s*"
    r"CHECK \(run_type IN \(([^)]*)\)\)"
)


def declared_run_types() -> list[str]:
    """The role vocabulary the schema actually permits, read from the migrations.

    The identity contract is only sound for the roles the database can hold, so
    these tests enumerate the constraint rather than a list invented here. Every
    migration is scanned and the *last* redeclaration wins, because reading 062
    alone would keep asserting the original three roles forever: a later
    migration that adds a role would silently never be covered, which is exactly
    the case this contract most needs to catch.
    """
    declared: list[str] | None = None
    for path in sorted((REPO_ROOT / "db/migrations").glob("*.sql")):
        # Last declaration wins within a file as well as across files: a
        # migration may restate the constraint after an interim form.
        for match in _RUN_TYPE_VOCABULARY.finditer(path.read_text()):
            declared = [
                item.strip().strip("'") for item in match.group(1).split(",") if item.strip()
            ]
    assert declared, "no migration declares the run_type vocabulary in the expected shape"
    return declared


def test_schedule_identity_invariant_is_client_dataset_run_type() -> None:
    """Why a role-qualified key is *sufficient*, stated against the schema.

    If two schedules could share (client, dataset, run_type), the qualified key
    would still fold them together and would have to carry `schedule_id`.
    """
    sql = _migration_062()
    assert re.search(
        r"ADD CONSTRAINT uq_client_dataset_schedule\s*\n\s*"
        r"UNIQUE \(client_id, dataset_name, run_type\)",
        sql,
    ), "schedule uniqueness is no longer (client_id, dataset_name, run_type)"
    # At most one DAILY per (client, dataset) is what lets the DAILY subject
    # stay unqualified and still be unique.
    assert re.search(
        r"CREATE UNIQUE INDEX uq_client_dataset_schedule_base[\s\S]{0,300}?"
        r"WHERE run_type = 'DAILY'",
        sql,
    ), "the one-DAILY-per-dataset invariant is gone; unqualified DAILY keys are no longer unique"
    print("PASS: (client, dataset, run_type) is the schedule identity a subject key must carry")


def test_daily_subject_key_is_byte_identical_to_the_legacy_key() -> None:
    """Load-bearing: live DAILY observation, epoch and incident state is addressed
    by this exact string, and an incident fingerprint hashes it."""
    legacy = "workflow_a:ALPHA00001:trips_sync"
    assert wd.workflow_a_subject_key(schedule()) == legacy
    assert wd.workflow_a_subject_key(schedule(run_type="DAILY")) == legacy
    # A row from a database predating migration 062, or a caller that does not
    # project the column, describes a DAILY schedule.
    assert wd.workflow_a_subject_key(schedule(run_type=None)) == legacy
    assert wd.workflow_a_subject_key(schedule(run_type="")) == legacy
    assert wd.workflow_a_subject_key(schedule(run_type=" daily ")) == legacy
    assert wd.workflow_a_subject_key(
        {k: v for k, v in schedule().items() if k != "run_type"}
    ) == legacy

    observation = wd.evaluate_schedule_subject(
        row=schedule(), history=None, now_utc=NOW, config=CONFIG,
    )
    assert observation.subject_key == legacy
    print("PASS: the DAILY subject key is byte-identical to the pre-fix key")


def test_a_client_without_a_code_is_still_its_own_subject() -> None:
    """`client_code` is nullable *by design* (migration 017), and a unique index
    does not constrain repeated NULLs, so it cannot be the whole client identity.

    This is the same collision one level up: two code-less clients sharing a
    dataset would share a verdict, an epoch, a counter and an incident.
    """
    assert "NULL remains allowed" in (
        REPO_ROOT / "db/migrations/017_workflow_a_add_client_code_to_control_tables.sql"
    ).read_text(), "migration 017 no longer documents a code-less client as legal"

    first = schedule(client_code=None, client_id="99999999-0000-0000-0000-000000000001")
    second = schedule(client_code=None, client_id="99999999-0000-0000-0000-000000000002")
    assert wd.workflow_a_subject_key(first) != wd.workflow_a_subject_key(second)
    assert "None" not in wd.workflow_a_subject_key(first)
    # A blank code is *present*, not absent, and the schema has no non-empty
    # CHECK on `client_code`, so `workflow_a::{dataset}` is a key a database can
    # already hold. Falling back for it would move that live key — the very thing
    # keeping DAILY unqualified exists to prevent — so absence is tested as
    # `is None`, never as falsiness.
    assert wd.workflow_a_subject_key(schedule(client_code="")) == "workflow_a::trips_sync"
    assert wd.workflow_a_subject_key(schedule(client_code="   ")) == "workflow_a:   :trips_sync"
    assert wd.workflow_a_subject_key(schedule(client_code="")) != wd.workflow_a_subject_key(
        schedule(client_code=None)
    )
    # Two clients sharing a blank code are a collision like any other: reported,
    # not silently re-keyed.
    assert wd.report_subject_key_collisions([
        schedule(client_code="", client_id="99999999-0000-0000-0000-000000000001",
                 schedule_id="55555555-0000-0000-0000-00000000000a"),
        schedule(client_code="", client_id="99999999-0000-0000-0000-000000000002",
                 schedule_id="55555555-0000-0000-0000-00000000000b"),
    ]) == ["workflow_a::trips_sync"]
    # The fallback is unreachable for every client that has a code, which is why
    # no existing production key moves.
    assert wd.workflow_a_subject_key(schedule()) == "workflow_a:ALPHA00001:trips_sync"
    # And it composes with the role qualifier rather than replacing it.
    assert wd.workflow_a_subject_key(
        schedule(client_code=None, client_id="99999999-0000-0000-0000-000000000001",
                 run_type="WEEKLY_RECONCILIATION", frequency="weekly", day_of_week=6)
    ) == "workflow_a:99999999-0000-0000-0000-000000000001:trips_sync:WEEKLY_RECONCILIATION"
    print("PASS: a client without a code is identified by client_id, not by 'None'")


def test_a_shared_subject_key_is_reported_and_the_identity_does_not_move() -> None:
    """`client_code` is unconstrained TEXT, so uniqueness cannot rest on it alone.

    The pathological case: one client is coded as another client's UUID while
    that other client has no code. The watchdog reports it and leaves both keys
    exactly where they are. It must NOT disambiguate by appending `schedule_id`:
    that would make a subject's identity depend on which *other* rows exist, so
    deleting or correcting one schedule would move the survivor's key and orphan
    the observation history and open incident fingerprints filed under it.
    """
    victim_id = "99999999-0000-0000-0000-000000000001"
    coded = schedule(schedule_id="55555555-0000-0000-0000-000000000001",
                     client_id="88888888-0000-0000-0000-000000000002",
                     client_code=victim_id)
    codeless = schedule(schedule_id="55555555-0000-0000-0000-000000000002",
                        client_id=victim_id, client_code=None)
    shared = wd.workflow_a_subject_key(coded)
    assert shared == wd.workflow_a_subject_key(codeless), (
        "this test no longer constructs the collision it exists to detect"
    )

    assert wd.report_subject_key_collisions([coded, codeless]) == [shared]
    # Identity is a fact about one schedule: reporting must not rewrite it, and
    # removing the intruder must leave the survivor exactly where it was.
    assert wd.workflow_a_subject_key(coded) == shared
    assert wd.workflow_a_subject_key(codeless) == shared
    assert wd.report_subject_key_collisions([codeless]) == []
    assert wd.workflow_a_subject_key(codeless) == shared

    # A fleet whose codes are distinct reports nothing, including sibling roles.
    healthy = [schedule(), schedule(schedule_id="55555555-0000-0000-0000-000000000003",
                                    run_type="WEEKLY_RECONCILIATION", frequency="weekly",
                                    day_of_week=6)]
    assert wd.report_subject_key_collisions(healthy) == []
    print("PASS: a shared subject key is reported loudly and no identity is silently moved")


def test_every_schedule_role_gets_its_own_subject_key() -> None:
    roles = declared_run_types()
    assert "DAILY" in roles and len(roles) >= 2, roles
    keys = {role: wd.workflow_a_subject_key(schedule(run_type=role)) for role in roles}
    assert len(set(keys.values())) == len(roles), keys
    assert keys["DAILY"] == "workflow_a:ALPHA00001:trips_sync"
    for role in roles:
        if role != "DAILY":
            assert keys[role] == f"workflow_a:ALPHA00001:trips_sync:{role}", keys[role]
    print(f"PASS: {len(roles)} schedule roles produce {len(roles)} distinct subject keys")


def test_a_qualified_key_can_never_be_read_as_an_unqualified_one() -> None:
    """The two key shapes cannot alias.

    A qualified key could only collide with another subject's unqualified key if
    a legal `dataset_name` could spell a `run_type`. Migration 011 constrains
    dataset names to lower case; every declared role is upper case.
    """
    pattern = re.search(
        r"CHECK \(dataset_name ~ '([^']+)'\)",
        (REPO_ROOT / "db/migrations/011_workflow_a_dataset_registry.sql").read_text(),
    )
    assert pattern, "the dataset_name format constraint moved"
    legal_dataset = re.compile(pattern.group(1))
    for role in declared_run_types():
        assert not legal_dataset.match(role), (
            f"run_type {role} is also a legal dataset_name, so the qualified and "
            f"unqualified key shapes can alias"
        )
    print("PASS: no legal dataset name can spell a role, so the key shapes cannot alias")


def test_sibling_roles_over_one_dataset_are_evaluated_as_two_subjects() -> None:
    """The reported defect at the evaluation layer: same client, same dataset,
    same scan, opposite verdicts."""
    daily = schedule()
    weekly = schedule(
        schedule_id="33333333-3333-3333-3333-333333333333",
        run_type="WEEKLY_RECONCILIATION", frequency="weekly", day_of_week=6,
    )
    success = {
        "run_history_id": "44444444-4444-4444-4444-444444444444", "status": "SUCCESS",
        "started_at": datetime(2026, 8, 9, 2, 5, tzinfo=UTC),
        "created_at": datetime(2026, 8, 9, 2, 5, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 9, 2, 30, tzinfo=UTC),
    }

    missing = wd.evaluate_schedule_subject(row=daily, history=None, now_utc=NOW, config=CONFIG)
    healthy = wd.evaluate_schedule_subject(row=weekly, history=success, now_utc=NOW, config=CONFIG)

    assert missing.verdict == wd.VERDICT_MISSING
    assert healthy.verdict == wd.VERDICT_OK
    assert missing.subject_key != healthy.subject_key, (
        "a DAILY schedule and its reconciliation sibling still share one subject key"
    )
    assert missing.detail["run_type"] == "DAILY"
    assert healthy.detail["run_type"] == "WEEKLY_RECONCILIATION"
    print("PASS: sibling roles over one dataset evaluate as two independent subjects")


def test_the_role_is_reported_without_entering_incident_identity() -> None:
    """`run_type` is debugging metadata on an already-distinct subject.

    It travels in `details`, which `fingerprint_identity()` excludes, so adding
    it cannot perturb an existing DAILY incident fingerprint.
    """
    from api.suspected_bug import SuspectedBugEvent

    observation = wd.evaluate_schedule_subject(
        row=schedule(), history=None, now_utc=NOW, config=CONFIG,
    )
    assert "run_type" in observation.detail
    identity = SuspectedBugEvent(
        incident_code=wd.INCIDENT_SCHEDULED_RUN_MISSING, title="t", summary="s",
        occurred_at=NOW, environment="test", component=observation.component,
        subject_type="watchdog_subject", subject_key=observation.subject_key,
        details=dict(observation.detail),
        fingerprint_fields={"verdict": observation.verdict},
    ).fingerprint_identity()
    assert identity["subject_key"] == "workflow_a:ALPHA00001:trips_sync"
    assert "run_type" not in json.dumps(identity["fingerprint_fields"], default=str)
    print("PASS: the role is reported in details and stays out of incident identity")


# ------------------------------- sibling-role isolation (disposable database)

#: One client, one dataset, three schedule roles — the shape M6 introduces.
_ROLE_CLIENT = "cccccccc-0000-0000-0000-000000000001"
_ROLE_SCHEDULES = {
    # run_type: (schedule_id, frequency, day_of_week, day_of_month, run_time, fire)
    "DAILY": ("dddddddd-0000-0000-0000-000000000001", "daily", None, None, "02:00",
              datetime(2026, 8, 9, 2, 0, tzinfo=UTC)),
    "WEEKLY_RECONCILIATION": ("dddddddd-0000-0000-0000-000000000002", "weekly", 6, None,
                              "02:00", datetime(2026, 8, 9, 2, 0, tzinfo=UTC)),
    "MONTHLY_RECONCILIATION": ("dddddddd-0000-0000-0000-000000000003", "monthly", None, 1,
                               "03:30", datetime(2026, 8, 1, 3, 30, tzinfo=UTC)),
}
#: Long-established schedules: a subject whose row is younger than the fire it
#: missed is legitimately NOT_YET_EXPECTED, which is a different test.
_ROLE_ESTABLISHED = datetime(2026, 1, 1, tzinfo=UTC)
_DAILY_KEY = "workflow_a:ALPHA00001:trips_sync"
_WEEKLY_KEY = f"{_DAILY_KEY}:WEEKLY_RECONCILIATION"
_MONTHLY_KEY = f"{_DAILY_KEY}:MONTHLY_RECONCILIATION"


def _reset_role_fixture(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
        cur.execute("DELETE FROM workflow_a_control.dataset_registry")
        cur.execute("DELETE FROM ops_control.watchdog_observation")
        cur.execute("DELETE FROM suspected_bug_incidents")
        cur.execute(
            "INSERT INTO workflow_a_control.client_account "
            "(client_id, client_code, client_name, enabled) VALUES (%s,'ALPHA00001','Alpha',true)",
            (_ROLE_CLIENT,),
        )
        cur.execute(
            "INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module) "
            "VALUES ('trips_sync','jobs.api.telematics.sync_trips_and_speeding')"
        )
    conn.commit()


def _register_role(conn, run_type: str, *, enabled: bool = True) -> None:
    schedule_id, frequency, dow, dom, run_time, _ = _ROLE_SCHEDULES[run_type]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
                (schedule_id, client_id, client_code, dataset_name, enabled, frequency,
                 run_type, day_of_week, day_of_month, day_of_month_last, run_time,
                 timezone, lookback_days, created_at, updated_at)
            VALUES (%s,%s,'ALPHA00001','trips_sync',%s,%s,%s,%s,%s,false,%s,'UTC',1,
                    %s,%s)
            ON CONFLICT (schedule_id) DO UPDATE SET enabled = EXCLUDED.enabled
            """,
            (schedule_id, _ROLE_CLIENT, enabled, frequency, run_type, dow, dom, run_time,
             _ROLE_ESTABLISHED, _ROLE_ESTABLISHED),
        )
    conn.commit()


def _set_role_enabled(conn, run_type: str, enabled: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_schedule SET enabled = %s "
            " WHERE schedule_id = %s",
            (enabled, _ROLE_SCHEDULES[run_type][0]),
        )
    conn.commit()


def _role_executed(conn, run_type: str, *, status: str | None) -> None:
    """Give one role a claim record for its own fire, or take it away.

    `status=None` is the missed execution: no run-history row for that fire.
    """
    schedule_id, _, _, _, _, fire = _ROLE_SCHEDULES[run_type]
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM workflow_a_control.client_schedule_run_history WHERE schedule_id = %s",
            (schedule_id,),
        )
        if status is not None:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                    (run_history_id, schedule_id, scheduled_fire_ts, status,
                     started_at, created_at, finished_at)
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s)
                """,
                (schedule_id, fire, status, fire, fire, fire + timedelta(minutes=20)),
            )
    conn.commit()


def _observations(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT subject_key, verdict, observation_count, open_incident_fingerprints, "
            "       first_observed_at, detail "
            "  FROM ops_control.watchdog_observation ORDER BY subject_key"
        )
        return {str(row["subject_key"]): dict(row) for row in cur.fetchall()}


def _incidents(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint, state, occurrence_count FROM suspected_bug_incidents "
            " WHERE incident_code = ANY(%s) ORDER BY fingerprint",
            (list(wd.WATCHDOG_INCIDENT_CODES),),
        )
        return {str(row["fingerprint"]): dict(row) for row in cur.fetchall()}


def _open_fingerprints(conn) -> set[str]:
    return {fp for fp, row in _incidents(conn).items() if row["state"] == "open"}


def _scan(conn, *, now=None, reverse: bool = False) -> None:
    """One real alerting scan against the disposable database.

    `scan()` reports through `report_operational_failure`, which opens its *own*
    connection — the production one. Only that connection is rebound here: the
    real reporter, the real fingerprint and the real incident write still run,
    because the identity this test is about is exactly what the fingerprint
    hashes. `reverse` feeds the same rows to the same scan in the opposite
    order, which must not change the outcome now that the keys are distinct.
    """
    real_report = wd.report_operational_failure
    real_loader = wd.load_workflow_a_subjects

    def bound_report(**kwargs):
        return real_report(conn=conn, **kwargs)

    def reversed_loader(*args, **kwargs):
        return list(reversed(real_loader(*args, **kwargs)))

    wd.report_operational_failure = bound_report
    if reverse:
        wd.load_workflow_a_subjects = reversed_loader
    try:
        wd.scan(conn=conn, config=wd.WatchdogConfig(), now_utc=now or NOW, alert=True)
    finally:
        wd.report_operational_failure = real_report
        wd.load_workflow_a_subjects = real_loader


def _assert_existing_daily_state_continues_across_the_fix(conn) -> None:
    """The backward-compatibility proof, end to end.

    A DAILY-only fleet is scanned first — which is byte-for-byte the pre-fix
    situation, because the pre-fix key and the post-fix DAILY key are the same
    string. Then the M6 sibling is registered, exactly as enabling a
    reconciliation schedule would. The DAILY subject must *continue*: same row,
    same epoch, same incident, incremented counters — not a fresh identity that
    silently abandons the open incident.
    """
    _reset_role_fixture(conn)
    _register_role(conn, "DAILY")
    _role_executed(conn, "DAILY", status=None)

    _scan(conn)
    before = _observations(conn)
    assert set(before) == {_DAILY_KEY}, before
    assert before[_DAILY_KEY]["verdict"] == wd.VERDICT_MISSING
    assert before[_DAILY_KEY]["observation_count"] == 1
    daily_fingerprints = [str(fp) for fp in before[_DAILY_KEY]["open_incident_fingerprints"]]
    assert len(daily_fingerprints) == 1, daily_fingerprints
    daily_fingerprint = daily_fingerprints[0]
    assert _incidents(conn)[daily_fingerprint]["state"] == "open"

    # The M6 enable: a second role appears over the same client and dataset.
    _register_role(conn, "WEEKLY_RECONCILIATION")
    _role_executed(conn, "WEEKLY_RECONCILIATION", status="SUCCESS")
    _scan(conn)

    after = _observations(conn)
    assert set(after) == {_DAILY_KEY, _WEEKLY_KEY}, set(after)
    assert after[_DAILY_KEY]["first_observed_at"] == before[_DAILY_KEY]["first_observed_at"], (
        "the DAILY subject was re-created instead of continued"
    )
    assert after[_DAILY_KEY]["observation_count"] == 2, after[_DAILY_KEY]["observation_count"]
    assert after[_DAILY_KEY]["verdict"] == wd.VERDICT_MISSING
    assert [str(fp) for fp in after[_DAILY_KEY]["open_incident_fingerprints"]] == [
        daily_fingerprint
    ], "the DAILY subject lost or replaced the incident it already owned"
    incidents = _incidents(conn)
    assert incidents[daily_fingerprint]["state"] == "open", (
        "the healthy WEEKLY sibling closed the DAILY incident"
    )
    assert incidents[daily_fingerprint]["occurrence_count"] == 2, (
        "the second scan opened a new incident instead of continuing the existing one"
    )
    assert after[_WEEKLY_KEY]["verdict"] == wd.VERDICT_OK
    assert after[_WEEKLY_KEY]["observation_count"] == 1
    print("PASS: existing DAILY observation, epoch and incident state survives the sibling")


def _assert_sibling_roles_persist_as_independent_subjects(conn) -> None:
    """Same client, same dataset, same scan: DAILY MISSING and WEEKLY OK."""
    _reset_role_fixture(conn)
    for role in ("DAILY", "WEEKLY_RECONCILIATION", "MONTHLY_RECONCILIATION"):
        _register_role(conn, role)
    _role_executed(conn, "DAILY", status=None)
    _role_executed(conn, "WEEKLY_RECONCILIATION", status="SUCCESS")
    _role_executed(conn, "MONTHLY_RECONCILIATION", status="SUCCESS")

    _scan(conn)
    rows = _observations(conn)
    assert set(rows) == {_DAILY_KEY, _WEEKLY_KEY, _MONTHLY_KEY}, set(rows)
    assert rows[_DAILY_KEY]["verdict"] == wd.VERDICT_MISSING
    assert rows[_WEEKLY_KEY]["verdict"] == wd.VERDICT_OK, (
        "the WEEKLY sibling was overwritten by the DAILY verdict"
    )
    assert rows[_MONTHLY_KEY]["verdict"] == wd.VERDICT_OK
    for key in (_DAILY_KEY, _WEEKLY_KEY, _MONTHLY_KEY):
        assert rows[key]["observation_count"] == 1, (
            f"{key} counted {rows[key]['observation_count']} observations for one scan"
        )
        assert rows[key]["detail"]["run_type"], f"{key} does not report its role"
    assert len(_open_fingerprints(conn)) == 1, "the healthy siblings raised incidents"
    print("PASS: three roles over one dataset persist as three independent subjects")


def _assert_incident_lifecycle_is_scoped_to_one_role(conn) -> None:
    """The full transition matrix across two siblings."""
    _reset_role_fixture(conn)
    _register_role(conn, "DAILY")
    _register_role(conn, "WEEKLY_RECONCILIATION")
    _role_executed(conn, "DAILY", status=None)
    _role_executed(conn, "WEEKLY_RECONCILIATION", status="SUCCESS")

    # 1) DAILY MISSING opens its own incident; 2) WEEKLY OK does not close it.
    _scan(conn)
    rows = _observations(conn)
    daily_fp = str(rows[_DAILY_KEY]["open_incident_fingerprints"][0])
    assert _open_fingerprints(conn) == {daily_fp}
    assert rows[_WEEKLY_KEY]["open_incident_fingerprints"] == []
    _scan(conn)
    assert _open_fingerprints(conn) == {daily_fp}, "a repeated WEEKLY OK closed the DAILY incident"

    # 3) WEEKLY MISSING opens an independent incident. DAILY keeps its own.
    _role_executed(conn, "WEEKLY_RECONCILIATION", status=None)
    _scan(conn)
    rows = _observations(conn)
    weekly_fp = str(rows[_WEEKLY_KEY]["open_incident_fingerprints"][0])
    assert weekly_fp != daily_fp, "both roles are reporting one incident fingerprint"
    assert _open_fingerprints(conn) == {daily_fp, weekly_fp}
    assert rows[_DAILY_KEY]["verdict"] == wd.VERDICT_MISSING
    assert rows[_WEEKLY_KEY]["verdict"] == wd.VERDICT_MISSING

    # 4) DAILY recovers: its incident resolves, the WEEKLY one does not.
    _role_executed(conn, "DAILY", status="SUCCESS")
    _scan(conn)
    rows = _observations(conn)
    assert rows[_DAILY_KEY]["verdict"] == wd.VERDICT_OK
    assert rows[_DAILY_KEY]["open_incident_fingerprints"] == []
    assert _open_fingerprints(conn) == {weekly_fp}, (
        "DAILY recovery closed the WEEKLY incident too"
    )

    # 5) WEEKLY recovers: its own incident resolves, and only now.
    _role_executed(conn, "WEEKLY_RECONCILIATION", status="SUCCESS")
    _scan(conn)
    rows = _observations(conn)
    assert rows[_WEEKLY_KEY]["verdict"] == wd.VERDICT_OK
    assert _open_fingerprints(conn) == set()
    assert _incidents(conn)[daily_fp]["state"] == "resolved"
    assert _incidents(conn)[weekly_fp]["state"] == "resolved"

    # 6) A NON_EXPECTING sibling cannot disturb the other. DAILY relapses, then
    #    WEEKLY is disabled — which resolves nothing, least of all DAILY's
    #    incident, even though DISABLED is not a failing verdict.
    _role_executed(conn, "DAILY", status=None)
    _scan(conn)
    rows = _observations(conn)
    relapsed_fp = str(rows[_DAILY_KEY]["open_incident_fingerprints"][0])
    assert _open_fingerprints(conn) == {relapsed_fp}
    _set_role_enabled(conn, "WEEKLY_RECONCILIATION", False)
    _scan(conn)
    rows = _observations(conn)
    assert rows[_WEEKLY_KEY]["verdict"] == wd.VERDICT_DISABLED
    assert rows[_WEEKLY_KEY]["open_incident_fingerprints"] == []
    assert rows[_DAILY_KEY]["verdict"] == wd.VERDICT_MISSING
    assert _open_fingerprints(conn) == {relapsed_fp}, "disabling one role closed the sibling's incident"

    # 7) Every transition above read each key's own row. Had the siblings shared
    #    a key, the DAILY row would have carried a WEEKLY verdict as `previous`
    #    and none of these assertions could hold together.
    print("PASS: incidents open, persist and resolve strictly within one schedule role")


def _assert_scan_order_cannot_change_the_outcome(conn) -> None:
    """Ordering is for humans. Reversing the enumeration must change nothing."""
    def build() -> None:
        _reset_role_fixture(conn)
        for role in ("DAILY", "WEEKLY_RECONCILIATION", "MONTHLY_RECONCILIATION"):
            _register_role(conn, role)
        _role_executed(conn, "DAILY", status=None)
        _role_executed(conn, "WEEKLY_RECONCILIATION", status="SUCCESS")
        _role_executed(conn, "MONTHLY_RECONCILIATION", status=None)

    def settled() -> tuple:
        rows = _observations(conn)
        return tuple(
            (key, rows[key]["verdict"], rows[key]["observation_count"],
             len(rows[key]["open_incident_fingerprints"]))
            for key in sorted(rows)
        )

    build()
    _scan(conn)
    forward = settled()
    forward_open = len(_open_fingerprints(conn))

    build()
    _scan(conn, reverse=True)
    assert settled() == forward, f"scan order changed the outcome: {settled()} != {forward}"
    assert len(_open_fingerprints(conn)) == forward_open
    assert forward_open == 2, forward_open

    # And the loader itself enumerates in one stable order.
    first = [item["row"]["schedule_id"] for item in wd.load_workflow_a_subjects(conn, now_utc=NOW)]
    second = [item["row"]["schedule_id"] for item in wd.load_workflow_a_subjects(conn, now_utc=NOW)]
    assert first == second and len(first) == 3, first
    print("PASS: subject enumeration is deterministic and its order cannot change the outcome")


def main() -> None:
    test_expected_fire_with_success_is_ok()
    test_expected_fire_with_failure_defers_to_the_job_alert_path()
    test_missing_execution_raises_only_after_the_grace_window()
    test_running_past_stale_grace_is_stuck()
    test_disabled_schedule_expects_nothing()
    test_weekly_and_monthly_fires_use_dispatcher_arithmetic()
    test_dataset_override_widens_only_the_named_dataset()
    test_eligibility_mirrors_the_dispatcher()
    test_dispatcher_eligibility_contract_has_not_drifted()
    test_newly_eligible_subjects_have_no_retroactive_missing_fires()
    test_codex_disabled_client_re_enable_does_not_backdate_expectation()
    test_first_fire_after_re_enable_is_monitored_normally()
    test_repeated_eligibility_cycles_start_fresh_epochs()
    test_unregistered_dataset_registered_later_does_not_backdate()
    test_never_seen_subjects_use_row_evidence_not_a_free_pass()
    test_systemd_fires_are_enumerated_in_the_lookback_window()
    test_workflow_b_run_that_never_registered_is_missing()
    test_workflow_b_non_terminal_run_becomes_stale()
    test_every_terminal_run_status_settles_the_fire()
    test_terminal_status_vocabulary_matches_the_platform()
    test_a_continuing_outage_is_one_root_subject_not_one_per_fire()
    test_a_later_successful_fire_makes_the_root_healthy()
    test_a_fire_inside_its_grace_window_decides_nothing()
    test_a_disabled_expectation_expects_nothing()
    test_heartbeat_absence_is_scheduler_death()
    test_alert_worker_liveness_is_observed_independently()
    test_dead_letter_and_backlog_are_durably_observable()
    test_alert_delivery_incident_is_resolvable_and_not_self_alerting()
    test_repository_expectations_file_loads()
    test_schedule_identity_invariant_is_client_dataset_run_type()
    test_daily_subject_key_is_byte_identical_to_the_legacy_key()
    test_a_client_without_a_code_is_still_its_own_subject()
    test_a_shared_subject_key_is_reported_and_the_identity_does_not_move()
    test_every_schedule_role_gets_its_own_subject_key()
    test_a_qualified_key_can_never_be_read_as_an_unqualified_one()
    test_sibling_roles_over_one_dataset_are_evaluated_as_two_subjects()
    test_the_role_is_reported_without_entering_incident_identity()

    dsn = os.environ.get("WATCHDOG_TEST_DSN")
    if dsn:
        if "logdb" in dsn:
            raise SystemExit("refusing to run against logdb; use a disposable database")
        test_persistence_and_recovery(dsn)
    else:
        print("SKIP: WATCHDOG_TEST_DSN unset - persistence and recovery tests not run")
    print("OK - execution watchdog tests passed")


if __name__ == "__main__":
    main()
