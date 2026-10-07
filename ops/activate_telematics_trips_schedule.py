#!/usr/bin/env python3
"""Dry-run-first activation of an authoritative Telematics `trips_sync` schedule.

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §5.2/§5.2.1, §13.6 (the
    ordered enablement gates; activation is the last of them, never the first)
  docs/14_telematics_trips_compatibility_implementation_plan.md §3/C11, §12
  docs/15_telematics_coverage_mutation_contract.md (this tool writes no coverage)
  docs/07_operations.md §5.5 (the cold-start onboarding path)

WHAT THIS IS.
    The last transition of the cold-start onboarding path: it flips exactly one
    boolean, ``client_dataset_schedule.enabled``, from ``false`` to ``true``, and
    only for a schedule whose client has already proven — in this exact
    database, at this exact moment — that a controlled recovery **chain**
    succeeded.

    A cold-start range longer than the client's recovery horizon needs more than
    one window, so this tool does **not** require exactly one recovery row. It
    requires that the target's entire recovery state is exactly one named
    chain's complete, contiguous, successful sequence: ordinals ``1..N`` with no
    gap or duplicate, window 1 starting at the original baseline ``A``, each
    later window starting where the previous ended, the last ending exactly at
    the stored watermark ``W``, ``N`` matching the operator's explicit
    ``--expected-successful-window-count``, and one ``SUCCESS`` platform business
    run per window with no unrelated target run left over. A one-window cold
    start is simply a chain of length one and remains fully supported.

    Until this tool runs, the dispatcher cannot claim the schedule at all:
    ``dispatcher._load_enabled_schedules`` selects `WHERE cds.enabled = true`,
    so a disabled schedule is invisible to it. That is precisely why the
    onboarding order puts activation last: every earlier failure leaves the
    client unable to produce scheduled work, with no operator action required to
    keep it that way.

WHAT THIS IS NOT.
    It is not a schedule editor. It cannot change a cadence, a run time, a
    timezone, a lookback, an enrichment mode or an overwrite policy — the
    post-write verification compares every other column byte-for-byte against
    the pre-image read under the same lock and refuses the transaction if
    anything else moved, including ``updated_at``.

    It writes no coverage row, no recovery row, no history row and no client
    configuration, and it never re-runs or repairs a recovery.

HARD GUARANTEES.
    * dry-run is the default; a write requires ``--execute`` and a second
      deliberate ``--confirm-client-code``;
    * exactly one `UPDATE`, exactly one affected row, exactly one changed field;
    * a schedule that is already enabled is reported as
      ``ALREADY_ENABLED`` with zero writes rather than written a second time;
    * zero provider requests, zero subprocesses, zero business-database access.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/activate_telematics_trips_schedule.py \\
        --client-code ECHO00001 --dataset trips_sync \\
        --expected-schedule-id 60c80b85-f294-4a00-8e09-b6a3688af443 \\
        --expected-covered-through 2026-08-04T09:00:00Z \\
        --expected-coverage-fingerprint <64 hex> \\
        --cold-start-chain-ref TELEMATICS-COLD-START-ECHO00001-2026-08 \\
        --expected-successful-window-count 2 \\
        --approved-final-chain-boundary 2026-08-04T09:00:00Z \\
        --approval-ref TELEMATICS-ACTIVATE-ECHO00001-1 \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
    # only after the dry-run plan is reviewed, add:
    #   --execute --confirm-client-code ECHO00001
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.coverage_finalization import (  # noqa: E402
    COVERAGE_SOURCE_MANUAL_RECOVERY,
    COVERAGE_STATUS_READY,
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_SYNC_DATASET_NAME,
    coverage_fingerprint,
    read_coverage_row,
)
from jobs.api.telematics.execution_outcome import (  # noqa: E402
    ExecutionOutcome,
    ExecutionOutcomeError,
    is_coverage_eligible,
    require_platform_run_identity,
)
from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
    SURFACE_ACTIVATE_TRIPS_SCHEDULE,
    ScheduleMutationRefused,
    assert_activation_permitted,
)
from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    canonical_uuid,
    iso_utc,
    open_read_only_connection,
    parse_iso_utc,
    platform_dsn_from_env,
    _load_dotenv,
)
from ops.telematics_cold_start_chain import (  # noqa: E402
    CHAIN_SEMANTICS_VERSION,
    ChainContractError,
    evaluate_business_run_correspondence,
    evaluate_chain,
    partition_recovery_rows,
    validate_chain_ref,
)
from ops.recover_telematics_trips_window import REQUIRED_MIGRATIONS  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
EXIT_WRITE_CONFLICT = 6
EXIT_POSTWRITE_VERIFICATION_FAILED = 7

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ACTIVATION_SEMANTICS_VERSION = "telematics-schedule-activation/1"

# The single field this tool may change. Everything else in the row is compared
# byte-for-byte before and after the update.
MUTABLE_FIELD = "enabled"


class ActivationRefused(RuntimeError):
    """Stable, sanitized refusal. Carries identifiers only."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> ActivationRefused:
    return ActivationRefused(code, message, exit_code)


def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            f"{label} must be 1-200 characters of [A-Za-z0-9._:@/+-]",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def parse_instant(raw: object, *, label: str) -> datetime:
    try:
        parsed = parse_iso_utc(str(raw or ""), label=label)
    except Exception as exc:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            f"{label} must be a timezone-aware ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.microsecond != 0:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            f"{label} must be a whole-second instant; it is never rounded here",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def verify_platform_identity(
    cur, *, expected_environment: str, expected_platform_uuid: str
) -> Dict[str, Any]:
    """The same marker contract every Telematics coverage tool enforces.

    Reimplemented here rather than imported so that an identity failure carries
    this module's own refusal type and exit code, instead of leaking another
    tool's exception class through the CLI boundary.
    """
    cur.execute(
        "SELECT to_regclass('ops_control.environment_identity')::text AS marker"
    )
    if not (cur.fetchone() or {}).get("marker"):
        raise _refuse(
            "ACTIVATION_REFUSED_IDENTITY",
            "ops_control.environment_identity does not exist",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    cur.execute(
        """
        SELECT identity_key, environment,
               database_identity_id::text AS database_identity_id,
               database_role
          FROM ops_control.environment_identity
         ORDER BY identity_key
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1 or rows[0].get("identity_key") != "primary":
        raise _refuse(
            "ACTIVATION_REFUSED_IDENTITY",
            "the identity table must hold exactly the primary marker row",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    marker = rows[0]
    if str(marker.get("environment")) != expected_environment:
        raise _refuse(
            "ACTIVATION_REFUSED_IDENTITY",
            "database environment does not match --expected-environment",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_identity_id")) != expected_platform_uuid:
        raise _refuse(
            "ACTIVATION_REFUSED_IDENTITY",
            "database identity UUID does not match --expected-platform-uuid",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_role")) != "platform":
        raise _refuse(
            "ACTIVATION_REFUSED_IDENTITY",
            "the connected database is not the platform database",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    return marker


def verify_schema(cur) -> None:
    cur.execute("SELECT filename FROM public.schema_migrations")
    applied = {str(row["filename"]) for row in cur.fetchall()}
    missing = [name for name in REQUIRED_MIGRATIONS if name not in applied]
    if missing:
        raise _refuse(
            "ACTIVATION_REFUSED_SCHEMA",
            f"required migration(s) not applied: {', '.join(missing)}",
        )


def _schedule_row(cur, *, schedule_id: str) -> Optional[Dict[str, Any]]:
    """The whole schedule row as JSON, so nothing can change unnoticed."""
    cur.execute(
        "SELECT to_jsonb(t) AS row"
        "  FROM workflow_a_control.client_dataset_schedule AS t"
        " WHERE schedule_id = %s",
        (schedule_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return None
    return dict(rows[0]["row"])


def _target_platform_runs(cur, *, client_id: str, client_code: str) -> list:
    """Every platform business run attributable to the target, with its status."""
    cur.execute("SELECT to_regclass('public.runs')::text AS present")
    if not (cur.fetchone() or {}).get("present"):
        return []
    cur.execute(
        """
        SELECT run_id::text AS run_id, status
          FROM public.runs
         WHERE params ->> 'client_id' = %s
            OR params ->> 'client_code' = %s
         ORDER BY run_id
        """,
        (client_id, client_code),
    )
    return [dict(row) for row in cur.fetchall()]


def _verify_chain_execution_proofs(
    chain_rows: list,
    *,
    client_id: str,
    client_code: Optional[str],
    schedule_id: str,
    dataset_name: str,
) -> list:
    """Require a genuine committed execution record for every chain window.

    The record was written by the business job itself and persisted by the
    recovery launcher under `job_summary.execution_outcome`. Here it is parsed
    strictly and re-verified against this activation's own idea of the target,
    so a proof that belongs to another client, schedule, dataset, recovery or
    window is a refusal rather than a pass. Every accepted window must also carry
    a valid platform-run identity that exactly equals the one its recovery row
    recorded; an absent, empty, malformed or differing value refuses.

    Rows that are not `SUCCESS` are ignored: the chain evaluator has already
    refused the activation if any of them exist. This function is only ever
    asked about the windows the chain is built from.
    """
    proofs = []
    for row in chain_rows:
        if str(row.get("status") or "") != "SUCCESS":
            continue
        recovery_run_id = str(row.get("recovery_run_id"))
        summary = row.get("job_summary")
        if not isinstance(summary, dict):
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} carries no job_summary; a chain "
                "window recorded before structured execution proof existed is "
                "never accepted as evidence of committed business work",
            )
        raw = summary.get("execution_outcome")
        if raw is None:
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} carries no structured execution "
                "outcome; a SUCCESS status alone does not prove the business "
                "job executed and committed",
            )
        try:
            outcome = ExecutionOutcome.from_mapping(raw)
        except ExecutionOutcomeError as exc:
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} carries a malformed execution "
                f"outcome ({exc})",
            ) from exc
        if not is_coverage_eligible(outcome):
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} reports terminal outcome "
                f"{outcome.outcome} with transaction_status "
                f"{outcome.transaction_status}; a skipped or uncommitted "
                "execution is never a successful chain member",
            )
        mismatches = []
        if outcome.client_id != client_id:
            mismatches.append("client_id")
        if client_code is not None and outcome.client_code != str(client_code):
            mismatches.append("client_code")
        if outcome.schedule_id != schedule_id:
            mismatches.append("schedule_id")
        if outcome.dataset_name != dataset_name:
            mismatches.append("dataset_name")
        if outcome.recovery_run_id != recovery_run_id:
            mismatches.append("recovery_run_id")
        row_start = row.get("window_start_ts")
        row_end = row.get("window_end_ts")
        if not isinstance(row_start, datetime) or not isinstance(row_end, datetime):
            mismatches.append("window_bounds_absent")
        else:
            if outcome.requested_window_start_ts != row_start.astimezone(timezone.utc):
                mismatches.append("window_start_ts")
            if outcome.requested_window_end_ts != row_end.astimezone(timezone.utc):
                mismatches.append("window_end_ts")
        if mismatches:
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} carries an execution outcome that "
                f"does not describe this window: {', '.join(sorted(mismatches))}",
            )
        # Platform-run identity is mandatory for every chain member, on both
        # sides and exactly equal. A proof that names no platform run — or names
        # a different one than the recovery row recorded — cannot be tied back to
        # a real platform run, so it is not evidence of committed business work.
        try:
            proof_run_id = require_platform_run_identity(
                outcome.platform_run_id,
                field=f"recovery {recovery_run_id} execution outcome "
                      "platform_run_id",
                required=True,
            )
            row_run_id = require_platform_run_identity(
                row.get("platform_run_id"),
                field=f"recovery {recovery_run_id} row platform_run_id",
                required=True,
            )
        except ExecutionOutcomeError as exc:
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF", str(exc),
            ) from exc
        if proof_run_id != row_run_id:
            raise _refuse(
                "ACTIVATION_REFUSED_EXECUTION_PROOF",
                f"recovery {recovery_run_id} execution outcome names platform "
                f"run {proof_run_id}, but the recovery row records "
                f"{row_run_id}; a proof is accepted only for the platform run "
                "the recovery itself recorded",
            )
        proofs.append({
            "recovery_run_id": recovery_run_id,
            "outcome": outcome.outcome,
            "transaction_status": outcome.transaction_status,
            "prepared_count": outcome.prepared_count,
            "upserted_count": outcome.upserted_count,
            "provider_execution_entered": outcome.provider_execution_entered,
            "platform_run_id": proof_run_id,
        })
    if not proofs:
        raise _refuse(
            "ACTIVATION_REFUSED_EXECUTION_PROOF",
            "no chain window carries structured execution proof",
        )
    return proofs


def evaluate_gates(
    cur,
    *,
    client_code: str,
    dataset_name: str,
    expected_schedule_id: str,
    expected_covered_through_ts: datetime,
    expected_coverage_fingerprint: str,
    expected_recovery_run_id: Optional[str],
    chain_ref: str,
    expected_successful_window_count: int,
    approved_final_chain_boundary_ts: datetime,
) -> Dict[str, Any]:
    """Every activation precondition, evaluated read-only against live state.

    Re-run verbatim under the write lock, so a change between planning and
    activation cannot slip through.
    """
    if approved_final_chain_boundary_ts != expected_covered_through_ts:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            "--approved-final-chain-boundary must equal "
            "--expected-covered-through; the chain's approved end and the "
            "watermark being activated on are the same instant",
            EXIT_INVALID_PARAMETERS,
        )
    if dataset_name != TRIPS_SYNC_DATASET_NAME:
        raise _refuse(
            "ACTIVATION_REFUSED_TARGET",
            f"this activation contract is {TRIPS_SYNC_DATASET_NAME}-only",
            EXIT_INVALID_PARAMETERS,
        )

    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code, enabled,
               trips_pagination_mode
          FROM workflow_a_control.client_account
         WHERE client_code = %s
        """,
        (client_code,),
    )
    clients = [dict(row) for row in cur.fetchall()]
    if len(clients) != 1:
        raise _refuse(
            "ACTIVATION_REFUSED_TARGET",
            f"client_code {client_code} resolves to {len(clients)} clients; "
            "exactly one target is required",
        )
    client = clients[0]
    # The shared, deny-by-default prevention of an active strict_meta schedule.
    # Evaluated through the registry rather than inline so every supported
    # activation path — this one and any future one — enforces the identical
    # rule, and an unregistered path cannot enforce it at all.
    try:
        assert_activation_permitted(
            surface=SURFACE_ACTIVATE_TRIPS_SCHEDULE,
            dataset_name=dataset_name,
            pagination_mode=str(client["trips_pagination_mode"]),
        )
    except ScheduleMutationRefused as exc:
        raise _refuse("ACTIVATION_REFUSED_MODE", str(exc)) from exc

    # Base schedules only (M5). Activation turns on the schedule that carries
    # forward coverage for the dataset; a reconciliation cadence is a different
    # decision with a different review, and M6 will own enabling one.
    #
    # This scope is what keeps the "exactly one row" requirement below a
    # statement about the BASE schedule rather than about however many cadences
    # happen to be registered. Without it, M6 would break this operator surface
    # the day it lands, for a reason unrelated to the client being activated.
    # Today it changes nothing: every schedule row is base.
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, enabled
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s
           AND run_type = %s
         ORDER BY schedule_id
        """,
        (client["client_id"], dataset_name, SCHEDULE_RUN_TYPE_BASE),
    )
    schedules = [dict(row) for row in cur.fetchall()]
    if len(schedules) != 1:
        raise _refuse(
            "ACTIVATION_REFUSED_SCHEDULE",
            f"{dataset_name} resolves to {len(schedules)} base schedule rows "
            "for this client; exactly one authoritative base schedule is "
            "required and no competing base schedule may exist",
        )
    if str(schedules[0]["schedule_id"]) != expected_schedule_id:
        raise _refuse(
            "ACTIVATION_REFUSED_SCHEDULE",
            "the resolved schedule does not match --expected-schedule-id",
        )
    schedule_id = str(schedules[0]["schedule_id"])
    already_enabled = bool(schedules[0]["enabled"])

    coverage = read_coverage_row(
        cur, client_id=str(client["client_id"]), dataset_name=dataset_name,
    )
    if coverage is None:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            "no coverage row exists for this client and dataset",
        )
    if str(coverage.get("bootstrap_status")) != COVERAGE_STATUS_READY:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            f"the coverage row is {coverage.get('bootstrap_status')}, "
            f"expected {COVERAGE_STATUS_READY}",
        )
    if str(coverage.get("covered_through_source")) != COVERAGE_SOURCE_MANUAL_RECOVERY:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            "covered_through_source is "
            f"{coverage.get('covered_through_source')!r}, expected "
            f"{COVERAGE_SOURCE_MANUAL_RECOVERY!r}. Activation follows a "
            "verified recovery, never a bare baseline",
        )
    stored_w = coverage.get("covered_through_ts")
    if stored_w is None or stored_w.astimezone(timezone.utc) != \
            expected_covered_through_ts:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            "--expected-covered-through does not match the stored watermark; "
            "the authorization is stale and is never adjusted here",
        )
    observed_fingerprint = coverage_fingerprint(coverage)
    if observed_fingerprint != expected_coverage_fingerprint:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            "--expected-coverage-fingerprint does not match the stored "
            "coverage row",
        )

    coverage_start = coverage.get("coverage_start_ts")
    if coverage_start is None:
        raise _refuse(
            "ACTIVATION_REFUSED_COVERAGE",
            "the coverage row carries no coverage_start_ts",
        )
    coverage_start = coverage_start.astimezone(timezone.utc)

    # --- the recovery chain -------------------------------------------------
    #
    # A cold start is a chain of *one or more* windows, so "exactly one recovery
    # row" is the wrong question. The right one is: is the target's entire
    # recovery state exactly this chain's complete, contiguous, successful
    # sequence from the original baseline A to the current watermark W?
    cur.execute(
        """
        SELECT recovery_run_id::text AS recovery_run_id,
               client_id::text AS client_id,
               schedule_id::text AS schedule_id,
               dataset_name, status, approval_ref,
               window_start_ts, window_end_ts,
               platform_run_id::text AS platform_run_id,
               job_summary
          FROM workflow_a_control.client_dataset_recovery_run
         WHERE schedule_id = %s OR client_id = %s
         ORDER BY created_at, recovery_run_id
        """,
        (schedule_id, client["client_id"]),
    )
    recoveries = [dict(row) for row in cur.fetchall()]
    partitioned = partition_recovery_rows(recoveries, chain_ref=chain_ref)
    if partitioned["foreign"]:
        raise _refuse(
            "ACTIVATION_REFUSED_RECOVERY",
            f"{len(partitioned['foreign'])} recovery run(s) exist for this "
            f"target outside chain {chain_ref!r}; a schedule is never activated "
            "while unrelated recovery state exists",
        )
    if not partitioned["chain"]:
        raise _refuse(
            "ACTIVATION_REFUSED_RECOVERY",
            f"no recovery run of chain {chain_ref!r} exists for this target; at "
            "least one successful window is required",
        )

    target_runs = _target_platform_runs(
        cur,
        client_id=str(client["client_id"]),
        client_code=str(client["client_code"]),
    )
    try:
        chain_summary = evaluate_chain(
            partitioned["chain"],
            baseline_ts=coverage_start,
            current_covered_through_ts=expected_covered_through_ts,
            client_id=str(client["client_id"]),
            schedule_id=schedule_id,
            dataset_name=dataset_name,
        )
        run_correspondence = evaluate_business_run_correspondence(
            chain_summary, target_runs=target_runs,
        )
    except ChainContractError as exc:
        raise _refuse("ACTIVATION_REFUSED_RECOVERY", str(exc)) from exc

    successful_windows = int(chain_summary["successful_window_count"])
    if successful_windows != expected_successful_window_count:
        raise _refuse(
            "ACTIVATION_REFUSED_RECOVERY",
            f"chain {chain_ref!r} has {successful_windows} successful "
            f"window(s); --expected-successful-window-count declares "
            f"{expected_successful_window_count}",
        )
    final_window = chain_summary["windows"][-1]
    if expected_recovery_run_id and \
            str(final_window["recovery_run_id"]) != expected_recovery_run_id:
        raise _refuse(
            "ACTIVATION_REFUSED_RECOVERY",
            "the final chain recovery run does not match "
            "--expected-recovery-run-id",
        )

    # --- structured execution proof, per window -----------------------------
    #
    # A `SUCCESS` recovery row is a statement about the *launcher's* verdict. It
    # is no longer accepted on its own, because the launcher used to reach that
    # verdict from a bare return code. Every window of the chain must carry the
    # business job's own strictly parsed terminal record proving a committed
    # execution over exactly that window. A skipped execution is never a valid
    # chain member, whatever status the row carries.
    execution_proofs = _verify_chain_execution_proofs(
        partitioned["chain"],
        client_id=str(client["client_id"]),
        client_code=client.get("client_code"),
        schedule_id=schedule_id,
        dataset_name=dataset_name,
    )

    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s AND status = 'RUNNING'
        """,
        (schedule_id,),
    )
    running_history = int((cur.fetchone() or {})["n"])
    if running_history:
        raise _refuse(
            "ACTIVATION_REFUSED_CONCURRENCY",
            f"{running_history} RUNNING schedule-history row(s) exist for this "
            "schedule",
        )

    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_dataset_recovery_run
         WHERE schedule_id = %s AND status IN ('PLANNED', 'RUNNING')
        """,
        (schedule_id,),
    )
    active_recoveries = int((cur.fetchone() or {})["n"])
    if active_recoveries:
        raise _refuse(
            "ACTIVATION_REFUSED_CONCURRENCY",
            f"{active_recoveries} non-terminal recovery run(s) exist for this "
            "schedule",
        )

    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s OR client_id = %s
        """,
        (schedule_id, client["client_id"]),
    )
    total_history = int((cur.fetchone() or {})["n"])
    if total_history:
        # A cold-start chain never produces a scheduled fire: the schedule is
        # disabled for its whole duration, so the dispatcher cannot claim it.
        # Any history row here means the state being activated was not produced
        # by the chain this activation is authorized against.
        raise _refuse(
            "ACTIVATION_REFUSED_HISTORY",
            f"{total_history} schedule-history row(s) exist for this target; a "
            "cold-start chain runs entirely with the schedule disabled and "
            "synthesizes no fire",
        )

    return {
        "client": client,
        "schedule_id": schedule_id,
        "already_enabled": already_enabled,
        "coverage": coverage,
        "coverage_fingerprint": observed_fingerprint,
        "chain_ref": chain_ref,
        "chain_summary": chain_summary,
        "chain_run_correspondence": run_correspondence,
        "final_window": final_window,
        "execution_proofs": execution_proofs,
        "successful_window_count": successful_windows,
        "total_history_rows": total_history,
        "historical_scheduled_fires": total_history,
    }


# ---------------------------------------------------------------------------
# The single approved schedule UPDATE
# ---------------------------------------------------------------------------

def execute_activation(
    conn,
    *,
    client_code: str,
    dataset_name: str,
    schedule_id: str,
    expected_schedule_id: str,
    expected_covered_through_ts: datetime,
    expected_coverage_fingerprint: str,
    expected_recovery_run_id: Optional[str],
    chain_ref: str,
    expected_successful_window_count: int,
    approved_final_chain_boundary_ts: datetime,
) -> Dict[str, Any]:
    """One explicit transaction: lock, re-gate, flip one boolean, verify."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '60s'")
        # Documented lock order, identical to the recovery claim:
        #   client_account -> client_dataset_schedule.
        cur.execute(
            "SELECT client_id::text AS client_id, trips_pagination_mode"
            "  FROM workflow_a_control.client_account"
            " WHERE client_code = %s FOR UPDATE",
            (client_code,),
        )
        if len(cur.fetchall()) != 1:
            conn.rollback()
            raise _refuse(
                "ACTIVATION_WRITE_CONFLICT",
                "the client disappeared or became ambiguous under the lock",
                EXIT_WRITE_CONFLICT,
            )
        cur.execute(
            "SELECT schedule_id::text AS schedule_id, enabled"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s FOR UPDATE",
            (schedule_id,),
        )
        locked = [dict(row) for row in cur.fetchall()]
        if len(locked) != 1:
            conn.rollback()
            raise _refuse(
                "ACTIVATION_WRITE_CONFLICT",
                "the authoritative schedule disappeared before the update",
                EXIT_WRITE_CONFLICT,
            )
        if bool(locked[0]["enabled"]):
            # Another activation already committed. Reporting the completed
            # state is safe; issuing a second UPDATE would not be.
            conn.rollback()
            return {"result": "ALREADY_ENABLED", "rows_updated": 0, "before": None,
                    "after": None}

        before = _schedule_row(cur, schedule_id=schedule_id)
        if before is None:  # pragma: no cover - locked above
            conn.rollback()
            raise _refuse(
                "ACTIVATION_WRITE_CONFLICT",
                "the schedule row vanished under the lock",
                EXIT_WRITE_CONFLICT,
            )

        try:
            gates = evaluate_gates(
                cur,
                client_code=client_code,
                dataset_name=dataset_name,
                expected_schedule_id=expected_schedule_id,
                expected_covered_through_ts=expected_covered_through_ts,
                expected_coverage_fingerprint=expected_coverage_fingerprint,
                expected_recovery_run_id=expected_recovery_run_id,
                chain_ref=chain_ref,
                expected_successful_window_count=(
                    expected_successful_window_count
                ),
                approved_final_chain_boundary_ts=(
                    approved_final_chain_boundary_ts
                ),
            )
        except ActivationRefused:
            conn.rollback()
            raise
        if gates["schedule_id"] != schedule_id:
            conn.rollback()
            raise _refuse(
                "ACTIVATION_WRITE_CONFLICT",
                "the resolved schedule changed between planning and activation",
                EXIT_WRITE_CONFLICT,
            )

        # NOTE. An earlier M4 candidate took a release-preflight advisory lock
        # here, because the activation gate then defined its fleet as "enabled
        # clients that own a trips_sync schedule" — which made this write able
        # to change fleet membership. That definition was wrong and has been
        # corrected: the gate now covers EVERY enabled client account, so
        # enabling a schedule cannot add or remove a client from it. The lock
        # protected nothing and has been removed rather than left in place
        # implying a synchronization protocol that is no longer the mechanism.
        # Fleet membership is fenced where it is actually defined — a SHARE lock
        # on `workflow_a_control.client_account`, held across the pointer swap
        # (`ops/release_schema_preflight.activation_fence`).

        # Exactly one field, exactly one row, guarded by the expected old value.
        cur.execute(
            """
            UPDATE workflow_a_control.client_dataset_schedule
               SET enabled = true
             WHERE schedule_id = %s
               AND enabled = false
            """,
            (schedule_id,),
        )
        rows_updated = cur.rowcount
        if rows_updated != 1:
            conn.rollback()
            raise _refuse(
                "ACTIVATION_WRITE_CONFLICT",
                f"the activation update affected {rows_updated} row(s); "
                "exactly one is required",
                EXIT_WRITE_CONFLICT,
            )

        after = _schedule_row(cur, schedule_id=schedule_id)
        mismatches = _verify_single_field_change(before, after)
        if mismatches:
            conn.rollback()
            raise _refuse(
                "ACTIVATION_POSTWRITE_VERIFICATION_FAILED",
                "the activation changed more than the enabled flag: "
                f"{', '.join(sorted(mismatches))}",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )

    conn.commit()
    return {
        "result": "ACTIVATED",
        "rows_updated": rows_updated,
        "before": before,
        "after": after,
    }


def _verify_single_field_change(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> list:
    """Prove that `enabled` went false → true and nothing else moved at all."""
    if before is None or after is None:
        return ["row_absent"]
    mismatches = []
    if set(before) != set(after):
        return ["column_set"]
    for key in sorted(before):
        if key == MUTABLE_FIELD:
            continue
        if before[key] != after[key]:
            mismatches.append(key)
    if before.get(MUTABLE_FIELD) is not False:
        mismatches.append("enabled_before")
    if after.get(MUTABLE_FIELD) is not True:
        mismatches.append("enabled_after")
    return mismatches


def independent_read_back(dsn: str, *, schedule_id: str) -> Dict[str, Any]:
    """Confirm the committed state on a fresh read-only connection."""
    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            row = _schedule_row(cur, schedule_id=schedule_id)
            cur.execute(
                """
                SELECT count(*) AS n
                  FROM workflow_a_control.client_dataset_schedule AS cds
                  JOIN workflow_a_control.client_account AS ca
                    ON ca.client_id = cds.client_id
                 WHERE cds.schedule_id = %s
                   AND cds.enabled = true
                   AND ca.enabled = true
                """,
                (schedule_id,),
            )
            dispatcher_visible = int((cur.fetchone() or {})["n"])
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    if row is None:
        raise _refuse(
            "ACTIVATION_POSTWRITE_VERIFICATION_FAILED",
            "the schedule row is not visible on an independent read",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    return {"row": row, "dispatcher_visible": dispatcher_visible}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first activation of one authoritative Telematics trips_sync "
            "schedule after a verified recovery. Changes exactly one field."
        ),
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=TRIPS_SYNC_DATASET_NAME)
    parser.add_argument("--expected-schedule-id", required=True)
    parser.add_argument("--expected-covered-through", required=True)
    parser.add_argument("--expected-coverage-fingerprint", required=True)
    parser.add_argument(
        "--expected-recovery-run-id",
        help="Optional exact binding to the final successful recovery run of "
             "the chain.",
    )
    parser.add_argument(
        "--cold-start-chain-ref", required=True,
        help="The cold-start recovery chain this activation is authorized "
             "against; every target recovery row must be one of its windows.",
    )
    parser.add_argument(
        "--expected-successful-window-count", required=True, type=int,
        help="How many successful contiguous windows the chain must hold. A "
             "one-window cold start is a chain of length one.",
    )
    parser.add_argument(
        "--approved-final-chain-boundary", required=True,
        help="The explicitly approved final chain boundary; it must equal "
             "--expected-covered-through.",
    )
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-client-code")
    parser.add_argument("--dsn")
    return parser


def run(args) -> tuple:
    dry_run = not args.execute
    client_code = _safe_token(args.client_code, label="--client-code")
    dataset_name = _safe_token(args.dataset, label="--dataset")
    approval_ref = _safe_token(args.approval_ref, label="--approval-ref")
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    expected_schedule_id = canonical_uuid(
        args.expected_schedule_id, label="--expected-schedule-id"
    )
    expected_recovery_run_id = (
        canonical_uuid(
            args.expected_recovery_run_id, label="--expected-recovery-run-id"
        )
        if args.expected_recovery_run_id else None
    )
    expected_environment = str(args.expected_environment or "").strip()
    if not expected_environment:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            "--expected-environment is required",
            EXIT_INVALID_PARAMETERS,
        )
    fingerprint = str(args.expected_coverage_fingerprint or "").strip().lower()
    if not SHA256_RE.match(fingerprint):
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            "--expected-coverage-fingerprint must be a lowercase 64-character "
            "SHA-256",
            EXIT_INVALID_PARAMETERS,
        )
    expected_w = parse_instant(
        args.expected_covered_through, label="--expected-covered-through"
    )
    approved_final_boundary = parse_instant(
        args.approved_final_chain_boundary,
        label="--approved-final-chain-boundary",
    )
    try:
        chain_ref = validate_chain_ref(args.cold_start_chain_ref)
    except ChainContractError as exc:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            f"--cold-start-chain-ref is invalid ({exc})",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    expected_window_count = int(args.expected_successful_window_count)
    if expected_window_count < 1:
        raise _refuse(
            "ACTIVATION_REFUSED_PARAMETER",
            "--expected-successful-window-count must be at least 1; a chain of "
            "length one is the minimum, and zero recoveries never activate",
            EXIT_INVALID_PARAMETERS,
        )

    if not dry_run:
        confirmed = str(args.confirm_client_code or "").strip()
        if not confirmed:
            raise _refuse(
                "ACTIVATION_REFUSED_CONFIRMATION",
                "--execute additionally requires --confirm-client-code",
                EXIT_INVALID_PARAMETERS,
            )
        if confirmed != client_code:
            raise _refuse(
                "ACTIVATION_REFUSED_CONFIRMATION",
                "--confirm-client-code does not match --client-code",
                EXIT_INVALID_PARAMETERS,
            )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            verify_platform_identity(
                cur,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
            )
            verify_schema(cur)
            gates = evaluate_gates(
                cur,
                client_code=client_code,
                dataset_name=dataset_name,
                expected_schedule_id=expected_schedule_id,
                expected_covered_through_ts=expected_w,
                expected_coverage_fingerprint=fingerprint,
                expected_recovery_run_id=expected_recovery_run_id,
                chain_ref=chain_ref,
                expected_successful_window_count=expected_window_count,
                approved_final_chain_boundary_ts=approved_final_boundary,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    plan: Dict[str, Any] = {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "semantics_version": ACTIVATION_SEMANTICS_VERSION,
        "expected_environment": expected_environment,
        "expected_platform_uuid": expected_uuid,
        "client_code": client_code,
        "client_id": gates["client"]["client_id"],
        "dataset_name": dataset_name,
        "schedule_id": gates["schedule_id"],
        "trips_pagination_mode": str(gates["client"]["trips_pagination_mode"]),
        "schedule_enabled_before": gates["already_enabled"],
        "would_set_enabled": True,
        "covered_through_ts": iso_utc(gates["coverage"]["covered_through_ts"]),
        "coverage_start_ts": iso_utc(gates["coverage"]["coverage_start_ts"]),
        "covered_through_source": gates["coverage"]["covered_through_source"],
        "coverage_fingerprint": gates["coverage_fingerprint"],
        "cold_start_chain_ref": chain_ref,
        "cold_start_chain_semantics_version": CHAIN_SEMANTICS_VERSION,
        "chain_successful_window_count": gates["successful_window_count"],
        "expected_successful_window_count": expected_window_count,
        "approved_final_chain_boundary_ts": iso_utc(approved_final_boundary),
        "chain_windows": gates["chain_summary"]["windows"],
        "chain_business_run_correspondence": gates["chain_run_correspondence"],
        "chain_execution_proofs": gates["execution_proofs"],
        "recovery_run_id": gates["final_window"]["recovery_run_id"],
        "recovery_status": "SUCCESS",
        "historical_scheduled_fires": gates["historical_scheduled_fires"],
        "approval_ref": approval_ref,
        "fields_to_change": [MUTABLE_FIELD],
        "rows_to_update": 0 if gates["already_enabled"] else 1,
        "rows_to_insert": 0,
        "rows_to_delete": 0,
        "timing_fields_changed": 0,
        "client_mode_changes": 0,
        "coverage_mutations": 0,
        "recovery_mutations": 0,
        "history_rows_created": 0,
        "history_mutations": 0,
        "provider_requests": 0,
        "subprocesses_launched": 0,
        "database_writes_performed": 0,
    }

    if gates["already_enabled"]:
        plan["activation_result"] = "ALREADY_ENABLED"
        plan["would_set_enabled"] = False
        return EXIT_OK, plan

    if dry_run:
        plan["would_activate"] = True
        return EXIT_OK, plan

    import psycopg
    from psycopg.rows import dict_row

    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        outcome = execute_activation(
            write_conn,
            client_code=client_code,
            dataset_name=dataset_name,
            schedule_id=gates["schedule_id"],
            expected_schedule_id=expected_schedule_id,
            expected_covered_through_ts=expected_w,
            expected_coverage_fingerprint=fingerprint,
            expected_recovery_run_id=expected_recovery_run_id,
            chain_ref=chain_ref,
            expected_successful_window_count=expected_window_count,
            approved_final_chain_boundary_ts=approved_final_boundary,
        )
    except ActivationRefused:
        try:
            write_conn.rollback()
        except Exception:
            pass
        raise
    finally:
        write_conn.close()

    plan["activation_result"] = outcome["result"]
    plan["affected_row_count"] = outcome["rows_updated"]
    plan["database_writes_performed"] = outcome["rows_updated"]
    if outcome["result"] == "ALREADY_ENABLED":
        plan["transaction_result"] = "ROLLED_BACK_NO_WRITE"
        return EXIT_OK, plan

    plan["transaction_result"] = "COMMITTED"
    verified = independent_read_back(dsn, schedule_id=gates["schedule_id"])
    plan["schedule_enabled_after"] = bool(verified["row"]["enabled"])
    plan["dispatcher_visible_after"] = verified["dispatcher_visible"]
    plan["changed_fields"] = sorted(
        key for key in outcome["before"]
        if outcome["before"][key] != outcome["after"][key]
    )
    return EXIT_OK, plan


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, plan = run(args)
    except ActivationRefused as exc:
        print(f"ACTIVATION_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(f"ACTIVATION_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(plan, sort_keys=True, indent=2, default=str))
    print(plan["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
