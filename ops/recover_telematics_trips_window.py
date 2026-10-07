#!/usr/bin/env python3
"""Dry-run-first reviewed manual Telematics `trips_sync` compatibility recovery (C11).

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §5.4, §12 (the revised manual
    and recovery rules), §13.6 (enablement ordering)
  docs/14_telematics_trips_compatibility_implementation_plan.md §1.1, §3/C11, §12
  docs/15_telematics_coverage_mutation_contract.md §4, §5, §7 (the shared
    claim-time compare-and-swap this tool reuses)
  db/migrations/057_workflow_a_trips_coverage_state.sql
  db/migrations/058_telematics_trips_manual_recovery.sql

WHAT THIS IS.
    A separately identifiable, one-shot production operation that executes the
    **normal** trips synchronization over an explicitly authorized UTC interval
    in `data_invariants_v1`, and — only after that business work fully succeeds —
    advances `covered_through_ts` through the same low-level compare-and-swap the
    scheduled C6 success finalizer uses.

    Its purpose is to exercise and close a *compatibility* interval, which is why
    it runs the production sync path rather than
    `jobs.api.telematics.backfill_trips_insert_only`. That backfill module remains
    a separate business-data repair tool: it inserts missing raw rows and never
    reads or writes coverage.

WHAT THIS IS NOT.
    It is **not** a scheduled fire. It never creates, edits, deletes, retries or
    reclassifies a `workflow_a_control.client_schedule_run_history` row, never
    back-inserts a missing fire and never reuses a `scheduled_fire_ts`. Its
    identity lives in `workflow_a_control.client_dataset_recovery_run`.

    It contains no coverage SQL of its own. Every coverage lock and the single
    authorized advancement go through
    `jobs.api.telematics.coverage_finalization`, which is the only module in the
    repository permitted to hold them.

HARD GUARANTEES.
    * dry-run is the default; `--execute` additionally requires
      `--confirm-client-code` equal to `--client-code`;
    * a dry-run performs zero database writes, zero provider requests and
      launches no business subprocess. The only subprocesses this tool ever
      runs are the two local read-only `git` commands that bind the plan to an
      exact commit, and the single `ops/runner.py` launch on `--execute`;
    * `coverage_start_ts` is never written; `bootstrap_status` is never written;
    * provider, business, orchestration or transaction failure leaves the
      coverage row byte-identical and marks the recovery `FAILED`;
    * a business success whose coverage CAS is refused marks the recovery
      `FINALIZATION_CONFLICT`, overwrites nothing and emits a stable code;
    * nothing is ever retried automatically;
    * the client mode, the schedule, the stabilization parameters and every
      historical scheduled-fire row are untouched;
    * one invocation plans and executes **exactly one** window. A cold-start
      chain of several windows is several separately approved, separately
      reviewed invocations; this tool contains no loop over a plan.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/recover_telematics_trips_window.py \\
        --client-code BRAVO00016 --dataset trips_sync \\
        --window-start 2026-07-27T00:00:00Z \\
        --window-end 2026-08-03T00:00:00Z \\
        --expected-old-covered-through 2026-07-27T00:00:00Z \\
        --reason "recover the 2026-08-03 PAGINATION_MISMATCH interval" \\
        --approval-ref TELEMATICS-C11-BRAVO00016-2026-08-03 \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
    # only after the dry-run plan is reviewed, add:
    #   --execute --confirm-client-code BRAVO00016

Cold start, window 2 of a chain — again dry-run first, and again one window::

    PYTHONPATH="$PWD" python3 ops/recover_telematics_trips_window.py \\
        --client-code ECHO00001 --dataset trips_sync \\
        --allow-disabled-schedule-for-cold-start --confirm-schedule-disabled \\
        --cold-start-chain-ref TELEMATICS-COLD-START-ECHO00001-2026-08 \\
        --approval-ref TELEMATICS-COLD-START-ECHO00001-2026-08-W02 \\
        --window-start 2026-08-01T00:00:00Z \\
        --window-end <approved final boundary> \\
        --approved-shifted-cutoff-boundary <same> \\
        --approved-final-chain-boundary <same> \\
        --expected-old-covered-through 2026-08-01T00:00:00Z \\
        --expected-schedule-id <schedule uuid> \\
        --expected-coverage-fingerprint <64 hex> \\
        --reason "cold-start chain window 2" \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import manual_recovery_authority  # noqa: E402
from jobs.api.telematics.execution_outcome import (  # noqa: E402
    EXECUTION_OUTCOME_FILE_ENV,
    OUTCOME_SKIPPED_DISABLED_SCHEDULE,
    ExecutionOutcomeError,
    is_coverage_eligible,
    read_outcome,
    verify_outcome,
)
from jobs.api.telematics.coverage_finalization import (  # noqa: E402
    COVERAGE_FINGERPRINT_VERSION,
    COVERAGE_SOURCE_BOOTSTRAP,
    COVERAGE_SOURCE_MANUAL_RECOVERY,
    COVERAGE_STATUS_READY,
    TRIPS_COVERAGE_ADVANCE_CONFLICT,
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_SYNC_DATASET_NAME,
    CoverageCasConflict,
    CoverageClaimSnapshot,
    advance_covered_through_cas,
    coverage_fingerprint,
    lock_coverage_row_for_update,
    read_coverage_row,
    snapshot_matches_row,
)
from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
)
from ops.telematics_cold_start_chain import (  # noqa: E402
    CHAIN_SEMANTICS_VERSION,
    ChainContractError,
    evaluate_business_run_correspondence,
    evaluate_chain,
    parse_window_approval_ref,
    partition_recovery_rows,
    plan_recovery_windows,
    serialize_window_plan,
    validate_chain_ref,
)

SYNC_JOB_MODULE = "jobs.api.telematics.sync_trips_and_speeding"
RECOVERY_TRIGGER = "MANUAL_RECOVERY"
RECOVERY_SEMANTICS_VERSION = "telematics-trips-manual-recovery/1"

# --- Cold-start extension (docs/07_operations.md §5.5, cold-start path) ------
#
# A newly onboarded client reaches its first recovery with the authoritative
# schedule still disabled, because the reviewed onboarding order activates the
# schedule only *after* a verified recovery. Everything else about that recovery
# is identical to the historical one, so this module gains a strictly isolated
# opt-in path rather than a second tool that would duplicate the claim state
# machine, the CAS and the failure taxonomy.
#
# The isolation is structural: without `--allow-disabled-schedule-for-cold-start`
# every code path below behaves exactly as before, including the refusal of a
# disabled schedule. With it, the tool does not merely *tolerate* a disabled
# schedule — it requires one, together with a set of conditions that only a
# provably empty, freshly cold-start-bootstrapped client can satisfy — or a
# client that is provably *mid-chain* in the very cold start this tool started.
#
# MULTI-WINDOW CHAINS.
#   A cold-start range longer than the client's `trips_max_recovery_span_seconds`
#   cannot be closed by one recovery, so the first window's "zero prior recovery
#   rows, zero prior platform runs" gates cannot stay unconditional after that
#   first window succeeds. They are not relaxed; they are *split*:
#
#     * the first window of a chain keeps every original zero-state gate;
#     * a subsequent window requires the target's entire execution state to be
#       exactly the successful, contiguous prefix of *this* chain and nothing
#       else — same client, schedule and dataset, ordinals 1..N with no gap, each
#       window starting where the previous ended, the first starting at the
#       original baseline A, the last ending at the current W, and one SUCCESS
#       platform business run per window with no unrelated run left over.
#
#   Chain identity is an operator-supplied `--cold-start-chain-ref` persisted
#   structurally in the existing `approval_ref` column as `<chain>-W<NN>`. No
#   migration is added for convenience; see `ops/telematics_cold_start_chain.py`
#   for why that binding is unambiguous and why `reason` is not used for it.
#
#   The schedule stays disabled for the whole chain, no scheduled-history row is
#   ever synthesized, and every window remains a separate operator approval and a
#   separate execution: this tool never loops over a plan.
COLD_START_EVIDENCE_REF_PREFIX = "telematics-cold-start-bootstrap/1"

REQUIRED_MIGRATIONS = (
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
EXIT_BUSINESS_FAILED = 6
EXIT_FINALIZATION_CONFLICT = 7

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")
REASON_RE = re.compile(r"^[A-Za-z0-9 ._:@/+,()\[\]-]{1,500}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Bounded error classifications. Every terminal non-success writes exactly one.
RECOVERY_BUSINESS_FAILED = "RECOVERY_BUSINESS_FAILED"
RECOVERY_ORCHESTRATION_FAILED = "RECOVERY_ORCHESTRATION_FAILED"
RECOVERY_FINALIZATION_UNRESOLVED = "RECOVERY_FINALIZATION_UNRESOLVED"

# A subprocess that exited 0 without proving it committed business work. This is
# the classification the ECHO00001 cold start would have received: return code
# 0, `SKIPPED_DISABLED_SCHEDULE`, zero provider requests, zero prepared rows,
# zero upserted rows, no business transaction — and, now, no coverage
# advancement.
RECOVERY_BUSINESS_NOT_EXECUTED = "RECOVERY_BUSINESS_NOT_EXECUTED"

MAX_ERROR_SUMMARY_CHARS = 4000


class RecoveryRefused(RuntimeError):
    """Stable, sanitized refusal. Carries no secret and no trip payload."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> RecoveryRefused:
    return RecoveryRefused(code, message, exit_code)


# ---------------------------------------------------------------------------
# Operator input
# ---------------------------------------------------------------------------

def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            f"{label} must be 1-200 characters of [A-Za-z0-9._:@/+-]",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def _safe_reason(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not REASON_RE.match(text):
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--reason must be 1-500 printable characters and carries no payload",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def parse_instant(raw: object, *, label: str) -> datetime:
    """Parse an aware, whole-second UTC instant. Never rounded, never guessed."""
    text = str(raw or "").strip()
    if not text:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER", f"{label} is required",
            EXIT_INVALID_PARAMETERS,
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            f"{label} must be an ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.utcoffset() is None:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER", f"{label} must be timezone-aware",
            EXIT_INVALID_PARAMETERS,
        )
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond != 0:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            f"{label} must be a whole-second instant; it is never rounded here",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


def _canonical_uuid(value: object, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER", f"{label} must be a canonical UUID",
            EXIT_INVALID_PARAMETERS,
        ) from exc


def _iso(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


# ---------------------------------------------------------------------------
# Repository and environment identity
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True,
        check=True, text=True, timeout=15,
    )
    return completed.stdout.strip()


def repository_state(*, require_clean: bool) -> Dict[str, Any]:
    try:
        head = _git("rev-parse", "HEAD")
        porcelain = _git("status", "--porcelain")
    except Exception as exc:
        raise _refuse(
            "RECOVERY_REFUSED_REPOSITORY",
            "the repository HEAD/worktree state could not be resolved",
            EXIT_RUNTIME_FAILURE,
        ) from exc
    if not GIT_SHA_RE.match(head):
        raise _refuse(
            "RECOVERY_REFUSED_REPOSITORY",
            "the repository HEAD is not a full 40-character commit id",
            EXIT_RUNTIME_FAILURE,
        )
    clean = porcelain == ""
    if require_clean and not clean:
        raise _refuse(
            "RECOVERY_REFUSED_REPOSITORY",
            "the worktree is not clean; a production recovery must be "
            "attributable to an exact reviewed commit",
        )
    return {"repository_head": head, "worktree_clean": clean}


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def platform_dsn_from_env() -> str:
    host = os.getenv("POSTGRES_HOST") or "127.0.0.1"
    port = os.getenv("POSTGRES_PORT") or "5432"
    database = os.getenv("POSTGRES_DB")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    missing = [
        name for name, value in (
            ("POSTGRES_DB", database),
            ("POSTGRES_USER", user),
            ("POSTGRES_PASSWORD", password),
        ) if not value
    ]
    if missing:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            f"missing platform connection variables: {', '.join(missing)}",
            EXIT_INVALID_PARAMETERS,
        )
    return (
        f"host={host} port={port} dbname={database} "
        f"user={user} password={password}"
    )


def open_read_only_connection(dsn: str):
    """A connection PostgreSQL itself refuses to write through."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "RECOVERY_RUNTIME_FAILURE", "psycopg is required",
            EXIT_RUNTIME_FAILURE,
        ) from exc
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '60s'")
    except Exception:
        conn.close()
        raise
    return conn


def open_write_connection(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=False, row_factory=dict_row)


def verify_platform_identity(
    cur, *, expected_environment: str, expected_platform_uuid: str,
) -> Dict[str, Any]:
    cur.execute(
        "SELECT to_regclass('ops_control.environment_identity')::text AS marker"
    )
    if not (cur.fetchone() or {}).get("marker"):
        raise _refuse(
            "RECOVERY_REFUSED_IDENTITY",
            "ops_control.environment_identity does not exist",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    cur.execute(
        """
        SELECT identity_key, environment,
               database_identity_id::text AS database_identity_id,
               database_role, database_name
          FROM ops_control.environment_identity
         ORDER BY identity_key
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1 or rows[0].get("identity_key") != "primary":
        raise _refuse(
            "RECOVERY_REFUSED_IDENTITY",
            "the identity table must hold exactly the primary marker row",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    marker = rows[0]
    if str(marker.get("environment")) != expected_environment:
        raise _refuse(
            "RECOVERY_REFUSED_IDENTITY",
            "database environment does not match --expected-environment",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_identity_id")) != expected_platform_uuid:
        raise _refuse(
            "RECOVERY_REFUSED_IDENTITY",
            "database identity UUID does not match --expected-platform-uuid",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_role")) != "platform":
        raise _refuse(
            "RECOVERY_REFUSED_IDENTITY",
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
            "RECOVERY_REFUSED_SCHEMA",
            f"required migration(s) not applied: {', '.join(missing)}",
        )
    cur.execute(
        "SELECT to_regclass("
        "'workflow_a_control.client_dataset_recovery_run')::text AS t"
    )
    if not (cur.fetchone() or {}).get("t"):
        raise _refuse(
            "RECOVERY_REFUSED_SCHEMA",
            "workflow_a_control.client_dataset_recovery_run does not exist",
        )


# ---------------------------------------------------------------------------
# Target resolution and gates
# ---------------------------------------------------------------------------

def resolve_client(cur, *, client_code: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code,
               trips_pagination_mode,
               trips_stabilization_delay_seconds,
               trips_overlap_seconds,
               trips_max_recovery_span_seconds
          FROM workflow_a_control.client_account
         WHERE client_code = %s
        """,
        (client_code,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1:
        raise _refuse(
            "RECOVERY_REFUSED_TARGET",
            f"client_code {client_code} resolves to {len(rows)} clients; "
            "exactly one is required",
        )
    return rows[0]


def resolve_schedule(
    cur,
    *,
    client_id: str,
    dataset_name: str,
    cold_start: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the one authoritative BASE schedule for client × dataset.

    Default (``cold_start is None``) behavior is unchanged: exactly one
    **enabled** schedule, anything else refused.

    On the explicit cold-start path the requirement inverts and tightens: there
    must be exactly one schedule row in total — so no competing schedule of
    either kind exists — and that row must be **disabled**. An enabled schedule
    is refused there, because a schedule that the dispatcher can already claim
    has left the cold-start state.

    **Scoped to ``run_type = 'DAILY'`` since M5.** A manual recovery closes an
    interval of *forward coverage*, and forward coverage belongs to the base
    schedule; a reconciliation cadence shares the watermark but does not own it.
    Without this scope the "exactly one row" requirements above would start
    counting reconciliation rows the moment M6 registers one and would refuse
    every cold-start recovery for a reason that has nothing to do with the
    client's state. Today the scope changes nothing: every schedule row is base.
    """
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,
               client_code, dataset_name, enabled, frequency, day_of_week,
               day_of_month, day_of_month_last, run_time::text AS run_time,
               timezone, lookback_days, overwrite_existing, event_enrichment_mode,
               run_type
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s
           AND run_type = %s
         ORDER BY schedule_id
        """,
        (client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if cold_start is None:
        enabled = [row for row in rows if row["enabled"]]
        if len(enabled) != 1:
            raise _refuse(
                "RECOVERY_REFUSED_TARGET",
                f"{dataset_name} resolves to {len(enabled)} enabled schedules for "
                "this client; exactly one authoritative schedule is required",
            )
        return enabled[0]

    if len(rows) != 1:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_SCHEDULE",
            f"{dataset_name} resolves to {len(rows)} schedule rows for this "
            "client; the cold-start path requires exactly one authoritative "
            "schedule and no competing enabled or disabled schedule",
        )
    schedule = rows[0]
    if bool(schedule["enabled"]):
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_SCHEDULE",
            "the authoritative schedule is enabled; "
            "--allow-disabled-schedule-for-cold-start exists only for a "
            "schedule that has never been allowed to fire and must not be used "
            "to recover an active client",
        )
    if str(schedule["schedule_id"]) != str(cold_start["expected_schedule_id"]):
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_SCHEDULE",
            "the resolved schedule does not match --expected-schedule-id",
        )
    return schedule


def _running_scheduled_fires(cur, *, schedule_id: str) -> int:
    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s AND status = 'RUNNING'
        """,
        (schedule_id,),
    )
    return int((cur.fetchone() or {})["n"])


def _active_recoveries(cur, *, schedule_id: str) -> int:
    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_dataset_recovery_run
         WHERE schedule_id = %s AND status IN ('PLANNED', 'RUNNING')
        """,
        (schedule_id,),
    )
    return int((cur.fetchone() or {})["n"])


def _duplicate_approved_recoveries(
    cur, *, client_id: str, schedule_id: str,
    window_start_ts: datetime, window_end_ts: datetime, approval_ref: str,
) -> int:
    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_dataset_recovery_run
         WHERE client_id = %s AND schedule_id = %s
           AND window_start_ts = %s AND window_end_ts = %s
           AND approval_ref = %s
        """,
        (client_id, schedule_id, window_start_ts, window_end_ts, approval_ref),
    )
    return int((cur.fetchone() or {})["n"])


def _total_history_rows(cur, *, schedule_id: str, client_id: str) -> int:
    """Every history row attributable to the target, for every status."""
    cur.execute(
        """
        SELECT count(*) AS n
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s OR client_id = %s
        """,
        (schedule_id, client_id),
    )
    return int((cur.fetchone() or {})["n"])


def _target_platform_runs(cur, *, client_id: str, client_code: str) -> list:
    """Every platform business run attributable to the target, with its status.

    Returned as rows rather than a count because the chain contract needs both
    directions of the correspondence: which runs a chain claims, and which target
    runs no chain window claims.
    """
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


def _target_recovery_rows(cur, *, schedule_id: str, client_id: str) -> list:
    """Every recovery row attributable to the target, for every status."""
    cur.execute(
        """
        SELECT recovery_run_id::text AS recovery_run_id,
               client_id::text AS client_id,
               schedule_id::text AS schedule_id,
               dataset_name, status, approval_ref,
               window_start_ts, window_end_ts,
               platform_run_id::text AS platform_run_id,
               created_at
          FROM workflow_a_control.client_dataset_recovery_run
         WHERE schedule_id = %s OR client_id = %s
         ORDER BY created_at, recovery_run_id
        """,
        (schedule_id, client_id),
    )
    return [dict(row) for row in cur.fetchall()]


def _chain_refusal_code(error: ChainContractError) -> str:
    """Map a pure chain violation onto this module's refusal vocabulary."""
    if error.code.startswith("CHAIN_RUN_"):
        return "RECOVERY_REFUSED_COLD_START_RUNS"
    return "RECOVERY_REFUSED_COLD_START_CHAIN"


def _evaluate_cold_start_gates(
    cur,
    *,
    cold_start: Dict[str, Any],
    client: Dict[str, Any],
    schedule: Dict[str, Any],
    coverage: Dict[str, Any],
    coverage_fingerprint_value: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    max_recovery_span_seconds: int,
) -> Dict[str, Any]:
    """Additional refusals that only the explicit cold-start path evaluates.

    Every condition here is *narrowing*: it can only reject a state the normal
    gates already accepted. None of them relaxes an existing rule, so this
    function cannot make the historical path more permissive even if it were
    called with historical inputs.

    The chain split lives here. Which branch applies is decided by evidence, not
    by an operator flag: a target with no row of this chain is a first window and
    must be provably empty; a target that already carries chain rows is a
    continuation and must be provably *only* this chain's successful prefix.
    """
    chain_ref = str(cold_start["chain_ref"])
    requested_ordinal = int(cold_start["window_ordinal"])
    final_boundary = cold_start["approved_final_chain_boundary_ts"]

    if bool(schedule["enabled"]):  # pragma: no cover - resolver refuses first
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_SCHEDULE",
            "the authoritative schedule is enabled",
        )

    # --- conditions that hold for every window of every chain ---------------
    evidence_ref = str(coverage.get("bootstrap_evidence_ref") or "")
    if not evidence_ref.startswith(COLD_START_EVIDENCE_REF_PREFIX):
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_COVERAGE",
            "the coverage row was not created from accepted cold-start "
            "evidence; a historically bootstrapped row is never recovered "
            "through the disabled-schedule path",
        )
    if coverage_fingerprint_value != str(cold_start["expected_coverage_fingerprint"]):
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_COVERAGE",
            "--expected-coverage-fingerprint does not match the stored "
            "coverage row; the authorization is stale and is never adjusted here",
        )
    coverage_start = coverage.get("coverage_start_ts")
    covered_through = coverage.get("covered_through_ts")
    if coverage_start is None or covered_through is None:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_COVERAGE",
            "the coverage row does not carry both bounds",
        )
    coverage_start = coverage_start.astimezone(timezone.utc)
    covered_through = covered_through.astimezone(timezone.utc)

    # The schedule has never been allowed to fire, in any window of the chain.
    history_rows = _total_history_rows(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"],
    )
    if history_rows:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_HISTORY",
            f"{history_rows} schedule-history row(s) exist for this target; the "
            "cold-start path is only for a client that has never been scheduled",
        )

    recovery_rows = _target_recovery_rows(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"],
    )
    partitioned = partition_recovery_rows(recovery_rows, chain_ref=chain_ref)
    if partitioned["foreign"]:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_RECOVERY",
            f"{len(partitioned['foreign'])} recovery row(s) exist for this "
            "target that do not belong to the declared cold-start chain; "
            "unrelated execution state is never continued through",
        )
    platform_runs = _target_platform_runs(
        cur,
        client_id=client["client_id"],
        client_code=str(client["client_code"]),
    )

    if not partitioned["chain"]:
        # --- first window of the chain: the original zero-state contract ----
        if requested_ordinal != 1:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_CHAIN",
                f"--approval-ref declares window {requested_ordinal} but this "
                "chain has no successful window yet; the first window of a "
                "chain is always W01",
            )
        if str(coverage.get("covered_through_source")) != COVERAGE_SOURCE_BOOTSTRAP:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_COVERAGE",
                "the coverage watermark has already been moved by an execution; "
                "the first window of a cold-start chain may only run against an "
                "untouched baseline",
            )
        if coverage_start != covered_through:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_COVERAGE",
                "the coverage row is not a zero-width cold-start baseline "
                "(A must equal W)",
            )
        if platform_runs:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_RUNS",
                f"{len(platform_runs)} platform run(s) reference this target; "
                "it is not a client that has never run",
            )
        chain_summary: Dict[str, Any] = {
            "chain_semantics_version": CHAIN_SEMANTICS_VERSION,
            "successful_window_count": 0,
            "windows": [],
            "next_window_ordinal": 1,
            "next_window_start_ts": _iso(coverage_start),
        }
        run_correspondence = {
            "chain_business_runs": 0,
            "target_business_runs": 0,
            "unrelated_business_runs": 0,
        }
    else:
        # --- a subsequent window: the target must be exactly this chain -----
        if str(coverage.get("covered_through_source")) != COVERAGE_SOURCE_MANUAL_RECOVERY:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_COVERAGE",
                "the chain has already advanced W, so covered_through_source "
                f"must be {COVERAGE_SOURCE_MANUAL_RECOVERY!r}; it is "
                f"{coverage.get('covered_through_source')!r}",
            )
        if coverage_start >= covered_through:
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_COVERAGE",
                "a continued chain must have advanced W strictly past the "
                "original baseline A",
            )
        try:
            chain_summary = evaluate_chain(
                partitioned["chain"],
                baseline_ts=coverage_start,
                current_covered_through_ts=covered_through,
                client_id=str(client["client_id"]),
                schedule_id=str(schedule["schedule_id"]),
                dataset_name=str(coverage.get("dataset_name")),
            )
            run_correspondence = evaluate_business_run_correspondence(
                chain_summary, target_runs=platform_runs,
            )
        except ChainContractError as exc:
            raise _refuse(_chain_refusal_code(exc), str(exc)) from exc
        if requested_ordinal != int(chain_summary["next_window_ordinal"]):
            raise _refuse(
                "RECOVERY_REFUSED_COLD_START_CHAIN",
                f"--approval-ref declares window {requested_ordinal} but the "
                f"next contiguous window of this chain is "
                f"{chain_summary['next_window_ordinal']}",
            )

    # --- the deterministic split, re-derived from live state -----------------
    #
    # Planned from the *current* watermark rather than from A: that is the only
    # instant a new window may start at, and re-planning from it keeps the split
    # correct even if an earlier window was closed short.
    if final_boundary < window_end_ts:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_BOUNDARY",
            "--approved-final-chain-boundary is before --window-end; the chain "
            "boundary is the end of the whole chain, never of one window",
        )
    if final_boundary <= covered_through:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_BOUNDARY",
            "--approved-final-chain-boundary is not after the current "
            "watermark; the chain is already complete and needs activation, "
            "not another recovery",
        )
    try:
        remaining = plan_recovery_windows(
            start=covered_through,
            final_end=final_boundary,
            max_span_seconds=int(max_recovery_span_seconds),
        )
    except ChainContractError as exc:
        raise _refuse("RECOVERY_REFUSED_COLD_START_CHAIN", str(exc)) from exc

    planned_start, planned_end = remaining[0]
    if window_start_ts != planned_start:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_BOUNDARY",
            "--window-start must equal the current coverage watermark, so the "
            "chain joins without a hole",
        )
    if window_end_ts != planned_end:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_BOUNDARY",
            "--window-end must equal the deterministic next boundary "
            f"{_iso(planned_end)} for this chain: a non-final window spans "
            "exactly trips_max_recovery_span_seconds and the final window ends "
            "exactly at --approved-final-chain-boundary",
        )
    approved_boundary = cold_start["approved_shifted_cutoff_boundary_ts"]
    if window_end_ts != approved_boundary:
        raise _refuse(
            "RECOVERY_REFUSED_COLD_START_BOUNDARY",
            "--window-end must equal the explicitly approved boundary for this "
            "window; it is never widened or narrowed here",
        )

    return {
        "cold_start_schedule_enabled": False,
        "cold_start_chain_ref": chain_ref,
        "cold_start_chain_semantics_version": CHAIN_SEMANTICS_VERSION,
        "cold_start_window_ordinal": requested_ordinal,
        "cold_start_baseline_instant": _iso(coverage_start),
        "cold_start_current_watermark": _iso(covered_through),
        "cold_start_evidence_ref_prefix": COLD_START_EVIDENCE_REF_PREFIX,
        "cold_start_history_rows": history_rows,
        "cold_start_recovery_rows": len(recovery_rows),
        "cold_start_foreign_recovery_rows": 0,
        "cold_start_platform_runs": len(platform_runs),
        "cold_start_approved_boundary_ts": _iso(approved_boundary),
        "cold_start_approved_final_chain_boundary_ts": _iso(final_boundary),
        "cold_start_chain_completed_windows": chain_summary["windows"],
        "cold_start_chain_successful_window_count": chain_summary[
            "successful_window_count"
        ],
        "cold_start_chain_remaining_plan": serialize_window_plan(
            remaining, chain_ref=chain_ref, start_ordinal=requested_ordinal,
        ),
        "cold_start_chain_total_windows": (
            int(chain_summary["successful_window_count"]) + len(remaining)
        ),
        "cold_start_chain_business_run_correspondence": run_correspondence,
        "cold_start_is_final_window": len(remaining) == 1,
    }


def _compatibility_client_codes(cur) -> list:
    cur.execute(
        """
        SELECT client_code
          FROM workflow_a_control.client_account
         WHERE trips_pagination_mode = %s
         ORDER BY client_code
        """,
        (TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,),
    )
    return [str(row["client_code"]) for row in cur.fetchall()]


def evaluate_gates(
    cur,
    *,
    client_code: str,
    dataset_name: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    expected_old_covered_through_ts: datetime,
    approval_ref: str,
    db_now: datetime,
    cold_start: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Every refusal condition, evaluated read-only against current state.

    Re-run verbatim under the claim locks, so a change between planning and
    claiming cannot slip through.

    ``cold_start`` is ``None`` for every historical recovery and the evaluation
    is then byte-identical to the pre-cold-start contract. When supplied it adds
    the narrowing gates of ``_evaluate_cold_start_gates`` on top of — never
    instead of — the gates below.
    """
    if dataset_name != TRIPS_SYNC_DATASET_NAME:
        raise _refuse(
            "RECOVERY_REFUSED_TARGET",
            f"manual compatibility recovery is {TRIPS_SYNC_DATASET_NAME}-only",
            EXIT_INVALID_PARAMETERS,
        )

    client = resolve_client(cur, client_code=client_code)
    if str(client["trips_pagination_mode"]) != TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1:
        raise _refuse(
            "RECOVERY_REFUSED_MODE",
            "the client is not data_invariants_v1; a strict_meta client has no "
            "coverage semantics and must not be recovered through this tool",
        )

    schedule = resolve_schedule(
        cur,
        client_id=client["client_id"],
        dataset_name=dataset_name,
        cold_start=cold_start,
    )

    coverage = read_coverage_row(
        cur, client_id=client["client_id"], dataset_name=dataset_name,
    )
    if coverage is None:
        raise _refuse(
            "RECOVERY_REFUSED_COVERAGE",
            "no coverage row exists for this client and dataset; a reviewed "
            "bootstrap must precede any recovery",
        )
    if str(coverage.get("bootstrap_status")) != COVERAGE_STATUS_READY:
        raise _refuse(
            "RECOVERY_REFUSED_COVERAGE",
            "the coverage row is "
            f"{coverage.get('bootstrap_status')}, expected READY",
        )
    if str(coverage.get("client_id")) != str(client["client_id"]):
        raise _refuse(
            "RECOVERY_REFUSED_COVERAGE",
            "the coverage row identity does not match the resolved client",
        )
    if str(coverage.get("dataset_name")) != dataset_name:
        raise _refuse(
            "RECOVERY_REFUSED_COVERAGE",
            "the coverage row dataset does not match the requested dataset",
        )

    current_w = coverage.get("covered_through_ts")
    if current_w is None:
        raise _refuse(
            "RECOVERY_REFUSED_COVERAGE",
            "the coverage row has no covered_through_ts",
        )
    current_w = current_w.astimezone(timezone.utc)
    if current_w != expected_old_covered_through_ts:
        raise _refuse(
            "RECOVERY_REFUSED_WATERMARK",
            "--expected-old-covered-through does not match the stored "
            "watermark; the authorization is stale and is never adjusted here",
        )
    if window_start_ts != expected_old_covered_through_ts:
        raise _refuse(
            "RECOVERY_REFUSED_WINDOW",
            "--window-start must equal the expected old watermark, so a "
            "success cannot claim an interval the run did not fetch",
        )
    if window_end_ts <= window_start_ts:
        raise _refuse(
            "RECOVERY_REFUSED_WINDOW",
            "--window-end must be strictly after --window-start; a recovery "
            "that cannot advance W is an operator error",
        )

    delay = int(client["trips_stabilization_delay_seconds"])
    overlap = int(client["trips_overlap_seconds"])
    max_span = int(client["trips_max_recovery_span_seconds"])

    span_seconds = int((window_end_ts - window_start_ts).total_seconds())
    if span_seconds > max_span:
        raise _refuse(
            "RECOVERY_REFUSED_WINDOW",
            f"the requested interval spans {span_seconds}s, above the client's "
            f"trips_max_recovery_span_seconds of {max_span}s",
        )

    if window_end_ts > db_now:
        raise _refuse(
            "RECOVERY_REFUSED_WINDOW",
            "--window-end is in the future relative to the database clock",
        )
    eligibility_boundary = db_now - timedelta(seconds=delay)
    if window_end_ts > eligibility_boundary:
        raise _refuse(
            "RECOVERY_REFUSED_WINDOW",
            "--window-end is inside the stabilization delay; the compatibility "
            "eligibility guard would refuse it before any provider request",
        )

    running_fires = _running_scheduled_fires(
        cur, schedule_id=schedule["schedule_id"],
    )
    if running_fires:
        raise _refuse(
            "RECOVERY_REFUSED_CONCURRENCY",
            f"{running_fires} RUNNING scheduled-history row(s) exist for this "
            "schedule",
        )
    active = _active_recoveries(cur, schedule_id=schedule["schedule_id"])
    if active:
        raise _refuse(
            "RECOVERY_REFUSED_CONCURRENCY",
            f"{active} non-terminal recovery run(s) already exist for this "
            "schedule",
        )
    duplicates = _duplicate_approved_recoveries(
        cur,
        client_id=client["client_id"],
        schedule_id=schedule["schedule_id"],
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        approval_ref=approval_ref,
    )
    if duplicates:
        raise _refuse(
            "RECOVERY_REFUSED_DUPLICATE",
            "this exact client/schedule/interval/approval recovery has already "
            "been executed; it is never repeated automatically",
        )

    snapshot = CoverageClaimSnapshot.from_row(coverage)
    fingerprint = coverage_fingerprint(coverage)

    cold_start_evidence: Dict[str, Any] = {}
    if cold_start is not None:
        cold_start_evidence = _evaluate_cold_start_gates(
            cur,
            cold_start=cold_start,
            client=client,
            schedule=schedule,
            coverage=coverage,
            coverage_fingerprint_value=fingerprint,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            max_recovery_span_seconds=max_span,
        )

    return {
        "client": client,
        "schedule": schedule,
        "coverage": coverage,
        "snapshot": snapshot,
        "coverage_fingerprint": fingerprint,
        "stabilization_delay_seconds": delay,
        "overlap_seconds": overlap,
        "max_recovery_span_seconds": max_span,
        "span_seconds": span_seconds,
        "compatibility_clients": _compatibility_client_codes(cur),
        "cold_start": cold_start_evidence,
    }


def _snapshot_json(coverage: Dict[str, Any]) -> Dict[str, Any]:
    """The immutable claim-time projection stored as evidence. No payloads."""
    return {
        "snapshot_version": RECOVERY_SEMANTICS_VERSION,
        "fingerprint_version": COVERAGE_FINGERPRINT_VERSION,
        "schedule_id": str(coverage["schedule_id"]),
        "client_id": str(coverage["client_id"]),
        "client_code": coverage.get("client_code"),
        "dataset_name": coverage.get("dataset_name"),
        "coverage_start_ts": _iso(coverage.get("coverage_start_ts")),
        "covered_through_ts": _iso(coverage.get("covered_through_ts")),
        "bootstrap_status": coverage.get("bootstrap_status"),
        "bootstrap_evidence_ref": coverage.get("bootstrap_evidence_ref"),
        "covered_through_source": coverage.get("covered_through_source"),
        "seeded_at": _iso(coverage.get("seeded_at")),
        "seeded_by": coverage.get("seeded_by"),
        "last_gap_detected_ts": _iso(coverage.get("last_gap_detected_ts")),
        "updated_at": _iso(coverage.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# Stage 1 — claim
# ---------------------------------------------------------------------------

def claim_recovery(conn, *, plan_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """One transaction: lock, re-gate, insert exactly one RUNNING recovery row.

    Documented total lock order, used identically here and in finalization:

        client_account -> client_dataset_schedule -> the coverage row
        -> client_dataset_recovery_run

    The coverage row is locked only through
    `coverage_finalization.lock_coverage_row_for_update`; this module never
    names or writes that table itself, which the static guard proves.

    It creates no `client_schedule_run_history` row and touches none.
    """
    client_code = plan_inputs["client_code"]
    dataset_name = plan_inputs["dataset_name"]
    cold_start = plan_inputs.get("cold_start")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("SELECT date_trunc('second', now()) AS db_now")
        db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)

        # Lock the authoritative configuration for the duration of the claim,
        # so a concurrent mode flip or schedule edit cannot interleave.
        cur.execute(
            "SELECT client_id::text AS client_id, client_code,"
            " trips_pagination_mode"
            "  FROM workflow_a_control.client_account"
            " WHERE client_code = %s FOR UPDATE",
            (client_code,),
        )
        locked_clients = [dict(row) for row in cur.fetchall()]
        if len(locked_clients) != 1:
            raise _refuse(
                "RECOVERY_REFUSED_TARGET",
                "the client disappeared or became ambiguous under the lock",
            )
        locked_client_id = str(locked_clients[0]["client_id"])
        # Base schedules only, for the reason given in `resolve_schedule`.
        cur.execute(
            "SELECT schedule_id::text AS schedule_id, enabled"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE client_id = %s"
            "   AND dataset_name = %s"
            "   AND run_type = %s"
            " ORDER BY schedule_id FOR UPDATE",
            (locked_client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
        )
        locked_schedules = [dict(row) for row in cur.fetchall()]
        if cold_start is None:
            if len([row for row in locked_schedules if row["enabled"]]) != 1:
                raise _refuse(
                    "RECOVERY_REFUSED_TARGET",
                    "the authoritative schedule became ambiguous under the lock",
                )
        else:
            # Under the lock the cold-start requirement is the same inversion as
            # in the resolver: exactly one row, still disabled. A schedule that
            # was enabled between planning and claiming is a different, active
            # client and must not be recovered through this path.
            if len(locked_schedules) != 1:
                raise _refuse(
                    "RECOVERY_REFUSED_COLD_START_SCHEDULE",
                    "the authoritative schedule became ambiguous under the lock",
                )
            if bool(locked_schedules[0]["enabled"]):
                raise _refuse(
                    "RECOVERY_REFUSED_COLD_START_SCHEDULE",
                    "the schedule was enabled between planning and claiming; a "
                    "cold-start recovery runs only while it stays disabled",
                )
            if str(locked_schedules[0]["schedule_id"]) != str(
                plan_inputs["schedule_id"]
            ):
                raise _refuse(
                    "RECOVERY_REFUSED_COLD_START_SCHEDULE",
                    "the locked schedule identity changed between planning and "
                    "claiming",
                )

        # Coverage is always locked before the recovery-identity row.
        #
        # Since M5 this locks the row owned by (client_id, dataset_name), which
        # is shared by every cadence over the dataset. That is what makes the
        # re-keyed `uq_client_dataset_recovery_run_active` and this lock agree:
        # one watermark, one active recovery, one serialization point.
        lock_coverage_row_for_update(
            cur,
            client_id=locked_client_id,
            dataset_name=dataset_name,
        )

        gates = evaluate_gates(
            cur,
            client_code=client_code,
            dataset_name=dataset_name,
            window_start_ts=plan_inputs["window_start_ts"],
            window_end_ts=plan_inputs["window_end_ts"],
            expected_old_covered_through_ts=plan_inputs[
                "expected_old_covered_through_ts"
            ],
            approval_ref=plan_inputs["approval_ref"],
            db_now=db_now,
            cold_start=cold_start,
        )
        if gates["schedule"]["schedule_id"] != plan_inputs["schedule_id"]:
            raise _refuse(
                "RECOVERY_REFUSED_TARGET",
                "the resolved schedule changed between planning and claiming",
            )
        if gates["coverage_fingerprint"] != plan_inputs["coverage_fingerprint"]:
            raise _refuse(
                "RECOVERY_REFUSED_COVERAGE",
                "the coverage row changed between planning and claiming",
            )

        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_recovery_run (
                client_id, client_code, schedule_id, dataset_name,
                window_start_ts, window_end_ts,
                expected_old_covered_through_ts,
                status, reason, approval_ref, repository_head,
                pagination_mode, stabilization_delay_seconds,
                overlap_seconds, max_recovery_span_seconds,
                initial_coverage_snapshot, initial_coverage_fingerprint,
                created_at, started_at, updated_at
            ) VALUES (
                %(client_id)s, %(client_code)s, %(schedule_id)s,
                %(dataset_name)s, %(window_start_ts)s, %(window_end_ts)s,
                %(expected_old_covered_through_ts)s,
                'RUNNING', %(reason)s, %(approval_ref)s, %(repository_head)s,
                %(pagination_mode)s, %(stabilization_delay_seconds)s,
                %(overlap_seconds)s, %(max_recovery_span_seconds)s,
                %(initial_coverage_snapshot)s, %(initial_coverage_fingerprint)s,
                %(now)s, %(now)s, %(now)s
            )
            RETURNING recovery_run_id::text AS recovery_run_id
            """,
            {
                "client_id": gates["client"]["client_id"],
                "client_code": client_code,
                "schedule_id": gates["schedule"]["schedule_id"],
                "dataset_name": dataset_name,
                "window_start_ts": plan_inputs["window_start_ts"],
                "window_end_ts": plan_inputs["window_end_ts"],
                "expected_old_covered_through_ts": plan_inputs[
                    "expected_old_covered_through_ts"
                ],
                "reason": plan_inputs["reason"],
                "approval_ref": plan_inputs["approval_ref"],
                "repository_head": plan_inputs["repository_head"],
                "pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
                "stabilization_delay_seconds": gates[
                    "stabilization_delay_seconds"
                ],
                "overlap_seconds": gates["overlap_seconds"],
                "max_recovery_span_seconds": gates["max_recovery_span_seconds"],
                "initial_coverage_snapshot": json.dumps(
                    _snapshot_json(gates["coverage"]),
                    sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "initial_coverage_fingerprint": gates["coverage_fingerprint"],
                "now": db_now,
            },
        )
        recovery_run_id = (cur.fetchone() or {})["recovery_run_id"]
    conn.commit()
    return {
        "recovery_run_id": recovery_run_id,
        "gates": gates,
        "claimed_at": db_now,
    }


# ---------------------------------------------------------------------------
# Stage 2 — business execution through the standard runner contract
# ---------------------------------------------------------------------------

def build_job_params(
    *,
    client_id: str,
    client_code: str,
    event_enrichment_mode: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    recovery_run_id: str,
    schedule_id: str,
    schedule_disabled: bool,
) -> Dict[str, Any]:
    """Explicit literal window, compatibility mode, recovery identity.

    The window is passed literally and is never shifted: shifting happens
    exactly once, in `dispatcher.evaluate_schedule`, and never for a manual or
    recovery run (`docs/13_…` §12).

    On the cold-start path the parameters additionally carry the explicit
    disabled-schedule opt-in and the exact schedule this recovery is authorized
    against. They are one half of the manual-recovery authority and are
    worthless on their own: the business job also requires the launch
    attestation `launch_sync` puts in the environment *and* a matching `RUNNING`
    recovery row. For an enabled schedule neither key is emitted and the job's
    behavior is unchanged.
    """
    params = {
        "client_id": client_id,
        "client_code": client_code,
        "trigger": RECOVERY_TRIGGER,
        "actor": "ops/recover_telematics_trips_window.py",
        "window_start_ts": _iso(window_start_ts),
        "window_end_ts": _iso(window_end_ts),
        "event_enrichment_mode": event_enrichment_mode,
        "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
        "manual_recovery_run_id": recovery_run_id,
    }
    if schedule_disabled:
        params[manual_recovery_authority.PARAM_DISABLED_SCHEDULE_FLAG] = True
        params[manual_recovery_authority.PARAM_EXPECTED_SCHEDULE_ID] = str(
            schedule_id
        )
    return params


def launch_sync(
    *,
    job_params: Dict[str, Any],
    authority: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the production sync once through `ops/runner.py`. Never retried.

    Two out-of-band channels are established for exactly this one subprocess:

      * `TELEMATICS_MANUAL_RECOVERY_AUTHORITY` — the launch attestation, present
        only for a disabled-schedule cold-start recovery. It is an operational
        attestation carried out-of-band, which an ordinary hand-typed runner
        command does not carry. It is **not** authentication: it holds no secret
        and a same-user process that knows the identities can construct an
        equivalent string. What it cannot do is authorize anything on its own —
        the business job re-checks every identity in it against the job
        parameters and against the durable `RUNNING` recovery row;
      * `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE` — where the job writes its single
        strictly parsed terminal record. The record, not the return code, is what
        the coverage gate reads.

    Both live in a private temporary directory that is removed when this
    function returns, so the record is read here and nowhere else.
    """
    cmd = [
        sys.executable or "python3",
        "ops/runner.py",
        SYNC_JOB_MODULE,
        json.dumps(job_params),
    ]
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) if not pythonpath
        else f"{REPO_ROOT}{os.pathsep}{pythonpath}"
    )
    # Never inherit an attestation from this tool's own environment: the only
    # authority a subprocess may see is the one this invocation just built.
    manual_recovery_authority.strip_authority_from_env(env)
    if authority:
        env[manual_recovery_authority.MANUAL_RECOVERY_AUTHORITY_ENV] = authority
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="telematics-recovery-run-id-") as tmp:
        run_id_file = Path(tmp) / "platform_run_id.txt"
        outcome_file = Path(tmp) / "execution_outcome.json"
        env["LOG_PLATFORM_RUN_ID_FILE"] = str(run_id_file)
        env[EXECUTION_OUTCOME_FILE_ENV] = str(outcome_file)
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        platform_run_id = None
        try:
            raw = run_id_file.read_text(encoding="utf-8").strip()
            platform_run_id = str(uuid.UUID(raw)) if raw else None
        except (OSError, ValueError):
            platform_run_id = None
        # Read inside the temporary directory's lifetime. An absent or
        # unparseable record is carried as a structured error, never as silence.
        execution_outcome: Optional[Dict[str, Any]] = None
        execution_outcome_error: Optional[str] = None
        try:
            execution_outcome = read_outcome(outcome_file).as_dict()
        except ExecutionOutcomeError as exc:
            execution_outcome_error = str(exc)
    finished = datetime.now(timezone.utc)
    return {
        "returncode": proc.returncode,
        "platform_run_id": platform_run_id,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": int((finished - started).total_seconds()),
        # stdout is deliberately not persisted; stderr is bounded to the tail.
        "stderr_tail": (proc.stderr or "")[-MAX_ERROR_SUMMARY_CHARS:],
        "sanitized_command": (
            f"{cmd[0]} ops/runner.py {SYNC_JOB_MODULE} <params>"
        ),
        "execution_outcome": execution_outcome,
        "execution_outcome_error": execution_outcome_error,
    }


def _platform_run_status(cur, *, platform_run_id: Optional[str]) -> Optional[str]:
    if not platform_run_id:
        return None
    cur.execute(
        "SELECT status FROM public.runs WHERE run_id = %s", (platform_run_id,),
    )
    row = cur.fetchone()
    return None if not row else str(row["status"])


# ---------------------------------------------------------------------------
# Stage 3 — finalization
# ---------------------------------------------------------------------------

def _update_recovery_terminal(
    cur,
    *,
    recovery_run_id: str,
    status: str,
    finished_at: datetime,
    job_summary: Optional[Dict[str, Any]],
    provider_summary: Optional[Dict[str, Any]],
    platform_run_id: Optional[str],
    finalizer_result: Optional[Dict[str, Any]] = None,
    final_coverage_fingerprint: Optional[str] = None,
    error_classification: Optional[str] = None,
    error_summary: Optional[str] = None,
) -> int:
    cur.execute(
        """
        UPDATE workflow_a_control.client_dataset_recovery_run
           SET status = %(status)s,
               finished_at = %(finished_at)s,
               updated_at = %(finished_at)s,
               platform_run_id = %(platform_run_id)s,
               job_summary = %(job_summary)s,
               provider_summary = %(provider_summary)s,
               finalizer_result = %(finalizer_result)s,
               final_coverage_fingerprint = %(final_coverage_fingerprint)s,
               error_classification = %(error_classification)s,
               error_summary = %(error_summary)s
         WHERE recovery_run_id = %(recovery_run_id)s
           AND status = 'RUNNING'
        """,
        {
            "status": status,
            "finished_at": finished_at,
            "platform_run_id": platform_run_id,
            "job_summary": (
                None if job_summary is None
                else json.dumps(job_summary, sort_keys=True, default=str)
            ),
            "provider_summary": (
                None if provider_summary is None
                else json.dumps(provider_summary, sort_keys=True, default=str)
            ),
            "finalizer_result": (
                None if finalizer_result is None
                else json.dumps(finalizer_result, sort_keys=True, default=str)
            ),
            "final_coverage_fingerprint": final_coverage_fingerprint,
            "error_classification": error_classification,
            "error_summary": (
                None if error_summary is None
                else error_summary[-MAX_ERROR_SUMMARY_CHARS:]
            ),
            "recovery_run_id": recovery_run_id,
        },
    )
    return cur.rowcount


def finalize_success(
    conn,
    *,
    recovery_run_id: str,
    schedule_id: str,
    snapshot: CoverageClaimSnapshot,
    window_end_ts: datetime,
    execution: Dict[str, Any],
) -> Dict[str, Any]:
    """Advance W atomically with the recovery terminal state, or refuse.

    Lock order: coverage row, then the recovery-identity row — the same
    "coverage first" rule the scheduled finalizer uses for coverage/history.
    """
    mutation_ts = datetime.now(timezone.utc).replace(microsecond=0)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        coverage_rows = lock_coverage_row_for_update(
            cur,
            client_id=snapshot.client_id,
            dataset_name=snapshot.dataset_name,
        )
        if len(coverage_rows) != 1 or not snapshot_matches_row(
            coverage_rows[0], snapshot
        ):
            raise CoverageCasConflict(
                TRIPS_COVERAGE_ADVANCE_CONFLICT,
                detail=(
                    "the coverage row changed between the recovery claim and "
                    "finalization"
                ),
                schedule_id=schedule_id,
                client_id=snapshot.client_id,
                dataset_name=snapshot.dataset_name,
            )

        cur.execute(
            """
            SELECT recovery_run_id::text AS recovery_run_id, status
              FROM workflow_a_control.client_dataset_recovery_run
             WHERE recovery_run_id = %s AND status = 'RUNNING'
             FOR UPDATE
            """,
            (recovery_run_id,),
        )
        if len(cur.fetchall()) != 1:
            raise _refuse(
                "RECOVERY_REFUSED_CLAIM_LOST",
                "the recovery row is no longer RUNNING; its terminal state is "
                "never overwritten",
            )

        advance = advance_covered_through_cas(
            cur,
            snapshot=snapshot,
            candidate_covered_through_ts=window_end_ts,
            source=COVERAGE_SOURCE_MANUAL_RECOVERY,
            mutation_ts=mutation_ts,
            require_advance=True,
        )
        final_row = read_coverage_row(
            cur,
            client_id=snapshot.client_id,
            dataset_name=snapshot.dataset_name,
        )
        final_fingerprint = coverage_fingerprint(final_row or {})

        affected = _update_recovery_terminal(
            cur,
            recovery_run_id=recovery_run_id,
            status="SUCCESS",
            finished_at=mutation_ts,
            job_summary=execution["job_summary"],
            provider_summary=execution["provider_summary"],
            platform_run_id=execution["platform_run_id"],
            finalizer_result=advance.as_dict(),
            final_coverage_fingerprint=final_fingerprint,
        )
        if affected != 1:
            raise _refuse(
                "RECOVERY_REFUSED_CLAIM_LOST",
                "the recovery terminal update affected "
                f"{affected} row(s); expected exactly one",
            )
    conn.commit()
    return {
        "finalizer_result": advance.as_dict(),
        "final_coverage_fingerprint": final_fingerprint,
        "mutation_ts": _iso(mutation_ts),
    }


def finalize_non_success(
    conn,
    *,
    recovery_run_id: str,
    client_id: str,
    dataset_name: str,
    status: str,
    error_classification: str,
    error_summary: str,
    execution: Dict[str, Any],
    initial_fingerprint: str,
) -> Dict[str, Any]:
    """Record a terminal non-success and prove coverage is byte-unchanged.

    The coverage finalizer is never called on this path, so no coverage
    statement of any kind is issued.
    """
    finished_at = datetime.now(timezone.utc).replace(microsecond=0)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        observed = read_coverage_row(
            cur, client_id=client_id, dataset_name=dataset_name,
        )
        observed_fingerprint = coverage_fingerprint(observed or {})
        coverage_unchanged = observed_fingerprint == initial_fingerprint

        affected = _update_recovery_terminal(
            cur,
            recovery_run_id=recovery_run_id,
            status=status,
            finished_at=finished_at,
            job_summary=execution["job_summary"],
            provider_summary=execution["provider_summary"],
            platform_run_id=execution["platform_run_id"],
            error_classification=error_classification,
            error_summary=error_summary,
        )
        if affected != 1:
            raise _refuse(
                "RECOVERY_REFUSED_CLAIM_LOST",
                "the recovery terminal update affected "
                f"{affected} row(s); expected exactly one",
            )
    conn.commit()
    return {
        "coverage_unchanged": coverage_unchanged,
        "observed_coverage_fingerprint": observed_fingerprint,
        "finished_at": _iso(finished_at),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def evaluate_execution_evidence(
    *,
    result: Dict[str, Any],
    orchestration_error: Optional[str],
    client_id: str,
    client_code: str,
    schedule_id: str,
    dataset_name: str,
    recovery_run_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
) -> Dict[str, Any]:
    """The coverage-finalization gate. Pure; every input is already collected.

    Coverage may advance only when **all** of the following hold:

        the subprocess exited 0
        AND the structured terminal outcome is EXECUTED_COMMITTED
            or EXECUTED_ZERO_ROWS_COMMITTED
        AND transaction_status is COMMITTED
        AND client, schedule, dataset, recovery and window identities match

    It must not advance when the outcome is a skip of any kind, is absent, is
    malformed, reports no committed transaction, or belongs to another or a
    stale execution. A zero-row committed execution is valid work and does
    advance; a skipped zero-work execution never does, however clean its exit
    code was.

    Returns the verdict plus bounded evidence for the recovery row. It performs
    no I/O and decides nothing about retries — nothing is ever retried here.
    """
    if recovery_run_id is None or not str(recovery_run_id).strip():
        # `verify_outcome` accepts `recovery_run_id=None` because a *scheduled*
        # fire legitimately carries none and must assert its absence. A recovery
        # always has one, and inheriting the scheduled-fire reading here would
        # silently accept a record with no recovery identity as proof of this
        # recovery. Refuse before the comparison can be weakened.
        raise ValueError(
            "evaluate_execution_evidence requires a recovery_run_id; the "
            "scheduled-fire reading of an absent value must never reach the "
            "recovery coverage gate"
        )
    evidence: Dict[str, Any] = {
        "returncode": result.get("returncode"),
        "returncode_is_zero": result.get("returncode") == 0,
        "orchestration_error": bool(orchestration_error),
        # Attached whatever the verdict turns out to be: a record written by a
        # run that then failed is still the most informative thing an operator
        # can read on the recovery row.
        "execution_outcome": result.get("execution_outcome"),
        "execution_outcome_verified": False,
        "coverage_advance_permitted": False,
        "refusal_code": None,
        "refusal_detail": None,
    }

    if orchestration_error is not None:
        evidence["refusal_code"] = RECOVERY_ORCHESTRATION_FAILED
        evidence["refusal_detail"] = orchestration_error
        return evidence
    if result.get("returncode") != 0:
        evidence["refusal_code"] = RECOVERY_BUSINESS_FAILED
        evidence["refusal_detail"] = f"rc={result.get('returncode')}"
        return evidence

    # From here the process exited 0 — which on its own proves nothing.
    raw_outcome = result.get("execution_outcome")
    if raw_outcome is None:
        evidence["refusal_code"] = RECOVERY_BUSINESS_NOT_EXECUTED
        evidence["refusal_detail"] = (
            result.get("execution_outcome_error")
            or "the business process exited 0 but wrote no structured terminal "
               "execution record; return code 0 is not evidence of business work"
        )
        return evidence

    try:
        from jobs.api.telematics.execution_outcome import ExecutionOutcome
        outcome = ExecutionOutcome.from_mapping(raw_outcome)
        verify_outcome(
            outcome,
            client_id=client_id,
            client_code=client_code,
            schedule_id=schedule_id,
            dataset_name=dataset_name,
            recovery_run_id=recovery_run_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            platform_run_id=result.get("platform_run_id"),
        )
    except ExecutionOutcomeError as exc:
        evidence["refusal_code"] = RECOVERY_BUSINESS_NOT_EXECUTED
        evidence["refusal_detail"] = str(exc)
        return evidence

    evidence["execution_outcome_verified"] = True
    evidence["outcome"] = outcome.outcome
    evidence["transaction_status"] = outcome.transaction_status
    evidence["prepared_count"] = outcome.prepared_count
    evidence["upserted_count"] = outcome.upserted_count
    evidence["malformed_count"] = outcome.malformed_count
    evidence["provider_execution_entered"] = outcome.provider_execution_entered
    evidence["business_transaction_entered"] = (
        outcome.business_transaction_entered
    )

    if not is_coverage_eligible(outcome):
        evidence["refusal_code"] = RECOVERY_BUSINESS_NOT_EXECUTED
        detail = (
            "the business process skipped its work because the dataset "
            "schedule was disabled and no manual-recovery authority applied"
            if outcome.outcome == OUTCOME_SKIPPED_DISABLED_SCHEDULE
            else f"terminal outcome {outcome.outcome} with transaction_status "
                 f"{outcome.transaction_status} is not committed business work"
        )
        evidence["refusal_detail"] = detail
        return evidence

    evidence["coverage_advance_permitted"] = True
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reviewed manual Telematics trips compatibility recovery. Dry-run is "
            "the default; --execute additionally requires --confirm-client-code."
        )
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=TRIPS_SYNC_DATASET_NAME)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--expected-old-covered-through", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-client-code")
    parser.add_argument("--dsn")
    cold = parser.add_argument_group(
        "cold start",
        "Opt-in path for a provably empty, freshly cold-start-bootstrapped "
        "client whose authoritative schedule is still disabled. Without "
        "--allow-disabled-schedule-for-cold-start every option below is "
        "refused and a disabled schedule is rejected exactly as before.",
    )
    cold.add_argument(
        "--allow-disabled-schedule-for-cold-start", action="store_true",
        help="Require — not merely tolerate — a single disabled authoritative "
             "schedule, plus the full zero-state and baseline conditions.",
    )
    cold.add_argument(
        "--expected-schedule-id",
        help="Exact schedule this cold-start recovery is authorized against.",
    )
    cold.add_argument(
        "--expected-coverage-fingerprint",
        help="Canonical SHA-256 of the reviewed zero-width baseline row.",
    )
    cold.add_argument(
        "--approved-shifted-cutoff-boundary",
        help="The explicitly approved boundary for *this* window; it must equal "
             "--window-end. For a chain, intermediate windows end at the "
             "deterministic split boundary and only the final window ends at "
             "the approved latest safe shifted-cutoff boundary.",
    )
    cold.add_argument(
        "--cold-start-chain-ref",
        help="Operator-supplied identifier binding every window of one "
             "cold-start recovery chain, e.g. "
             "TELEMATICS-COLD-START-ECHO00001-2026-08. --approval-ref must be "
             "exactly <chain-ref>-W<NN> for this window's ordinal.",
    )
    cold.add_argument(
        "--approved-final-chain-boundary",
        help="The explicitly approved end of the whole chain. The window split "
             "is derived deterministically from the current watermark to this "
             "boundary; it never authorizes more than the one window this "
             "invocation executes.",
    )
    cold.add_argument(
        "--confirm-schedule-disabled", action="store_true",
        help="Explicit confirmation that the schedule is expected to stay "
             "disabled for the whole of this recovery.",
    )
    return parser


def _cold_start_inputs(
    args, *, window_end_ts: datetime, approval_ref: str,
) -> Optional[Dict[str, Any]]:
    """Validate and bind the cold-start options, or prove none were supplied.

    Supplying any cold-start option without the explicit flag is a refusal
    rather than a silent no-op, so a half-typed cold-start command can never be
    executed as an ordinary recovery.

    The chain reference and the approval reference are validated *together*:
    the approval reference must be exactly this chain's window token, which is
    what makes the ordinal an operator-approved value rather than a derived one.
    """
    supplied = {
        "--expected-schedule-id": args.expected_schedule_id,
        "--expected-coverage-fingerprint": args.expected_coverage_fingerprint,
        "--approved-shifted-cutoff-boundary": args.approved_shifted_cutoff_boundary,
        "--confirm-schedule-disabled": args.confirm_schedule_disabled or None,
        "--cold-start-chain-ref": args.cold_start_chain_ref,
        "--approved-final-chain-boundary": args.approved_final_chain_boundary,
    }
    if not args.allow_disabled_schedule_for_cold_start:
        present = sorted(name for name, value in supplied.items() if value)
        if present:
            raise _refuse(
                "RECOVERY_REFUSED_PARAMETER",
                f"{', '.join(present)} require "
                "--allow-disabled-schedule-for-cold-start",
                EXIT_INVALID_PARAMETERS,
            )
        return None

    missing = sorted(name for name, value in supplied.items() if not value)
    if missing:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--allow-disabled-schedule-for-cold-start additionally requires "
            f"{', '.join(missing)}",
            EXIT_INVALID_PARAMETERS,
        )

    fingerprint = str(args.expected_coverage_fingerprint or "").strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", fingerprint):
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--expected-coverage-fingerprint must be a lowercase 64-character "
            "SHA-256",
            EXIT_INVALID_PARAMETERS,
        )
    boundary = parse_instant(
        args.approved_shifted_cutoff_boundary,
        label="--approved-shifted-cutoff-boundary",
    )
    if boundary != window_end_ts:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--approved-shifted-cutoff-boundary must equal --window-end",
            EXIT_INVALID_PARAMETERS,
        )
    final_boundary = parse_instant(
        args.approved_final_chain_boundary,
        label="--approved-final-chain-boundary",
    )
    try:
        chain_ref = validate_chain_ref(args.cold_start_chain_ref)
    except ChainContractError as exc:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            f"--cold-start-chain-ref is invalid ({exc})",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    parsed = parse_window_approval_ref(approval_ref)
    if parsed is None or parsed[0] != chain_ref:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--approval-ref must be exactly <--cold-start-chain-ref>-W<NN> "
            "with a zero-padded window ordinal, so every window carries its own "
            "unique approval reference inside one identifiable chain",
            EXIT_INVALID_PARAMETERS,
        )
    return {
        "expected_schedule_id": _canonical_uuid(
            args.expected_schedule_id, label="--expected-schedule-id"
        ),
        "expected_coverage_fingerprint": fingerprint,
        "approved_shifted_cutoff_boundary_ts": boundary,
        "approved_final_chain_boundary_ts": final_boundary,
        "chain_ref": chain_ref,
        "window_ordinal": parsed[1],
    }


def run(args) -> tuple:
    dry_run = not args.execute
    client_code = _safe_token(args.client_code, label="--client-code")
    dataset_name = _safe_token(args.dataset, label="--dataset")
    approval_ref = _safe_token(args.approval_ref, label="--approval-ref")
    reason = _safe_reason(args.reason)
    expected_uuid = _canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    expected_environment = str(args.expected_environment or "").strip()
    if not expected_environment:
        raise _refuse(
            "RECOVERY_REFUSED_PARAMETER",
            "--expected-environment is required",
            EXIT_INVALID_PARAMETERS,
        )

    if not dry_run:
        confirmed = str(args.confirm_client_code or "").strip()
        if not confirmed:
            raise _refuse(
                "RECOVERY_REFUSED_CONFIRMATION",
                "--execute additionally requires --confirm-client-code",
                EXIT_INVALID_PARAMETERS,
            )
        if confirmed != client_code:
            raise _refuse(
                "RECOVERY_REFUSED_CONFIRMATION",
                "--confirm-client-code does not match --client-code",
                EXIT_INVALID_PARAMETERS,
            )

    window_start_ts = parse_instant(args.window_start, label="--window-start")
    window_end_ts = parse_instant(args.window_end, label="--window-end")
    expected_old_w = parse_instant(
        args.expected_old_covered_through,
        label="--expected-old-covered-through",
    )
    cold_start = _cold_start_inputs(
        args, window_end_ts=window_end_ts, approval_ref=approval_ref,
    )

    repo = repository_state(require_clean=not dry_run)

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT date_trunc('second', now()) AS db_now")
            db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)
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
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                expected_old_covered_through_ts=expected_old_w,
                approval_ref=approval_ref,
                db_now=db_now,
                cold_start=cold_start,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    plan: Dict[str, Any] = {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "semantics_version": RECOVERY_SEMANTICS_VERSION,
        "fingerprint_version": COVERAGE_FINGERPRINT_VERSION,
        "repository_head": repo["repository_head"],
        "worktree_clean": repo["worktree_clean"],
        "expected_environment": expected_environment,
        "expected_platform_uuid": expected_uuid,
        "client_code": client_code,
        "client_id": gates["client"]["client_id"],
        "schedule_id": gates["schedule"]["schedule_id"],
        "dataset_name": dataset_name,
        "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
        "window_start_ts": _iso(window_start_ts),
        "window_end_ts": _iso(window_end_ts),
        "window_span_seconds": gates["span_seconds"],
        "expected_old_covered_through_ts": _iso(expected_old_w),
        "would_advance_covered_through_to": _iso(window_end_ts),
        "would_set_covered_through_source": COVERAGE_SOURCE_MANUAL_RECOVERY,
        "coverage_start_ts_written": False,
        "bootstrap_status_written": False,
        "initial_coverage_snapshot": _snapshot_json(gates["coverage"]),
        "initial_coverage_fingerprint": gates["coverage_fingerprint"],
        "stabilization_delay_seconds": gates["stabilization_delay_seconds"],
        "overlap_seconds": gates["overlap_seconds"],
        "max_recovery_span_seconds": gates["max_recovery_span_seconds"],
        "compatibility_clients": gates["compatibility_clients"],
        "sole_compatibility_client": (
            gates["compatibility_clients"] == [client_code]
        ),
        "reason": reason,
        "approval_ref": approval_ref,
        "planned_recovery_executions": 1,
        "schedule_history_rows_created": 0,
        "schedule_history_rows_modified": 0,
        "schedule_rows_modified": 0,
        "client_rows_modified": 0,
        "automatic_retries": 0,
        "job_module": SYNC_JOB_MODULE,
        "cold_start_path": cold_start is not None,
        "schedule_enabled": bool(gates["schedule"]["enabled"]),
        "schedule_enabled_changes": 0,
        # Chain identity is reported at the top level of both the dry-run plan
        # and the terminal result, so a reviewer never has to reach into the
        # evidence sub-object to learn which chain an execution belongs to.
        "cold_start_chain_ref": (
            None if cold_start is None else cold_start["chain_ref"]
        ),
        "cold_start_window_ordinal": (
            None if cold_start is None else cold_start["window_ordinal"]
        ),
        "cold_start_chain_semantics_version": (
            None if cold_start is None else CHAIN_SEMANTICS_VERSION
        ),
        "cold_start_evidence": gates.get("cold_start") or {},
        "database_writes_performed": 0,
        "provider_requests": 0,
        # Business subprocesses only. The local read-only `git` identity
        # commands are not counted here and are never the business execution.
        "business_subprocesses_launched": 0,
    }

    if dry_run:
        plan["would_claim_recovery"] = True
        return EXIT_OK, plan

    plan_inputs = {
        "client_code": client_code,
        "dataset_name": dataset_name,
        "schedule_id": gates["schedule"]["schedule_id"],
        "window_start_ts": window_start_ts,
        "window_end_ts": window_end_ts,
        "expected_old_covered_through_ts": expected_old_w,
        "reason": reason,
        "approval_ref": approval_ref,
        "repository_head": repo["repository_head"],
        "coverage_fingerprint": gates["coverage_fingerprint"],
        "cold_start": cold_start,
    }

    write_conn = open_write_connection(dsn)
    try:
        claim = claim_recovery(write_conn, plan_inputs=plan_inputs)
    except Exception:
        try:
            write_conn.rollback()
        finally:
            write_conn.close()
        raise

    recovery_run_id = claim["recovery_run_id"]
    claim_gates = claim["gates"]
    snapshot = claim_gates["snapshot"]
    schedule_id = claim_gates["schedule"]["schedule_id"]
    plan["recovery_run_id"] = recovery_run_id
    plan["database_writes_performed"] = 1

    schedule_disabled = not bool(claim_gates["schedule"]["enabled"])
    job_params = build_job_params(
        client_id=claim_gates["client"]["client_id"],
        client_code=client_code,
        event_enrichment_mode=str(
            claim_gates["schedule"]["event_enrichment_mode"]
        ),
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        recovery_run_id=recovery_run_id,
        schedule_id=schedule_id,
        schedule_disabled=schedule_disabled,
    )
    # The attestation is built only for a disabled schedule, because that is the
    # only case in which the business job consults it. An enabled-schedule
    # recovery launches with no attestation at all and is byte-identical to its
    # historical behavior.
    authority = (
        manual_recovery_authority.build_launch_attestation(
            client_id=claim_gates["client"]["client_id"],
            client_code=client_code,
            schedule_id=schedule_id,
            dataset_name=dataset_name,
            recovery_run_id=recovery_run_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
        )
        if schedule_disabled else None
    )

    orchestration_error: Optional[str] = None
    try:
        result = launch_sync(job_params=job_params, authority=authority)
    except Exception as exc:  # subprocess could not be launched at all
        result = {
            "returncode": None,
            "platform_run_id": None,
            "started_at": claim["claimed_at"],
            "finished_at": datetime.now(timezone.utc),
            "duration_seconds": None,
            "stderr_tail": "",
            "sanitized_command": (
                f"{sys.executable} ops/runner.py {SYNC_JOB_MODULE} <params>"
            ),
            "execution_outcome": None,
            "execution_outcome_error": None,
        }
        orchestration_error = f"{type(exc).__name__}: {exc}"

    plan["business_subprocesses_launched"] = 1
    plan["sanitized_command"] = result["sanitized_command"]
    plan["business_started_at"] = _iso(result["started_at"])
    plan["business_finished_at"] = _iso(result["finished_at"])
    plan["business_returncode"] = result["returncode"]
    plan["platform_run_id"] = result["platform_run_id"]

    platform_status = None
    try:
        with write_conn.cursor() as cur:
            platform_status = _platform_run_status(
                cur, platform_run_id=result["platform_run_id"],
            )
        write_conn.rollback()
    except Exception:
        try:
            write_conn.rollback()
        except Exception:
            pass

    verdict = evaluate_execution_evidence(
        result=result,
        orchestration_error=orchestration_error,
        client_id=str(claim_gates["client"]["client_id"]),
        client_code=client_code,
        schedule_id=schedule_id,
        dataset_name=dataset_name,
        recovery_run_id=recovery_run_id,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
    )

    execution = {
        "platform_run_id": result["platform_run_id"],
        "job_summary": {
            "job_module": SYNC_JOB_MODULE,
            "trigger": RECOVERY_TRIGGER,
            "returncode": result["returncode"],
            "duration_seconds": result["duration_seconds"],
            "window_start_ts": _iso(window_start_ts),
            "window_end_ts": _iso(window_end_ts),
            "automatic_retries": 0,
            # The authoritative statement about what the run did. Persisted on
            # the recovery row so schedule activation can later require genuine
            # committed execution proof for every window of a chain, instead of
            # inferring it from a SUCCESS status.
            "execution_outcome": verdict.get("execution_outcome"),
            "execution_outcome_verified": verdict["execution_outcome_verified"],
            "coverage_advance_permitted": verdict["coverage_advance_permitted"],
            "disabled_schedule_manual_recovery": schedule_disabled,
        },
        "provider_summary": {
            "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
            "platform_run_status": platform_status,
            "evidence_location": (
                "platform logs and runs rows for platform_run_id"
            ),
        },
    }

    plan["execution_outcome"] = verdict.get("execution_outcome")
    plan["execution_outcome_verified"] = verdict["execution_outcome_verified"]
    plan["coverage_advance_permitted"] = verdict["coverage_advance_permitted"]

    try:
        if not verdict["coverage_advance_permitted"]:
            classification = verdict["refusal_code"]
            summary = "\n".join(
                part for part in (
                    verdict["refusal_detail"],
                    result.get("stderr_tail") or "",
                ) if part
            )
            outcome = finalize_non_success(
                write_conn,
                recovery_run_id=recovery_run_id,
                client_id=snapshot.client_id,
                dataset_name=snapshot.dataset_name,
                status="FAILED",
                error_classification=classification,
                error_summary=summary,
                execution=execution,
                initial_fingerprint=claim_gates["coverage_fingerprint"],
            )
            plan["recovery_status"] = "FAILED"
            plan["error_classification"] = classification
            plan["coverage_unchanged"] = outcome["coverage_unchanged"]
            plan["observed_coverage_fingerprint"] = outcome[
                "observed_coverage_fingerprint"
            ]
            plan["coverage_advanced"] = False
            plan["database_writes_performed"] = 2
            return EXIT_BUSINESS_FAILED, plan

        try:
            finalized = finalize_success(
                write_conn,
                recovery_run_id=recovery_run_id,
                schedule_id=schedule_id,
                snapshot=snapshot,
                window_end_ts=window_end_ts,
                execution=execution,
            )
        except CoverageCasConflict as conflict:
            try:
                write_conn.rollback()
            except Exception:
                pass
            outcome = finalize_non_success(
                write_conn,
                recovery_run_id=recovery_run_id,
                client_id=snapshot.client_id,
                dataset_name=snapshot.dataset_name,
                status="FINALIZATION_CONFLICT",
                error_classification=conflict.code,
                error_summary=conflict.detail,
                execution=execution,
                initial_fingerprint=claim_gates["coverage_fingerprint"],
            )
            plan["recovery_status"] = "FINALIZATION_CONFLICT"
            plan["error_classification"] = conflict.code
            plan["coverage_unchanged"] = outcome["coverage_unchanged"]
            plan["observed_coverage_fingerprint"] = outcome[
                "observed_coverage_fingerprint"
            ]
            plan["coverage_advanced"] = False
            plan["database_writes_performed"] = 2
            print(f"RECOVERY_INCIDENT {conflict.code}", file=sys.stderr)
            return EXIT_FINALIZATION_CONFLICT, plan

        plan["recovery_status"] = "SUCCESS"
        plan["coverage_advanced"] = True
        plan["finalizer_result"] = finalized["finalizer_result"]
        plan["final_coverage_fingerprint"] = finalized[
            "final_coverage_fingerprint"
        ]
        plan["coverage_mutation_ts"] = finalized["mutation_ts"]
        plan["database_writes_performed"] = 2
        return EXIT_OK, plan
    finally:
        try:
            write_conn.close()
        except Exception:
            pass


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, plan = run(args)
    except RecoveryRefused as exc:
        print(f"RECOVERY_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except CoverageCasConflict as exc:  # pragma: no cover - mapped above
        print(f"RECOVERY_INCIDENT {exc}", file=sys.stderr)
        return EXIT_FINALIZATION_CONFLICT
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(f"RECOVERY_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(plan, sort_keys=True, indent=2, default=str))
    print(plan["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
