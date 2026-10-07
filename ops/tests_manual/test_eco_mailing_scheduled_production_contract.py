#!/usr/bin/env python3
"""THE BRAVO00016 scheduled production execution contract, proved offline.

Nothing here sends mail, opens SMTP or IMAP, contacts a publisher, mints a
capability, touches R2/D1 or reaches a database. Every assertion is either a
pure function call or a statement about repository text.

WHAT IS BEING PROVED

One canonical path. A scheduled fire, an operator's manual command and a future
email-command trigger must all become the SAME invocation of the SAME job with
the SAME validation — differing only in that the scheduler resolves the period
automatically instead of being handed explicit dates.

    trigger -> canonical runner -> EcoDriving Person job -> candidates ->
    dashboard snapshot/publication/capability -> rendering -> SMTP ->
    Sent archive -> durable log/state

and, for the dashboard, that BOTH conditions still hold independently:

    a declared (client, dataset) scheduled-production entry   [intent]
  AND
    client-level rollout permission                            [permission]

Run:  PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_eco_mailing_scheduled_production_contract.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry
from jobs.ecodriving import email_safety as safety
from jobs.ecodriving import scheduled_mailing_contract as smc
from jobs.ecodriving_dashboard import dashboard_rollout as roll
from jobs.ecodriving_dashboard import eco_mailing_integration as emi
from jobs.ecodriving_person import email_idempotency as idem

WARSAW = ZoneInfo("Europe/Warsaw")
BRAVO = "BRAVO00016"
ALPHA = "ALPHA00001"
WEEKLY_DATASET = "eco_person_driving_weekly_email_notifications"
MONTHLY_DATASET = "eco_person_driving_monthly_email_notifications"
WEEKLY_JOB = (
    "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications"
)
WEEKLY_JOB_PATH = (
    "jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py"
)

FAILURES: list[str] = []
PASSED: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{what}{(' — ' + detail) if detail else ''}")


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def declaration(entries: list[dict]) -> smc.ScheduledMailingDeclaration:
    return smc.parse_schedule_declaration(
        {"contract": smc.SCHEDULE_CONTRACT, "schedules": entries},
        source="<test>",
    )


def rollout(enabled: set[str], declared: set[str] | None = None):
    return roll.DashboardMailingRollout(
        source="<test-rollout>",
        enabled_clients=frozenset(enabled),
        declared_clients=frozenset(declared if declared is not None else enabled),
    )


PRODUCTION_ROLLOUT = roll.load_rollout(roll.DEFAULT_ROLLOUT_PATH)
PRODUCTION_SCHEDULE = smc.load_schedule_declaration(smc.DEFAULT_SCHEDULE_PATH)


# ==============================================================================
# 1 — THE DECLARATION, AND WHAT IT REFUSES TO SAY
# ==============================================================================

def test_the_production_declaration_states_exactly_one_scheduled_send() -> None:
    entries = PRODUCTION_SCHEDULE.entries
    check("BRAVO00016 weekly is declared as a production send",
          entries.get((WEEKLY_DATASET, BRAVO)) == "normal_send", str(entries))
    check("and nothing else is declared at all",
          set(entries) == {(WEEKLY_DATASET, BRAVO)}, str(entries))
    check("so the monthly mailing is not scheduled production",
          (MONTHLY_DATASET, BRAVO) not in entries)
    check("and ALPHA00001 has no scheduled production entry",
          not any(client == ALPHA for _dataset, client in entries))
    PASSED.append("the_production_declaration_states_exactly_one_scheduled_send")


def test_the_declaration_fails_closed_on_everything_ambiguous() -> None:
    valid = {"client_code": BRAVO, "dataset_name": WEEKLY_DATASET,
             "execution_mode": "normal_send"}
    refusals = {
        "a wildcard client": [{**valid, "client_code": "*"}],
        "a wildcard dataset": [{**valid, "dataset_name": "ALL"}],
        "a duplicated pair": [valid, {**valid, "note": "again"}],
        "an unknown entry key": [{**valid, "enabled": True}],
        "a dataset that is not Eco mailing": [
            {**valid, "dataset_name": "trips_sync"}],
        "a mode a timer may not decide (test_send)": [
            {**valid, "execution_mode": "test_send"}],
        "a mode a timer may not decide (force_resend)": [
            {**valid, "execution_mode": "force_resend"}],
        "an unknown mode": [{**valid, "execution_mode": "send_it"}],
        "a non-string mode": [{**valid, "execution_mode": True}],
        "a blank client code": [{**valid, "client_code": "   "}],
    }
    for what, entries in refusals.items():
        try:
            declaration(entries)
        except smc.ScheduledMailingDeclarationError as error:
            check(f"{what} is MALFORMED, never widened",
                  error.code == smc.MALFORMED, f"{what}: {error.code}")
        else:
            FAILURES.append(f"{what} was accepted")

    for what, document in {
        "a wrong contract identity": {"contract": "something/1", "schedules": []},
        "a missing contract identity": {"schedules": []},
        "an unknown top-level key": {"contract": smc.SCHEDULE_CONTRACT,
                                     "schedules": [], "enabled": True},
        "a declaration that states no schedules": {
            "contract": smc.SCHEDULE_CONTRACT},
        "a list instead of an object": [],
    }.items():
        try:
            smc.parse_schedule_declaration(document, source="<test>")
        except smc.ScheduledMailingDeclarationError as error:
            check(f"{what} is MALFORMED", error.code == smc.MALFORMED, what)
        else:
            FAILURES.append(f"{what} was accepted")

    with tempfile.TemporaryDirectory() as directory:
        missing = Path(directory) / "absent.json"
        try:
            smc.load_schedule_declaration(missing)
        except smc.ScheduledMailingDeclarationError as error:
            check("a source that cannot be read is UNREADABLE, not MALFORMED",
                  error.code == smc.UNREADABLE, error.code)
        else:
            FAILURES.append("a missing declaration was accepted")
        broken = Path(directory) / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        try:
            smc.load_schedule_declaration(broken)
        except smc.ScheduledMailingDeclarationError as error:
            check("unparseable JSON is MALFORMED", error.code == smc.MALFORMED)
        else:
            FAILURES.append("unparseable JSON was accepted")

    check("an empty declaration is legal and enables nobody",
          declaration([]).entries == {})
    PASSED.append("the_declaration_fails_closed_on_everything_ambiguous")


# ==============================================================================
# 2 — WHAT A SCHEDULED FIRE RESOLVES TO
# ==============================================================================

def test_scheduled_bravo00016_resolves_normal_send_with_the_dashboard_on() -> None:
    invocation = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=BRAVO,
        declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
    )
    check("the scheduled weekly fire is declared production", invocation is not None)
    assert invocation is not None
    check("it uses normal_send", invocation.execution_mode == "normal_send")
    check("it actually sends", invocation.sends_email is True)
    check("the dashboard is ON", invocation.with_dashboard is True)
    check("and reaches the job as THE runner option, not a smuggled parameter",
          invocation.runner_options == (smc.RUNNER_DASHBOARD_OPTION,),
          str(invocation.runner_options))
    check("the only parameter this contract contributes is the execution mode",
          invocation.params_overrides() == {"execution_mode": "normal_send"})
    check("it names the canonical weekly job", invocation.job_module == WEEKLY_JOB)
    check("the audit record states the dashboard state explicitly",
          invocation.audit()["dashboard_enabled"] is True
          and invocation.audit()["execution_mode"] == "normal_send")
    PASSED.append("scheduled_bravo00016_resolves_normal_send_with_the_dashboard_on")


def test_alpha00001_and_unknown_clients_stay_off() -> None:
    for client_code in (ALPHA, "NEWCLIENT", "", None, "   "):
        check("an undeclared client resolves to no scheduled production at all",
              smc.resolve_scheduled_invocation(
                  dataset_name=WEEKLY_DATASET, client_code=client_code,
                  declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
              ) is None, repr(client_code))
    check("and neither does the undeclared monthly dataset",
          smc.resolve_scheduled_invocation(
              dataset_name=MONTHLY_DATASET, client_code=BRAVO,
              declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
          ) is None)
    for dataset in ("trips_sync", "fuel_daily_aggregation",
                    "eco_person_driving_weekly_snapshot", "", None):
        check("a non-mailing dataset can never resolve one",
              smc.resolve_scheduled_invocation(
                  dataset_name=dataset, client_code=BRAVO,
                  declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
              ) is None, repr(dataset))

    # The two conditions are independent, proved in both directions.
    declared_alpha = declaration([
        {"client_code": ALPHA, "dataset_name": WEEKLY_DATASET,
         "execution_mode": "normal_send"}])
    forced = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=ALPHA,
        declaration=declared_alpha, rollout=PRODUCTION_ROLLOUT,
    )
    check("declaring ALPHA00001 for scheduled sending does NOT give it a dashboard",
          forced is not None and forced.with_dashboard is False
          and forced.runner_options == ())
    permitted_but_undeclared = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=ALPHA,
        declaration=PRODUCTION_SCHEDULE, rollout=rollout({BRAVO, ALPHA}),
    )
    check("and rollout permission alone schedules nothing",
          permitted_but_undeclared is None)
    PASSED.append("alpha00001_and_unknown_clients_stay_off")


def test_the_rollout_declaration_remains_the_only_dashboard_switch() -> None:
    source = read("jobs/ecodriving/scheduled_mailing_contract.py")
    check("the contract module declares no dashboard boolean of its own",
          "with_dashboard=rollout.is_enabled(code)" in source.replace(" ", "")
          .replace("\n", ""), "resolution must come from the rollout object")
    check("it holds no client code",
          BRAVO not in source and ALPHA not in source)
    check("the production schedule declaration holds no dashboard field",
          "dashboard" not in json.dumps(
              json.loads(read("ops/eco_mailing_production_schedule.json"))
              ["schedules"]).lower())
    off = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=BRAVO,
        declaration=PRODUCTION_SCHEDULE, rollout=rollout(set(), {BRAVO}),
    )
    check("revoking the rollout turns the scheduled dashboard off by itself",
          off is not None and off.with_dashboard is False
          and off.runner_options == ())
    check("while the send itself remains declared",
          off is not None and off.execution_mode == "normal_send")
    PASSED.append("the_rollout_declaration_remains_the_only_dashboard_switch")


# ==============================================================================
# 3 — ONE CANONICAL INVOCATION PATH
# ==============================================================================

def test_the_scheduled_option_is_the_operators_option() -> None:
    import importlib

    runner = importlib.import_module("ops.runner")
    check("the scheduled option is spelled exactly as the runner spells it",
          smc.RUNNER_DASHBOARD_OPTION == runner.WITH_DASHBOARD_FLAG,
          smc.RUNNER_DASHBOARD_OPTION)
    check("and every schedulable option is one the runner knows",
          smc.ALLOWED_SCHEDULED_RUNNER_OPTIONS <= set(runner.KNOWN_FLAGS))
    check("every schedulable dataset maps to a module the runner lets the "
          "option through for",
          {spec["job_module"] for spec in smc.MAILING_DATASETS.values()}
          <= set(runner.ECO_MAILING_JOB_MODULES))

    invocation = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=BRAVO,
        declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
    )
    assert invocation is not None
    # THE proof that the scheduled path is the manual path: feed the resolved
    # argv through the runner's own option handling.
    scheduled_params = runner._apply_options(
        invocation.job_module,
        {"client_id": "6018be20-5faa-41b6-89c9-fe2b54a8283e",
         "trigger": "SCHEDULED", **invocation.params_overrides()},
        list(invocation.runner_options),
    )
    check("the runner turns the scheduled option into the job's opt-in",
          scheduled_params[emi.PARAM_WITH_DASHBOARD] is True)
    check("and the scheduled run is a normal send",
          scheduled_params["execution_mode"] == "normal_send")

    contract = safety.resolve_execution_contract(scheduled_params)
    check("the job resolves it as the production send mode",
          contract.mode is safety.ExecutionMode.NORMAL_SEND)
    check("with the real recipient scope", contract.send_scope == "normal")
    check("no test redirection is possible on this path",
          contract.test_recipient_email is None)
    check("and it is gated on unresolved ambiguity like every production send",
          contract.blocks_on_unresolved_ambiguous_send is True)
    settings = emi.DashboardLinkSettings.from_params(
        {**scheduled_params, "dashboard_base_url": "https://eco.example.test"},
        render_only=True)
    check("and the dashboard integration is required, not best effort",
          settings.enabled is True)
    PASSED.append("the_scheduled_option_is_the_operators_option")


def test_the_datasets_are_exactly_the_registry_mailing_datasets() -> None:
    for dataset, spec in smc.MAILING_DATASETS.items():
        check("every schedulable mailing dataset is a registered dataset",
              dataset in registry.DATASETS, dataset)
        check("and runs the job the registry says it runs",
              registry.DATASETS[dataset].job_module == spec["job_module"], dataset)
    registered_mailers = {
        name for name, spec in registry.DATASETS.items()
        if "email_notifications" in name
    }
    check("and no registered mailing dataset is left outside the contract",
          registered_mailers == set(smc.MAILING_DATASETS), str(registered_mailers))
    PASSED.append("the_datasets_are_exactly_the_registry_mailing_datasets")


def test_the_dispatcher_builds_exactly_this_invocation() -> None:
    from jobs.api.telematics import dispatcher

    fire = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)
    common = dict(client_id="6018be20-5faa-41b6-89c9-fe2b54a8283e",
                  event_enrichment_mode="enabled",
                  window_start_ts=fire, window_end_ts=fire)
    invocation = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=BRAVO,
        declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
    )
    scheduled = dispatcher._build_job_params(
        client_code=BRAVO, dataset_name=WEEKLY_DATASET,
        scheduled_mailing=invocation, **common)
    check("a scheduled BRAVO00016 weekly fire carries normal_send",
          scheduled["execution_mode"] == "normal_send", str(scheduled))
    check("it is marked as a scheduled trigger",
          scheduled["trigger"] == "SCHEDULED")
    check("it carries no window; the job selects its own period",
          "window_start_ts" not in scheduled and "window_end_ts" not in scheduled)
    check("it carries no explicit period, no limit and no recipient override",
          not {"period_start_date", "period_end_date", "limit",
               "test_recipient_email"} & set(scheduled), str(scheduled))
    check("the dashboard opt-in is NOT in the parameters",
          emi.PARAM_WITH_DASHBOARD not in scheduled
          and "dashboard_link" not in scheduled)

    undeclared = dispatcher._build_job_params(
        client_code=ALPHA, dataset_name=WEEKLY_DATASET,
        scheduled_mailing=None, **common)
    check("an undeclared fire carries no execution mode at all",
          "execution_mode" not in undeclared, str(undeclared))
    check("so the job's own default applies",
          safety.resolve_execution_contract(undeclared).mode
          is safety.ExecutionMode.RENDER_ONLY)

    try:
        dispatcher._build_job_params(
            client_code=BRAVO, dataset_name=MONTHLY_DATASET,
            scheduled_mailing=invocation, **common)
    except ValueError:
        pass
    else:
        FAILURES.append("a contract for another dataset was accepted")

    try:
        dispatcher._launch_job(
            job_module=WEEKLY_JOB, job_params={}, log_fn=lambda *a, **k: None,
            runner_options=("--force-resend",))
    except ValueError as error:
        check("an option outside the allowlist is refused before any process "
              "is started", "may not carry option" in str(error), str(error))
    else:
        FAILURES.append("an undeclared runner option was launched")
    PASSED.append("the_dispatcher_builds_exactly_this_invocation")


def test_manual_and_future_triggers_reuse_the_same_contract() -> None:
    """A future email-command listener must add no business logic."""
    invocation = smc.resolve_scheduled_invocation(
        dataset_name=WEEKLY_DATASET, client_code=BRAVO,
        declaration=PRODUCTION_SCHEDULE, rollout=PRODUCTION_ROLLOUT,
    )
    assert invocation is not None
    # Everything a trigger needs to build the command, from one call.
    check("the contract yields the module, the parameters and the options",
          (invocation.job_module, invocation.params_overrides(),
           invocation.runner_options)
          == (WEEKLY_JOB, {"execution_mode": "normal_send"},
              (smc.RUNNER_DASHBOARD_OPTION,)))
    import ast

    tree = ast.parse(read("jobs/ecodriving/scheduled_mailing_contract.py"))
    imported = {
        node.module or "" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    }
    check("resolution imports no dispatcher, no scheduler and no database",
          not any(name.startswith("jobs.api.telematics") or "psycopg" in name
                  or name.startswith("ops.") for name in imported),
          str(sorted(imported)))
    check("and it is injectable, so a trigger can resolve without touching the "
          "filesystem",
          smc.resolve_scheduled_invocation(
              dataset_name=WEEKLY_DATASET, client_code=BRAVO,
              declaration=declaration([
                  {"client_code": BRAVO, "dataset_name": WEEKLY_DATASET,
                   "execution_mode": "render_only"}]),
              rollout=rollout({BRAVO}),
          ).execution_mode == "render_only")

    # A manual operator command and the scheduled fire resolve the same
    # business semantics; only the period source differs.
    manual = {"client_id": "6018be20-5faa-41b6-89c9-fe2b54a8283e",
              "execution_mode": "normal_send", emi.PARAM_WITH_DASHBOARD: True}
    scheduled = {"client_id": "6018be20-5faa-41b6-89c9-fe2b54a8283e",
                 "trigger": "SCHEDULED", **invocation.params_overrides(),
                 emi.PARAM_WITH_DASHBOARD: True}
    manual_contract = safety.resolve_execution_contract(manual)
    scheduled_contract = safety.resolve_execution_contract(scheduled)
    check("manual and scheduled resolve an identical execution contract",
          manual_contract == scheduled_contract,
          f"{manual_contract} != {scheduled_contract}")
    check("and an identical dashboard requirement",
          emi.DashboardLinkSettings.from_params(
              {**manual, "dashboard_base_url": "https://x.test"}, render_only=True)
          == emi.DashboardLinkSettings.from_params(
              {**scheduled, "dashboard_base_url": "https://x.test"},
              render_only=True))
    PASSED.append("manual_and_future_triggers_reuse_the_same_contract")


# ==============================================================================
# 4 — THE PERIOD CONTRACT
# ==============================================================================

def _weekly_rows(month_start: date, *, finalized: bool = True) -> list[dict]:
    """Persisted weekly snapshot candidates exactly as the job reads them."""
    rows = []
    for index, end in enumerate(safety._weekly_boundaries(month_start), start=1):
        finalized_at = datetime.combine(end, time.min, WARSAW)
        rows.append({
            "period_start_date": month_start,
            "period_end_date": end,
            "period_label": f"{month_start:%Y-%m}-W{index}",
            "snapshot_min_updated_at": (
                finalized_at if finalized else finalized_at - timedelta(days=2)),
            "snapshot_max_updated_at": finalized_at,
        })
    return rows


def test_the_weekly_business_calendar_is_the_existing_one() -> None:
    august = safety._weekly_boundaries(date(2026, 8, 1))
    check("August 2026 closes W1..W5 on Mondays and ends on the 1st of September",
          august == (date(2026, 8, 3), date(2026, 8, 10), date(2026, 8, 17),
                     date(2026, 8, 24), date(2026, 8, 31), date(2026, 9, 1)),
          str(august))
    check("the verified E2E period is W3 of that calendar",
          august[2] == date(2026, 8, 17))
    february = safety._weekly_boundaries(date(2027, 2, 1))
    check("a month starting on a Monday has no zero-length first segment",
          february[0] == date(2027, 2, 8) and february[-1] == date(2027, 3, 1),
          str(february))
    leap = safety._weekly_boundaries(date(2028, 2, 1))
    check("a leap February still ends at the next month start",
          leap[-1] == date(2028, 3, 1), str(leap))
    for month, rows in (("2026-08", _weekly_rows(date(2026, 8, 1))),
                        ("2027-02", _weekly_rows(date(2027, 2, 1)))):
        for row in rows:
            decision = safety.evaluate_period(
                row, report_type="weekly",
                clock=datetime(2030, 1, 1, tzinfo=WARSAW))
            check("every generated period is a valid weekly shape",
                  decision.state == "closed", f"{month} {row['period_label']}")
    PASSED.append("the_weekly_business_calendar_is_the_existing_one")


def test_period_selection_is_deterministic_at_every_boundary() -> None:
    rows = _weekly_rows(date(2026, 8, 1))
    expected = {
        # (business-local instant) -> the period a run at that instant sends
        datetime(2026, 8, 3, 0, 0, tzinfo=WARSAW): date(2026, 8, 3),
        datetime(2026, 8, 3, 0, 0, tzinfo=WARSAW) - timedelta(seconds=1): None,
        datetime(2026, 8, 10, 7, 0, tzinfo=WARSAW): date(2026, 8, 10),
        datetime(2026, 8, 17, 7, 0, tzinfo=WARSAW): date(2026, 8, 17),
        datetime(2026, 8, 24, 7, 0, tzinfo=WARSAW): date(2026, 8, 24),
        datetime(2026, 8, 31, 7, 0, tzinfo=WARSAW): date(2026, 8, 31),
        datetime(2026, 9, 1, 7, 0, tzinfo=WARSAW): date(2026, 9, 1),
        datetime(2026, 9, 7, 7, 0, tzinfo=WARSAW): date(2026, 9, 1),
    }
    for instant, period_end in expected.items():
        try:
            selection = safety.select_latest_closed_period(
                rows, report_type="weekly", clock=instant,
                require_finalized_snapshot=True)
        except safety.EcoEmailPreconditionError as error:
            check("before the first boundary nothing is eligible",
                  period_end is None
                  and error.code == safety.NO_ELIGIBLE_CLOSED_PERIOD,
                  f"{instant}: {error.code}")
            continue
        check("a run at this instant sends exactly one determined period",
              selection.selected.period_end_date == period_end,
              f"{instant} -> {selection.selected.period_end_date}")
        check("and it is the cumulative month-to-date period",
              selection.selected.period_start_date == date(2026, 8, 1))
        check("selection is automatic, not overridden",
              selection.selection_source == "automatic"
              and selection.override_used is False)

    repeated = [
        safety.select_latest_closed_period(
            rows, report_type="weekly",
            clock=datetime(2026, 8, 17, 7, 0, tzinfo=WARSAW),
            require_finalized_snapshot=True).selected.period_end_date
        for _ in range(5)
    ]
    check("the same effective instant always resolves the same period",
          set(repeated) == {date(2026, 8, 17)}, str(repeated))
    PASSED.append("period_selection_is_deterministic_at_every_boundary")


def test_period_boundaries_are_business_timezone_boundaries() -> None:
    rows = _weekly_rows(date(2026, 8, 1))
    midnight_warsaw = datetime(2026, 8, 17, 0, 0, tzinfo=WARSAW)
    check("the boundary instant itself is closed",
          safety.select_latest_closed_period(
              rows, report_type="weekly", clock=midnight_warsaw,
              require_finalized_snapshot=True).selected.period_end_date
          == date(2026, 8, 17))
    check("one second earlier it is not",
          safety.select_latest_closed_period(
              rows, report_type="weekly",
              clock=midnight_warsaw - timedelta(seconds=1),
              require_finalized_snapshot=True).selected.period_end_date
          == date(2026, 8, 10))
    check("a UTC clock is converted, not reinterpreted",
          safety.select_latest_closed_period(
              rows, report_type="weekly",
              clock=midnight_warsaw.astimezone(timezone.utc),
              require_finalized_snapshot=True).selected.period_end_date
          == date(2026, 8, 17))
    check("22:30Z on the 16th is already the 17th in Warsaw (UTC+2)",
          safety.select_latest_closed_period(
              rows, report_type="weekly",
              clock=datetime(2026, 8, 16, 22, 30, tzinfo=timezone.utc),
              require_finalized_snapshot=True).selected.period_end_date
          == date(2026, 8, 17))

    # Across the Warsaw DST change (2026-10-25 03:00 -> 02:00, UTC+2 -> UTC+1).
    november = _weekly_rows(date(2026, 11, 1))
    check("after the DST change the offset used is UTC+1, not a frozen UTC+2",
          safety.select_latest_closed_period(
              november, report_type="weekly",
              clock=datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc),
              require_finalized_snapshot=True).selected.period_end_date
          == date(2026, 11, 2))
    try:
        safety.select_latest_closed_period(
            november, report_type="weekly",
            clock=datetime(2026, 11, 1, 22, 30, tzinfo=timezone.utc),
            require_finalized_snapshot=True)
    except safety.EcoEmailPreconditionError as error:
        check("and an hour earlier the November W1 period is not yet closed",
              error.code == safety.NO_ELIGIBLE_CLOSED_PERIOD, error.code)
    else:
        FAILURES.append("a not-yet-closed period was selected across DST")

    try:
        safety._now_local(datetime(2026, 8, 17, 7, 0))
    except safety.EcoEmailPreconditionError as error:
        check("a naive clock is refused rather than assumed",
              error.code == safety.INVALID_PERIOD_BOUNDARY)
    else:
        FAILURES.append("a naive clock was accepted")
    check("the business timezone is stated once, and is Europe/Warsaw",
          safety.BUSINESS_TIMEZONE_NAME == "Europe/Warsaw")
    PASSED.append("period_boundaries_are_business_timezone_boundaries")


def test_an_unfinalized_snapshot_is_never_sent() -> None:
    rows = _weekly_rows(date(2026, 8, 1), finalized=False)
    decision = safety.evaluate_period(
        rows[2], report_type="weekly",
        clock=datetime(2026, 8, 17, 7, 0, tzinfo=WARSAW),
        require_finalized_snapshot=True)
    check("a snapshot written before the period closed is refused",
          decision.rejection_code == safety.SNAPSHOT_NOT_FINALIZED,
          str(decision.rejection_code))
    check("and the run has no eligible period at all",
          not decision.eligible)
    PASSED.append("an_unfinalized_snapshot_is_never_sent")


def test_scheduled_and_manual_period_selection_agree() -> None:
    rows = _weekly_rows(date(2026, 8, 1))
    instant = datetime(2026, 8, 17, 7, 0, tzinfo=WARSAW)
    contract = safety.resolve_execution_contract({"execution_mode": "normal_send"})
    scheduled = safety.select_period_for_send(
        rows, report_type="weekly", contract=contract, clock=instant)
    manual = safety.select_period_for_send(
        rows, report_type="weekly", contract=contract,
        explicit_period=(date(2026, 8, 1), date(2026, 8, 17)), clock=instant)
    check("the scheduler's automatic period is the operator's explicit one",
          (scheduled.selected.period_start_date, scheduled.selected.period_end_date)
          == (manual.selected.period_start_date, manual.selected.period_end_date),
          f"{scheduled.selected} vs {manual.selected}")
    check("only the source of the choice differs",
          (scheduled.selection_source, manual.selection_source)
          == ("automatic", "explicit"))
    check("the scheduled decision is explicit in the run summary",
          {"selected_period", "selection_source", "evaluated_local_datetime",
           "timezone"} <= set(scheduled.diagnostics()))
    check("and it names the business timezone it was evaluated in",
          scheduled.diagnostics()["timezone"] == "Europe/Warsaw")

    try:
        safety.select_period_for_send(
            rows, report_type="weekly", contract=contract,
            explicit_period=(date(2026, 8, 1), date(2026, 8, 18)), clock=instant)
    except safety.EcoEmailPreconditionError as error:
        check("a period that is not a persisted snapshot is refused",
              error.code == safety.NO_ELIGIBLE_CLOSED_PERIOD, error.code)
    else:
        FAILURES.append("an unpersisted explicit period was accepted")

    try:
        safety.resolve_execution_contract(
            {"execution_mode": "normal_send",
             "allow_unclosed_period_for_test": True})
    except safety.EcoEmailPreconditionError as error:
        check("a production send can never override an unclosed period",
              error.code == safety.UNCLOSED_OVERRIDE_NOT_ALLOWED, error.code)
    else:
        FAILURES.append("a production send accepted the unclosed override")
    PASSED.append("scheduled_and_manual_period_selection_agree")


# ==============================================================================
# 5 — IDEMPOTENCY, FAILURE AND RETRY
# ==============================================================================

def test_the_same_logical_recipient_and_period_is_one_identity() -> None:
    base = dict(report_type="weekly", client_id="6018be20-5faa-41b6-89c9-fe2b54a8283e",
                person_name_group_key="jan kowalski",
                period_start_date=date(2026, 8, 1), period_end_date=date(2026, 8, 17))
    first = idem.build_idempotency_key(**base)
    check("the identity is stable across runs", first == idem.build_idempotency_key(**base))
    check("it does not depend on the template chosen",
          first == idem.build_idempotency_key(**base, template_type="bezpieczny"))
    check("a different period is a different identity",
          first != idem.build_idempotency_key(
              **{**base, "period_end_date": date(2026, 8, 24)}))
    check("a different person is a different identity",
          first != idem.build_idempotency_key(
              **{**base, "person_name_group_key": "anna nowak"}))
    check("the raw person key never appears in it",
          "jan kowalski" not in first, first)
    check("a scheduled production send uses the normal scope",
          idem.normal_send_scope(force_resend=False, test_recipient_email=None)
          == "normal")
    check("and a test send can never occupy it",
          idem.normal_send_scope(force_resend=False,
                                 test_recipient_email="x@example.test") == "test")

    reserve = read("jobs/ecodriving_person/email_idempotency.py")
    check("a rerun finds an existing pending or sent row by that identity",
          "AND status IN ('pending','sent')" in reserve)
    check("and reports it rather than sending again",
          'outcome = "existing"' in reserve)
    check("an unresolved ambiguous row blocks the identity entirely",
          idem.AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION in reserve)
    check("a stale pending reservation is an operator question, not a free retry",
          idem.STALE_PENDING_REQUIRES_RECONCILIATION in reserve)
    PASSED.append("the_same_logical_recipient_and_period_is_one_identity")


def test_a_dashboard_failure_blocks_that_candidates_smtp() -> None:
    job = read(WEEKLY_JOB_PATH)
    publish = job.index("render_with_dashboard_link(")
    block = job.index('summary["dashboard_link_blocked_count"] += 1')
    send = job.index("acceptance = submit_prepared_email(")
    check("the dashboard link is produced before SMTP is ever reached",
          publish < send, f"{publish} !< {send}")
    check("a failed link is counted before SMTP, not after",
          publish < block < send, f"{publish}/{block}/{send}")
    check("that candidate is abandoned instead of sent without its link",
          "if html_body is None:" in job
          and job.index("if html_body is None:") < send)
    check("and it is recorded as a failure before SMTP",
          'summary["failed_before_smtp_count"] += 1' in job)
    check("the whole run stops before any candidate when the client is not "
          "authorized for dashboard mailing",
          job.index("authorize_dashboard_mailing(")
          < job.index("period_selection = select_period_for_send("))
    check("and the shared transaction is not corrupted by one candidate",
          "continue" in job[block:send])
    PASSED.append("a_dashboard_failure_blocks_that_candidates_smtp")


def test_ambiguous_smtp_is_operator_required_and_never_auto_retried() -> None:
    job = read(WEEKLY_JOB_PATH)
    check("an ambiguous submission is classified, not guessed",
          "classify_exception(exc) == AMBIGUOUS_SUBMISSION" in job)
    check("and frozen for an operator rather than retried",
          "mark_send_ambiguous(" in job and '"operator_action_required": ambiguous' in job)
    check("SMTP is submitted exactly once per candidate",
          job.count("acceptance = submit_prepared_email(") == 1)
    check("with no retry loop around it",
          "for attempt in range" not in job and "while True" not in job)
    check("a later run refuses to touch the candidate at all",
          "ambiguous_reconciliation_blocked_count" in job
          and "unresolved_ambiguous_send(" in job)
    check("including a forced one",
          "blocks_on_unresolved_ambiguous_send" in
          read("jobs/ecodriving/email_safety.py"))

    for path in ("jobs/api/telematics/dispatcher.py",
                 "jobs/ecodriving/scheduled_mailing_contract.py"):
        source = read(path)
        check("no scheduler path re-fires a mailing job by itself",
              "submit_prepared_email" not in source and "smtplib" not in source, path)
    check("and the scheduled contract cannot express a forced resend",
          "force_resend" not in {mode for mode in smc.SCHEDULABLE_EXECUTION_MODES})
    PASSED.append("ambiguous_smtp_is_operator_required_and_never_auto_retried")


def test_a_sent_archive_failure_never_causes_a_resend() -> None:
    job = read(WEEKLY_JOB_PATH)
    accepted = job.index('summary["smtp_accepted_count"] += 1')
    archived = job.index("sent_copy = store_sent_copy(")
    check("the archive is attempted only after SMTP acceptance is recorded",
          accepted < archived, f"{accepted} !< {archived}")

    # THE STRUCTURAL GUARANTEE. The copy is not merely wrapped in its own
    # try/except inside the send block — it is OUTSIDE the send block
    # entirely, guarded by the flag that says SMTP acceptance was committed.
    # There is therefore no expression in the copy path that the SMTP
    # classifier could ever evaluate.
    send_failure = job.index("except Exception as exc:", accepted)
    check("and it runs outside the SMTP try/except, not inside it",
          send_failure < job.index("if accepted_message is not None:") < archived,
          f"{send_failure}/{archived}")
    check("guarded by the committed acceptance, so an unsent message is never "
          "copied", "accepted_message = prepared" in job
          and "accepted_message: PreparedEmail | None = None" in job)

    shared = read("jobs/common/eco_sent_archive.py")
    body = shared[shared.index("def store_sent_copy("):]
    raises = [line.strip() for line in body.splitlines()
              if line.strip().startswith("raise ")]
    check("the copy step itself cannot raise into any caller",
          "**NEVER RAISES.**" in body and not raises, str(raises))
    check("the send stays 'sent'; only the copy is marked",
          "Sent-folder copy" in job and "failed after SMTP " in job
          and '"resend_authorized": False' in shared)
    check("and the operator recovery is the archive-only path, not a resend",
          '"archive_only_recovery": True' in job and "archive_only" in job)
    check("a dedicated operator tool exists for it",
          (REPO_ROOT / "ops" / "reconcile_eco_email_ambiguous_send.py").exists()
          and (REPO_ROOT / "ops"
               / "recover_eco_dashboard_operator_required_delivery.py").exists())
    PASSED.append("a_sent_archive_failure_never_causes_a_resend")


def test_a_restart_or_reentry_respects_durable_state() -> None:
    job = read(WEEKLY_JOB_PATH)
    check("the reservation is committed before SMTP is attempted",
          job.index("reserve_send(") < job.index("conn.commit()\n                summary[\"smtp_attempt_count\"]"),
          "reservation must be durable before the non-idempotent effect")
    check("acceptance is recorded and committed immediately",
          job.index("mark_send_sent(") < job.index('summary["sent_count"] += 1'))
    check("a rerun consults the durable send log BEFORE anything remote — the "
          "dashboard publication below mints and may rotate a capability",
          job.index("if not decision.should_send:")
          < job.index("unresolved_ambiguous_send(")
          < job.index("render_with_dashboard_link("))
    check("already-sent candidates are skipped, not resent",
          'status="skipped_already_sent"' in job
          and "summary[decision.status] += 1" in job
          and 'reason="sent_log_exists"' in job)
    check("and an existing reservation is not stolen",
          'summary["skipped_existing_reservation"]' in job)
    dispatcher = read("jobs/api/telematics/dispatcher.py")
    check("a scheduled fire is claimed durably before it launches anything",
          dispatcher.index("_claim_fire(") < dispatcher.index("_launch_job("))
    check("and an interrupted fire becomes a stale RUNNING row an operator "
          "sees, not an automatic resend",
          "_mark_stale_running(" in dispatcher)
    PASSED.append("a_restart_or_reentry_respects_durable_state")


# ==============================================================================
# 6 — THE SCHEDULE ITSELF STAYS OFF
# ==============================================================================

def test_the_first_production_schedule_is_still_disabled() -> None:
    migration = read("db/migrations/046_workflow_a_eco_person_registry.sql")
    check("the seeded Eco Person schedules are created disabled",
          "false AS enabled" in migration)
    check("and are never re-seeded over an existing row",
          "ON CONFLICT (client_id, dataset_name) DO NOTHING" in migration)
    check("their business timezone is Europe/Warsaw",
          "'Europe/Warsaw' AS timezone" in migration)
    check("the weekly mailing seed is a weekly cadence",
          "('eco_person_driving_weekly_email_notifications', 'weekly'" in migration)

    for path in sorted((REPO_ROOT / "db" / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "client_dataset_schedule" not in text:
            continue
        lowered = text.lower()
        check("no migration enables an Eco mailing schedule",
              not ("email_notifications" in lowered
                   and "set enabled = true" in lowered), str(path))

    activator = read("ops/activate_telematics_trips_schedule.py")
    check("the only schedule-activation tool refuses every dataset but trips",
          "if dataset_name != TRIPS_SYNC_DATASET_NAME:" in activator)
    check("so enabling the first mailing schedule is a deliberate, separate act",
          "email_notifications" not in activator)

    declaration_text = read("ops/eco_mailing_production_schedule.json")
    check("and the declaration says so in the file an operator reads",
          "still DISABLED" in declaration_text)
    PASSED.append("the_first_production_schedule_is_still_disabled")


def test_nothing_here_can_send_anything() -> None:
    source = read("jobs/ecodriving/scheduled_mailing_contract.py")
    check("it opens no connection and starts no process",
          "import subprocess" not in source and "import psycopg" not in source
          and "smtplib" not in source and "imaplib" not in source)
    check("and it writes nothing",
          ".write_text(" not in source and "INSERT" not in source
          and "UPDATE" not in source)

    inspector = read("ops/inspect_eco_mailing_schedule_contract.py")
    check("the operator inspection surface is read-only too",
          "psycopg" not in inspector and "smtplib" not in inspector
          and "subprocess" not in inspector and ".write_text(" not in inspector)
    check("and says so in its own output",
          '"inspection_only": True' in inspector
          and '"sends_nothing": True' in inspector)
    PASSED.append("nothing_here_can_send_anything")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"PASSED {len(PASSED)} groups")
    for name in sorted(PASSED):
        print(f"  ok  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
