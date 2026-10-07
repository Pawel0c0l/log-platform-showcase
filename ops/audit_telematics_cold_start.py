#!/usr/bin/env python3
"""Read-only zero-state cold-start audit for a new Telematics `trips_sync` client.

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §5.2/§5.2.1 (bounded coverage
    interval and the fail-closed preconditions), §13.6 (the ordered enablement
    gates this path parallels for a client that has no history at all)
  docs/14_telematics_trips_compatibility_implementation_plan.md §3/C10, §4.1–§4.6
  docs/07_operations.md §5.5 (the cold-start onboarding path)

WHY THIS EXISTS, NEXT TO THE HISTORICAL C10 AUDIT.
    ``ops/audit_telematics_coverage_bootstrap.py`` inventories a client that *has
    run*: it enumerates expected fires, diffs them against
    `client_schedule_run_history`, and asks a human to choose ``A``/``W`` inside
    proven evidence. That contract is unchanged and stays the only path for any
    client with successful or failed executions.

    A newly onboarded client has no such evidence and cannot acquire it: its
    schedule is disabled, so it has never fired, so there is nothing to
    inventory. The historical audit correctly refuses it twice over — once
    because a disabled schedule resolves to zero enabled schedules, and once
    because empty history classifies as ``INSUFFICIENT_HISTORY_EVIDENCE``, which
    the historical writer refuses. Neither refusal is a defect.

    This tool answers a different, narrower question: **is this client provably
    empty?** It never inventories intervals, never proposes a bound and never
    reuses ``UNRESOLVED_GAPS_PRESENT`` for a client that has never run — a
    client with no history has no gaps, and saying it does would be a false
    statement about production.

WHAT IT PROVES.
    Exactly one classification is ever emitted: ``COLD_START_ZERO_STATE_CONFIRMED``,
    and only when every zero-state gate below passes. Any non-zero historical or
    business state is a refusal that directs the operator back to the existing
    C10 inventory path; this tool never produces a bundle for such a client.

HARD GUARANTEES.
    * permanently read-only: no execute switch, no write switch, both platform
      and client-business connections opened ``read_only``;
    * zero provider requests, zero business subprocesses, zero platform or
      business writes;
    * the bundle is a regular file, mode ``0600``, inside a mode ``0700``
      directory outside the repository tree, opened ``O_NOFOLLOW``;
    * it carries counts, identifiers, instants and hashes — never credentials,
      DSNs, provider payloads or personal trip data.

Typical use::

    PYTHONPATH="$PWD" python3 ops/audit_telematics_cold_start.py \\
        --client-code ECHO00001 \\
        --dataset trips_sync \\
        --expected-schedule-id 60c80b85-f294-4a00-8e09-b6a3688af443 \\
        --desired-managed-start 2026-07-01T00:00:00Z \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629 \\
        --output /var/lib/log-platform/cold-start/ECHO00001.json
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
)

from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    canonical_json,
    canonical_uuid,
    iso_utc,
    open_read_only_connection,
    parse_iso_utc,
    platform_dsn_from_env,
    verify_platform_identity,
    _load_dotenv,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_NOT_COLD_START = 4
EXIT_RUNTIME_FAILURE = 5

DEFAULT_DATASET = "trips_sync"

# Envelope and semantics versions. They are deliberately *different strings*
# from the historical C10 audit's, which is what makes a historical bundle
# unusable by the cold-start writer and a cold-start bundle unusable by the
# historical writer — neither tool has to know about the other to fail closed.
COLD_START_BUNDLE_VERSION = "telematics-cold-start-audit/1"
COLD_START_SEMANTICS_VERSION = "telematics-cold-start-semantics/1"

# The recovery table is part of the cold-start contract, because the path ends
# in one controlled C11 recovery. A database without migration 058 cannot carry
# this path through and is refused up front rather than half-way.
REQUIRED_MIGRATIONS = (
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
)
MIGRATION_CEILING = REQUIRED_MIGRATIONS[-1]

# The one and only classification this tool can emit. There is deliberately no
# "partial", "probably empty" or "gaps present" variant: a client is provably
# in the zero state or the audit refuses.
CLASSIFICATION_ZERO_STATE_CONFIRMED = "COLD_START_ZERO_STATE_CONFIRMED"

STRICT_META = "strict_meta"
DATA_INVARIANTS_V1 = "data_invariants_v1"

# Control-plane relations a concurrent writer would have to lock. Only locks
# stronger than AccessShareLock count, so an ordinary concurrent reader is not
# mistaken for a live mutation.
CONTROL_RELATIONS = (
    "workflow_a_control.client_account",
    "workflow_a_control.client_dataset_schedule",
    "workflow_a_control.client_schedule_run_history",
    "workflow_a_control.client_dataset_coverage",
    "workflow_a_control.client_dataset_recovery_run",
)


class ColdStartAuditError(RuntimeError):
    """Fail-closed refusal carrying a stable code and exit status."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(
    code: str, message: str, exit_code: int = EXIT_NOT_COLD_START
) -> ColdStartAuditError:
    return ColdStartAuditError(code, message, exit_code)


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

def bundle_sha256(bundle: Dict[str, Any]) -> str:
    """SHA-256 over the canonical bundle with `bundle_sha256` excluded."""
    import hashlib

    without_hash = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    return hashlib.sha256(
        canonical_json(without_hash).encode("utf-8")
    ).hexdigest()


def parse_instant(raw: object, *, label: str) -> datetime:
    """Parse an aware, whole-second UTC instant. Never rounded, never guessed."""
    try:
        parsed = parse_iso_utc(str(raw or ""), label=label)
    except Exception as exc:
        raise _refuse(
            "COLD_START_REFUSED_PARAMETER",
            f"{label} must be a timezone-aware ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.microsecond != 0:
        raise _refuse(
            "COLD_START_REFUSED_PARAMETER",
            f"{label} must be a whole-second instant; it is never rounded here",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


def repository_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        raise _refuse(
            "COLD_START_REFUSED_REPOSITORY",
            "the repository HEAD could not be resolved for the bundle",
            EXIT_RUNTIME_FAILURE,
        ) from exc
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# Schema and identity
# ---------------------------------------------------------------------------

def verify_schema(cur) -> None:
    cur.execute("SELECT filename FROM public.schema_migrations")
    applied = {str(row["filename"]) for row in cur.fetchall()}
    missing = [name for name in REQUIRED_MIGRATIONS if name not in applied]
    if missing:
        raise _refuse(
            "COLD_START_REFUSED_SCHEMA",
            f"required migration(s) not applied: {', '.join(missing)}",
        )
    for relation in (
        "workflow_a_control.client_dataset_coverage",
        "workflow_a_control.client_dataset_recovery_run",
    ):
        cur.execute("SELECT to_regclass(%s)::text AS present", (relation,))
        if not (cur.fetchone() or {}).get("present"):
            raise _refuse(
                "COLD_START_REFUSED_SCHEMA", f"{relation} does not exist",
            )


# ---------------------------------------------------------------------------
# Zero-state gates (each one refuses; none of them repairs anything)
# ---------------------------------------------------------------------------

def resolve_cold_start_client(cur, *, client_code: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code, enabled,
               trips_pagination_mode,
               trips_stabilization_delay_seconds,
               trips_overlap_seconds,
               trips_max_recovery_span_seconds,
               client_db_host, client_db_port, client_db_name, client_db_user,
               client_db_password_secret_ref, client_db_schema,
               client_db_environment,
               client_db_identity_id::text AS client_db_identity_id
          FROM workflow_a_control.client_account
         WHERE client_code = %s
        """,
        (client_code,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1:
        raise _refuse(
            "COLD_START_REFUSED_TARGET",
            f"client_code {client_code} resolves to {len(rows)} client "
            "accounts; exactly one target is required",
        )
    client = rows[0]
    if str(client["trips_pagination_mode"]) != STRICT_META:
        raise _refuse(
            "COLD_START_REFUSED_MODE",
            "the target is "
            f"{client['trips_pagination_mode']}, expected {STRICT_META}. A "
            "client already in compatibility mode is past the cold-start "
            "entry state and must not re-enter it",
        )
    return client


def resolve_cold_start_schedule(
    cur, *, client_id: str, dataset_name: str, expected_schedule_id: str
) -> Dict[str, Any]:
    """Resolve the one authoritative *disabled* schedule for client × dataset.

    Unlike the historical resolver this counts every **base** schedule row,
    enabled or not: a competing base row of either kind means the authoritative
    schedule is ambiguous, and a cold start must not guess which one it is
    onboarding.

    Scoped to `run_type = 'DAILY'` since M5. A reconciliation cadence is a valid
    additional row that is neither onboarded nor cold-started, so counting it
    here would make every cold-start audit ambiguous the moment M6 registers one.
    Today the scope changes nothing: every schedule row is base.
    """
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,
               client_code, dataset_name, enabled, frequency, day_of_week,
               day_of_month, day_of_month_last, run_time::text AS run_time,
               timezone, lookback_days, overwrite_existing,
               event_enrichment_mode, created_at, updated_at
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s
           AND run_type = %s
         ORDER BY schedule_id
        """,
        (client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1:
        raise _refuse(
            "COLD_START_REFUSED_SCHEDULE",
            f"{dataset_name} resolves to {len(rows)} schedule rows for this "
            "client; exactly one authoritative schedule is required and no "
            "competing enabled or disabled schedule may exist",
        )
    schedule = rows[0]
    if bool(schedule["enabled"]):
        raise _refuse(
            "COLD_START_REFUSED_SCHEDULE",
            "the authoritative schedule is enabled; the cold-start path exists "
            "for a schedule that has never been allowed to fire. An enabled "
            "schedule belongs to the historical C10 inventory path",
        )
    if str(schedule["schedule_id"]) != expected_schedule_id:
        raise _refuse(
            "COLD_START_REFUSED_SCHEDULE",
            "the resolved schedule does not match --expected-schedule-id",
        )
    return schedule


def count_history_rows(cur, *, schedule_id: str, client_id: str) -> Dict[str, int]:
    """Every history row attributable to the target, for every status.

    The predicate is deliberately broad *within* the target — this schedule or
    this client, any dataset — because a fire recorded anywhere for this client
    means it is not a client that has never run.
    """
    cur.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'RUNNING') AS running
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s OR client_id = %s
        """,
        (schedule_id, client_id),
    )
    row = cur.fetchone() or {}
    return {"total": int(row["total"]), "running": int(row["running"])}


def count_platform_runs(cur, *, client_id: str, client_code: str) -> Dict[str, int]:
    """Platform business runs attributable to the target, by params identity."""
    cur.execute("SELECT to_regclass('public.runs')::text AS present")
    if not (cur.fetchone() or {}).get("present"):
        # No runs table means no run can exist. The honest count is zero.
        return {"total": 0, "running": 0, "table_present": 0}
    cur.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'RUNNING') AS running
          FROM public.runs
         WHERE params ->> 'client_id' = %s
            OR params ->> 'client_code' = %s
        """,
        (client_id, client_code),
    )
    row = cur.fetchone() or {}
    return {
        "total": int(row["total"]),
        "running": int(row["running"]),
        "table_present": 1,
    }


def count_coverage_rows(cur, *, schedule_id: str, client_id: str) -> int:
    """Count every coverage row this client could possibly be described by.

    Deliberately still `OR`, and deliberately still naming `schedule_id`: this is
    a cold-start *emptiness* audit, and its job is to notice ANY pre-existing
    coverage — including a row that names this schedule but a different client,
    which would itself be corruption. Narrowing it to the M5 owner key would make
    the audit blind to exactly the malformed rows it exists to find.
    """
    cur.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
        " WHERE schedule_id = %s OR client_id = %s",
        (schedule_id, client_id),
    )
    return int((cur.fetchone() or {})["n"])


def count_recovery_rows(cur, *, schedule_id: str, client_id: str) -> Dict[str, int]:
    cur.execute(
        "SELECT count(*) AS total,"
        " count(*) FILTER (WHERE status IN ('PLANNED','RUNNING')) AS active"
        "  FROM workflow_a_control.client_dataset_recovery_run"
        " WHERE schedule_id = %s OR client_id = %s",
        (schedule_id, client_id),
    )
    row = cur.fetchone() or {}
    return {"total": int(row["total"]), "active": int(row["active"])}


def count_blocking_backends(cur) -> int:
    """Other backends holding a write-intent lock on a control-plane relation.

    A cold start reads the whole control plane and then writes exactly one row
    much later; a concurrent transaction that already holds a stronger-than-read
    lock on any of those relations could be mutating the target between this
    evidence and that write. It is reported as a refusal rather than a race.

    Ordinary readers take only ``AccessShareLock`` and are therefore excluded,
    so a second read-only audit does not block this one.
    """
    cur.execute(
        """
        SELECT count(*) AS n
          FROM pg_locks AS l
          JOIN pg_stat_activity AS a ON a.pid = l.pid
         WHERE l.pid <> pg_backend_pid()
           AND a.datname = current_database()
           AND l.locktype = 'relation'
           AND l.mode <> 'AccessShareLock'
           AND l.granted
           AND l.relation IN (
                 SELECT to_regclass(name)::oid
                   FROM unnest(%s::text[]) AS name
                  WHERE to_regclass(name) IS NOT NULL
           )
        """,
        (list(CONTROL_RELATIONS),),
    )
    return int((cur.fetchone() or {})["n"])


# ---------------------------------------------------------------------------
# Client business database — identity and emptiness
# ---------------------------------------------------------------------------

def inspect_client_business_database(client: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only proof that the target business database is empty and unambiguous.

    Only three things are read: the connected database name, the presence and
    content of the client identity marker when it exists, and the row count of
    ``public.client_trips``. No business column, no trip payload and no personal
    data is selected, and the connection is opened read-only so the server
    refuses a write regardless of this module's discipline.
    """
    from jobs.api.telematics.secret_resolver import (
        SecretResolutionError,
        resolve_secret,
    )

    try:
        password = resolve_secret(str(client["client_db_password_secret_ref"]))
    except SecretResolutionError as exc:
        raise _refuse(
            "COLD_START_REFUSED_CLIENT_DATABASE",
            "the client database secret reference could not be resolved",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    dsn = (
        f"host={client['client_db_host']} port={int(client['client_db_port'])} "
        f"dbname={client['client_db_name']} user={client['client_db_user']} "
        f"password={password}"
    )
    try:
        conn = open_read_only_connection(dsn)
    except Exception as exc:
        raise _refuse(
            "COLD_START_REFUSED_CLIENT_DATABASE",
            "the client business database could not be opened read-only: "
            f"{type(exc).__name__}",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db, now() AS db_now")
            row = cur.fetchone() or {}
            connected_database = str(row["db"])
            business_now = row["db_now"].astimezone(timezone.utc)

            if connected_database != str(client["client_db_name"]):
                raise _refuse(
                    "COLD_START_REFUSED_CLIENT_DATABASE",
                    "the connected business database name does not match the "
                    "control-plane declaration; the target identity is "
                    "ambiguous",
                    EXIT_IDENTITY_NOT_VERIFIED,
                )

            cur.execute(
                "SELECT to_regclass('ops_control.environment_identity')::text"
                " AS marker"
            )
            marker_present = bool((cur.fetchone() or {}).get("marker"))
            marker: Optional[Dict[str, Any]] = None
            if marker_present:
                cur.execute(
                    """
                    SELECT identity_key, environment,
                           database_identity_id::text AS database_identity_id,
                           database_role, database_name
                      FROM ops_control.environment_identity
                     ORDER BY identity_key
                    """
                )
                marker_rows = [dict(r) for r in cur.fetchall()]
                if len(marker_rows) != 1:
                    raise _refuse(
                        "COLD_START_REFUSED_CLIENT_DATABASE",
                        "the client identity table must hold exactly one "
                        "marker row",
                        EXIT_IDENTITY_NOT_VERIFIED,
                    )
                marker = marker_rows[0]
                declared_env = client.get("client_db_environment")
                declared_uuid = client.get("client_db_identity_id")
                if declared_env and str(marker["environment"]) != str(declared_env):
                    raise _refuse(
                        "COLD_START_REFUSED_CLIENT_DATABASE",
                        "the client database environment contradicts the "
                        "control-plane declaration",
                        EXIT_IDENTITY_NOT_VERIFIED,
                    )
                if declared_uuid and str(
                    marker["database_identity_id"]
                ) != str(declared_uuid):
                    raise _refuse(
                        "COLD_START_REFUSED_CLIENT_DATABASE",
                        "the client database identity UUID contradicts the "
                        "control-plane declaration",
                        EXIT_IDENTITY_NOT_VERIFIED,
                    )

            schema = str(client.get("client_db_schema") or "public")
            cur.execute(
                "SELECT to_regclass(%s)::text AS present",
                (f"{schema}.client_trips",),
            )
            if not (cur.fetchone() or {}).get("present"):
                raise _refuse(
                    "COLD_START_REFUSED_CLIENT_DATABASE",
                    f"{schema}.client_trips does not exist; the client business "
                    "schema must be onboarded before a cold start",
                )
            # `schema` is a control-plane identifier that just resolved through
            # `to_regclass`, so the relation exists and the name is a real
            # identifier; the count selects no business column.
            cur.execute(
                "SELECT count(*) AS n FROM "  # noqa: S608 - validated above
                f"{_safe_ident(schema)}.client_trips"
            )
            client_trips_rows = int((cur.fetchone() or {})["n"])
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    return {
        "business_database_name": connected_database,
        "business_database_schema": str(client.get("client_db_schema") or "public"),
        "business_database_now_utc": iso_utc(business_now),
        "business_identity_marker_present": marker_present,
        "business_identity_environment": (
            None if marker is None else str(marker["environment"])
        ),
        "business_identity_uuid": (
            None if marker is None else str(marker["database_identity_id"])
        ),
        "business_identity_role": (
            None if marker is None else str(marker["database_role"])
        ),
        "control_plane_declared_environment": client.get("client_db_environment"),
        "control_plane_declared_identity_uuid": client.get("client_db_identity_id"),
        "client_trips_row_count": client_trips_rows,
    }


def _safe_ident(value: str) -> str:
    import re

    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", str(value)):
        raise _refuse(
            "COLD_START_REFUSED_CLIENT_DATABASE",
            "the client database schema name is not a plain SQL identifier",
            EXIT_INVALID_PARAMETERS,
        )
    return str(value)


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------

def build_bundle(
    *,
    environment_name: str,
    platform_uuid: str,
    repo_head: str,
    db_now: datetime,
    client: Dict[str, Any],
    schedule: Dict[str, Any],
    business: Dict[str, Any],
    history: Dict[str, int],
    platform_runs: Dict[str, int],
    coverage_rows: int,
    recovery: Dict[str, int],
    blocking_backends: int,
    desired_managed_start_ts: datetime,
    latest_safe_boundary_ts: datetime,
) -> Dict[str, Any]:
    """Assemble the deterministic zero-state bundle. Facts only."""
    delay = int(client["trips_stabilization_delay_seconds"])
    overlap = int(client["trips_overlap_seconds"])
    max_span = int(client["trips_max_recovery_span_seconds"])
    recovery_span = int(
        (latest_safe_boundary_ts - desired_managed_start_ts).total_seconds()
    )

    bundle: Dict[str, Any] = {
        "bundle_version": COLD_START_BUNDLE_VERSION,
        "cold_start_semantics_version": COLD_START_SEMANTICS_VERSION,
        "generated_at_utc": iso_utc(db_now),
        "database_now_utc": iso_utc(db_now),
        "environment_name": environment_name,
        "platform_uuid": platform_uuid,
        "repository_head": repo_head,
        "migration_ceiling": MIGRATION_CEILING,
        "client_id": client["client_id"],
        "client_code": client["client_code"],
        "client_account_enabled": bool(client["enabled"]),
        "dataset_name": str(schedule["dataset_name"]),
        "schedule_id": schedule["schedule_id"],
        "schedule_enabled": bool(schedule["enabled"]),
        "client_mode": str(client["trips_pagination_mode"]),
        "schedule_parameters": {
            "enabled": bool(schedule["enabled"]),
            "frequency": str(schedule["frequency"]),
            "day_of_week": schedule["day_of_week"],
            "day_of_month": schedule["day_of_month"],
            "day_of_month_last": bool(schedule["day_of_month_last"]),
            "run_time": str(schedule["run_time"]),
            "timezone": str(schedule["timezone"]),
            "lookback_days": int(schedule["lookback_days"]),
            "overwrite_existing": bool(schedule["overwrite_existing"]),
            "event_enrichment_mode": str(schedule["event_enrichment_mode"]),
            "created_at": iso_utc(schedule["created_at"]),
            "updated_at": iso_utc(schedule["updated_at"]),
            "trips_stabilization_delay_seconds": delay,
            "trips_overlap_seconds": overlap,
            "trips_max_recovery_span_seconds": max_span,
        },
        "business_database": {
            key: business[key]
            for key in sorted(business)
        },
        "zero_state_counts": {
            "coverage_rows": coverage_rows,
            "recovery_rows": recovery["total"],
            "active_recovery_rows": recovery["active"],
            "schedule_history_rows": history["total"],
            "running_schedule_history_rows": history["running"],
            "platform_business_runs": platform_runs["total"],
            "running_platform_business_runs": platform_runs["running"],
            "client_trips_rows": business["client_trips_row_count"],
            "competing_schedules": 0,
        },
        "active_process_result": {
            "running_schedule_history_rows": history["running"],
            "active_recovery_rows": recovery["active"],
            "running_platform_business_runs": platform_runs["running"],
            "blocking_control_plane_backends": blocking_backends,
            "live_target_process_detected": False,
        },
        "desired_managed_start_ts": iso_utc(desired_managed_start_ts),
        "latest_safe_shifted_cutoff_recovery_boundary_ts": iso_utc(
            latest_safe_boundary_ts
        ),
        "recovery_boundary_derivation": {
            "formula": "latest_safe_boundary = database_now - D",
            "stabilization_delay_seconds": delay,
            "span_from_managed_start_seconds": recovery_span,
            "max_recovery_span_seconds": max_span,
            "fits_single_recovery_window": recovery_span <= max_span,
            "note": (
                "This boundary ages with the database clock. The recovery tool "
                "recomputes it and refuses a stale approval; the value here is "
                "evidence, never an authorization."
            ),
        },
        "baseline_contract": {
            "required_coverage_start_ts": iso_utc(desired_managed_start_ts),
            "required_covered_through_ts": iso_utc(desired_managed_start_ts),
            "covered_interval_seconds": 0,
            "note": (
                "A cold-start baseline is a zero-width initialization instant, "
                "not a historical interval. It claims no elapsed coverage and "
                "no business data; the client is not reporting-ready until an "
                "authorized recovery has succeeded."
            ),
        },
        "audit_classification": CLASSIFICATION_ZERO_STATE_CONFIRMED,
        "evidence_items": [
            {"item": "client_account", "count": 1},
            {"item": "authoritative_schedule", "count": 1},
            {"item": "schedule_history_rows", "count": history["total"]},
            {"item": "platform_business_runs", "count": platform_runs["total"]},
            {"item": "coverage_rows", "count": coverage_rows},
            {"item": "recovery_rows", "count": recovery["total"]},
            {
                "item": "client_trips_rows",
                "count": business["client_trips_row_count"],
            },
        ],
        "decision_contract": {
            "coverage_start_ts_recommended": False,
            "covered_through_ts_recommended": False,
            "schedule_activation_authorized": False,
            "reporting_ready": False,
            "note": (
                "This bundle confirms a provably empty client. It authorizes "
                "nothing: the baseline instant, the mode change, the recovery "
                "boundary and the schedule activation each remain separate "
                "reviewed operator decisions."
            ),
        },
    }
    bundle["bundle_sha256"] = bundle_sha256(bundle)
    return bundle


def write_bundle(path: Path, bundle: Dict[str, Any]) -> Path:
    """Write `0600` into a mode-`0700` directory outside the repository tree.

    Symlinks are refused at both levels: a symlinked parent or output path could
    redirect evidence somewhere world-readable, and evidence that can be
    redirected is not evidence.
    """
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise _refuse(
            "COLD_START_REFUSED_OUTPUT",
            "the output path is a symlink",
            EXIT_INVALID_PARAMETERS,
        )
    # The literal parent is checked before resolution: `resolve()` would follow
    # a symlinked directory silently and the evidence would land somewhere the
    # operator did not name.
    if expanded.parent.is_symlink():
        raise _refuse(
            "COLD_START_REFUSED_OUTPUT",
            "the output directory is a symlink",
            EXIT_INVALID_PARAMETERS,
        )
    resolved = expanded.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise _refuse(
            "COLD_START_REFUSED_OUTPUT",
            "the evidence bundle must be written outside the repository tree",
            EXIT_INVALID_PARAMETERS,
        )

    parent = resolved.parent
    try:
        if parent.is_symlink():
            raise _refuse(
                "COLD_START_REFUSED_OUTPUT",
                "the output directory is a symlink",
                EXIT_INVALID_PARAMETERS,
            )
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        parent_mode = stat.S_IMODE(os.stat(parent).st_mode)
        if parent_mode != 0o700:
            raise _refuse(
                "COLD_START_REFUSED_OUTPUT",
                f"the output directory mode is {oct(parent_mode)}, required 0o700",
                EXIT_INVALID_PARAMETERS,
            )
        if resolved.exists() and not resolved.is_file():
            raise _refuse(
                "COLD_START_REFUSED_OUTPUT",
                "the output path exists and is not a regular file",
                EXIT_INVALID_PARAMETERS,
            )
        payload = canonical_json(bundle) + "\n"
        fd = os.open(
            resolved,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(resolved, 0o600)
    except ColdStartAuditError:
        raise
    except OSError as exc:
        raise _refuse(
            "COLD_START_REFUSED_OUTPUT",
            f"the evidence bundle could not be written: {exc.strerror}",
            EXIT_RUNTIME_FAILURE,
        ) from exc
    return resolved


# ---------------------------------------------------------------------------
# The shared zero-state evaluation, reused verbatim by the writer
# ---------------------------------------------------------------------------

def evaluate_zero_state(
    cur,
    *,
    client_code: str,
    dataset_name: str,
    expected_schedule_id: str,
    expected_environment: str,
    expected_platform_uuid: str,
) -> Dict[str, Any]:
    """Every zero-state condition, evaluated read-only against live state.

    The cold-start writer calls this again inside its own transaction, so a
    change between evidence and write cannot slip through. It performs no I/O
    other than the supplied cursor and no mutation of any kind.
    """
    if dataset_name != DEFAULT_DATASET:
        raise _refuse(
            "COLD_START_REFUSED_TARGET",
            f"the cold-start path is {DEFAULT_DATASET}-only",
            EXIT_INVALID_PARAMETERS,
        )

    # The identity marker contract is shared verbatim with the historical audit
    # so the two tools can never disagree about what "this database" means. Only
    # the exception type is translated, so a cold-start CLI reports a
    # cold-start refusal with its own exit code and the same stable code string.
    from ops.audit_telematics_coverage_bootstrap import AuditError

    try:
        verify_platform_identity(
            cur,
            expected_environment=expected_environment,
            expected_platform_uuid=expected_platform_uuid,
        )
    except AuditError as exc:
        raise _refuse(
            exc.code, str(exc), EXIT_IDENTITY_NOT_VERIFIED
        ) from exc
    verify_schema(cur)

    client = resolve_cold_start_client(cur, client_code=client_code)
    schedule = resolve_cold_start_schedule(
        cur,
        client_id=client["client_id"],
        dataset_name=dataset_name,
        expected_schedule_id=expected_schedule_id,
    )

    history = count_history_rows(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"]
    )
    if history["total"]:
        raise _refuse(
            "COLD_START_REFUSED_HISTORY_PRESENT",
            f"{history['total']} schedule-history row(s) exist for this target; "
            "a client with executions of any status must go through the "
            "existing C10 inventory path "
            "(ops/audit_telematics_coverage_bootstrap.py)",
        )

    platform_runs = count_platform_runs(
        cur, client_id=client["client_id"], client_code=client_code
    )
    if platform_runs["total"]:
        raise _refuse(
            "COLD_START_REFUSED_RUNS_PRESENT",
            f"{platform_runs['total']} platform run(s) reference this target; "
            "it is not a client that has never run",
        )

    coverage_rows = count_coverage_rows(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"]
    )
    if coverage_rows:
        raise _refuse(
            "COLD_START_REFUSED_COVERAGE_PRESENT",
            f"{coverage_rows} coverage row(s) exist for this target; the "
            "cold-start baseline is inserted exactly once and never repaired",
        )

    recovery = count_recovery_rows(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"]
    )
    if recovery["total"]:
        raise _refuse(
            "COLD_START_REFUSED_RECOVERY_PRESENT",
            f"{recovery['total']} recovery row(s) exist for this target; a "
            "client with recovery history is not in the zero state",
        )

    blocking = count_blocking_backends(cur)
    if blocking:
        raise _refuse(
            "COLD_START_REFUSED_ACTIVE_PROCESS",
            f"{blocking} other backend(s) hold a write-intent lock on a "
            "control-plane relation; the zero state cannot be attested while "
            "another transaction may be mutating it",
        )

    cur.execute("SELECT date_trunc('second', now()) AS db_now")
    db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)

    return {
        "client": client,
        "schedule": schedule,
        "history": history,
        "platform_runs": platform_runs,
        "coverage_rows": coverage_rows,
        "recovery": recovery,
        "blocking_backends": blocking,
        "db_now": db_now,
    }


def validate_managed_start(
    *,
    desired_managed_start_ts: datetime,
    db_now: datetime,
    stabilization_delay_seconds: int,
) -> datetime:
    """Return the latest safe shifted-cutoff boundary, or refuse the start.

    The boundary is ``now − D``: the newest instant a compatibility execution
    may end at without entering the stabilization lag. A managed start at or
    after it leaves no interval to recover, which is an operator error rather
    than something to silently move.
    """
    boundary = db_now - timedelta(seconds=int(stabilization_delay_seconds))
    if desired_managed_start_ts > db_now:
        raise _refuse(
            "COLD_START_REFUSED_MANAGED_START",
            "--desired-managed-start is in the future relative to the database "
            "clock",
            EXIT_INVALID_PARAMETERS,
        )
    if desired_managed_start_ts >= boundary:
        raise _refuse(
            "COLD_START_REFUSED_MANAGED_START",
            "--desired-managed-start is at or after the latest safe "
            "shifted-cutoff boundary (now − D); there would be no interval "
            "left for the controlled recovery to fetch",
            EXIT_INVALID_PARAMETERS,
        )
    return boundary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only zero-state cold-start audit for a newly onboarded "
            "Telematics trips_sync client. Reports facts; recommends nothing; "
            "never writes."
        ),
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--expected-schedule-id", required=True)
    parser.add_argument(
        "--desired-managed-start", required=True,
        help="The operator-approved first managed instant. Recorded as "
             "evidence; this tool never proposes it.",
    )
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dsn")
    return parser


def run_audit(args) -> tuple:
    client_code = str(args.client_code or "").strip()
    dataset = str(args.dataset or "").strip()
    if not client_code or not dataset:
        raise _refuse(
            "COLD_START_REFUSED_PARAMETER",
            "--client-code and --dataset must be non-empty",
            EXIT_INVALID_PARAMETERS,
        )
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    expected_schedule_id = canonical_uuid(
        args.expected_schedule_id, label="--expected-schedule-id"
    )
    expected_environment = str(args.expected_environment or "").strip()
    if not expected_environment:
        raise _refuse(
            "COLD_START_REFUSED_PARAMETER",
            "--expected-environment is required",
            EXIT_INVALID_PARAMETERS,
        )
    desired_managed_start = parse_instant(
        args.desired_managed_start, label="--desired-managed-start"
    )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    repo_head = repository_head()

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            state = evaluate_zero_state(
                cur,
                client_code=client_code,
                dataset_name=dataset,
                expected_schedule_id=expected_schedule_id,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    client = state["client"]
    business = inspect_client_business_database(client)
    if business["client_trips_row_count"]:
        raise _refuse(
            "COLD_START_REFUSED_BUSINESS_ROWS_PRESENT",
            f"{business['client_trips_row_count']} row(s) exist in the client "
            "business client_trips table; the client is not empty",
        )

    boundary = validate_managed_start(
        desired_managed_start_ts=desired_managed_start,
        db_now=state["db_now"],
        stabilization_delay_seconds=client["trips_stabilization_delay_seconds"],
    )

    bundle = build_bundle(
        environment_name=expected_environment,
        platform_uuid=expected_uuid,
        repo_head=repo_head,
        db_now=state["db_now"],
        client=client,
        schedule=state["schedule"],
        business=business,
        history=state["history"],
        platform_runs=state["platform_runs"],
        coverage_rows=state["coverage_rows"],
        recovery=state["recovery"],
        blocking_backends=state["blocking_backends"],
        desired_managed_start_ts=desired_managed_start,
        latest_safe_boundary_ts=boundary,
    )
    output = write_bundle(Path(args.output), bundle)
    return bundle, output


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle, output = run_audit(args)
    except ColdStartAuditError as exc:
        print(f"COLD_START_AUDIT_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(
            f"COLD_START_AUDIT_FAILED {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE

    summary = {
        "audit_classification": bundle["audit_classification"],
        "client_code": bundle["client_code"],
        "client_id": bundle["client_id"],
        "dataset_name": bundle["dataset_name"],
        "schedule_id": bundle["schedule_id"],
        "schedule_enabled": bundle["schedule_enabled"],
        "client_mode": bundle["client_mode"],
        "zero_state_counts": bundle["zero_state_counts"],
        "desired_managed_start_ts": bundle["desired_managed_start_ts"],
        "latest_safe_shifted_cutoff_recovery_boundary_ts": bundle[
            "latest_safe_shifted_cutoff_recovery_boundary_ts"
        ],
        "bundle_sha256": bundle["bundle_sha256"],
        "output": str(output),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    print(
        "READ_ONLY_COLD_START_EVIDENCE_COMPLETE — no coverage row was created, "
        "no mode was changed, no schedule was enabled; the client is not "
        "reporting-ready until an authorized recovery has succeeded.",
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
