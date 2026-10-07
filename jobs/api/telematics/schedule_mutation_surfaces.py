#!/usr/bin/env python3
"""Deny-by-default registry of every surface allowed to create, enable or
disable a Workflow A dataset schedule, plus the invariants each one must
satisfy.

WHY A REGISTRY AND NOT A DATABASE CONSTRAINT.
    The rule this module enforces — *an active `trips_sync` schedule must never
    be paired with `strict_meta` pagination* — spans two tables
    (`client_dataset_schedule.enabled` and `client_account.trips_pagination_mode`)
    that are mutated by different tools at different times. A CHECK constraint
    cannot see across rows, so the database-level alternative would be a trigger
    or a materialized redundancy, and both are heavier than the problem.

    The application-level enforcement is complete because the set of supported
    mutation paths is small, closed and enumerated here:

      * `scripts/onboard_workflow_a_client.py` — the only creator of BASE
        schedule rows; creates `trips_sync` disabled, always;
      * `ops/activate_telematics_trips_schedule.py` — the only enabler of a base
        `trips_sync` schedule, at the end of the cold-start state machine;
      * `ops/manage_telematics_reconciliation_schedule.py` — the only creator,
        the only enabler and the only disabler of a RECONCILIATION cadence
        (M6/M7). It is registered in all three sets because a reconciliation row
        is created, enabled and disabled by the same reviewed tool through three
        separate, separately confirmed subcommands. It is deliberately NOT the
        cold-start path: activating a reconciliation cadence has different
        preconditions (a live, enabled base schedule and a READY watermark) than
        activating a client's first schedule, and forcing it through the
        cold-start gates would have meant weakening them.

    NOTHING IS REGISTERED TO DISABLE A BASE SCHEDULE. `DEACTIVATION_SURFACES`
    holds the reconciliation tool alone, so the deny-by-default rule means a
    base `trips_sync` row cannot be switched off through any reviewed surface at
    all. That is not an omission: the base cadence is what advances the
    watermark, and turning it off silently is the failure mode the coverage
    model cannot detect.

    Platform migrations `032` and `046` insert Eco Driving schedule rows only,
    always disabled, and never touch `trips_sync`. No other module in the
    repository issues an INSERT or an `enabled` UPDATE against
    `client_dataset_schedule`.

    Because that enumeration is exactly what a future change is most likely to
    break, the registry is **deny by default**: a surface that has not been
    reviewed and registered here cannot pass these checks at all, so adding an
    unregistered mutation path fails loudly rather than silently inheriting an
    exemption. `ops/tests_manual/test_telematics_schedule_activation_postgres.py`
    additionally proves that no unregistered module issues such a statement, in
    `test_no_unregistered_module_mutates_schedule_enablement`, which scans every
    non-test module in the repository.

WHAT THIS MODULE NEVER DOES.
    It opens no connection, reads no configuration and writes nothing. It is a
    pure policy oracle its callers consult before their own write.
"""
from __future__ import annotations

from datetime import time
from typing import Any, Dict, Mapping, Optional

TRIPS_SYNC_DATASET_NAME = "trips_sync"

#: The one coverage bootstrap status a fire may run against. RESTATED here
#: rather than imported, deliberately: the pure coverage-window helper has
#: exactly one authorized production importer — the dispatcher — and that count
#: is asserted by
#: `ops/tests_manual/test_telematics_trips_stabilization_windows.py`
#: (`test_runtime_non_activation`). Importing it here would have made this
#: policy oracle a second one. `coverage_finalization.py` restates it for the
#: same reason. Drift between the definitions is caught by a test, not hidden
#: by an import: see
#: `ops/tests_manual/test_telematics_m6_weekly_reconciliation.py`
#: (`test_the_restated_coverage_status_has_not_drifted`).
COVERAGE_STATUS_READY = "READY"

TRIPS_PAGINATION_MODE_STRICT_META = "strict_meta"
TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 = "data_invariants_v1"

#: The only reviewed surface that may *create* a schedule row.
SURFACE_ONBOARDING = "scripts/onboard_workflow_a_client.py"

#: The only reviewed surface that may flip a BASE schedule `enabled` false ->
#: true. It is the terminal step of the cold-start state machine.
SURFACE_ACTIVATE_TRIPS_SCHEDULE = "ops/activate_telematics_trips_schedule.py"

#: The only reviewed surface for the RECONCILIATION lifecycle (M6/M7): it both
#: creates the row (disabled) and, as a separate confirmed operation, enables
#: it. Registered in both sets for that reason.
SURFACE_RECONCILIATION_SCHEDULE = (
    "ops/manage_telematics_reconciliation_schedule.py"
)

CREATION_SURFACES = frozenset({
    SURFACE_ONBOARDING,
    SURFACE_RECONCILIATION_SCHEDULE,
})
ACTIVATION_SURFACES = frozenset({
    SURFACE_ACTIVATE_TRIPS_SCHEDULE,
    SURFACE_RECONCILIATION_SCHEDULE,
})

#: The only reviewed surface that may flip a schedule `enabled` true -> false.
#: A THIRD class rather than a reuse of `ACTIVATION_SURFACES`, and the asymmetry
#: is the point: `ops/activate_telematics_trips_schedule.py` is registered to
#: ACTIVATE a base schedule and is deliberately NOT registered here, because
#: disabling a base `trips_sync` row stops the only cadence that carries the
#: watermark forward and is not a supported operation on any surface. Collapsing
#: the two classes would have granted that authority by accident.
#:
#: Deactivation is also the one direction that does not need the activation
#: preconditions: it reduces execution capability rather than granting it, so a
#: client whose base schedule is disabled, whose coverage is not READY or whose
#: account is switched off may still have its reconciliation cadence turned off.
#: Requiring the activation gates here would mean the states most in need of a
#: reversal are the ones that cannot get one.
DEACTIVATION_SURFACES = frozenset({
    SURFACE_RECONCILIATION_SCHEDULE,
})
REGISTERED_SURFACES = (
    CREATION_SURFACES | ACTIVATION_SURFACES | DEACTIVATION_SURFACES
)

#: Datasets whose schedule must be created disabled without exception. A
#: `trips_sync` schedule that is enabled at creation time skips every state of
#: the onboarding state machine at once.
CREATE_DISABLED_ONLY_DATASETS = frozenset({TRIPS_SYNC_DATASET_NAME})


# ---------------------------------------------------------------------------
# M5 — the schedule role vocabulary (migration 062)
# ---------------------------------------------------------------------------
#
# `run_type` answers "what part does this schedule play for its dataset"; it is
# ORTHOGONAL to `frequency`, which answers "how often does it fire". A base
# schedule may legitimately be daily, weekly or monthly — migrations 032 and 046
# already seed weekly base schedules for the Eco datasets — so the vocabulary
# must never be inferred from, or collapsed into, the cadence.
#
# The vocabulary lives here rather than in the dispatcher because it is a
# *schedule invariant*, and this module is already the deny-by-default owner of
# those. Keeping it in one place is what stops the database CHECK, the dispatcher
# projection and the creation surfaces from drifting into three vocabularies.

#: The single base ingestion schedule for a (client, dataset). It is the row
#: that carries forward coverage, and exactly one may exist per dataset.
SCHEDULE_RUN_TYPE_BASE = "DAILY"

#: Additive reconciliation passes. Registered by M6/M7; no such row exists yet.
SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION = "WEEKLY_RECONCILIATION"
SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION = "MONTHLY_RECONCILIATION"

SCHEDULE_RUN_TYPES = frozenset({
    SCHEDULE_RUN_TYPE_BASE,
    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
    SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION,
})

#: Cadence required by each reconciliation role. The base role is deliberately
#: absent: it is unconstrained, and adding it here would forbid the weekly Eco
#: base schedules that already exist in production.
SCHEDULE_RUN_TYPE_REQUIRED_FREQUENCY = {
    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION: "weekly",
    SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION: "monthly",
}

#: Every role that is a reconciliation pass rather than the base ingestion
#: schedule. Derived from the vocabulary rather than restated, so a fourth role
#: cannot be added without landing here too.
SCHEDULE_RECONCILIATION_RUN_TYPES = frozenset(
    SCHEDULE_RUN_TYPES - {SCHEDULE_RUN_TYPE_BASE}
)

#: Datasets for which a reconciliation cadence is meaningful at all. A
#: reconciliation pass exists to re-request a *coverage-bearing* history, so a
#: dataset with no watermark has nothing for it to reconcile. Kept as a set
#: rather than a hard-coded equality so M7, or a future coverage-bearing
#: dataset, extends it in one visible place.
RECONCILIATION_ELIGIBLE_DATASETS = frozenset({TRIPS_SYNC_DATASET_NAME})


# ---------------------------------------------------------------------------
# M6 — deriving a reconciliation schedule from its base schedule
# ---------------------------------------------------------------------------
#
# WHY THIS PARTITION EXISTS.
#     `client_dataset_schedule` has column defaults that are correct for
#     onboarding a brand-new client and WRONG for deriving a second cadence over
#     a client that is already live. The concrete case that motivated it:
#     `event_enrichment_mode` defaults to `'enabled'`, but ALPHA00001's base
#     schedule carries `'disabled'`. A reconciliation row created by naming only
#     the cadence columns would silently start issuing `/vehicles/events`
#     requests that the base schedule deliberately does not make.
#
#     So every column is classified exactly once, and the three classes are
#     asserted disjoint and exhaustive against the live table by
#     `ops/tests_manual/test_telematics_m6_weekly_reconciliation.py`. Adding a
#     column to the table without classifying it here fails that test rather
#     than silently inheriting a default.
#
#     `timezone` is DECLARED, not INHERITED, and that is deliberate: ALPHA's base
#     schedule is `UTC` while the approved M6 cadence is `Europe/Warsaw`. The
#     cadence owns when it fires; the client owns how its data is fetched.

#: Owner identity. Copied from the base row so a reconciliation cadence can
#: never be created against a different client, dataset or client_code than the
#: schedule whose watermark it will share.
SCHEDULE_IDENTITY_FIELDS = ("client_id", "client_code", "dataset_name")

#: Behaviour that belongs to the client/dataset rather than to the cadence.
#: Copied from the base row; never defaulted, never supplied by the caller.
SCHEDULE_INHERITED_FIELDS = ("overwrite_existing", "event_enrichment_mode")

#: The cadence and role. Supplied explicitly by the reviewed surface.
SCHEDULE_DECLARED_FIELDS = (
    "frequency",
    "day_of_week",
    "day_of_month",
    "day_of_month_last",
    "run_time",
    "timezone",
    "lookback_days",
    "run_type",
)

#: Owned by the database or by the creation contract itself. `enabled` is here
#: because a reconciliation row is ALWAYS created disabled — enabling it is a
#: separate, separately confirmed operation.
SCHEDULE_GENERATED_FIELDS = (
    "schedule_id",
    "enabled",
    "created_at",
    "updated_at",
)

#: The exact column set this derivation writes, in a stable order.
RECONCILIATION_INSERT_FIELDS = (
    SCHEDULE_IDENTITY_FIELDS + SCHEDULE_INHERITED_FIELDS
    + SCHEDULE_DECLARED_FIELDS
)


def _require_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleMutationRefused(
            "RECONCILIATION_PARAMETER_INVALID",
            f"{label} must be an integer; got {value!r}",
        )
    if not (minimum <= value <= maximum):
        raise ScheduleMutationRefused(
            "RECONCILIATION_PARAMETER_INVALID",
            f"{label} must be within [{minimum}, {maximum}]; got {value!r}",
        )
    return value


def assert_reconciliation_cadence_fields(
    *,
    run_type: str,
    frequency: object,
    day_of_week: object,
    day_of_month: object,
    day_of_month_last: object,
) -> None:
    """Refuse a cadence the dispatcher could not compute a fire for.

    `dispatcher.latest_scheduled_fire_local` returns ``None`` — silently, with
    no error and no run — for a weekly row with a NULL `day_of_week` or a
    monthly row with neither `day_of_month` nor `day_of_month_last`. A schedule
    that can never fire is the worst outcome available here, because it looks
    healthy in every listing while reconciling nothing. Refusing at creation is
    the only place this is cheap to catch.
    """
    role = validate_schedule_run_type(run_type)
    assert_run_type_cadence_coherent(run_type=role, frequency=frequency)
    last = bool(day_of_month_last)

    if role == SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION:
        _require_int(day_of_week, label="day_of_week", minimum=0, maximum=6)
        if day_of_month is not None or last:
            raise ScheduleMutationRefused(
                "RECONCILIATION_CADENCE_FIELDS_INVALID",
                "a weekly cadence must not carry day_of_month or "
                "day_of_month_last",
            )
        return

    if role == SCHEDULE_RUN_TYPE_MONTHLY_RECONCILIATION:
        if day_of_week is not None:
            raise ScheduleMutationRefused(
                "RECONCILIATION_CADENCE_FIELDS_INVALID",
                "a monthly cadence must not carry day_of_week",
            )
        if last:
            if day_of_month is not None:
                raise ScheduleMutationRefused(
                    "RECONCILIATION_CADENCE_FIELDS_INVALID",
                    "day_of_month_last and an explicit day_of_month are "
                    "mutually exclusive",
                )
            return
        # 28 is the dispatcher's own safe ceiling: a higher day silently skips
        # February, which is a missed reconciliation nobody would notice.
        _require_int(day_of_month, label="day_of_month", minimum=1, maximum=28)
        return

    raise ScheduleMutationRefused(
        "RECONCILIATION_ROLE_REQUIRED",
        f"{role!r} is the base ingestion role, not a reconciliation pass; "
        f"expected one of {sorted(SCHEDULE_RECONCILIATION_RUN_TYPES)}",
    )


def assert_reconciliation_creation_permitted(
    *,
    surface: str,
    dataset_name: str,
    run_type: str,
    enabled: bool,
) -> None:
    """Deny-by-default entry gate for creating a reconciliation cadence.

    Narrower than `assert_creation_permitted` on purpose: only the one
    registered reconciliation surface may pass, onboarding may not, the dataset
    must be coverage-bearing, the role must be a reconciliation pass, and the
    row must be created disabled without exception.
    """
    assert_registered_surface(
        surface, allowed={SURFACE_RECONCILIATION_SCHEDULE}
    )
    role = validate_schedule_run_type(run_type)
    if role not in SCHEDULE_RECONCILIATION_RUN_TYPES:
        raise ScheduleMutationRefused(
            "RECONCILIATION_ROLE_REQUIRED",
            f"{role!r} is the base ingestion role; the reconciliation surface "
            "must never create or replace a base schedule",
        )
    if dataset_name not in RECONCILIATION_ELIGIBLE_DATASETS:
        raise ScheduleMutationRefused(
            "RECONCILIATION_DATASET_NOT_ELIGIBLE",
            f"{dataset_name!r} is not a coverage-bearing dataset; a "
            "reconciliation cadence has no watermark to reconcile against. "
            f"Eligible: {sorted(RECONCILIATION_ELIGIBLE_DATASETS)}",
        )
    if enabled:
        raise ScheduleMutationRefused(
            "SCHEDULE_CREATION_REFUSED_ENABLED",
            "a reconciliation schedule must be created disabled; enabling it "
            "is a separate reviewed operation with its own preconditions",
        )


def assert_reconciliation_activation_permitted(
    *,
    surface: str,
    dataset_name: str,
    run_type: str,
    pagination_mode: Optional[str],
    base_schedule_enabled: object,
    coverage_bootstrap_status: Optional[str],
) -> None:
    """The preconditions for enabling a reconciliation cadence.

    Deliberately different from the cold-start activation gate. A reconciliation
    pass is additive to a client that is ALREADY running: it shares the base
    schedule's watermark, so the base must exist and be enabled, and the
    watermark must be a usable `READY` claim. Enabling a reconciliation cadence
    for a client whose base is off would create a cadence that advances a
    watermark nothing else maintains.
    """
    assert_registered_surface(
        surface, allowed={SURFACE_RECONCILIATION_SCHEDULE}
    )
    role = validate_schedule_run_type(run_type)
    if role not in SCHEDULE_RECONCILIATION_RUN_TYPES:
        raise ScheduleMutationRefused(
            "RECONCILIATION_ROLE_REQUIRED",
            f"{role!r} is the base ingestion role; base activation belongs to "
            f"{SURFACE_ACTIVATE_TRIPS_SCHEDULE}",
        )
    if dataset_name not in RECONCILIATION_ELIGIBLE_DATASETS:
        raise ScheduleMutationRefused(
            "RECONCILIATION_DATASET_NOT_ELIGIBLE",
            f"{dataset_name!r} is not a coverage-bearing dataset",
        )
    mode = str(pagination_mode or "")
    if mode != TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1:
        raise ScheduleMutationRefused(
            "SCHEDULE_ACTIVATION_REFUSED_STRICT_META",
            f"{dataset_name} activation requires pagination mode "
            f"{TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1!r}; the client is "
            f"{mode!r}",
        )
    if not bool(base_schedule_enabled):
        raise ScheduleMutationRefused(
            "RECONCILIATION_ACTIVATION_REFUSED_BASE_DISABLED",
            "the base schedule for this client and dataset is not enabled; a "
            "reconciliation cadence is additive to a running base schedule, "
            "never a substitute for one",
        )
    status = str(coverage_bootstrap_status or "")
    if status != COVERAGE_STATUS_READY:
        raise ScheduleMutationRefused(
            "RECONCILIATION_ACTIVATION_REFUSED_COVERAGE_NOT_READY",
            "the shared coverage row is "
            f"{status or 'absent'}, not {COVERAGE_STATUS_READY}; a "
            "reconciliation fire would be refused by the coverage gate anyway",
        )


def assert_reconciliation_deactivation_permitted(
    *,
    surface: str,
    dataset_name: str,
    run_type: str,
) -> None:
    """The preconditions for returning a reconciliation cadence to disabled.

    DELIBERATELY NARROWER THAN ACTIVATION IN WHAT IT ADDRESSES, AND WEAKER IN
    WHAT IT DEMANDS.

    Narrower: only a reconciliation role on a reconciliation-eligible dataset
    may pass, so a base (`DAILY`) row can never be reached through the disable
    path — not even when the operator names it explicitly. That is the whole
    DAILY-protection invariant at the policy layer; the tool then repeats it as
    a predicate in the UPDATE itself.

    Weaker: it asks nothing about the base schedule's `enabled`, the coverage
    bootstrap status, the pagination mode or the client account. Every one of
    those exists to stop a cadence from *starting* to run. Disabling removes
    execution capability, so gating it on them would make the reversal
    unavailable in precisely the degraded states an operator most needs it —
    a client whose base schedule was just switched off, or whose watermark is
    no longer READY. The reversal must not depend on the health of the thing
    it is reversing.
    """
    assert_registered_surface(surface, allowed=DEACTIVATION_SURFACES)
    role = validate_schedule_run_type(run_type)
    if role not in SCHEDULE_RECONCILIATION_RUN_TYPES:
        raise ScheduleMutationRefused(
            "RECONCILIATION_ROLE_REQUIRED",
            f"{role!r} is the base ingestion role; disabling a base schedule "
            "is not a supported operation on any registered surface, because "
            "it would stop the only cadence that carries the watermark",
        )
    if dataset_name not in RECONCILIATION_ELIGIBLE_DATASETS:
        raise ScheduleMutationRefused(
            "RECONCILIATION_DATASET_NOT_ELIGIBLE",
            f"{dataset_name!r} is not a coverage-bearing dataset; no "
            "reconciliation cadence exists here to disable",
        )


def derive_reconciliation_schedule(
    *,
    base_row: Mapping[str, Any],
    run_type: str,
    frequency: str,
    run_time: object,
    timezone_name: str,
    lookback_days: int,
    day_of_week: Optional[int] = None,
    day_of_month: Optional[int] = None,
    day_of_month_last: bool = False,
) -> Dict[str, Any]:
    """Project a base schedule row into the reconciliation row to be INSERTed.

    Pure: no connection, no clock, no configuration. Returns exactly
    `RECONCILIATION_INSERT_FIELDS`, so a caller cannot accidentally omit an
    inherited column and let the table default fill it in.
    """
    if not isinstance(base_row, Mapping):
        raise ScheduleMutationRefused(
            "RECONCILIATION_BASE_SCHEDULE_INVALID",
            f"base_row must be a mapping; got {type(base_row).__name__}",
        )

    base_role = str(base_row.get("run_type") or "").strip()
    if base_role != SCHEDULE_RUN_TYPE_BASE:
        raise ScheduleMutationRefused(
            "RECONCILIATION_BASE_NOT_BASE_ROLE",
            "a reconciliation cadence must be derived from the base schedule; "
            f"the supplied row carries run_type {base_role!r}",
        )

    missing = [
        name
        for name in SCHEDULE_IDENTITY_FIELDS + SCHEDULE_INHERITED_FIELDS
        if name not in base_row
    ]
    if missing:
        raise ScheduleMutationRefused(
            "RECONCILIATION_BASE_FIELD_MISSING",
            "the base schedule row is missing field(s) whose value must be "
            f"inherited rather than defaulted: {missing}",
        )
    for name in SCHEDULE_INHERITED_FIELDS:
        if base_row[name] is None:
            raise ScheduleMutationRefused(
                "RECONCILIATION_BASE_FIELD_MISSING",
                f"base schedule field {name!r} is NULL; it is NOT NULL in the "
                "table and must be inherited, never defaulted",
            )

    role = validate_schedule_run_type(run_type)
    assert_reconciliation_cadence_fields(
        run_type=role,
        frequency=frequency,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        day_of_month_last=day_of_month_last,
    )
    lookback = _require_int(
        lookback_days, label="lookback_days", minimum=1, maximum=366
    )
    if not isinstance(run_time, time):
        raise ScheduleMutationRefused(
            "RECONCILIATION_PARAMETER_INVALID",
            f"run_time must be a datetime.time; got {run_time!r}",
        )
    if run_time.tzinfo is not None:
        raise ScheduleMutationRefused(
            "RECONCILIATION_PARAMETER_INVALID",
            "run_time must be a naive local time; the column is TIME WITHOUT "
            "TIME ZONE and the zone is carried by `timezone`",
        )
    zone = str(timezone_name or "").strip()
    if not zone:
        raise ScheduleMutationRefused(
            "RECONCILIATION_PARAMETER_INVALID",
            "timezone is required; it is NOT NULL and its default (UTC) is a "
            "silent cadence shift",
        )

    derived: Dict[str, Any] = {
        "client_id": base_row["client_id"],
        "client_code": base_row["client_code"],
        "dataset_name": base_row["dataset_name"],
        "overwrite_existing": bool(base_row["overwrite_existing"]),
        "event_enrichment_mode": str(base_row["event_enrichment_mode"]),
        "frequency": str(frequency),
        "day_of_week": day_of_week,
        "day_of_month": day_of_month,
        "day_of_month_last": bool(day_of_month_last),
        "run_time": run_time,
        "timezone": zone,
        "lookback_days": lookback,
        "run_type": role,
    }
    # Structural, not decorative: if a field is ever added to one tuple and not
    # to the projection, this is where it stops.
    if tuple(derived) != RECONCILIATION_INSERT_FIELDS:
        raise ScheduleMutationRefused(
            "RECONCILIATION_PROJECTION_INCOMPLETE",
            "the derived row does not match RECONCILIATION_INSERT_FIELDS; "
            f"got {tuple(derived)} expected {RECONCILIATION_INSERT_FIELDS}",
        )
    return derived


def validate_schedule_run_type(value: object) -> str:
    """Return the canonical `run_type`, or fail closed on anything else.

    Fail-closed rather than defaulting: a row whose role the control plane does
    not recognize must stop the tick, exactly as an unknown dataset or an
    out-of-contract pagination mode already does. Silently treating it as the
    base schedule would be the one mistake that can produce two base schedules
    for one dataset and therefore two claims on one watermark.
    """
    text = str(value or "").strip()
    if text not in SCHEDULE_RUN_TYPES:
        raise ScheduleMutationRefused(
            "SCHEDULE_RUN_TYPE_UNKNOWN",
            f"{text!r} is not a known schedule run_type; expected one of "
            f"{sorted(SCHEDULE_RUN_TYPES)}",
        )
    return text


def assert_run_type_cadence_coherent(*, run_type: str, frequency: object) -> None:
    """Refuse a reconciliation role whose cadence contradicts it.

    Mirrors `ck_client_dataset_schedule_run_type_cadence` (migration 062) so the
    contradiction is refused by the application before it reaches the database,
    and so the two definitions are visibly the same rule rather than two rules
    that happen to agree today.
    """
    role = validate_schedule_run_type(run_type)
    required = SCHEDULE_RUN_TYPE_REQUIRED_FREQUENCY.get(role)
    if required is None:
        return
    observed = str(frequency or "").strip()
    if observed != required:
        raise ScheduleMutationRefused(
            "SCHEDULE_RUN_TYPE_CADENCE_INCOHERENT",
            f"run_type {role!r} requires frequency {required!r}; got "
            f"{observed!r}",
        )


class ScheduleMutationRefused(RuntimeError):
    """A refused schedule creation or activation. Stable, sanitized."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def assert_registered_surface(surface: str, *, allowed) -> None:
    """Deny by default. An unreviewed surface is refused before anything else."""
    if surface not in REGISTERED_SURFACES:
        raise ScheduleMutationRefused(
            "SCHEDULE_MUTATION_SURFACE_UNREGISTERED",
            f"{surface!r} is not a registered schedule-mutation surface; a new "
            "mutation path must be reviewed and added to "
            "jobs.api.telematics.schedule_mutation_surfaces before it may create "
            "or enable a schedule",
        )
    if surface not in allowed:
        raise ScheduleMutationRefused(
            "SCHEDULE_MUTATION_SURFACE_NOT_PERMITTED",
            f"{surface!r} is registered but not permitted to perform this "
            "mutation",
        )


def assert_creation_permitted(
    *,
    surface: str,
    dataset_name: str,
    enabled: bool,
) -> None:
    """Refuse any schedule creation that would produce an enabled `trips_sync`.

    Called by onboarding immediately before its INSERT, so the intended creation
    contract is refused before any statement runs. This validates the caller's
    declared intent, not the bytes the statement carries — onboarding's INSERT
    writes a disabled schedule through an SQL literal, and what proves the stored
    value is its post-commit read-back.
    """
    assert_registered_surface(surface, allowed=CREATION_SURFACES)
    if dataset_name in CREATE_DISABLED_ONLY_DATASETS and enabled:
        raise ScheduleMutationRefused(
            "SCHEDULE_CREATION_REFUSED_ENABLED",
            f"{dataset_name} must be created disabled; a newly onboarded client "
            "is not production-ready and reaches an enabled schedule only "
            "through the reviewed cold-start state machine",
        )


def assert_activation_permitted(
    *,
    surface: str,
    dataset_name: str,
    pagination_mode: Optional[str],
) -> None:
    """The narrowest technical prevention of an active strict_meta schedule.

    A `strict_meta` client has no coverage semantics at all: no watermark is
    maintained for it, so a dispatcher fire cannot be reconciled against one and
    a gap cannot be detected. Enabling its `trips_sync` schedule therefore
    creates a client that appears to be running while nothing observes whether
    it is complete — which is exactly the shape of failure this hardening
    exists to prevent.

    Every supported activation path must call this before its UPDATE.
    """
    assert_registered_surface(surface, allowed=ACTIVATION_SURFACES)
    if dataset_name != TRIPS_SYNC_DATASET_NAME:
        return
    mode = str(pagination_mode or "")
    if mode != TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1:
        raise ScheduleMutationRefused(
            "SCHEDULE_ACTIVATION_REFUSED_STRICT_META",
            f"{TRIPS_SYNC_DATASET_NAME} activation requires pagination mode "
            f"{TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1!r}; the client is "
            f"{mode!r}, and an active {TRIPS_SYNC_DATASET_NAME} schedule paired "
            "with strict_meta is never an approved production state",
        )
