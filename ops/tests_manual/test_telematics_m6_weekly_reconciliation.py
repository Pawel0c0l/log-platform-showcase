#!/usr/bin/env python3
"""M6 — the WEEKLY_RECONCILIATION schedule lifecycle.

Specification of record:
  docs/20_telematics_ingestion_permanent_repair_plan.md §3.1 (the cadence table),
    §3.7c (the guaranteed-capture-horizon arithmetic and why `L = 16`),
    §7 (cadence interaction), §24 (the M6 delivery record)
  jobs/api/telematics/schedule_mutation_surfaces.py (the deny-by-default oracle)
  ops/manage_telematics_reconciliation_schedule.py (the one registered surface)

Scope, stated so this file is not mistaken for a coverage suite: M5 already
proves — on PostgreSQL, in
`test_telematics_m5_multi_cadence_identity_postgres.py` — that three roles
coexist, that two cadences resolve one watermark, that a reconciliation fire is
not refused for a schedule mismatch, and that a behind-`W` reconciliation run is
a validated no-op. M6 reuses those contracts unchanged and does not restate
them. What is new here, and therefore what this file owns, is:

  * the approved M6 cadence is representable and is what the tool defaults to;
  * `L = 16` derives the intended window and is NOT clamped by the current `R`;
  * inherited columns are copied from the base row rather than defaulted — the
    `event_enrichment_mode` regression that would silently turn ALPHA's
    `/vehicles/events` back on;
  * the column partition is exhaustive, so a future column cannot be forgotten;
  * the creation/activation guards fail closed;
  * Monday 00:30 Europe/Warsaw fires correctly across both DST transitions;
  * DAILY window derivation is bit-for-bit what it was.

Run::

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_telematics_m6_weekly_reconciliation.py

PostgreSQL checks run only when `TELEMATICS_M6_WEEKLY_TEST_DSN` names a
disposable loopback database; everything else is pure and always runs.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED,
    COVERAGE_STATUS_READY,
    CoverageState,
    derive_effective_window,
    evaluate_coverage_gate,
)
from jobs.api.telematics.dispatcher import (  # noqa: E402
    ScheduleRow,
    evaluate_schedule,
    latest_scheduled_fire_local,
)
from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    RECONCILIATION_INSERT_FIELDS,
    SCHEDULE_DECLARED_FIELDS,
    SCHEDULE_GENERATED_FIELDS,
    SCHEDULE_IDENTITY_FIELDS,
    SCHEDULE_INHERITED_FIELDS,
    SCHEDULE_RUN_TYPE_BASE,
    SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
    SURFACE_ONBOARDING,
    SURFACE_RECONCILIATION_SCHEDULE,
    ScheduleMutationRefused,
    assert_reconciliation_activation_permitted,
    assert_reconciliation_creation_permitted,
    assert_reconciliation_deactivation_permitted,
    derive_reconciliation_schedule,
)

import ops.manage_telematics_reconciliation_schedule as m6  # noqa: E402

ENV = "TELEMATICS_M6_WEEKLY_TEST_DSN"

# ALPHA00001's live tuning, which is what the M6 arithmetic is argued against.
D = 10_800      # stabilization delay, seconds
O = 3_600       # overlap, seconds
R = 2_678_400   # max recovery span, seconds (31 days) — unchanged by M6
L_WEEKLY = 16
L_BASE_ALPHA = 3

WARSAW = ZoneInfo("Europe/Warsaw")

#: An ALPHA-shaped base row: event enrichment OFF, which is the value M6 must
#: inherit rather than default.
BASE_ROW = {
    "schedule_id": "7cac378a-5787-4d62-85d1-282bed208c8c",
    "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
    "client_code": "TST00001",
    "dataset_name": "trips_sync",
    "enabled": True,
    "frequency": "daily",
    "day_of_week": None,
    "day_of_month": None,
    "day_of_month_last": False,
    "run_time": time(2, 0),
    "timezone": "UTC",
    "lookback_days": L_BASE_ALPHA,
    "overwrite_existing": True,
    "event_enrichment_mode": "disabled",
    "run_type": SCHEDULE_RUN_TYPE_BASE,
}

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _refused(label: str, fn, expected_code: str) -> None:
    try:
        fn()
    except ScheduleMutationRefused as exc:
        _check(label, exc.code == expected_code, f"got {exc.code}")
    else:
        _check(label, False, "it was permitted")


def _m6_row(**overrides):
    kwargs = dict(
        base_row=BASE_ROW,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        frequency="weekly",
        day_of_week=0,
        run_time=time(0, 30),
        timezone_name="Europe/Warsaw",
        lookback_days=L_WEEKLY,
    )
    kwargs.update(overrides)
    return derive_reconciliation_schedule(**kwargs)


# ===========================================================================
# A — the approved configuration is representable, and is the tool's default
# ===========================================================================

def test_the_approved_m6_shape_is_what_the_tool_defaults_to() -> None:
    _check("default run_type", m6.M6_RUN_TYPE == SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION)
    _check("default frequency", m6.M6_FREQUENCY == "weekly")
    _check("default day_of_week is Monday", m6.M6_DAY_OF_WEEK == 0)
    _check("default run_time", m6.M6_RUN_TIME == "00:30")
    _check("default timezone", m6.M6_TIMEZONE == "Europe/Warsaw")
    _check("default lookback", m6.M6_LOOKBACK_DAYS == L_WEEKLY)

    parser = m6.build_parser()
    args = parser.parse_args([
        "register", "--client-code", "TST00001",
        "--expected-environment", "test",
        "--expected-platform-uuid", "bd7662a5-eeb4-4614-8720-d477abfcb227",
    ])
    _check(
        "cadence arguments default to None, not to the weekly shape",
        all(
            getattr(args, name) is None
            for name in ("frequency", "day_of_week", "run_time", "timezone",
                         "lookback_days")
        ),
        "a weekly default leaking into another role would refuse M7 for the "
        "wrong reason",
    )
    m6.apply_role_defaults(args)
    row = derive_reconciliation_schedule(
        base_row=BASE_ROW,
        run_type=args.run_type,
        frequency=args.frequency,
        run_time=m6.parse_run_time(args.run_time),
        timezone_name=m6.verify_timezone(args.timezone),
        lookback_days=args.lookback_days,
        day_of_week=args.day_of_week,
        day_of_month=args.day_of_month,
        day_of_month_last=args.day_of_month_last,
    )
    _check(
        "the CLI defaults derive exactly the approved M6 cadence",
        (
            row["run_type"] == SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION
            and row["frequency"] == "weekly"
            and row["day_of_week"] == 0
            and row["day_of_month"] is None
            and row["day_of_month_last"] is False
            and row["run_time"] == time(0, 30)
            and row["timezone"] == "Europe/Warsaw"
            and row["lookback_days"] == 16
        ),
        f"derived {row}",
    )
    _check(
        "the tool is dry-run unless --execute is given",
        args.execute is False and args.confirm_client_code is None,
    )

    # A non-weekly role has no approved default cadence and must state one.
    monthly = parser.parse_args([
        "register", "--client-code", "TST00001",
        "--run-type", SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
        "--expected-environment", "test",
        "--expected-platform-uuid", "bd7662a5-eeb4-4614-8720-d477abfcb227",
    ])
    try:
        m6.apply_role_defaults(monthly)
    except m6.ReconciliationRefused as exc:
        _check(
            "a role with no approved cadence must state one explicitly",
            exc.code == "CADENCE_NOT_STATED",
            f"got {exc.code}",
        )
    else:
        _check(
            "a role with no approved cadence must state one explicitly",
            False, "M7's cadence was silently defaulted to M6's",
        )


def test_the_restated_coverage_status_has_not_drifted() -> None:
    """`schedule_mutation_surfaces` restates READY; it must not import it.

    `coverage_windows` has exactly one authorized production importer — the
    dispatcher — and `test_telematics_trips_stabilization_windows.py` asserts
    that count. So the activation guard restates the constant, exactly as
    `coverage_finalization.py` does, and the drift check lives here instead.
    """
    import jobs.api.telematics.schedule_mutation_surfaces as sms
    from jobs.api.telematics import coverage_finalization

    _check(
        "the restated READY matches the pure helper's definition",
        sms.COVERAGE_STATUS_READY == COVERAGE_STATUS_READY
        == coverage_finalization.COVERAGE_STATUS_READY,
        f"{sms.COVERAGE_STATUS_READY!r} vs {COVERAGE_STATUS_READY!r}",
    )
    source = (ROOT / "jobs" / "api" / "telematics"
              / "schedule_mutation_surfaces.py").read_text(encoding="utf-8")
    _check(
        "the policy oracle does not import the coverage helper",
        "from jobs.api.telematics.coverage_windows import" not in source
        and "import coverage_windows" not in source,
        "importing it would make this a second production importer and break "
        "the single-importer invariant",
    )


def test_the_column_partition_is_exhaustive_and_disjoint() -> None:
    """A new column must be classified, not silently defaulted.

    This is the general form of the `event_enrichment_mode` defect: any column
    added to `client_dataset_schedule` and not placed in one of the four sets
    would be filled by its table default on a derived row.
    """
    groups = {
        "identity": set(SCHEDULE_IDENTITY_FIELDS),
        "inherited": set(SCHEDULE_INHERITED_FIELDS),
        "declared": set(SCHEDULE_DECLARED_FIELDS),
        "generated": set(SCHEDULE_GENERATED_FIELDS),
    }
    all_named = [n for group in groups.values() for n in group]
    _check(
        "the four partitions are disjoint",
        len(all_named) == len(set(all_named)),
        f"duplicated: {sorted({n for n in all_named if all_named.count(n) > 1})}",
    )
    # The physical column set of migration 012 + 017 + 018 + 062. Restated here
    # so the pure test has teeth without a database; the PostgreSQL section
    # below re-derives it from `information_schema` and must agree.
    expected = {
        "schedule_id", "client_id", "dataset_name", "enabled", "frequency",
        "day_of_week", "day_of_month", "run_time", "timezone", "lookback_days",
        "overwrite_existing", "created_at", "updated_at", "day_of_month_last",
        "client_code", "event_enrichment_mode", "run_type",
    }
    _check(
        "every column of client_dataset_schedule is classified exactly once",
        set(all_named) == expected,
        f"unclassified: {sorted(expected - set(all_named))}; "
        f"unknown: {sorted(set(all_named) - expected)}",
    )
    _check(
        "the INSERT projection is identity + inherited + declared",
        set(RECONCILIATION_INSERT_FIELDS)
        == groups["identity"] | groups["inherited"] | groups["declared"],
    )
    _check(
        "`enabled` is generated, never declared — creation is always disabled",
        "enabled" in groups["generated"] and "enabled" not in groups["declared"],
    )


# ===========================================================================
# C — L = 16 window semantics, and no R clamp
# ===========================================================================

def test_l16_derives_the_existing_rolling_window() -> None:
    fire = datetime(2026, 8, 17, 0, 30, tzinfo=WARSAW).astimezone(timezone.utc)
    # A watermark well ahead of the L=16 base start, so `min(base, W − O)`
    # resolves to `base` and the pure formula is what is being asserted.
    w = fire - timedelta(days=1)
    window = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=L_WEEKLY,
        stabilization_delay_seconds=D,
        overlap_seconds=O,
        max_recovery_span_seconds=R,
        coverage_start_ts=fire - timedelta(days=60),
        covered_through_ts=w,
    )
    expected_start = fire - timedelta(seconds=L_WEEKLY * 86_400 + D + O)
    _check(
        "E_start == F − 16·86400 − D − O",
        window.effective_window_start_ts == expected_start,
        f"{window.effective_window_start_ts} != {expected_start}",
    )
    _check(
        "E_end == F − D",
        window.effective_window_end_ts == fire - timedelta(seconds=D),
    )
    _check(
        "nominal window is exactly 16 days",
        window.nominal_window_end_ts - window.nominal_window_start_ts
        == timedelta(days=16),
    )
    _check(
        "the window is connected to the watermark",
        window.is_connected,
    )


def test_r_does_not_clamp_l16_at_the_current_value() -> None:
    """The clamp binds iff `R < L·86400 + O`. At L=16 that is 1,386,000 s."""
    required = L_WEEKLY * 86_400 + O
    _check(
        "R = 2,678,400 exceeds the L=16 requirement of 1,386,000",
        R > required,
        f"required {required}, configured {R}",
    )

    fire = datetime(2026, 8, 17, 0, 30, tzinfo=WARSAW).astimezone(timezone.utc)
    window = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=L_WEEKLY,
        stabilization_delay_seconds=D,
        overlap_seconds=O,
        max_recovery_span_seconds=R,
        coverage_start_ts=fire - timedelta(days=60),
        covered_through_ts=fire - timedelta(days=1),
    )
    clamp_floor = window.effective_window_end_ts - timedelta(seconds=R)
    _check(
        "E_start is strictly above the R floor, so the clamp is inactive",
        window.effective_window_start_ts > clamp_floor,
        f"E_start {window.effective_window_start_ts} vs floor {clamp_floor}",
    )
    # And the honest negative: a lookback the current R *would* clamp, proving
    # the assertion above is not vacuous.
    clamped = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=40,
        stabilization_delay_seconds=D,
        overlap_seconds=O,
        max_recovery_span_seconds=R,
        coverage_start_ts=fire - timedelta(days=90),
        covered_through_ts=fire - timedelta(days=1),
    )
    _check(
        "the R clamp is real — L=40 IS truncated at the current R",
        clamped.effective_window_start_ts
        == clamped.effective_window_end_ts - timedelta(seconds=R),
        "if this passes trivially the clamp assertion above proves nothing",
    )


def test_the_weekly_guaranteed_horizon_clears_the_proven_maximum() -> None:
    """`H = L + (D+O)/86400 − P`. docs/20 §3.7c carried 0.208; it is 0.1667."""
    constant = (D + O) / 86_400
    _check(
        "the horizon constant is (D+O)/86400 = 0.1667, not 0.208",
        abs(constant - 0.16667) < 1e-4,
        f"got {constant}",
    )
    horizon_weekly = L_WEEKLY + constant - 7
    horizon_base = L_BASE_ALPHA + constant - 1
    proven_max_days = 8.608  # docs/19 §5.1, 206.60 h
    _check(
        "weekly L=16 guarantees ~9.167 d, clearing the 8.608 d proven maximum",
        horizon_weekly > proven_max_days,
        f"horizon {horizon_weekly:.3f} d",
    )
    _check(
        "weekly L=15 would NOT clear it — the decision is not arbitrary",
        (15 + constant - 7) < proven_max_days,
    )
    _check(
        "weekly L=8 adds nothing over the deployed daily L=3",
        (8 + constant - 7) < horizon_base,
    )


# ===========================================================================
# D/E — the shared coverage gate accepts a reconciliation fire unchanged
# ===========================================================================

def test_the_coverage_gate_admits_a_weekly_fire_on_the_shared_row() -> None:
    """The row's `schedule_id` is the BASE schedule; the weekly fires anyway.

    M5 made this true; M6 is its first real consumer, so it is asserted from
    the weekly schedule's point of view here rather than assumed.
    """
    fire = datetime(2026, 8, 17, 0, 30, tzinfo=WARSAW).astimezone(timezone.utc)
    now = fire + timedelta(minutes=1)
    state = CoverageState(
        schedule_id=BASE_ROW["schedule_id"],   # the BASE schedule, not the weekly
        client_id=BASE_ROW["client_id"],
        client_code=BASE_ROW["client_code"],
        dataset_name="trips_sync",
        coverage_start_ts=fire - timedelta(days=60),
        covered_through_ts=fire - timedelta(days=1),
        bootstrap_status=COVERAGE_STATUS_READY,
        bootstrap_evidence_ref="telematics-coverage-bootstrap/1:test",
        seeded_at=fire - timedelta(days=60),
        seeded_by="test",
        covered_through_source="scheduled_run",
        last_gap_detected_ts=None,
    )
    result = evaluate_coverage_gate(
        schedule_id="9c9c9261-2cf7-4b6f-8955-b515814be2f7",  # the WEEKLY schedule
        client_id=BASE_ROW["client_id"],
        client_code=BASE_ROW["client_code"],
        dataset_name="trips_sync",
        scheduled_fire_ts=fire,
        lookback_days=L_WEEKLY,
        stabilization_delay_seconds=D,
        overlap_seconds=O,
        max_recovery_span_seconds=R,
        coverage_state=state,
        now_utc=now,
    )
    _check(
        "a weekly fire is allowed against the base-anchored shared row",
        result.allowed and result.classification == COVERAGE_GATE_ALLOWED,
        f"{result.classification}: {result.reason}",
    )
    _check(
        "no per-role watermark is introduced — the gate echoes the shared bounds",
        result.covered_through_ts == state.covered_through_ts,
    )
    _check(
        "the deep historical start cannot make the window disconnected",
        result.effective_window.is_connected,
    )


def test_a_weekly_window_always_reaches_back_at_least_to_the_watermark() -> None:
    """Why a deeper scan can never corrupt forward coverage.

    `E_start = min(base, W − O)` means the candidate `E_end` is only ever
    reached over ground this very run also requested. Asserted across a month
    of watermark positions rather than one.
    """
    fire = datetime(2026, 8, 17, 0, 30, tzinfo=WARSAW).astimezone(timezone.utc)
    bad = []
    for days_behind in range(0, 30):
        w = fire - timedelta(days=days_behind, seconds=D)
        window = derive_effective_window(
            scheduled_fire_ts=fire,
            lookback_days=L_WEEKLY,
            stabilization_delay_seconds=D,
            overlap_seconds=O,
            max_recovery_span_seconds=R,
            coverage_start_ts=fire - timedelta(days=120),
            covered_through_ts=w,
        )
        if window.effective_window_start_ts > w:
            bad.append(days_behind)
    _check(
        "E_start never starts after W, for every watermark position tested",
        not bad,
        f"days_behind values where it did: {bad}",
    )


# ===========================================================================
# G — inherited configuration, and the event-enrichment regression
# ===========================================================================

def test_event_enrichment_mode_is_inherited_not_defaulted() -> None:
    row = _m6_row()
    _check(
        "event_enrichment_mode is copied from the base ('disabled')",
        row["event_enrichment_mode"] == "disabled",
        f"got {row['event_enrichment_mode']!r} — the column default is "
        "'enabled', which would turn ALPHA's /vehicles/events back on",
    )
    _check(
        "overwrite_existing is copied from the base",
        row["overwrite_existing"] is True,
    )
    _check(
        "owner identity is copied from the base, never supplied by the caller",
        (
            row["client_id"] == BASE_ROW["client_id"]
            and row["client_code"] == BASE_ROW["client_code"]
            and row["dataset_name"] == BASE_ROW["dataset_name"]
        ),
    )
    enabled_base = dict(BASE_ROW, event_enrichment_mode="enabled")
    _check(
        "and it tracks the base rather than being pinned to 'disabled'",
        _m6_row(base_row=enabled_base)["event_enrichment_mode"] == "enabled",
    )


def test_a_base_row_missing_an_inherited_field_is_refused() -> None:
    """The failure mode that matters: a caller passing a partial row."""
    for field in SCHEDULE_INHERITED_FIELDS:
        partial = {k: v for k, v in BASE_ROW.items() if k != field}
        _refused(
            f"a base row without {field} is refused rather than defaulted",
            lambda p=partial: _m6_row(base_row=p),
            "RECONCILIATION_BASE_FIELD_MISSING",
        )
        nulled = dict(BASE_ROW, **{field: None})
        _refused(
            f"a NULL {field} is refused rather than defaulted",
            lambda n=nulled: _m6_row(base_row=n),
            "RECONCILIATION_BASE_FIELD_MISSING",
        )


def test_the_declared_cadence_may_differ_from_the_base() -> None:
    """`timezone` is declared, not inherited — ALPHA's base is UTC."""
    row = _m6_row()
    _check(
        "the weekly cadence carries Europe/Warsaw while the base is UTC",
        row["timezone"] == "Europe/Warsaw" and BASE_ROW["timezone"] == "UTC",
    )
    _check(
        "the weekly lookback is 16 while the base keeps 3",
        row["lookback_days"] == 16 and BASE_ROW["lookback_days"] == 3,
    )


def test_the_derivation_does_not_mutate_the_base_row() -> None:
    snapshot = dict(BASE_ROW)
    _m6_row()
    _check(
        "deriving a reconciliation cadence leaves the base row untouched",
        BASE_ROW == snapshot,
    )


# ===========================================================================
# H — the mutation-surface guards fail closed
# ===========================================================================

def test_creation_is_deny_by_default() -> None:
    _refused(
        "an unregistered surface cannot create a reconciliation schedule",
        lambda: assert_reconciliation_creation_permitted(
            surface="ops/some_new_tool.py", dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, enabled=False,
        ),
        "SCHEDULE_MUTATION_SURFACE_UNREGISTERED",
    )
    _refused(
        "onboarding — registered, but not for the reconciliation lifecycle",
        lambda: assert_reconciliation_creation_permitted(
            surface=SURFACE_ONBOARDING, dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, enabled=False,
        ),
        "SCHEDULE_MUTATION_SURFACE_NOT_PERMITTED",
    )
    _refused(
        "the reconciliation surface cannot create a BASE schedule",
        lambda: assert_reconciliation_creation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE, dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_BASE, enabled=False,
        ),
        "RECONCILIATION_ROLE_REQUIRED",
    )
    _refused(
        "a reconciliation schedule can never be created enabled",
        lambda: assert_reconciliation_creation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE, dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, enabled=True,
        ),
        "SCHEDULE_CREATION_REFUSED_ENABLED",
    )
    _refused(
        "a non-coverage-bearing dataset has nothing to reconcile",
        lambda: assert_reconciliation_creation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE,
            dataset_name="fuel_daily_aggregation",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, enabled=False,
        ),
        "RECONCILIATION_DATASET_NOT_ELIGIBLE",
    )
    # The permitted case, so the guard is not vacuously strict.
    assert_reconciliation_creation_permitted(
        surface=SURFACE_RECONCILIATION_SCHEDULE, dataset_name="trips_sync",
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION, enabled=False,
    )
    _check("the approved creation is permitted", True)


def test_activation_preconditions_fail_closed() -> None:
    ok = dict(
        surface=SURFACE_RECONCILIATION_SCHEDULE,
        dataset_name="trips_sync",
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        pagination_mode="data_invariants_v1",
        base_schedule_enabled=True,
        coverage_bootstrap_status=COVERAGE_STATUS_READY,
    )
    _refused(
        "an unregistered surface cannot enable a reconciliation schedule",
        lambda: assert_reconciliation_activation_permitted(
            **dict(ok, surface="ops/some_new_tool.py")
        ),
        "SCHEDULE_MUTATION_SURFACE_UNREGISTERED",
    )
    _refused(
        "a strict_meta client is refused, exactly as for a base schedule",
        lambda: assert_reconciliation_activation_permitted(
            **dict(ok, pagination_mode="strict_meta")
        ),
        "SCHEDULE_ACTIVATION_REFUSED_STRICT_META",
    )
    _refused(
        "a reconciliation cadence cannot be enabled while the base is disabled",
        lambda: assert_reconciliation_activation_permitted(
            **dict(ok, base_schedule_enabled=False)
        ),
        "RECONCILIATION_ACTIVATION_REFUSED_BASE_DISABLED",
    )
    for status in ("GAP_DETECTED", "UNINITIALIZED", "RESEED_REQUIRED", None):
        _refused(
            f"coverage {status!r} is not a watermark to reconcile against",
            lambda s=status: assert_reconciliation_activation_permitted(
                **dict(ok, coverage_bootstrap_status=s)
            ),
            "RECONCILIATION_ACTIVATION_REFUSED_COVERAGE_NOT_READY",
        )
    _refused(
        "base activation is not this surface's business",
        lambda: assert_reconciliation_activation_permitted(
            **dict(ok, run_type=SCHEDULE_RUN_TYPE_BASE)
        ),
        "RECONCILIATION_ROLE_REQUIRED",
    )
    assert_reconciliation_activation_permitted(**ok)
    _check("the approved activation is permitted", True)


def test_a_cadence_the_dispatcher_could_never_fire_is_refused() -> None:
    """`latest_scheduled_fire_local` returns None silently for these."""
    _refused(
        "weekly without day_of_week — the dispatcher would compute no fire",
        lambda: _m6_row(day_of_week=None),
        "RECONCILIATION_PARAMETER_INVALID",
    )
    _refused(
        "weekly with an out-of-range day_of_week",
        lambda: _m6_row(day_of_week=7),
        "RECONCILIATION_PARAMETER_INVALID",
    )
    _refused(
        "weekly carrying monthly fields",
        lambda: _m6_row(day_of_month=1),
        "RECONCILIATION_CADENCE_FIELDS_INVALID",
    )
    _refused(
        "a role/frequency contradiction",
        lambda: _m6_row(frequency="daily"),
        "SCHEDULE_RUN_TYPE_CADENCE_INCOHERENT",
    )
    _refused(
        "monthly without day_of_month or day_of_month_last (M7's trap)",
        lambda: _m6_row(
            run_type=SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
            frequency="monthly", day_of_week=None,
        ),
        "RECONCILIATION_PARAMETER_INVALID",
    )
    _refused(
        "an aware run_time — the column is TIME WITHOUT TIME ZONE",
        lambda: _m6_row(run_time=time(0, 30, tzinfo=timezone.utc)),
        "RECONCILIATION_PARAMETER_INVALID",
    )
    _refused(
        "a base row that is not the base role",
        lambda: _m6_row(
            base_row=dict(BASE_ROW, run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION)
        ),
        "RECONCILIATION_BASE_NOT_BASE_ROLE",
    )
    # `verify_timezone` is the tool's own gate, so it raises the tool's refusal
    # type rather than the policy oracle's.
    try:
        m6.verify_timezone("Europe/Nowhere")
    except m6.ReconciliationRefused as exc:
        _check(
            "an unknown IANA zone would silently produce no fire at all",
            exc.code == "INVALID_PARAMETER",
            f"got {exc.code}",
        )
    else:
        _check(
            "an unknown IANA zone would silently produce no fire at all",
            False, "it was permitted",
        )
    _check(
        "a real zone is accepted, so the check is not vacuous",
        m6.verify_timezone("Europe/Warsaw") == "Europe/Warsaw",
    )


# ===========================================================================
# I — Monday 00:30 Europe/Warsaw across both DST transitions
# ===========================================================================

def _weekly_row() -> ScheduleRow:
    return ScheduleRow(
        schedule_id="9c9c9261-2cf7-4b6f-8955-b515814be2f7",
        client_id=BASE_ROW["client_id"], client_code="TST00001",
        client_name="Test", dataset_name="trips_sync",
        job_module="jobs.api.telematics.sync_trips_and_speeding", enabled=True,
        frequency="weekly", day_of_week=0, day_of_month=None,
        day_of_month_last=False, run_time=time(0, 30),
        timezone_name="Europe/Warsaw", lookback_days=L_WEEKLY,
        overwrite_existing=True, event_enrichment_mode="disabled",
        trips_pagination_mode="data_invariants_v1",
        trips_stabilization_delay_seconds=D, trips_overlap_seconds=O,
        trips_max_recovery_span_seconds=R,
        run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
    )


def test_monday_0030_warsaw_fires_correctly_across_both_dst_transitions() -> None:
    """Both 2026 transitions fall on a SUNDAY, so Monday 00:30 always exists.

    That is the reason 00:30 is safe, and it is asserted rather than assumed:
    a cadence whose local time does not exist on its own day would be resolved
    by `zoneinfo` into some other instant, silently.
    """
    sched = _weekly_row()
    # Spring forward 2026-03-29 02:00->03:00; autumn fold 2026-10-25 03:00->02:00.
    for label, transition in (
        ("spring", datetime(2026, 3, 29, tzinfo=WARSAW)),
        ("autumn", datetime(2026, 10, 25, tzinfo=WARSAW)),
    ):
        _check(
            f"the {label} transition falls on a Sunday, not a Monday",
            transition.weekday() == 6,
        )

    fires = []
    # Sweep every hour of a month around each transition and collect the
    # distinct fires the dispatcher would compute.
    for start in (datetime(2026, 3, 15, tzinfo=timezone.utc),
                  datetime(2026, 10, 11, tzinfo=timezone.utc)):
        seen = []
        for hour in range(24 * 28):
            now = start + timedelta(hours=hour)
            local = now.astimezone(WARSAW)
            fire = latest_scheduled_fire_local(now_local=local, sched=sched)
            if fire is not None and fire not in seen:
                seen.append(fire)
        fires.append(seen)

    for label, seen in zip(("spring", "autumn"), fires):
        _check(
            f"{label}: every fire is a Monday at exactly 00:30 local",
            all(f.weekday() == 0 and f.hour == 0 and f.minute == 30 for f in seen),
            f"got {[str(f) for f in seen]}",
        )
        _check(
            f"{label}: fires are exactly 7 calendar days apart, no skip, no dup",
            all(
                (b.date() - a.date()).days == 7
                for a, b in zip(seen, seen[1:])
            ),
            f"got {[str(f.date()) for f in seen]}",
        )
        # A 28-day sweep starting mid-week observes the Monday preceding the
        # start plus the four inside it. The load-bearing assertion is the
        # 7-day spacing above; this one guards against a transition silently
        # dropping or duplicating a week.
        _check(
            f"{label}: exactly one fire per week over the 28-day sweep",
            len(seen) == 5,
            f"got {len(seen)}: {[str(f.date()) for f in seen]}",
        )

    # And the UTC instant genuinely shifts with the offset — the local wall
    # clock is what is held fixed, which is the point of a local cadence.
    before = evaluate_schedule(
        now_utc=datetime(2026, 3, 23, 12, tzinfo=timezone.utc), sched=sched
    )
    after = evaluate_schedule(
        now_utc=datetime(2026, 3, 30, 12, tzinfo=timezone.utc), sched=sched
    )
    _check(
        "the CET fire is 23:30Z and the CEST fire is 22:30Z the day before",
        before[0].hour == 23 and after[0].hour == 22,
        f"before={before[0]} after={after[0]}",
    )
    _check(
        "the weekly window handed to the job is exactly 16 days",
        after[2] - after[1] == timedelta(days=16),
    )


# ===========================================================================
# B — DAILY behaviour is bit-for-bit unchanged
# ===========================================================================

def test_daily_window_derivation_is_unchanged() -> None:
    """The live DAILY shapes, asserted against the formula M2/M5 shipped."""
    for label, lookback in (
        ("ALPHA00001 L=3", 3), ("DELTA00001 L=7", 7), ("FOXTROT00001 L=1", 1),
    ):
        fire = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)
        window = derive_effective_window(
            scheduled_fire_ts=fire,
            lookback_days=lookback,
            stabilization_delay_seconds=D,
            overlap_seconds=O,
            max_recovery_span_seconds=R,
            coverage_start_ts=fire - timedelta(days=60),
            covered_through_ts=fire - timedelta(days=1, seconds=D),
        )
        _check(
            f"{label}: nominal window is exactly {lookback} d",
            window.nominal_window_end_ts - window.nominal_window_start_ts
            == timedelta(days=lookback),
        )
        _check(
            f"{label}: E_end is still F − D",
            window.effective_window_end_ts == fire - timedelta(seconds=D),
        )
    _check(
        "the dispatcher's ScheduleRow still defaults to the base role",
        ScheduleRow.__dataclass_fields__["run_type"].default
        == SCHEDULE_RUN_TYPE_BASE,
    )


def test_the_dispatcher_still_does_not_branch_on_run_type() -> None:
    """M6 adds a role to production; it must not add a branch to the loop."""
    import ast
    import inspect

    from jobs.api.telematics import dispatcher

    source = inspect.getsource(dispatcher)
    tree = ast.parse(source)
    branches = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue
        segment = ast.get_source_segment(source, node.test) or ""
        if "run_type" in segment:
            branches.append(segment.strip()[:120])
    _check(
        "no dispatcher control flow tests run_type",
        not branches,
        f"branches found: {branches}",
    )
    _check(
        "the dispatcher still enumerates every enabled row",
        "WHERE cds.enabled = true" in source and "cds.run_type" in source,
        "the loader must carry run_type as evidence without filtering on it",
    )


# ===========================================================================
# F — trip idempotency and immutable first-seen provenance (static)
# ===========================================================================

def test_overlap_cannot_duplicate_a_trip_or_restate_first_seen() -> None:
    """Weekly overlap is safe only while these two properties hold."""
    import ast

    path = ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py"
    source = path.read_text(encoding="utf-8")
    # SQL only. A comment or a docstring naming a column is prose, not a
    # statement, and matching on the raw file text would read the very comment
    # that explains why the column is omitted as evidence that it is present.
    sql_literals = [
        node.value
        for node in ast.walk(ast.parse(source, path.name))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "ON CONFLICT" in node.value
    ]
    _check(
        "the trips upsert still conflicts on (client_id, provider_trip_id)",
        any(
            "ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET" in text
            for text in sql_literals
        )
        and any(
            "ON CONFLICT (client_id, provider_trip_id) DO NOTHING" in text
            for text in sql_literals
        ),
    )
    update_clauses = [
        text.split("DO UPDATE SET", 1)[1]
        for text in sql_literals
        if "DO UPDATE SET" in text
    ]
    _check(
        "there is a DO UPDATE SET clause to inspect at all",
        bool(update_clauses),
        "the scan found no upsert clause; it would pass vacuously",
    )
    offenders = [
        clause.strip()[:160]
        for clause in update_clauses
        if "first_seen_request_id" in clause
    ]
    _check(
        "first_seen_request_id is absent from every DO UPDATE SET",
        not offenders,
        f"a re-observation would restate first-seen provenance: {offenders}",
    )
    _check(
        "Dysponent_ID is likewise absent from every SQL statement in the job",
        not any("Dysponent_ID" in clause for clause in update_clauses),
        "trip re-upsert must remain unable to destroy dispatcher enrichment",
    )


# ===========================================================================
# M6 disable — the deny-by-default policy layer (pure)
# ===========================================================================

def test_deactivation_is_its_own_registered_authority() -> None:
    """A third mutation class, not a reuse of the activation one.

    The concrete capability being withheld: `activate_telematics_trips_schedule.py`
    may enable a BASE schedule, so if disable had reused `ACTIVATION_SURFACES`
    that tool would silently have gained the authority to switch a base
    `trips_sync` row off — the one mutation the coverage model cannot detect.
    """
    from jobs.api.telematics import schedule_mutation_surfaces as sms

    _check(
        "the reconciliation tool is the only deactivation surface",
        sms.DEACTIVATION_SURFACES == frozenset({SURFACE_RECONCILIATION_SCHEDULE}),
        f"got {sorted(sms.DEACTIVATION_SURFACES)}",
    )
    _check(
        "the base activation tool may NOT disable anything",
        sms.SURFACE_ACTIVATE_TRIPS_SCHEDULE not in sms.DEACTIVATION_SURFACES,
    )
    _check(
        "onboarding may NOT disable anything",
        SURFACE_ONBOARDING not in sms.DEACTIVATION_SURFACES,
    )
    _check(
        "the three authorities remain distinguishable",
        sms.CREATION_SURFACES != sms.ACTIVATION_SURFACES
        and sms.ACTIVATION_SURFACES != sms.DEACTIVATION_SURFACES
        and sms.CREATION_SURFACES != sms.DEACTIVATION_SURFACES,
    )
    # The repository-wide scan in
    # `test_telematics_schedule_activation_postgres.py` asserts its allowlist
    # equals REGISTERED_SURFACES. Adding a class that widened that set would
    # break it; adding one that does not is what keeps deny-by-default intact.
    _check(
        "REGISTERED_SURFACES did not widen when the third class was added",
        sms.REGISTERED_SURFACES == (
            sms.CREATION_SURFACES | sms.ACTIVATION_SURFACES
        ),
        f"got {sorted(sms.REGISTERED_SURFACES)}",
    )


def test_deactivation_is_deny_by_default() -> None:
    from jobs.api.telematics import schedule_mutation_surfaces as sms

    _refused(
        "an unregistered surface may not disable",
        lambda: assert_reconciliation_deactivation_permitted(
            surface="ops/some_new_tool.py",
            dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        ),
        "SCHEDULE_MUTATION_SURFACE_UNREGISTERED",
    )
    _refused(
        "a registered but non-deactivation surface may not disable",
        lambda: assert_reconciliation_deactivation_permitted(
            surface=sms.SURFACE_ACTIVATE_TRIPS_SCHEDULE,
            dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        ),
        "SCHEDULE_MUTATION_SURFACE_NOT_PERMITTED",
    )
    _refused(
        "onboarding may not disable",
        lambda: assert_reconciliation_deactivation_permitted(
            surface=SURFACE_ONBOARDING,
            dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        ),
        "SCHEDULE_MUTATION_SURFACE_NOT_PERMITTED",
    )
    _refused(
        "the BASE role can never be disabled through this surface",
        lambda: assert_reconciliation_deactivation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE,
            dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_BASE,
        ),
        "RECONCILIATION_ROLE_REQUIRED",
    )
    _refused(
        "an unknown role is refused, not defaulted",
        lambda: assert_reconciliation_deactivation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE,
            dataset_name="trips_sync",
            run_type="HOURLY_RECONCILIATION",
        ),
        "SCHEDULE_RUN_TYPE_UNKNOWN",
    )
    _refused(
        "a non-coverage-bearing dataset has no cadence to disable",
        lambda: assert_reconciliation_deactivation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE,
            dataset_name="eco_driving_sync",
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        ),
        "RECONCILIATION_DATASET_NOT_ELIGIBLE",
    )
    # The monthly role must pass the same gate, or M7 would land with no
    # reversal for the same reason M6 nearly did.
    try:
        assert_reconciliation_deactivation_permitted(
            surface=SURFACE_RECONCILIATION_SCHEDULE,
            dataset_name="trips_sync",
            run_type=SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
        )
    except ScheduleMutationRefused as exc:
        _check("the monthly role is also disable-able", False, exc.code)
    else:
        _check("the monthly role is also disable-able", True)


def test_disable_does_not_inherit_the_activation_preconditions() -> None:
    """Disable must not depend on the health of the thing it is reversing.

    Asserted on the SIGNATURE, not on behaviour, because the failure this
    prevents is someone "restoring symmetry" by threading the activation
    preconditions through — which would make the reversal unavailable exactly
    when a base schedule has just been switched off or a watermark has stopped
    being READY.
    """
    import inspect

    params = set(
        inspect.signature(assert_reconciliation_deactivation_permitted)
        .parameters
    )
    forbidden = {
        "base_schedule_enabled", "coverage_bootstrap_status", "pagination_mode",
    }
    _check(
        "the disable gate asks nothing about base/coverage/pagination health",
        not (params & forbidden),
        f"it accepts {sorted(params & forbidden)}",
    )
    _check(
        "the disable gate takes exactly surface, dataset and role",
        params == {"surface", "dataset_name", "run_type"},
        f"got {sorted(params)}",
    )


def test_the_active_run_contract_is_stated_and_matches_the_dispatcher() -> None:
    """The chosen semantics is A: future fires only; a live run finishes.

    Proven against the dispatcher source rather than asserted in prose: the
    `enabled` filter appears once, in the tick's schedule loader, and no other
    statement in the module reads it back.
    """
    _check(
        "the tool declares the future-fires-only contract",
        m6.ACTIVE_RUN_SEMANTICS == "FUTURE_FIRES_ONLY_RUNNING_FIRE_COMPLETES",
        m6.ACTIVE_RUN_SEMANTICS,
    )
    import ast as _ast
    import re as _re

    text = (ROOT / "jobs/api/telematics/dispatcher.py").read_text()
    statements = [
        node.value
        for node in _ast.walk(_ast.parse(text, "dispatcher.py"))
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
        # A statement, not a docstring that happens to name the table.
        and _re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b",
                      node.value, _re.I)
        and _re.search(
            r"(FROM|JOIN|UPDATE|INSERT\s+INTO)\s+\S*client_dataset_schedule",
            node.value, _re.I,
        )
    ]
    reads_enabled = [
        sql for sql in statements
        if _re.search(r"cds\.enabled\s*=\s*true", sql, _re.I)
    ]
    _check(
        "the dispatcher reads schedule `enabled` in exactly one statement",
        len(reads_enabled) == 1,
        f"{len(reads_enabled)} statements; a second read would let a mid-run "
        "disable change an already-claimed fire",
    )
    others = [sql for sql in statements if sql not in reads_enabled]
    _check(
        "every other dispatcher schedule statement is an existence-only join "
        "on the BASE role",
        all(
            "enabled" not in sql.lower()
            and _re.search(r"run_type\s*=", sql, _re.I)
            for sql in others
        ),
        "a dispatcher statement re-reads schedule enablement after the claim; "
        "the future-fires-only contract must be re-derived",
    )
    _check(
        "the dispatcher never writes client_dataset_schedule at all",
        not [
            sql for sql in statements
            if _re.search(r"(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+\S*"
                          r"client_dataset_schedule", sql, _re.I)
        ],
    )

# ===========================================================================
# PostgreSQL — the partition against the real table
# ===========================================================================

def test_the_partition_matches_the_live_table(conn) -> None:
    import ops.tests_manual.test_telematics_m5_multi_cadence_identity_postgres as m5

    m5.apply_chain(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'workflow_a_control'
               AND table_name = 'client_dataset_schedule'
            """
        )
        physical = {row[0] for row in cur.fetchall()}
    classified = (
        set(SCHEDULE_IDENTITY_FIELDS) | set(SCHEDULE_INHERITED_FIELDS)
        | set(SCHEDULE_DECLARED_FIELDS) | set(SCHEDULE_GENERATED_FIELDS)
    )
    _check(
        "every physical column is classified, and none is invented",
        classified == physical,
        f"unclassified: {sorted(physical - classified)}; "
        f"not in the table: {sorted(classified - physical)}",
    )
    conn.rollback()


def test_the_derived_row_is_storable_and_coexists_with_the_base(conn) -> None:
    """The M5 uniqueness model accepts the derived row beside its base."""
    import ops.tests_manual.test_telematics_m5_multi_cadence_identity_postgres as m5

    m5.apply_chain(conn)
    m5.seed_client(conn)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO workflow_a_control.client_dataset_schedule
               (schedule_id, client_id, client_code, dataset_name, enabled,
                frequency, run_time, timezone, lookback_days,
                overwrite_existing, event_enrichment_mode, run_type)
               VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',3,
                       true,'disabled','DAILY')""",
            (m5.SID_BASE, m5.CID, m5.CODE),
        )
        cur.execute(
            """SELECT client_id::text AS client_id, client_code, dataset_name,
                      overwrite_existing, event_enrichment_mode, run_type
                 FROM workflow_a_control.client_dataset_schedule
                WHERE schedule_id = %s""",
            (m5.SID_BASE,),
        )
        cols = [d[0] for d in cur.description]
        base = dict(zip(cols, cur.fetchone()))

        derived = derive_reconciliation_schedule(
            base_row=base,
            run_type=SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
            frequency="weekly", day_of_week=0, run_time=time(0, 30),
            timezone_name="Europe/Warsaw", lookback_days=L_WEEKLY,
        )
        columns = ", ".join(RECONCILIATION_INSERT_FIELDS)
        marks = ", ".join(["%s"] * len(RECONCILIATION_INSERT_FIELDS))
        cur.execute(
            f"""INSERT INTO workflow_a_control.client_dataset_schedule
                ({columns}, enabled) VALUES ({marks}, FALSE)
                RETURNING schedule_id::text""",
            [derived[name] for name in RECONCILIATION_INSERT_FIELDS],
        )
        weekly_id = cur.fetchone()[0]

        cur.execute(
            """SELECT run_type, enabled, event_enrichment_mode, lookback_days,
                      timezone, frequency, day_of_week, run_time
                 FROM workflow_a_control.client_dataset_schedule
                WHERE client_id = %s AND dataset_name = 'trips_sync'
                ORDER BY run_type""",
            (m5.CID,),
        )
        rows = cur.fetchall()
    _check(
        "the base and the weekly cadence coexist for one (client, dataset)",
        len(rows) == 2 and {r[0] for r in rows} == {
            SCHEDULE_RUN_TYPE_BASE, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        },
        f"{rows}",
    )
    weekly = next(r for r in rows if r[0] == SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION)
    _check("the stored weekly row is DISABLED", weekly[1] is False)
    _check(
        "the stored weekly row inherited event_enrichment_mode='disabled'",
        weekly[2] == "disabled",
        f"got {weekly[2]!r}",
    )
    _check("the stored weekly row carries L=16", weekly[3] == 16)
    _check("the stored weekly row carries Europe/Warsaw", weekly[4] == "Europe/Warsaw")
    _check(
        "the stored weekly row fires Monday 00:30",
        weekly[5] == "weekly" and weekly[6] == 0 and str(weekly[7]) == "00:30:00",
        f"{weekly[5:]}",
    )
    base_row = next(r for r in rows if r[0] == SCHEDULE_RUN_TYPE_BASE)
    _check(
        "the base row was not modified by registering a reconciliation cadence",
        base_row[1] is True and base_row[3] == 3 and base_row[4] == "UTC",
        f"{base_row}",
    )
    _check("the weekly row has its own identity", weekly_id != m5.SID_BASE)
    conn.rollback()


def test_a_second_weekly_cadence_is_refused_by_the_database(conn) -> None:
    import ops.tests_manual.test_telematics_m5_multi_cadence_identity_postgres as m5

    m5.apply_chain(conn)
    m5.seed_client(conn)
    with conn.cursor() as cur:
        for sid, role in (
            (m5.SID_BASE, SCHEDULE_RUN_TYPE_BASE),
            (m5.SID_WEEKLY, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION),
        ):
            cur.execute(
                """INSERT INTO workflow_a_control.client_dataset_schedule
                   (schedule_id, client_id, client_code, dataset_name, enabled,
                    frequency, day_of_week, run_time, timezone, lookback_days,
                    overwrite_existing, event_enrichment_mode, run_type)
                   VALUES (%s,%s,%s,'trips_sync',false,%s,%s,'00:30',
                           'Europe/Warsaw',16,true,'disabled',%s)""",
                (
                    sid, m5.CID, m5.CODE,
                    "daily" if role == SCHEDULE_RUN_TYPE_BASE else "weekly",
                    None if role == SCHEDULE_RUN_TYPE_BASE else 0,
                    role,
                ),
            )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_a_control.client_dataset_schedule
                   (schedule_id, client_id, client_code, dataset_name, enabled,
                    frequency, day_of_week, run_time, timezone, lookback_days,
                    overwrite_existing, event_enrichment_mode, run_type)
                   VALUES (%s,%s,%s,'trips_sync',false,'weekly',0,'00:30',
                           'Europe/Warsaw',16,true,'disabled',%s)""",
                (
                    m5.SID_MONTHLY, m5.CID, m5.CODE,
                    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
                ),
            )
    except Exception:
        _check("a second WEEKLY_RECONCILIATION row is rejected by 062", True)
    else:
        _check("a second WEEKLY_RECONCILIATION row is rejected by 062", False,
               "the database accepted a duplicate role")
    conn.rollback()


# ===========================================================================
# PostgreSQL — the disable surface end to end
#
# Driven through `m6.build_parser()` and `m6.run()`, i.e. through the real CLI
# contract, so the argument gates are exercised rather than bypassed.
# ===========================================================================

DISABLE_PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
DISABLE_APPROVAL = "M6-DISABLE-TEST-1"
SID_M6 = "9c9c9261-2cf7-4b6f-8955-b515814be2f7"
SID_M6_DUPLICATE = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"
IDENTITY_MIGRATION = "042_platform_environment_identity.sql"


def _m5():
    import ops.tests_manual.test_telematics_m5_multi_cadence_identity_postgres as m5
    return m5


def _disable_bootstrap(conn) -> None:
    """A migrated platform database with an identity marker and a ledger.

    Destructive, and deliberately not shared with `apply_chain`: the tool
    verifies the platform identity and the migration ceiling before it reads
    anything, so both must exist here and neither belongs in the pure M5
    fixture.
    """
    m5 = _m5()
    names = list(m5.PREREQUISITE_MIGRATIONS) + [
        IDENTITY_MIGRATION, m5.MIGRATION_NAME,
    ]
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        for name in names:
            cur.execute(
                (ROOT / "db/migrations" / name).read_text(encoding="utf-8")
            )
        cur.execute(
            "CREATE TABLE public.schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.executemany(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
            [(name,) for name in names],
        )
        cur.execute(
            """
            INSERT INTO ops_control.environment_identity
              (identity_key, environment, database_identity_id, database_role,
               database_name, provisioned_by)
            VALUES ('primary','production',%s,'platform','logdb','test')
            """,
            (DISABLE_PLATFORM_UUID,),
        )
    conn.commit()


def _seed_lifecycle(
    conn, *, base_enabled: bool = True, m6_enabled: bool = True,
    m6_present: bool = True, client_enabled: bool = True,
) -> None:
    """One client, its base `trips_sync` schedule and (optionally) its M6 row.

    Both schedules are given run history, because "the disable preserved the
    audit trail" is only a real assertion when there is an audit trail.
    """
    m5 = _m5()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
    conn.commit()
    m5.seed_client(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_account SET enabled = %s "
            "WHERE client_id = %s",
            (client_enabled, m5.CID),
        )
        cur.execute(
            """INSERT INTO workflow_a_control.client_dataset_schedule
               (schedule_id, client_id, client_code, dataset_name, enabled,
                frequency, run_time, timezone, lookback_days,
                overwrite_existing, event_enrichment_mode, run_type)
               VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00','UTC',3,
                       true,'disabled','DAILY')""",
            (m5.SID_BASE, m5.CID, m5.CODE, base_enabled),
        )
        if m6_present:
            cur.execute(
                """INSERT INTO workflow_a_control.client_dataset_schedule
                   (schedule_id, client_id, client_code, dataset_name, enabled,
                    frequency, day_of_week, run_time, timezone, lookback_days,
                    overwrite_existing, event_enrichment_mode, run_type)
                   VALUES (%s,%s,%s,'trips_sync',%s,'weekly',0,'00:30',
                           'Europe/Warsaw',16,true,'disabled',%s)""",
                (
                    SID_M6, m5.CID, m5.CODE, m6_enabled,
                    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
                ),
            )
    m5.seed_coverage(conn)
    conn.commit()
    _seed_history(conn, m5.SID_BASE, "SUCCESS", days=3)
    if m6_present:
        _seed_history(conn, SID_M6, "SUCCESS", days=7)
    conn.commit()


def _seed_history(conn, schedule_id: str, status: str, *, days: int) -> str:
    m5 = _m5()
    fire = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO workflow_a_control.client_schedule_run_history
               (schedule_id, client_id, client_code, dataset_name,
                window_start_ts, window_end_ts, scheduled_fire_ts, status,
                started_at)
               VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,%s,%s)
               RETURNING run_history_id::text""",
            (
                schedule_id, m5.CID, m5.CODE, fire - timedelta(days=16), fire,
                fire, status, fire,
            ),
        )
        return cur.fetchone()[0]


def _disable_args(dsn, **overrides):
    m5 = _m5()
    spec = {
        "operation": "disable",
        "client_code": m5.CODE,
        "dataset": "trips_sync",
        "run_type": SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        "environment": "production",
        "platform_uuid": DISABLE_PLATFORM_UUID,
        "approval_ref": DISABLE_APPROVAL,
        "execute": False,
        "confirm": None,
    }
    spec.update(overrides)
    argv = [
        spec["operation"],
        "--client-code", spec["client_code"],
        "--dataset", spec["dataset"],
        "--run-type", spec["run_type"],
        "--expected-environment", spec["environment"],
        "--expected-platform-uuid", spec["platform_uuid"],
        "--dsn", dsn,
    ]
    if spec["approval_ref"] is not None:
        argv += ["--approval-ref", spec["approval_ref"]]
    if spec["execute"]:
        argv += ["--execute"]
    if spec["confirm"] is not None:
        argv += ["--confirm-client-code", spec["confirm"]]
    return m6.build_parser().parse_args(argv)


def _schedules(conn):
    """Every schedule row, keyed by role, with its audit columns."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_type, schedule_id::text, client_id::text, client_code,
                      dataset_name, enabled, frequency, day_of_week,
                      day_of_month, day_of_month_last, run_time, timezone,
                      lookback_days, overwrite_existing, event_enrichment_mode,
                      created_at, updated_at
                 FROM workflow_a_control.client_dataset_schedule
                ORDER BY run_type, schedule_id"""
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.rollback()
    return {row["run_type"]: row for row in rows}


def _history(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_history_id::text, schedule_id::text, status,
                      window_start_ts, window_end_ts, scheduled_fire_ts
                 FROM workflow_a_control.client_schedule_run_history
                ORDER BY run_history_id"""
        )
        rows = [tuple(str(v) for v in row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def _snapshot(conn):
    return (_schedules(conn), _history(conn))


def _refused_tool(label: str, fn, expected_code: str) -> None:
    try:
        fn()
    except (m6.ReconciliationRefused, ScheduleMutationRefused) as exc:
        _check(label, exc.code == expected_code, f"got {exc.code}: {exc}")
    else:
        _check(label, False, "it was permitted")


# --- 1. dry run -------------------------------------------------------------

def test_disable_dry_run_is_non_mutating(conn, dsn) -> None:
    _seed_lifecycle(conn, m6_enabled=True)
    before = _snapshot(conn)
    exit_code, plan = m6.run(_disable_args(dsn))
    _check("disable dry run exits OK", exit_code == m6.EXIT_OK, str(exit_code))
    _check("disable dry run reports DRY_RUN", plan["mode"] == "DRY_RUN")
    _check("disable dry run would change the row", plan["would_change"] is True)
    _check(
        "disable dry run names exactly the fields it would change",
        plan["fields_to_change"] == ["enabled", "updated_at"],
        str(plan["fields_to_change"]),
    )
    _check("disable dry run deletes nothing", plan["schedule_rows_deleted"] == 0
           and plan["run_history_rows_deleted"] == 0)
    _check(
        "disable dry run carries no private row objects into the report",
        not [key for key in plan if key.startswith("_")],
        str(sorted(plan)),
    )
    _check(
        "a disable dry run writes nothing at all",
        _snapshot(conn) == before,
    )


# --- 2..7. the successful transition ---------------------------------------

def test_disable_returns_the_row_to_disabled(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    before_schedules, before_history = _snapshot(conn)

    exit_code, plan = m6.run(
        _disable_args(dsn, execute=True, confirm=m5.CODE)
    )
    _check("disable executes OK", exit_code == m6.EXIT_OK, str(exit_code))
    _check("disable reports EXECUTE", plan["mode"] == "EXECUTE")
    _check(
        "disable classifies the transition",
        plan["result"]["classification"] == "DISABLED"
        and plan["result"]["changed"] is True
        and plan["result"]["rows_updated"] == 1,
        str(plan["result"]),
    )
    _check(
        "the approval reference is recorded in the report",
        plan["approval_ref"] == DISABLE_APPROVAL,
    )

    after_schedules, after_history = _snapshot(conn)
    weekly_before = before_schedules[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]
    weekly_after = after_schedules[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]

    _check("the M6 row still exists after disable", weekly_after is not None)
    _check("the M6 row is disabled", weekly_after["enabled"] is False)
    _check(
        "every other M6 field is unchanged",
        all(
            weekly_after[name] == weekly_before[name]
            for name in weekly_before
            if name not in ("enabled", "updated_at")
        ),
        str([
            name for name in weekly_before
            if name not in ("enabled", "updated_at")
            and weekly_after[name] != weekly_before[name]
        ]),
    )
    _check(
        "the inherited event_enrichment_mode survived the disable",
        weekly_after["event_enrichment_mode"] == "disabled",
    )
    _check(
        "updated_at advanced, which is the audit trail of the change",
        weekly_after["updated_at"] > weekly_before["updated_at"],
    )
    _check(
        "the DAILY row is field-for-field identical, updated_at included",
        after_schedules[SCHEDULE_RUN_TYPE_BASE]
        == before_schedules[SCHEDULE_RUN_TYPE_BASE],
    )
    _check(
        "the DAILY row is still enabled",
        after_schedules[SCHEDULE_RUN_TYPE_BASE]["enabled"] is True,
    )
    _check("run history is byte-identical", after_history == before_history)
    _check(
        "run history was not emptied",
        len(after_history) == 2,
        f"{len(after_history)} rows",
    )


def test_a_disabled_row_leaves_the_dispatcher_selection(conn, dsn) -> None:
    """The operational point of the whole surface, proven on the real loader."""
    import psycopg
    from jobs.api.telematics import dispatcher

    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    with psycopg.connect(dsn, autocommit=True) as probe:
        roles_before = {row.run_type for row in
                        dispatcher._load_enabled_schedules(probe)}
    _check(
        "before disable the dispatcher enumerates both cadences",
        roles_before == {
            SCHEDULE_RUN_TYPE_BASE, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
        },
        str(sorted(roles_before)),
    )
    m6.run(_disable_args(dsn, execute=True, confirm=m5.CODE))
    with psycopg.connect(dsn, autocommit=True) as probe:
        roles_after = {row.run_type for row in
                       dispatcher._load_enabled_schedules(probe)}
    _check(
        "after disable the dispatcher enumerates the base cadence only",
        roles_after == {SCHEDULE_RUN_TYPE_BASE},
        str(sorted(roles_after)),
    )


# --- 3. idempotency ---------------------------------------------------------

def test_disabling_an_already_disabled_row_is_an_idempotent_no_op(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=False)
    before = _snapshot(conn)

    exit_code, plan = m6.run(_disable_args(dsn))
    _check("the dry run reports no change is needed",
           plan["already_disabled"] is True and plan["would_change"] is False)
    _check("the dry run names no fields to change",
           plan["fields_to_change"] == [])

    exit_code, plan = m6.run(
        _disable_args(dsn, execute=True, confirm=m5.CODE)
    )
    _check(
        "an already-disabled row is a success, not an error",
        exit_code == m6.EXIT_OK,
        str(exit_code),
    )
    _check(
        "it is classified as an idempotent no-change",
        plan["result"]["classification"] == "ALREADY_DISABLED"
        and plan["result"]["changed"] is False
        and plan["result"]["rows_updated"] == 0,
        str(plan["result"]),
    )
    _check(
        "the idempotent path writes nothing, not even updated_at",
        _snapshot(conn) == before,
    )
    # Twice more, because idempotency that only holds on the second call is not
    # idempotency.
    m6.run(_disable_args(dsn, execute=True, confirm=m5.CODE))
    m6.run(_disable_args(dsn, execute=True, confirm=m5.CODE))
    _check("repeated disables remain a no-op", _snapshot(conn) == before)


# --- 8..10. the authorization contract --------------------------------------

def test_the_production_authorization_gates_are_not_weakened(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    before = _snapshot(conn)

    _refused_tool(
        "--execute without --confirm-client-code is refused",
        lambda: m6.run(_disable_args(dsn, execute=True, confirm=None)),
        "CONFIRMATION_MISMATCH",
    )
    _refused_tool(
        "a mismatched client confirmation is refused",
        lambda: m6.run(
            _disable_args(dsn, execute=True, confirm="WRONG0001")
        ),
        "CONFIRMATION_MISMATCH",
    )
    _refused_tool(
        "--execute without --approval-ref is refused",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE, approval_ref=None
            )
        ),
        "APPROVAL_REF_REQUIRED",
    )
    _refused_tool(
        "a malformed approval reference is refused",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE,
                approval_ref="not a token",
            )
        ),
        "INVALID_PARAMETER",
    )
    _check(
        "no refused authorization attempt mutated anything",
        _snapshot(conn) == before,
    )

    # Identity is verified before the plan is even built.
    try:
        m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE, environment="staging",
            )
        )
    except Exception as exc:
        _check(
            "a wrong --expected-environment is refused",
            getattr(exc, "code", "") == "IDENTITY_ENVIRONMENT_MISMATCH",
            f"got {type(exc).__name__}: {exc}",
        )
    else:
        _check("a wrong --expected-environment is refused", False)

    try:
        m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE,
                platform_uuid="db8055e0-e030-4d5a-816b-ec4dc338d698",
            )
        )
    except Exception as exc:
        _check(
            "a wrong --expected-platform-uuid is refused",
            getattr(exc, "code", "") == "IDENTITY_PLATFORM_UUID_MISMATCH",
            f"got {type(exc).__name__}: {exc}",
        )
    else:
        _check("a wrong --expected-platform-uuid is refused", False)

    _check(
        "no refused identity attempt mutated anything",
        _snapshot(conn) == before,
    )
    _check(
        "the row is still enabled after every refusal",
        _schedules(conn)[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]["enabled"]
        is True,
    )


def test_an_unknown_client_is_refused(conn, dsn) -> None:
    _seed_lifecycle(conn, m6_enabled=True)
    _refused_tool(
        "an unknown client code is refused",
        lambda: m6.run(_disable_args(dsn, client_code="NOPE00001")),
        "CLIENT_NOT_FOUND",
    )


# --- 11..13. fail-closed target validation ----------------------------------

def test_the_base_role_cannot_be_disabled_through_this_surface(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    before = _snapshot(conn)
    _refused_tool(
        "--run-type DAILY is refused by the policy oracle",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE,
                run_type=SCHEDULE_RUN_TYPE_BASE,
            )
        ),
        "RECONCILIATION_ROLE_REQUIRED",
    )
    _refused_tool(
        "an unknown --run-type is refused rather than defaulted",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE, run_type="DAILYISH",
            )
        ),
        "SCHEDULE_RUN_TYPE_UNKNOWN",
    )
    _refused_tool(
        "a dataset with no reconciliation cadence is refused",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE, dataset="eco_driving_sync",
            )
        ),
        "RECONCILIATION_DATASET_NOT_ELIGIBLE",
    )
    _check("the DAILY row survived every attempt", _snapshot(conn) == before)
    _check(
        "the DAILY row is still enabled",
        _schedules(conn)[SCHEDULE_RUN_TYPE_BASE]["enabled"] is True,
    )


def test_a_missing_reconciliation_row_fails_closed(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, m6_present=False)
    before = _snapshot(conn)
    _refused_tool(
        "no M6 row is a fail-closed refusal, not a silent success",
        lambda: m6.run(
            _disable_args(dsn, execute=True, confirm=m5.CODE)
        ),
        "RECONCILIATION_SCHEDULE_ABSENT",
    )
    _refused_tool(
        "a role that exists in the vocabulary but not in the table is refused",
        lambda: m6.run(
            _disable_args(
                dsn, execute=True, confirm=m5.CODE,
                run_type=SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
            )
        ),
        "RECONCILIATION_SCHEDULE_ABSENT",
    )
    _check("nothing was created to then disable", _snapshot(conn) == before)


def test_two_matching_rows_fail_closed(conn, dsn) -> None:
    """Ambiguity must stop the write even though 062 makes it unreachable.

    The uniqueness constraint is dropped for the duration precisely because a
    guard that can only be reached through a schema defect is a guard nothing
    has ever executed.
    """
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE workflow_a_control.client_dataset_schedule "
            "DROP CONSTRAINT uq_client_dataset_schedule"
        )
        cur.execute(
            """INSERT INTO workflow_a_control.client_dataset_schedule
               (schedule_id, client_id, client_code, dataset_name, enabled,
                frequency, day_of_week, run_time, timezone, lookback_days,
                overwrite_existing, event_enrichment_mode, run_type)
               VALUES (%s,%s,%s,'trips_sync',true,'weekly',0,'00:30',
                       'Europe/Warsaw',16,true,'disabled',%s)""",
            (
                SID_M6_DUPLICATE, m5.CID, m5.CODE,
                SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
            ),
        )
    conn.commit()
    _refused_tool(
        "two matching M6 rows fail closed",
        lambda: m6.run(
            _disable_args(dsn, execute=True, confirm=m5.CODE)
        ),
        "SCHEDULE_AMBIGUOUS",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM workflow_a_control.client_dataset_schedule "
            "WHERE run_type = %s AND enabled = true",
            (SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,),
        )
        still_enabled = cur.fetchone()[0]
    conn.rollback()
    _check(
        "neither ambiguous row was disabled",
        still_enabled == 2,
        f"{still_enabled} still enabled",
    )
    _disable_bootstrap(conn)


# --- 14..15. the SWEE shape -------------------------------------------------

def test_a_disabled_base_does_not_block_the_reversal(conn, dsn) -> None:
    """ECHO00001's shape: base `trips_sync` off, M6 row on.

    Disable must still work. It removes execution capability, so the activation
    preconditions do not apply — and this is exactly the state in which an
    operator most needs the cadence switched off.
    """
    m5 = _m5()
    _seed_lifecycle(conn, base_enabled=False, m6_enabled=True)
    exit_code, plan = m6.run(
        _disable_args(dsn, execute=True, confirm=m5.CODE)
    )
    _check(
        "a disabled base schedule does not block disable",
        exit_code == m6.EXIT_OK
        and plan["result"]["classification"] == "DISABLED",
        str(plan.get("result")),
    )
    _check("the plan reports the base as disabled", plan["base_enabled"] is False)
    after = _schedules(conn)
    _check(
        "the M6 row is disabled",
        after[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]["enabled"] is False,
    )
    _check(
        "the disabled DAILY row was left disabled and otherwise untouched",
        after[SCHEDULE_RUN_TYPE_BASE]["enabled"] is False,
    )

    # A disabled client account must not block the reversal either.
    _seed_lifecycle(conn, base_enabled=False, m6_enabled=True,
                    client_enabled=False)
    exit_code, plan = m6.run(
        _disable_args(dsn, execute=True, confirm=m5.CODE)
    )
    _check(
        "a disabled client account does not block disable either",
        exit_code == m6.EXIT_OK
        and plan["result"]["classification"] == "DISABLED",
        str(plan.get("result")),
    )


def test_the_swee_enable_refusal_is_unchanged(conn, dsn) -> None:
    m5 = _m5()
    _seed_lifecycle(conn, base_enabled=False, m6_enabled=False)
    before = _snapshot(conn)
    _refused_tool(
        "enable is still refused while the base schedule is disabled",
        lambda: m6.run(
            _disable_args(
                dsn, operation="enable", execute=True, confirm=m5.CODE,
            )
        ),
        "RECONCILIATION_ACTIVATION_REFUSED_BASE_DISABLED",
    )
    _check("the refused enable mutated nothing", _snapshot(conn) == before)

    # And with the base enabled, enable still works — the disable surface did
    # not disturb the activation path it is the reversal of.
    _seed_lifecycle(conn, base_enabled=True, m6_enabled=False)
    exit_code, plan = m6.run(
        _disable_args(dsn, operation="enable", execute=True, confirm=m5.CODE)
    )
    _check(
        "enable still activates a registered row",
        exit_code == m6.EXIT_OK and plan["result"]["enabled"] is True,
        str(plan.get("result")),
    )
    _check(
        "and disable reverses exactly that",
        m6.run(_disable_args(dsn, execute=True, confirm=m5.CODE))[1]
        ["result"]["classification"] == "DISABLED",
    )
    _check(
        "enable -> disable -> enable is a round trip, not a one-way door",
        m6.run(
            _disable_args(dsn, operation="enable", execute=True,
                          confirm=m5.CODE)
        )[1]["result"]["enabled"] is True,
    )


# --- 16. active-run semantics ----------------------------------------------

def test_disable_while_a_reconciliation_run_is_active(conn, dsn) -> None:
    """Contract A: the live fire finishes; only future fires are prevented."""
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    running_id = _seed_history(conn, SID_M6, "RUNNING", days=14)
    conn.commit()
    before_history = _history(conn)

    exit_code, plan = m6.run(_disable_args(dsn))
    _check(
        "the dry run surfaces the active run to the operator",
        plan["active_runs"] == 1,
        str(plan.get("active_runs")),
    )
    _check(
        "the dry run states the active-run contract",
        plan["active_run_semantics"] == m6.ACTIVE_RUN_SEMANTICS,
    )

    exit_code, plan = m6.run(
        _disable_args(dsn, execute=True, confirm=m5.CODE)
    )
    _check(
        "disable is NOT refused while a reconciliation run is active",
        exit_code == m6.EXIT_OK
        and plan["result"]["classification"] == "DISABLED",
        str(plan.get("result")),
    )
    after_history = _history(conn)
    _check(
        "the RUNNING claim is left exactly as it was",
        after_history == before_history,
    )
    running = [row for row in after_history if row[0] == running_id]
    _check(
        "the active run is still RUNNING, not cancelled or failed",
        len(running) == 1 and running[0][2] == "RUNNING",
        str(running),
    )
    _check(
        "the disabled row is still the one the running claim points at",
        _schedules(conn)[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]["schedule_id"]
        == SID_M6,
    )


# --- Codex review follow-ups ------------------------------------------------

def test_a_concurrent_enable_cannot_produce_a_false_already_disabled(
    conn, dsn
) -> None:
    """The idempotent answer must come from a LOCKED read, not from the plan.

    Constructed exactly as the race occurs: the plan is built while the row is
    disabled, another connection enables it and commits, and only then does the
    write run. Deciding from the plan snapshot would report ALREADY_DISABLED and
    exit 0 while leaving the schedule ENABLED — a false success on the one
    operation whose purpose is to stop future execution.
    """
    import psycopg
    from psycopg.rows import dict_row

    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=False)
    args = _disable_args(dsn, execute=True, confirm=m5.CODE)

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as writer:
        with writer.cursor() as cur:
            plan = m6.plan_disable(cur, args)
            _check(
                "the plan was built against a disabled row",
                plan["already_disabled"] is True,
            )
            # ... and now somebody enables it and commits.
            with psycopg.connect(dsn, autocommit=True) as other:
                other.execute(
                    "UPDATE workflow_a_control.client_dataset_schedule "
                    "SET enabled = TRUE WHERE schedule_id = %s "
                    "AND run_type = %s",
                    (SID_M6, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION),
                )
            result = m6.execute_disable(cur, plan)
        writer.commit()

    _check(
        "the stale plan does not win: the row is actually disabled",
        result["classification"] == "DISABLED" and result["changed"] is True,
        str(result),
    )
    _check(
        "and the durable state agrees",
        _schedules(conn)[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]["enabled"]
        is False,
    )


def test_a_row_locked_by_another_writer_is_refused_not_blocked(conn, dsn) -> None:
    import psycopg

    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    with psycopg.connect(dsn, autocommit=False) as holder:
        holder.execute(
            "SELECT schedule_id FROM workflow_a_control.client_dataset_schedule "
            "WHERE schedule_id = %s AND run_type = %s FOR UPDATE",
            (SID_M6, SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION),
        )
        _refused_tool(
            "a contended row is a write conflict, not an indefinite block",
            lambda: m6.run(_disable_args(dsn, execute=True, confirm=m5.CODE)),
            "DISABLE_LOCK_UNAVAILABLE",
        )
        holder.rollback()
    _check(
        "nothing was disabled while the lock was held",
        _schedules(conn)[SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION]["enabled"]
        is True,
    )


def test_identity_refusals_carry_their_declared_exit_code(conn, dsn) -> None:
    """A wrong database must not look like a crash.

    `verify_platform_identity` is borrowed from the audit tool and raises ITS
    exception type. Before this was handled, every identity refusal fell to the
    generic handler and reported EXIT_RUNTIME_FAILURE, so a wrapper script could
    not tell "you pointed this at the wrong database" from "the tool crashed".
    """
    m5 = _m5()
    _seed_lifecycle(conn, m6_enabled=True)
    before = _snapshot(conn)

    def _argv(**overrides):
        args = _disable_args(dsn, **overrides)
        argv = [
            args.operation,
            "--client-code", args.client_code,
            "--dataset", args.dataset,
            "--run-type", args.run_type,
            "--expected-environment", args.expected_environment,
            "--expected-platform-uuid", args.expected_platform_uuid,
            "--dsn", args.dsn,
        ]
        if args.approval_ref:
            argv += ["--approval-ref", args.approval_ref]
        if args.execute:
            argv += ["--execute"]
        if args.confirm_client_code:
            argv += ["--confirm-client-code", args.confirm_client_code]
        return argv

    code = m6.main(_argv(execute=True, confirm=m5.CODE, environment="staging"))
    _check(
        "a wrong environment exits EXIT_IDENTITY_NOT_VERIFIED",
        code == m6.EXIT_IDENTITY_NOT_VERIFIED,
        f"got {code}",
    )
    code = m6.main(_argv(
        execute=True, confirm=m5.CODE,
        platform_uuid="db8055e0-e030-4d5a-816b-ec4dc338d698",
    ))
    _check(
        "a wrong platform UUID exits EXIT_IDENTITY_NOT_VERIFIED",
        code == m6.EXIT_IDENTITY_NOT_VERIFIED,
        f"got {code}",
    )
    code = m6.main(_argv(
        execute=True, confirm=m5.CODE, platform_uuid="not-a-uuid",
    ))
    _check(
        "a malformed platform UUID exits EXIT_INVALID_PARAMETERS",
        code == m6.EXIT_INVALID_PARAMETERS,
        f"got {code}",
    )
    code = m6.main(_argv(execute=True, confirm="WRONG0001"))
    _check(
        "a confirmation mismatch still exits EXIT_INVALID_PARAMETERS",
        code == m6.EXIT_INVALID_PARAMETERS,
        f"got {code}",
    )
    code = m6.main(_argv(execute=True, confirm=m5.CODE,
                         run_type=SCHEDULE_RUN_TYPE_BASE))
    _check(
        "a policy refusal still exits EXIT_REFUSED",
        code == m6.EXIT_REFUSED,
        f"got {code}",
    )
    _check("no refusal mutated anything", _snapshot(conn) == before)

    code = m6.main(_argv(execute=True, confirm=m5.CODE))
    _check("and the authorized disable exits OK", code == m6.EXIT_OK, f"got {code}")


def test_the_borrowed_exit_codes_have_not_drifted() -> None:
    """The audit tool owns the identity exit codes this tool re-declares."""
    import ops.audit_telematics_coverage_bootstrap as audit

    _check(
        "EXIT_IDENTITY_NOT_VERIFIED agrees with the module it is borrowed from",
        m6.EXIT_IDENTITY_NOT_VERIFIED == audit.EXIT_IDENTITY_NOT_VERIFIED,
        f"{m6.EXIT_IDENTITY_NOT_VERIFIED} vs {audit.EXIT_IDENTITY_NOT_VERIFIED}",
    )
    _check(
        "EXIT_INVALID_PARAMETERS agrees too",
        m6.EXIT_INVALID_PARAMETERS == audit.EXIT_INVALID_PARAMETERS,
        f"{m6.EXIT_INVALID_PARAMETERS} vs {audit.EXIT_INVALID_PARAMETERS}",
    )


def test_disable_on_postgres(dsn) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=False) as conn:
        _disable_bootstrap(conn)
        test_disable_dry_run_is_non_mutating(conn, dsn)
        test_disable_returns_the_row_to_disabled(conn, dsn)
        test_a_disabled_row_leaves_the_dispatcher_selection(conn, dsn)
        test_disabling_an_already_disabled_row_is_an_idempotent_no_op(conn, dsn)
        test_the_production_authorization_gates_are_not_weakened(conn, dsn)
        test_an_unknown_client_is_refused(conn, dsn)
        test_the_base_role_cannot_be_disabled_through_this_surface(conn, dsn)
        test_a_missing_reconciliation_row_fails_closed(conn, dsn)
        test_two_matching_rows_fail_closed(conn, dsn)
        test_a_disabled_base_does_not_block_the_reversal(conn, dsn)
        test_the_swee_enable_refusal_is_unchanged(conn, dsn)
        test_disable_while_a_reconciliation_run_is_active(conn, dsn)
        test_a_concurrent_enable_cannot_produce_a_false_already_disabled(
            conn, dsn
        )
        test_a_row_locked_by_another_writer_is_refused_not_blocked(conn, dsn)
        test_identity_refusals_carry_their_declared_exit_code(conn, dsn)


# ===========================================================================

def main() -> int:
    print("=== pure ===")
    test_the_approved_m6_shape_is_what_the_tool_defaults_to()
    test_the_restated_coverage_status_has_not_drifted()
    test_the_column_partition_is_exhaustive_and_disjoint()
    test_l16_derives_the_existing_rolling_window()
    test_r_does_not_clamp_l16_at_the_current_value()
    test_the_weekly_guaranteed_horizon_clears_the_proven_maximum()
    test_the_coverage_gate_admits_a_weekly_fire_on_the_shared_row()
    test_a_weekly_window_always_reaches_back_at_least_to_the_watermark()
    test_event_enrichment_mode_is_inherited_not_defaulted()
    test_a_base_row_missing_an_inherited_field_is_refused()
    test_the_declared_cadence_may_differ_from_the_base()
    test_the_derivation_does_not_mutate_the_base_row()
    test_creation_is_deny_by_default()
    test_activation_preconditions_fail_closed()
    test_a_cadence_the_dispatcher_could_never_fire_is_refused()
    test_monday_0030_warsaw_fires_correctly_across_both_dst_transitions()
    test_daily_window_derivation_is_unchanged()
    test_the_dispatcher_still_does_not_branch_on_run_type()
    test_overlap_cannot_duplicate_a_trip_or_restate_first_seen()
    test_deactivation_is_its_own_registered_authority()
    test_deactivation_is_deny_by_default()
    test_disable_does_not_inherit_the_activation_preconditions()
    test_the_active_run_contract_is_stated_and_matches_the_dispatcher()
    test_the_borrowed_exit_codes_have_not_drifted()

    dsn = os.environ.get(ENV, "").strip()
    if not dsn:
        print(f"\nSKIP PostgreSQL checks — set {ENV} to a disposable database")
    else:
        from ops.tests_manual.postgres_dsn_safety import (
            require_loopback_dsn_or_exit,
        )

        require_loopback_dsn_or_exit(dsn, label=ENV)
        import psycopg

        print("\n=== postgres ===")
        with psycopg.connect(dsn, autocommit=False) as conn:
            test_the_partition_matches_the_live_table(conn)
            test_the_derived_row_is_storable_and_coexists_with_the_base(conn)
            test_a_second_weekly_cadence_is_refused_by_the_database(conn)

        print("\n=== postgres — the disable surface ===")
        test_disable_on_postgres(dsn)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {_failures}")
        return 1
    print("OK - M6 weekly reconciliation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
