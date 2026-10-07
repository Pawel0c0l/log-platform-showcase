#!/usr/bin/env python3
"""Dry-run-first lifecycle tool for Workflow A RECONCILIATION cadences (M6/M7).

Specification of record:
  docs/20_telematics_ingestion_permanent_repair_plan.md §3.1 (the cadence table),
    §3.7c (why the weekly lookback is `L = 16`), §7 (how the cadences interact),
    §24 (the M6 delivery record)
  jobs/api/telematics/schedule_mutation_surfaces.py (the deny-by-default policy
    oracle this tool consults before every write)
  db/migrations/062_workflow_a_multi_cadence_schedule_identity.sql (the physical
    contract: `run_type`, the re-keyed uniqueness, the base partial index)

This is the **only** registered surface for the reconciliation lifecycle, and it
is deliberately narrow:

* it never creates, modifies, enables or disables a **base** schedule. The base
  role belongs to onboarding and to `ops/activate_telematics_trips_schedule.py`;
* it never writes `client_dataset_coverage`, `client_schedule_run_history`,
  `client_dataset_recovery_run` or `client_account`;
* it never DELETEs anything. `disable` is the reversal of `enable`, not of
  `register`: the schedule row and every run-history row it owns survive it,
  and both are re-read after the write to prove they did;
* it never contacts a provider, never launches a subprocess and never restarts
  anything;
* **registration, activation and deactivation are three separate subcommands**,
  each with its own `--execute` and its own `--confirm-client-code`. A
  reconciliation row is always created disabled; enabling it is a second
  deliberate act with its own preconditions; returning it to disabled is a
  third;
* every inherited column is copied from the base schedule through the pure
  projection `derive_reconciliation_schedule`, never left to a table default.
  That is not decoration: `event_enrichment_mode` defaults to `'enabled'` while
  ALPHA00001's base schedule carries `'disabled'`, so a row built from the
  cadence columns alone would silently start issuing `/vehicles/events`.

Nothing here dispatches. A registered, enabled row is picked up by the existing
dispatcher on its own cadence, through the same claim, coverage gate, M3
outcome verification and M4 completeness evidence as every other schedule — the
dispatcher does not branch on `run_type` and this tool does not ask it to.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/manage_telematics_reconciliation_schedule.py \\
        register --client-code ALPHA00001 \\
        --expected-environment production \\
        --expected-platform-uuid <uuid>

    # ... review the plan, then:
    PYTHONPATH="$PWD" python3 ops/manage_telematics_reconciliation_schedule.py \\
        register --client-code ALPHA00001 \\
        --expected-environment production \\
        --expected-platform-uuid <uuid> \\
        --approval-ref OPS-1234 --execute --confirm-client-code ALPHA00001

    # ... and the reversal, which carries exactly the same gates:
    PYTHONPATH="$PWD" python3 ops/manage_telematics_reconciliation_schedule.py \\
        disable --client-code ALPHA00001 \\
        --expected-environment production \\
        --expected-platform-uuid <uuid> \\
        --approval-ref OPS-1234 --execute --confirm-client-code ALPHA00001
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    RECONCILIATION_INSERT_FIELDS,
    SCHEDULE_RUN_TYPE_BASE,
    SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION,
    SURFACE_RECONCILIATION_SCHEDULE,
    ScheduleMutationRefused,
    TRIPS_SYNC_DATASET_NAME,
    assert_reconciliation_activation_permitted,
    assert_reconciliation_creation_permitted,
    assert_reconciliation_deactivation_permitted,
    derive_reconciliation_schedule,
)
from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    AuditError,
    _load_dotenv,
    canonical_uuid,
    platform_dsn_from_env,
    verify_platform_identity,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
EXIT_WRITE_CONFLICT = 6
EXIT_POSTWRITE_VERIFICATION_FAILED = 7

#: `run_type` and the re-keyed uniqueness both arrive with 062. Without it the
#: INSERT would either fail on an unknown column or, worse, land against the
#: pre-M5 two-column unique constraint.
MIGRATION_CEILING = "062_workflow_a_multi_cadence_schedule_identity.sql"

#: The approved M6 contract (docs/20 §3.1, §3.7c). These are argparse defaults
#: rather than hard-coded constants so the same tool serves M7, but an operator
#: who supplies nothing gets exactly the reviewed weekly shape.
M6_RUN_TYPE = SCHEDULE_RUN_TYPE_WEEKLY_RECONCILIATION
M6_FREQUENCY = "weekly"
M6_DAY_OF_WEEK = 0            # Monday, Python weekday() convention
M6_RUN_TIME = "00:30"
M6_TIMEZONE = "Europe/Warsaw"
M6_LOOKBACK_DAYS = 16

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")


class ReconciliationRefused(RuntimeError):
    """A refused reconciliation lifecycle operation. Stable and sanitized."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> ReconciliationRefused:
    return ReconciliationRefused(code, message, exit_code)


def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "INVALID_PARAMETER",
            f"{label} must match {SAFE_TOKEN_RE.pattern}",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def parse_run_time(raw: object) -> time:
    text = str(raw or "").strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise _refuse(
            "INVALID_PARAMETER",
            f"--run-time must be an ISO local time such as 00:30; got {raw!r}",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.microsecond:
        raise _refuse(
            "INVALID_PARAMETER",
            "--run-time must not carry sub-second precision",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


def verify_timezone(name: str) -> str:
    """A bad zone makes `evaluate_schedule` return None — silently, forever."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    text = str(name or "").strip()
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise _refuse(
            "INVALID_PARAMETER",
            f"--timezone {text!r} is not a known IANA zone; the dispatcher "
            "would compute no fire at all for it",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    return text


# ---------------------------------------------------------------------------
# Read-side helpers. All of these run inside the write transaction so the plan
# and the write see the same state.
# ---------------------------------------------------------------------------

def verify_migration_ceiling(cur) -> None:
    cur.execute("SELECT filename FROM public.schema_migrations ORDER BY filename")
    applied = {str(row["filename"]) for row in cur.fetchall()}
    if MIGRATION_CEILING not in applied:
        raise _refuse(
            "MIGRATION_CEILING_MISSING",
            f"{MIGRATION_CEILING} is not applied to this database; the "
            "reconciliation role is not representable here",
        )


def resolve_client(cur, client_code: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code, enabled,
               trips_pagination_mode
          FROM workflow_a_control.client_account
         WHERE client_code = %s
        """,
        (client_code,),
    )
    rows = cur.fetchall()
    if not rows:
        raise _refuse("CLIENT_NOT_FOUND", f"no client_account for {client_code!r}")
    if len(rows) > 1:
        raise _refuse(
            "CLIENT_AMBIGUOUS", f"{len(rows)} client_account rows for {client_code!r}"
        )
    return dict(rows[0])


def _schedule_by_role(
    cur, *, client_id: str, dataset_name: str, run_type: str
) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id,
               client_id::text   AS client_id,
               client_code, dataset_name, enabled, frequency,
               day_of_week, day_of_month, day_of_month_last,
               run_time, timezone, lookback_days, overwrite_existing,
               event_enrichment_mode, run_type, created_at, updated_at
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s AND run_type = %s
        """,
        (client_id, dataset_name, run_type),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # `uq_client_dataset_schedule (client_id, dataset_name, run_type)` makes
        # this impossible; if it happens the schema is not what we think it is.
        raise _refuse(
            "SCHEDULE_AMBIGUOUS",
            f"{len(rows)} schedules for run_type={run_type!r}; the M5 "
            "uniqueness constraint is not in force",
            EXIT_RUNTIME_FAILURE,
        )
    return dict(rows[0])


def resolve_base_schedule(cur, *, client_id: str, dataset_name: str) -> Dict[str, Any]:
    base = _schedule_by_role(
        cur, client_id=client_id, dataset_name=dataset_name,
        run_type=SCHEDULE_RUN_TYPE_BASE,
    )
    if base is None:
        raise _refuse(
            "BASE_SCHEDULE_ABSENT",
            f"no base ({SCHEDULE_RUN_TYPE_BASE}) schedule exists for this "
            "client and dataset; a reconciliation cadence has no base to be "
            "additive to, and no watermark anchor to share",
        )
    return base


def read_coverage(cur, *, client_id: str, dataset_name: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, bootstrap_status,
               covered_through_ts, coverage_start_ts
          FROM workflow_a_control.client_dataset_coverage
         WHERE client_id = %s AND dataset_name = %s
        """,
        (client_id, dataset_name),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise _refuse(
            "COVERAGE_AMBIGUOUS",
            "more than one coverage row for this (client, dataset)",
            EXIT_RUNTIME_FAILURE,
        )
    return dict(rows[0])


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

#: The approved M6 cadence, applied only when the role is the weekly one and
#: only to arguments the operator did not supply. Every other role must state
#: its cadence in full — there is no approved shape to fall back on.
M6_CADENCE_DEFAULTS = {
    "frequency": M6_FREQUENCY,
    "day_of_week": M6_DAY_OF_WEEK,
    "run_time": M6_RUN_TIME,
    "timezone": M6_TIMEZONE,
    "lookback_days": M6_LOOKBACK_DAYS,
}


def apply_role_defaults(args) -> None:
    """Fill the reviewed weekly cadence, or require the caller to state one."""
    if args.run_type == M6_RUN_TYPE:
        for name, value in M6_CADENCE_DEFAULTS.items():
            if getattr(args, name) is None:
                setattr(args, name, value)
        return
    missing = [
        name
        for name in ("frequency", "run_time", "timezone", "lookback_days")
        if getattr(args, name) is None
    ]
    if missing:
        raise _refuse(
            "CADENCE_NOT_STATED",
            f"--run-type {args.run_type} has no approved default cadence; "
            f"supply {sorted('--' + n.replace('_', '-') for n in missing)}",
            EXIT_INVALID_PARAMETERS,
        )


def plan_register(cur, args) -> Dict[str, Any]:
    apply_role_defaults(args)
    verify_migration_ceiling(cur)
    client = resolve_client(cur, args.client_code)
    base = resolve_base_schedule(
        cur, client_id=client["client_id"], dataset_name=args.dataset
    )

    run_time = parse_run_time(args.run_time)
    zone = verify_timezone(args.timezone)

    derived = derive_reconciliation_schedule(
        base_row=base,
        run_type=args.run_type,
        frequency=args.frequency,
        run_time=run_time,
        timezone_name=zone,
        lookback_days=int(args.lookback_days),
        day_of_week=args.day_of_week,
        day_of_month=args.day_of_month,
        day_of_month_last=bool(args.day_of_month_last),
    )
    assert_reconciliation_creation_permitted(
        surface=SURFACE_RECONCILIATION_SCHEDULE,
        dataset_name=derived["dataset_name"],
        run_type=derived["run_type"],
        # Declared intent. The INSERT below writes the literal FALSE; this
        # asserts the contract the caller believes it is asking for.
        enabled=False,
    )

    existing = _schedule_by_role(
        cur, client_id=client["client_id"], dataset_name=args.dataset,
        run_type=derived["run_type"],
    )
    coverage = read_coverage(
        cur, client_id=client["client_id"], dataset_name=args.dataset
    )

    return {
        "operation": "register",
        "client_code": client["client_code"],
        "client_id": client["client_id"],
        "dataset": args.dataset,
        "base_schedule_id": base["schedule_id"],
        "base_enabled": bool(base["enabled"]),
        "base_lookback_days": base["lookback_days"],
        "base_event_enrichment_mode": base["event_enrichment_mode"],
        "coverage_bootstrap_status": (
            None if coverage is None else coverage["bootstrap_status"]
        ),
        "already_registered": existing is not None,
        "existing_schedule_id": None if existing is None else existing["schedule_id"],
        "derived_row": {
            key: (value.isoformat() if isinstance(value, time) else value)
            for key, value in derived.items()
        },
        "inherited_from_base": {
            "overwrite_existing": derived["overwrite_existing"],
            "event_enrichment_mode": derived["event_enrichment_mode"],
        },
        "_derived": derived,
        "_client": client,
        "_base": base,
    }


def execute_register(cur, plan: Dict[str, Any]) -> Dict[str, Any]:
    derived = plan["_derived"]
    columns = ", ".join(RECONCILIATION_INSERT_FIELDS)
    placeholders = ", ".join(["%s"] * len(RECONCILIATION_INSERT_FIELDS))
    values = [derived[name] for name in RECONCILIATION_INSERT_FIELDS]

    # `enabled` is written as the SQL literal FALSE, exactly as onboarding does:
    # the constant is the declared intent, the literal is what is stored, and
    # the read-back below is what proves it.
    # Deliberately `.format()` on one string literal rather than an f-string:
    # an f-string is several AST constants, so the static role-explicit audit in
    # `test_telematics_m5_multi_cadence_identity_postgres.py` would see the
    # `INSERT INTO` fragment without the `run_type` that appears further down.
    # Keeping the whole statement as a single literal keeps that audit able to
    # read it. The column list still comes from the audited projection.
    cur.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_schedule
            ({columns}, enabled)
        VALUES ({placeholders}, FALSE)
        ON CONFLICT (client_id, dataset_name, run_type) DO NOTHING
        RETURNING schedule_id::text AS schedule_id
        """.format(columns=columns, placeholders=placeholders),
        values,
    )
    inserted = cur.fetchone()
    if inserted is None:
        raise _refuse(
            "ALREADY_REGISTERED",
            "a schedule with this (client, dataset, run_type) already exists; "
            "this tool never updates an existing row",
            EXIT_WRITE_CONFLICT,
        )

    stored = _schedule_by_role(
        cur, client_id=derived["client_id"], dataset_name=derived["dataset_name"],
        run_type=derived["run_type"],
    )
    if stored is None:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            "the row is not readable on the writing cursor after INSERT",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    if stored["enabled"]:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            "the reconciliation row was stored ENABLED; it must be created "
            "disabled without exception",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    mismatched = {
        name: {"expected": derived[name], "stored": stored[name]}
        for name in RECONCILIATION_INSERT_FIELDS
        if _normalize(stored[name]) != _normalize(derived[name])
    }
    if mismatched:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            f"stored row differs from the derived row: {sorted(mismatched)}",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    return {"schedule_id": stored["schedule_id"], "enabled": False}


def _normalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, time):
        return value.replace(microsecond=0).isoformat()
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------

def plan_enable(cur, args) -> Dict[str, Any]:
    verify_migration_ceiling(cur)
    client = resolve_client(cur, args.client_code)
    base = resolve_base_schedule(
        cur, client_id=client["client_id"], dataset_name=args.dataset
    )
    target = _schedule_by_role(
        cur, client_id=client["client_id"], dataset_name=args.dataset,
        run_type=args.run_type,
    )
    if target is None:
        raise _refuse(
            "RECONCILIATION_SCHEDULE_ABSENT",
            f"no {args.run_type} schedule exists for this client and dataset; "
            "run the `register` subcommand first",
        )
    coverage = read_coverage(
        cur, client_id=client["client_id"], dataset_name=args.dataset
    )

    assert_reconciliation_activation_permitted(
        surface=SURFACE_RECONCILIATION_SCHEDULE,
        dataset_name=target["dataset_name"],
        run_type=target["run_type"],
        pagination_mode=client["trips_pagination_mode"],
        base_schedule_enabled=base["enabled"],
        coverage_bootstrap_status=(
            None if coverage is None else coverage["bootstrap_status"]
        ),
    )
    if not client["enabled"]:
        raise _refuse(
            "CLIENT_DISABLED",
            "the client_account is disabled; the dispatcher would not "
            "enumerate this schedule at all",
        )

    return {
        "operation": "enable",
        "client_code": client["client_code"],
        "client_id": client["client_id"],
        "dataset": args.dataset,
        "run_type": target["run_type"],
        "schedule_id": target["schedule_id"],
        "already_enabled": bool(target["enabled"]),
        "base_schedule_id": base["schedule_id"],
        "base_enabled": bool(base["enabled"]),
        "coverage_bootstrap_status": (
            None if coverage is None else coverage["bootstrap_status"]
        ),
        "cadence": {
            "frequency": target["frequency"],
            "day_of_week": target["day_of_week"],
            "day_of_month": target["day_of_month"],
            "day_of_month_last": target["day_of_month_last"],
            "run_time": str(target["run_time"]),
            "timezone": target["timezone"],
            "lookback_days": target["lookback_days"],
        },
        "event_enrichment_mode": target["event_enrichment_mode"],
        "_target": target,
    }


def execute_enable(cur, plan: Dict[str, Any]) -> Dict[str, Any]:
    target = plan["_target"]
    if target["enabled"]:
        raise _refuse(
            "ALREADY_ENABLED",
            "the reconciliation schedule is already enabled; this tool never "
            "re-writes an unchanged row",
            EXIT_WRITE_CONFLICT,
        )
    cur.execute(
        """
        UPDATE workflow_a_control.client_dataset_schedule
           SET enabled = TRUE, updated_at = now()
         WHERE schedule_id = %s
           AND client_id = %s
           AND dataset_name = %s
           AND run_type = %s
           AND enabled = FALSE
        """,
        (
            target["schedule_id"], target["client_id"],
            target["dataset_name"], target["run_type"],
        ),
    )
    if cur.rowcount != 1:
        raise _refuse(
            "ENABLE_CONFLICT",
            f"the UPDATE affected {cur.rowcount} rows; the row changed under "
            "us and nothing has been enabled",
            EXIT_WRITE_CONFLICT,
        )

    stored = _schedule_by_role(
        cur, client_id=target["client_id"], dataset_name=target["dataset_name"],
        run_type=target["run_type"],
    )
    if stored is None or not stored["enabled"]:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            "the row does not read back enabled",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    # Exactly one field may have changed. `updated_at` is excluded because the
    # statement sets it on purpose.
    changed = [
        name
        for name in (
            "schedule_id", "client_id", "client_code", "dataset_name",
            "frequency", "day_of_week", "day_of_month", "day_of_month_last",
            "run_time", "timezone", "lookback_days", "overwrite_existing",
            "event_enrichment_mode", "run_type",
        )
        if _normalize(stored[name]) != _normalize(target[name])
    ]
    if changed:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            f"enabling changed more than the `enabled` flag: {changed}",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    return {"schedule_id": stored["schedule_id"], "enabled": True}


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS.
#     `register` creates a reconciliation row disabled and `enable` switches it
#     on. Without a reversal, `enable` is a one-way door: the only way back
#     would be an ad-hoc UPDATE by an operator, which is exactly the
#     unregistered mutation path the deny-by-default registry exists to
#     prevent, or a DELETE, which would take the row's history with it.
#
# WHAT IT IS NOT.
#     It is not a cancellation and not a kill switch. It changes which rows a
#     FUTURE dispatcher tick enumerates; see ACTIVE_RUN_SEMANTICS below.
#     It never deletes a schedule row and never touches run history.

#: The contract for a disable issued while a matching reconciliation run is
#: already `RUNNING`. This is the platform's existing behaviour, restated so it
#: is a decided invariant rather than an accident:
#:
#:   * `_load_enabled_schedules` filters `cds.enabled = true` ONCE, at the top
#:     of a dispatcher tick. Everything after that — the claim, the coverage
#:     gate, `_build_job_params`, `_launch_job` and both finalizers — is keyed
#:     on `schedule_id` and on the history row's own status, and no post-claim
#:     statement re-reads `client_dataset_schedule.enabled` at all;
#:   * so a run that has already claimed its fire finishes, finalizes and
#:     writes its coverage decision exactly as it would have; and
#:   * the next tick simply does not enumerate the row.
#:
#: Refusing a disable while a run is active was considered and rejected: a
#: `RUNNING` row can persist for up to `stale_running_timeout_minutes`
#: (720 by default) after a killed dispatcher, and that is precisely the
#: situation in which an operator most needs the cadence switched off. A gate
#: that fails exactly when it is needed is worse than no gate.
ACTIVE_RUN_SEMANTICS = "FUTURE_FIRES_ONLY_RUNNING_FIRE_COMPLETES"

#: Every field a disable must leave byte-identical, on the target row and on the
#: base row alike. `enabled` and `updated_at` are absent on purpose: the first
#: is the one field this operation exists to change, the second is set by the
#: same statement.
SCHEDULE_IMMUTABLE_UNDER_DISABLE = (
    "schedule_id", "client_id", "client_code", "dataset_name",
    "frequency", "day_of_week", "day_of_month", "day_of_month_last",
    "run_time", "timezone", "lookback_days", "overwrite_existing",
    "event_enrichment_mode", "run_type", "created_at",
)


def _run_history_witness(cur, *, schedule_id: Optional[str]) -> Dict[str, Any]:
    """Identity of every run-history row this schedule owns, plus its counts.

    Read before and after the write so "history is preserved" is a checked
    property of the operation rather than a claim about the SQL. Only the row
    IDENTITIES are compared afterwards, deliberately: a concurrent dispatcher
    may legitimately finalize a `RUNNING` row to `SUCCESS` while an operator
    disables the schedule, and that must not fail the disable. What must never
    happen is a row DISAPPEARING, and an identity set catches exactly that.
    """
    if schedule_id is None:
        return {"row_ids": frozenset(), "rows": 0, "running": 0}
    cur.execute(
        """
        SELECT run_history_id::text AS run_history_id, status
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s
        """,
        (schedule_id,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    return {
        "row_ids": frozenset(row["run_history_id"] for row in rows),
        "rows": len(rows),
        "running": sum(1 for row in rows if str(row["status"]) == "RUNNING"),
    }


def _lock_schedule_by_role(
    cur, *, client_id: str, dataset_name: str, run_type: str
) -> Optional[Dict[str, Any]]:
    """`_schedule_by_role` under a row lock, for the write path only.

    The whole projection is re-read rather than just `enabled`, because the
    caller must also prove the row it locked is the row it planned against.
    `NOWAIT` is deliberate: an operator running a reversal interactively should
    be told another writer holds the row, not left blocking on it.
    """
    try:
        cur.execute(
            """
            SELECT schedule_id::text AS schedule_id,
                   client_id::text   AS client_id,
                   client_code, dataset_name, enabled, frequency,
                   day_of_week, day_of_month, day_of_month_last,
                   run_time, timezone, lookback_days, overwrite_existing,
                   event_enrichment_mode, run_type, created_at, updated_at
              FROM workflow_a_control.client_dataset_schedule
             WHERE client_id = %s AND dataset_name = %s AND run_type = %s
               FOR UPDATE NOWAIT
            """,
            (client_id, dataset_name, run_type),
        )
    except Exception as exc:
        if type(exc).__name__ != "LockNotAvailable":
            raise
        raise _refuse(
            "DISABLE_LOCK_UNAVAILABLE",
            "another transaction holds this reconciliation row; nothing has "
            "been disabled. Retry once the other writer has finished",
            EXIT_WRITE_CONFLICT,
        ) from exc
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise _refuse(
            "SCHEDULE_AMBIGUOUS",
            f"{len(rows)} schedules for run_type={run_type!r}; the M5 "
            "uniqueness constraint is not in force",
            EXIT_RUNTIME_FAILURE,
        )
    return dict(rows[0])


def _fingerprint(row: Optional[Dict[str, Any]], fields) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {name: _normalize(row[name]) for name in fields}


def plan_disable(cur, args) -> Dict[str, Any]:
    verify_migration_ceiling(cur)

    # The role/dataset policy is consulted BEFORE the target is resolved, so a
    # disable intent never even reads a base row. `--run-type DAILY` is refused
    # here, one statement before anything could address it.
    assert_reconciliation_deactivation_permitted(
        surface=SURFACE_RECONCILIATION_SCHEDULE,
        dataset_name=args.dataset,
        run_type=args.run_type,
    )

    client = resolve_client(cur, args.client_code)
    target = _schedule_by_role(
        cur, client_id=client["client_id"], dataset_name=args.dataset,
        run_type=args.run_type,
    )
    if target is None:
        raise _refuse(
            "RECONCILIATION_SCHEDULE_ABSENT",
            f"no {args.run_type} schedule exists for this client and dataset; "
            "there is nothing to disable, and this tool never creates a row to "
            "then switch off",
        )

    # The base row is read as an untouched-witness only. Its ABSENCE is not a
    # refusal — an orphaned reconciliation cadence is one of the states most in
    # need of a disable — and its `enabled` value is deliberately not a
    # precondition. Compare `plan_enable`, which requires both.
    base = _schedule_by_role(
        cur, client_id=client["client_id"], dataset_name=args.dataset,
        run_type=SCHEDULE_RUN_TYPE_BASE,
    )
    coverage = read_coverage(
        cur, client_id=client["client_id"], dataset_name=args.dataset
    )
    target_history = _run_history_witness(cur, schedule_id=target["schedule_id"])
    base_history = _run_history_witness(
        cur, schedule_id=None if base is None else base["schedule_id"]
    )

    return {
        "operation": "disable",
        "client_code": client["client_code"],
        "client_id": client["client_id"],
        "dataset": args.dataset,
        "run_type": target["run_type"],
        "schedule_id": target["schedule_id"],
        "currently_enabled": bool(target["enabled"]),
        "already_disabled": not bool(target["enabled"]),
        "would_change": bool(target["enabled"]),
        "fields_to_change": ["enabled", "updated_at"] if target["enabled"] else [],
        "base_schedule_present": base is not None,
        "base_schedule_id": None if base is None else base["schedule_id"],
        "base_enabled": None if base is None else bool(base["enabled"]),
        "client_account_enabled": bool(client["enabled"]),
        "coverage_bootstrap_status": (
            None if coverage is None else coverage["bootstrap_status"]
        ),
        "active_run_semantics": ACTIVE_RUN_SEMANTICS,
        "active_runs": target_history["running"],
        "run_history_rows": target_history["rows"],
        "base_run_history_rows": base_history["rows"],
        "cadence": {
            "frequency": target["frequency"],
            "day_of_week": target["day_of_week"],
            "day_of_month": target["day_of_month"],
            "day_of_month_last": target["day_of_month_last"],
            "run_time": str(target["run_time"]),
            "timezone": target["timezone"],
            "lookback_days": target["lookback_days"],
        },
        "event_enrichment_mode": target["event_enrichment_mode"],
        "schedule_rows_deleted": 0,
        "run_history_rows_deleted": 0,
        "_target": target,
        "_base": base,
        "_target_history": target_history,
        "_base_history": base_history,
    }


def _verify_disable_left_everything_else_alone(cur, plan: Dict[str, Any]) -> None:
    """The invariants that hold whether or not a row was actually written.

    Runs on the idempotent path too, so an "already disabled, nothing done"
    result is a CHECKED no-op rather than an unexamined one.
    """
    target = plan["_target"]
    base = plan["_base"]

    if base is not None:
        base_after = _schedule_by_role(
            cur, client_id=base["client_id"], dataset_name=base["dataset_name"],
            run_type=SCHEDULE_RUN_TYPE_BASE,
        )
        if base_after is None:
            raise _refuse(
                "POSTWRITE_VERIFICATION_FAILED",
                "the base schedule row disappeared during the disable",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )
        before = _fingerprint(
            base, SCHEDULE_IMMUTABLE_UNDER_DISABLE + ("enabled", "updated_at")
        )
        after = _fingerprint(
            base_after,
            SCHEDULE_IMMUTABLE_UNDER_DISABLE + ("enabled", "updated_at"),
        )
        if before != after:
            differing = sorted(
                name for name in before if before[name] != after[name]
            )
            raise _refuse(
                "POSTWRITE_VERIFICATION_FAILED",
                f"the base ({SCHEDULE_RUN_TYPE_BASE}) schedule changed during a "
                f"reconciliation disable: {differing}",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )

    for label, schedule_id, witness in (
        ("target", target["schedule_id"], plan["_target_history"]),
        (
            "base",
            None if base is None else base["schedule_id"],
            plan["_base_history"],
        ),
    ):
        after = _run_history_witness(cur, schedule_id=schedule_id)
        lost = witness["row_ids"] - after["row_ids"]
        if lost:
            raise _refuse(
                "POSTWRITE_VERIFICATION_FAILED",
                f"{len(lost)} {label} run-history row(s) are no longer present "
                "after the disable; history must be preserved in full",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )


def execute_disable(cur, plan: Dict[str, Any]) -> Dict[str, Any]:
    target = plan["_target"]

    # LOCK BEFORE DECIDING. The plan was read earlier in this READ COMMITTED
    # transaction, so `plan["_target"]["enabled"]` is a snapshot, not a fact: a
    # concurrent `enable` may have committed since. Deciding idempotency from
    # the snapshot would let this tool report ALREADY_DISABLED, exit 0, and
    # leave the schedule ENABLED — a false success on the one operation whose
    # entire purpose is to stop future execution. The row lock makes the read
    # and the write one decision.
    locked = _lock_schedule_by_role(
        cur, client_id=target["client_id"], dataset_name=target["dataset_name"],
        run_type=target["run_type"],
    )
    if locked is None:
        raise _refuse(
            "DISABLE_CONFLICT",
            "the reconciliation row disappeared between the plan and the "
            "write; nothing has been disabled",
            EXIT_WRITE_CONFLICT,
        )
    drifted = [
        name
        for name in SCHEDULE_IMMUTABLE_UNDER_DISABLE
        if _normalize(locked[name]) != _normalize(target[name])
    ]
    if drifted:
        raise _refuse(
            "DISABLE_CONFLICT",
            f"the reconciliation row changed under us before the write: "
            f"{drifted}; nothing has been disabled",
            EXIT_WRITE_CONFLICT,
        )

    if not locked["enabled"]:
        # IDEMPOTENT, NOT AN ERROR — and deliberately asymmetric with
        # `execute_enable`, which refuses `ALREADY_ENABLED`. Re-enabling
        # something already on is a request whose intent is ambiguous and whose
        # effect would be to grant capability, so refusing it is cheap. A
        # repeated disable asks for a state the row is already in, in the
        # direction that removes capability; refusing it would tell an operator
        # mid-incident that the reversal "failed" when the desired state holds.
        _verify_disable_left_everything_else_alone(cur, plan)
        return {
            "schedule_id": locked["schedule_id"],
            "enabled": False,
            "changed": False,
            "rows_updated": 0,
            "classification": "ALREADY_DISABLED",
        }

    cur.execute(
        """
        UPDATE workflow_a_control.client_dataset_schedule
           SET enabled = FALSE, updated_at = now()
         WHERE schedule_id = %s
           AND client_id = %s
           AND dataset_name = %s
           AND run_type = %s
           AND run_type <> %s
           AND enabled = TRUE
        """,
        (
            target["schedule_id"], target["client_id"],
            target["dataset_name"], target["run_type"],
            SCHEDULE_RUN_TYPE_BASE,
        ),
    )
    if cur.rowcount != 1:
        # Unreachable while the lock is held above, which is exactly why it is
        # a hard refusal rather than a convergence: reaching it means the row
        # moved under an exclusive lock.
        raise _refuse(
            "DISABLE_CONFLICT",
            f"the UPDATE affected {cur.rowcount} rows while the target row was "
            "locked; nothing may be trusted here and nothing has been disabled",
            EXIT_WRITE_CONFLICT,
        )

    stored = _schedule_by_role(
        cur, client_id=target["client_id"], dataset_name=target["dataset_name"],
        run_type=target["run_type"],
    )
    if stored is None:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            "the reconciliation row is not readable after the UPDATE; a "
            "disable must preserve the row, never remove it",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    if stored["enabled"]:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            "the row does not read back disabled",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    changed = [
        name
        for name in SCHEDULE_IMMUTABLE_UNDER_DISABLE
        if _normalize(stored[name]) != _normalize(target[name])
    ]
    if changed:
        raise _refuse(
            "POSTWRITE_VERIFICATION_FAILED",
            f"disabling changed more than the `enabled` flag: {changed}",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    _verify_disable_left_everything_else_alone(cur, plan)
    return {
        "schedule_id": stored["schedule_id"],
        "enabled": False,
        "changed": True,
        "rows_updated": 1,
        "classification": "DISABLED",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

#: Subcommand -> (planner, executor). A table rather than a chain of
#: conditionals so a third lifecycle operation cannot be half-wired: a
#: subcommand that argparse accepts but this table does not name is refused
#: before a connection is opened.
_PLANNERS = {
    "register": plan_register,
    "enable": plan_enable,
    "disable": plan_disable,
}
_EXECUTORS = {
    "register": execute_register,
    "enable": execute_enable,
    "disable": execute_disable,
}


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=TRIPS_SYNC_DATASET_NAME)
    parser.add_argument("--run-type", default=M6_RUN_TYPE)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument("--approval-ref")
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform the write. Without it the tool reports DRY_RUN only.",
    )
    parser.add_argument(
        "--confirm-client-code",
        help="Second deliberate confirmation; must equal --client-code when "
             "--execute is used.",
    )
    parser.add_argument("--dsn")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first Workflow A reconciliation-cadence lifecycle. "
            "Creates a reconciliation schedule disabled, enables it as a "
            "separate confirmed operation, and returns it to disabled as a "
            "third. Never touches a base schedule, never deletes a row and "
            "never deletes run history."
        ),
    )
    sub = parser.add_subparsers(dest="operation", required=True)

    register = sub.add_parser(
        "register",
        help="Create the reconciliation schedule row, always disabled.",
    )
    _add_common(register)
    # Cadence arguments default to None, NOT to the M6 values. The approved
    # weekly shape is filled in by `apply_role_defaults` and ONLY for the weekly
    # role — otherwise registering an M7 monthly cadence would silently inherit
    # `day_of_week = 0` and be refused as incoherent, which is a fail-closed
    # refusal for the wrong reason.
    register.add_argument("--frequency", default=None)
    register.add_argument("--day-of-week", type=int, default=None)
    register.add_argument("--day-of-month", type=int, default=None)
    register.add_argument("--day-of-month-last", action="store_true")
    register.add_argument("--run-time", default=None)
    register.add_argument("--timezone", default=None)
    register.add_argument("--lookback-days", type=int, default=None)

    enable = sub.add_parser(
        "enable", help="Enable an already-registered reconciliation schedule.",
    )
    _add_common(enable)

    disable = sub.add_parser(
        "disable",
        help=(
            "Return an enabled reconciliation schedule to disabled. Preserves "
            "the row and its history; never touches the base schedule."
        ),
    )
    _add_common(disable)
    return parser


def run(args) -> tuple:
    client_code = _safe_token(args.client_code, label="--client-code")
    dataset = _safe_token(args.dataset, label="--dataset")
    args.client_code = client_code
    args.dataset = dataset
    if args.approval_ref is not None:
        _safe_token(args.approval_ref, label="--approval-ref")

    dry_run = not bool(args.execute)
    if not dry_run:
        if not args.approval_ref:
            raise _refuse(
                "APPROVAL_REF_REQUIRED",
                "--approval-ref is required for a write",
                EXIT_INVALID_PARAMETERS,
            )
        if str(args.confirm_client_code or "") != client_code:
            raise _refuse(
                "CONFIRMATION_MISMATCH",
                "--confirm-client-code must equal --client-code for a write",
                EXIT_INVALID_PARAMETERS,
            )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "DEPENDENCY_MISSING", "psycopg is required", EXIT_RUNTIME_FAILURE
        ) from exc

    try:
        planner = _PLANNERS[args.operation]
        executor = _EXECUTORS[args.operation]
    except KeyError as exc:  # pragma: no cover - argparse already constrains it
        raise _refuse(
            "UNSUPPORTED_OPERATION",
            f"{args.operation!r} is not a supported subcommand; expected one "
            f"of {sorted(_PLANNERS)}",
            EXIT_INVALID_PARAMETERS,
        ) from exc

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            identity = verify_platform_identity(
                cur,
                expected_environment=str(args.expected_environment),
                expected_platform_uuid=expected_uuid,
            )
            plan = planner(cur, args)
            plan["mode"] = "DRY_RUN" if dry_run else "EXECUTE"
            plan["surface"] = SURFACE_RECONCILIATION_SCHEDULE
            plan["approval_ref"] = args.approval_ref
            plan["platform_identity"] = identity

            if dry_run:
                conn.rollback()
                return EXIT_OK, _public(plan)

            plan["result"] = executor(cur, plan)
        conn.commit()
    return EXIT_OK, _public(plan)


def _public(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the private carriers; they hold live row objects, not report data."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, plan = run(args)
    except ScheduleMutationRefused as exc:
        print(f"SCHEDULE_MUTATION_REFUSED {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ReconciliationRefused as exc:
        print(f"RECONCILIATION_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except AuditError as exc:
        # `verify_platform_identity`, `canonical_uuid` and `platform_dsn_from_env`
        # are borrowed from the audit tool and raise ITS exception type, each
        # carrying the stable exit code the refusal means —
        # EXIT_IDENTITY_NOT_VERIFIED for a wrong environment or platform UUID,
        # EXIT_INVALID_PARAMETERS for an incomplete DSN. Without this clause
        # every one of them fell through to the generic handler and reported
        # EXIT_RUNTIME_FAILURE, so an operator (or a wrapper script) could not
        # distinguish "you pointed this at the wrong database" from "the tool
        # crashed". Still fail-closed either way; the classification was wrong,
        # not the refusal.
        print(f"RECONCILIATION_IDENTITY_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(f"RECONCILIATION_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(plan, sort_keys=True, indent=2, default=str))
    print(plan["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
