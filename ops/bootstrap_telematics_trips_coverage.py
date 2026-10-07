#!/usr/bin/env python3
"""Dry-run-first Telematics `trips_sync` coverage bootstrap writer.

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §5.2/§5.2.1 (bounded coverage
    interval and the fail-closed preconditions), §13.3 (explicit operator
    selection of `A` and `W`), §13.6 (Gate 6 ordering), §13.7 (Gate 7)
  docs/14_telematics_trips_compatibility_implementation_plan.md §4.1–§4.6, §6.1
  docs/15_telematics_coverage_mutation_contract.md (C6 owns every later mutation)
  db/migrations/057_workflow_a_trips_coverage_state.sql (the physical contract)

This is the **only** authorized coverage `INSERT` surface in the repository, and
it is deliberately narrow:

* dry-run is the default; a write requires **both** ``--execute`` and a second
  deliberate ``--confirm-client-code`` that must equal ``--client-code``;
* it consumes a previously generated read-only audit bundle
  (``ops/audit_telematics_coverage_bootstrap.py``) and re-derives its canonical
  hash before opening any write transaction;
* it consumes **explicit operator-approved** ``A`` and ``W``. It never infers,
  derives, widens, narrows or silently moves either bound. There is no code path
  that reads a bound from schedule history, from client trips, from the newest
  `SUCCESS` row or from ``max(synced_at)`` — that inference is the exact defect
  the bootstrap correction removed (``docs/13_…`` §13.0, §17 R9);
* it inserts exactly one row and then **only** ever inserts. There is no
  ``UPDATE``, no ``DELETE``, no upsert conflict clause and no repair path. Every
  later mutation of a coverage row belongs to the C6 dispatcher finalizers under
  the approved contract of ``docs/15_…``;
* it never enables compatibility mode, never mutates a schedule or a client
  configuration, never writes `client_schedule_run_history`, never writes
  `client_dataset_recovery_run`, never contacts a provider and never launches a
  subprocess;
* every safety gate is scoped to the **one** target client. Bootstrap is a
  per-client operation, so an already approved compatibility client elsewhere in
  the fleet is an observation, not a blocker. The tool reads fleet state for
  evidence, and writes only the target's coverage row.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/bootstrap_telematics_trips_coverage.py \\
        --client-code BRAVO00016 --dataset trips_sync \\
        --evidence-file /var/lib/log-platform/coverage-bootstrap/BRAVO00016.json \\
        --evidence-sha256 <64 hex> \\
        --coverage-start-ts 2026-08-03T00:00:00Z \\
        --covered-through-ts 2026-08-10T00:00:00Z \\
        --seeded-by operator@example.invalid \\
        --approval-ref OPS-1234 \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
)

from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    BOOTSTRAP_SEMANTICS_VERSION,
    BUNDLE_VERSION,
    CLASSIFICATION_COMPLETE,
    CLASSIFICATION_UNRESOLVED_GAPS,
    DEFAULT_DATASET,
    MIGRATION_CEILING,
    bundle_sha256,
    canonical_uuid,
    iso_utc,
    open_read_only_connection,
    parse_iso_utc,
    platform_dsn_from_env,
    resolve_client,
    resolve_schedule,
    verify_migration_ceiling,
    verify_platform_identity,
    _load_dotenv,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
EXIT_WRITE_CONFLICT = 6
EXIT_POSTWRITE_VERIFICATION_FAILED = 7

# The approved initial state. `docs/13_…` §13.6 step 5 seeds `READY`;
# `docs/14_…` §6.1/§6.2 fixes the provenance of a bootstrapped `W`; migration
# 057 constrains both vocabularies. None of these are guessed here.
INITIAL_BOOTSTRAP_STATUS = "READY"
INITIAL_COVERED_THROUGH_SOURCE = "bootstrap"

# Evidence older than this cannot be trusted to still describe the schedule it
# was produced from: fires keep happening, so an aged inventory may no longer
# enumerate every unresolved interval inside the selected range.
MAX_EVIDENCE_AGE_SECONDS = 14 * 86_400

ACCEPTED_AUDIT_CLASSIFICATIONS = frozenset({
    CLASSIFICATION_COMPLETE,
    CLASSIFICATION_UNRESOLVED_GAPS,
})

EVIDENCE_REF_PREFIX = "telematics-coverage-bootstrap/1"
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Semantically load-bearing schedule/client configuration. A change to any of
# these between audit and write invalidates the inventory, because it changes
# which fires were expected or how a window is derived.
BOUND_SCHEDULE_PARAMETERS = (
    "enabled",
    "frequency",
    "day_of_week",
    "day_of_month",
    "day_of_month_last",
    "run_time",
    "timezone",
    "lookback_days",
    "trips_stabilization_delay_seconds",
    "trips_overlap_seconds",
    "trips_max_recovery_span_seconds",
)


class BootstrapRefused(RuntimeError):
    """Stable, sanitized refusal. Carries no secret and no raw evidence body."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> BootstrapRefused:
    return BootstrapRefused(code, message, exit_code)


# ---------------------------------------------------------------------------
# Operator input
# ---------------------------------------------------------------------------

def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be 1-200 characters of [A-Za-z0-9._:@/+-]",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def parse_bound(raw: str, *, label: str) -> datetime:
    """Parse an operator-approved bound as an aware whole-second UTC instant.

    Whole seconds are required rather than truncated: the C5 runtime gate
    refuses a sub-second bound as malformed state (`docs/13_…` §5.2.1), so
    silently rounding here would produce a row the runtime later rejects — and
    would move a bound the operator wrote down.
    """
    try:
        parsed = parse_iso_utc(raw, label=label)
    except Exception as exc:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be a timezone-aware ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.microsecond != 0:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be a whole-second instant; it is never rounded here",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


def build_evidence_ref(*, evidence_sha256: str, approval_ref: str) -> str:
    """A safe reference, never raw evidence content (`docs/13_…` §13.5).

    The stored value identifies the reviewed bundle by hash and the approval by
    ticket reference. It carries no bundle body, no counts and no client data.
    """
    return (
        f"{EVIDENCE_REF_PREFIX}:sha256={evidence_sha256}:approval={approval_ref}"
    )


# ---------------------------------------------------------------------------
# Evidence verification
# ---------------------------------------------------------------------------

def load_bundle(path: Path) -> dict:
    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
    except OSError as exc:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            f"the evidence bundle could not be read: {exc.strerror}",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    try:
        bundle = json.loads(raw)
    except ValueError as exc:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the evidence bundle is not valid JSON",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if not isinstance(bundle, dict):
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the evidence bundle must be a JSON object",
            EXIT_INVALID_PARAMETERS,
        )
    return bundle


def verify_bundle(
    *,
    bundle: dict,
    declared_sha256: str,
    client_code: str,
    dataset_name: str,
    expected_environment: str,
    expected_platform_uuid: str,
    now_utc: datetime,
) -> str:
    """Validate schema, hash, identity, classification and age. Returns the hash."""
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            f"unsupported bundle_version {bundle.get('bundle_version')!r}",
        )
    if bundle.get("bootstrap_semantics_version") != BOOTSTRAP_SEMANTICS_VERSION:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle was produced under a different bootstrap semantics "
            "contract; interval and gap meaning may differ",
        )

    stored_hash = str(bundle.get("bundle_sha256") or "")
    if not SHA256_RE.match(stored_hash):
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no canonical lowercase SHA-256",
        )
    recomputed = bundle_sha256(bundle)
    if recomputed != stored_hash:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle content does not match its own recorded SHA-256",
        )
    if declared_sha256 != stored_hash:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "--evidence-sha256 does not match the bundle hash",
        )

    for field, expected in (
        ("environment_name", expected_environment),
        ("platform_uuid", expected_platform_uuid),
        ("client_code", client_code),
        ("dataset_name", dataset_name),
    ):
        if str(bundle.get(field) or "") != str(expected):
            raise _refuse(
                "BOOTSTRAP_REFUSED_EVIDENCE",
                f"bundle {field} does not match the requested target",
            )
    if bundle.get("migration_ceiling") != MIGRATION_CEILING:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle was produced against a different migration ceiling",
        )

    classification = str(bundle.get("audit_classification") or "")
    if classification not in ACCEPTED_AUDIT_CLASSIFICATIONS:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            f"audit_classification {classification!r} cannot support a "
            "bootstrap",
        )

    generated_raw = str(bundle.get("generated_at_utc") or "")
    try:
        generated_at = parse_iso_utc(generated_raw, label="generated_at_utc")
    except Exception as exc:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no valid generated_at_utc",
        ) from exc
    age_seconds = (now_utc - generated_at).total_seconds()
    if age_seconds < 0:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle claims to have been generated in the future",
        )
    if age_seconds > MAX_EVIDENCE_AGE_SECONDS:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            f"the bundle is {int(age_seconds // 86400)} days old; the bounded "
            f"maximum is {MAX_EVIDENCE_AGE_SECONDS // 86400} days",
        )

    for field in ("client_id", "schedule_id"):
        canonical_uuid(bundle.get(field), label=f"bundle {field}")
    if bundle.get("existing_coverage") is not None:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle already records an existing coverage row",
        )
    return stored_hash


# ---------------------------------------------------------------------------
# Explicit A/W validation against the inventory
# ---------------------------------------------------------------------------

def find_blocking_interval(
    *,
    bundle: dict,
    coverage_start_ts: datetime,
    covered_through_ts: datetime,
) -> Optional[dict]:
    """Return the first inventoried interval intersecting the closed `[A, W]`.

    Both the selected interval and each inventoried interval are closed, so
    they intersect when ``gap_start <= W`` and ``gap_end >= A``. A gap entirely
    **before** `A` is excluded by the operator's choice of `A` and does not
    block (`docs/13_…` §13.3 — narrowing is always the safe response). A gap
    entirely **after** `W` lies outside the claim and does not block either.

    Nothing here moves a bound. The caller refuses; it never shrinks `W` or
    advances `A` to make an intersecting gap disappear (`docs/13_…` §13.7).
    """
    entries = bundle.get("missing_or_unproven_intervals") or []
    if not isinstance(entries, list):
        raise _refuse(
            "BOOTSTRAP_REFUSED_EVIDENCE",
            "missing_or_unproven_intervals is malformed",
        )
    for entry in entries:
        if not isinstance(entry, dict):
            raise _refuse(
                "BOOTSTRAP_REFUSED_EVIDENCE",
                "an inventoried interval entry is malformed",
            )
        raw_start = entry.get("interval_start_ts")
        raw_end = entry.get("interval_end_ts")
        if not raw_start or not raw_end:
            # An unresolved entry without both bounds cannot be proven
            # disjoint, so it is treated as blocking rather than ignored.
            return dict(entry)
        gap_start = parse_iso_utc(str(raw_start), label="interval_start_ts")
        gap_end = parse_iso_utc(str(raw_end), label="interval_end_ts")
        if gap_start <= covered_through_ts and gap_end >= coverage_start_ts:
            return dict(entry)
    return None


def validate_selected_interval(
    *,
    bundle: dict,
    coverage_start_ts: datetime,
    covered_through_ts: datetime,
    now_utc: datetime,
) -> None:
    if covered_through_ts < coverage_start_ts:
        raise _refuse(
            "BOOTSTRAP_REFUSED_INTERVAL",
            "--covered-through-ts precedes --coverage-start-ts; W must be >= A",
        )
    if covered_through_ts > now_utc:
        raise _refuse(
            "BOOTSTRAP_REFUSED_INTERVAL",
            "--covered-through-ts is in the future; the runtime gate refuses a "
            "future W as malformed coverage state",
        )
    blocking = find_blocking_interval(
        bundle=bundle,
        coverage_start_ts=coverage_start_ts,
        covered_through_ts=covered_through_ts,
    )
    if blocking is not None:
        raise _refuse(
            "BOOTSTRAP_REFUSED_INTERVAL",
            "an unresolved interval intersects the selected range: "
            f"kind={blocking.get('kind')} "
            f"fire={blocking.get('scheduled_fire_ts')} "
            f"start={blocking.get('interval_start_ts')} "
            f"end={blocking.get('interval_end_ts')}. Recover it under separate "
            "authorization, or choose a later A that excludes it. This tool "
            "never moves A or W for you.",
        )


# ---------------------------------------------------------------------------
# Fresh production preflight
# ---------------------------------------------------------------------------

def _bound_parameters(schedule: dict, client: dict) -> dict:
    merged = {**schedule, **client}
    return {
        name: (
            str(merged[name]) if name in {"run_time", "timezone", "frequency"}
            else merged[name]
        )
        for name in BOUND_SCHEDULE_PARAMETERS
    }


def _count_target_accounts(cur, client_code: str) -> int:
    cur.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_account "
        "WHERE client_code = %s",
        (client_code,),
    )
    return int((cur.fetchone() or {})["n"])


def _count_target_enabled_schedules(cur, *, client_id: str, dataset_name: str) -> int:
    """Count enabled BASE schedules for the target (M5).

    The caller refuses unless this is exactly 1, so the scope matters: a future
    reconciliation cadence is a legitimate second enabled row that neither owns
    nor bootstraps the watermark, and counting it would block every bootstrap
    for a reason that has nothing to do with the target's readiness.
    """
    cur.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_schedule "
        "WHERE client_id = %s AND dataset_name = %s AND enabled "
        "  AND run_type = %s",
        (client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
    )
    return int((cur.fetchone() or {})["n"])


def _recovery_table_present(cur) -> bool:
    cur.execute(
        "SELECT to_regclass('workflow_a_control.client_dataset_recovery_run')"
        "::text AS present"
    )
    return bool((cur.fetchone() or {}).get("present"))


def target_recovery_counts(cur, *, schedule_id: str, client_id: str) -> dict:
    """Count C11 recovery rows belonging to the target, and only the target.

    Migration 058 is additive and sits above this tool's migration ceiling, so a
    database that predates it simply has no recovery table. That is not a
    missing check: a table that does not exist cannot hold a recovery row, so
    the honest count is zero rather than a refusal.

    The predicate is deliberately broad within the target — `schedule_id` *or*
    `client_id` — because a recovery recorded against the same client under a
    different schedule still means this client is not in a fresh pre-bootstrap
    state. It never widens beyond the target.
    """
    if not _recovery_table_present(cur):
        return {"recovery_rows": 0, "active_recovery_rows": 0, "table_present": False}
    cur.execute(
        "SELECT count(*) AS total,"
        " count(*) FILTER (WHERE status IN ('PLANNED', 'RUNNING')) AS active"
        "  FROM workflow_a_control.client_dataset_recovery_run"
        " WHERE schedule_id = %s OR client_id = %s",
        (schedule_id, client_id),
    )
    row = cur.fetchone() or {}
    return {
        "recovery_rows": int(row["total"]),
        "active_recovery_rows": int(row["active"]),
        "table_present": True,
    }


def non_target_compatibility_clients(cur, *, client_id: str) -> list[str]:
    """Observe — never gate on — the other clients already running compatibility.

    Bootstrap is per client. `BRAVO00016` (or any later approved compatibility
    client) is fleet context an operator should see in the plan; it is not a
    precondition for a different client's first coverage row. The tool reads
    this list, reports it, and does not lock, touch or mutate any row behind it.
    """
    cur.execute(
        "SELECT client_code FROM workflow_a_control.client_account "
        "WHERE trips_pagination_mode = 'data_invariants_v1' "
        "  AND client_id <> %s "
        "ORDER BY client_code",
        (client_id,),
    )
    codes: list[str] = []
    for row in cur.fetchall():
        code = str(row["client_code"] or "")
        codes.append(code if SAFE_TOKEN_RE.match(code) else "<unsafe-client-code>")
    return codes


def preflight(
    cur,
    *,
    bundle: dict,
    client_code: str,
    dataset_name: str,
    expected_environment: str,
    expected_platform_uuid: str,
) -> dict:
    """Re-verify every precondition against live state. Read-only by itself.

    Every gate below is scoped to the one target client. The only fleet-wide
    read is the non-target compatibility observation, which is reported and
    never gated on.
    """
    verify_platform_identity(
        cur,
        expected_environment=expected_environment,
        expected_platform_uuid=expected_platform_uuid,
    )
    verify_migration_ceiling(cur)

    accounts = _count_target_accounts(cur, client_code)
    if accounts != 1:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            f"client_code resolves to {accounts} client accounts; exactly one "
            "target is required",
        )

    client = resolve_client(cur, client_code)
    enabled_schedules = _count_target_enabled_schedules(
        cur, client_id=client["client_id"], dataset_name=dataset_name
    )
    if enabled_schedules != 1:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            f"the target has {enabled_schedules} enabled {dataset_name} "
            "schedules; exactly one authoritative schedule is required",
        )
    schedule = resolve_schedule(
        cur, client_id=client["client_id"], dataset_name=dataset_name
    )

    if client["client_id"] != bundle["client_id"]:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved client_id differs from the evidence bundle",
        )
    if schedule["schedule_id"] != bundle["schedule_id"]:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved authoritative schedule differs from the bundle",
        )
    # The write identity is taken from these resolved rows, so they must agree
    # with the operator's request rather than merely resolve from it.
    if str(client["client_code"]) != client_code:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved client_code differs from the requested target",
        )
    if str(schedule["dataset_name"]) != dataset_name:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved schedule dataset differs from the requested target",
        )
    if str(schedule["client_id"]) != str(client["client_id"]):
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the authoritative schedule belongs to a different client",
        )
    if str(client["trips_pagination_mode"]) != "strict_meta":
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "the target client is not strict_meta; bootstrap must precede its "
            "own enablement (docs/13 §13.6 steps 5 and 6 are ordered, never "
            "reversed). Another client's mode is irrelevant here.",
        )

    recovery = target_recovery_counts(
        cur, schedule_id=schedule["schedule_id"], client_id=client["client_id"]
    )
    if recovery["active_recovery_rows"]:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EXISTING_RECOVERY",
            f"{recovery['active_recovery_rows']} PLANNED/RUNNING recovery "
            "run(s) exist for this target; a recovery in flight owns the "
            "watermark this bootstrap would seed",
        )
    if recovery["recovery_rows"]:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EXISTING_RECOVERY",
            f"{recovery['recovery_rows']} recovery run(s) already exist for "
            "this target; a client with recovery history is not in a fresh "
            "pre-bootstrap state",
        )

    observed = _bound_parameters(schedule, client)
    recorded = bundle.get("schedule_parameters") or {}
    drifted = [
        name for name in BOUND_SCHEDULE_PARAMETERS
        if str(recorded.get(name)) != str(observed[name])
    ]
    if drifted:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "schedule/client configuration changed since the audit: "
            f"{', '.join(sorted(drifted))}",
        )

    # Since M5 the watermark belongs to (client_id, dataset_name), so "a
    # coverage row already exists" is a question about the DATASET. Asking it by
    # schedule_id would answer "not for THIS schedule" and let a second bootstrap
    # attempt an INSERT that the shared-owner constraint then rejects with a raw
    # integrity error instead of this reviewed refusal.
    cur.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage "
        "WHERE client_id = %s AND dataset_name = %s",
        (schedule["client_id"], schedule["dataset_name"]),
    )
    coverage_rows = int((cur.fetchone() or {})["n"])
    if coverage_rows:
        raise _refuse(
            "BOOTSTRAP_REFUSED_EXISTING_ROW",
            "a coverage row already exists for this schedule; this tool only "
            "ever inserts and never repairs or overwrites",
        )

    cur.execute(
        "SELECT count(*) AS n "
        "FROM workflow_a_control.client_schedule_run_history "
        "WHERE schedule_id = %s AND status = 'RUNNING'",
        (schedule["schedule_id"],),
    )
    running_history_rows = int((cur.fetchone() or {})["n"])
    if running_history_rows:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PREFLIGHT",
            "a RUNNING schedule-history row exists for this schedule",
        )

    non_target_compat = non_target_compatibility_clients(
        cur, client_id=client["client_id"]
    )

    cur.execute("SELECT date_trunc('second', now()) AS db_now")
    db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)

    return {
        "client": client,
        "schedule": schedule,
        "db_now": db_now,
        "observation": {
            "target_coverage_row_count": coverage_rows,
            "target_recovery_row_count": recovery["recovery_rows"],
            "target_active_recovery_count": recovery["active_recovery_rows"],
            "target_running_history_row_count": running_history_rows,
            "recovery_table_present": recovery["table_present"],
            "non_target_compatibility_client_count": len(non_target_compat),
            "non_target_compatibility_client_codes": non_target_compat,
        },
    }


# ---------------------------------------------------------------------------
# The single approved coverage INSERT
# ---------------------------------------------------------------------------

def _insert_initial_coverage_row(cur, params: dict) -> int:
    """Insert exactly one initial `READY` coverage row and return the row count.

    This is the repository's only authorized coverage `INSERT`. The statement is
    a plain `INSERT ... VALUES`: no conflict clause, no upsert, no update clause
    and no repair. `last_gap_detected_ts` is deliberately absent from the column
    list — a fresh bootstrap has detected no gap, and the column's `NULL`
    default is the honest representation. `updated_at` is written explicitly so
    that the post-write read-back can verify every stored value exactly.

    A concurrent bootstrap loses on `pk_client_dataset_coverage` (same schedule)
    or on `uq_client_dataset_coverage_dataset` (a different schedule of the same
    dataset, possible since M5). Either way the caller maps it to a write
    conflict rather than retrying.
    """
    cur.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_coverage (
            schedule_id, client_id, client_code, dataset_name,
            coverage_start_ts, covered_through_ts,
            bootstrap_status, bootstrap_evidence_ref,
            seeded_at, seeded_by, covered_through_source, updated_at
        ) VALUES (
            %(schedule_id)s, %(client_id)s, %(client_code)s, %(dataset_name)s,
            %(coverage_start_ts)s, %(covered_through_ts)s,
            %(bootstrap_status)s, %(bootstrap_evidence_ref)s,
            %(seeded_at)s, %(seeded_by)s, %(covered_through_source)s,
            %(updated_at)s
        )
        """,
        params,
    )
    return cur.rowcount


def _read_back(cur, *, client_id: str, dataset_name: str) -> list[dict]:
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,
               client_code, dataset_name, coverage_start_ts, covered_through_ts,
               bootstrap_status, bootstrap_evidence_ref, seeded_at, seeded_by,
               covered_through_source, last_gap_detected_ts, updated_at
          FROM workflow_a_control.client_dataset_coverage
         WHERE client_id = %s
           AND dataset_name = %s
        """,
        (client_id, dataset_name),
    )
    return [dict(row) for row in cur.fetchall()]


def _verify_stored_row(stored: dict, params: dict) -> list[str]:
    mismatches: list[str] = []
    for field in (
        "schedule_id", "client_id", "client_code", "dataset_name",
        "bootstrap_status", "bootstrap_evidence_ref", "seeded_by",
        "covered_through_source",
    ):
        if str(stored.get(field)) != str(params[field]):
            mismatches.append(field)
    for field in (
        "coverage_start_ts", "covered_through_ts", "seeded_at", "updated_at",
    ):
        value = stored.get(field)
        if value is None or value.astimezone(timezone.utc) != params[field]:
            mismatches.append(field)
    if stored.get("last_gap_detected_ts") is not None:
        mismatches.append("last_gap_detected_ts")
    return mismatches


def execute_bootstrap(conn, *, schedule_id: str, params: dict) -> dict:
    """One explicit transaction: lock, re-check, insert exactly one, verify."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "BOOTSTRAP_RUNTIME_FAILURE", "psycopg is required",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    with conn.cursor() as cur:
        # Protect the authoritative identity/mode state for the duration of the
        # insert, so a concurrent enablement or schedule edit cannot interleave.
        cur.execute(
            "SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,"
            " client_code, dataset_name, enabled"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s FOR UPDATE",
            (schedule_id,),
        )
        locked_schedule = [dict(row) for row in cur.fetchall()]
        if len(locked_schedule) != 1:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "the authoritative schedule disappeared before the insert",
                EXIT_WRITE_CONFLICT,
            )
        cur.execute(
            "SELECT client_id::text AS client_id, trips_pagination_mode"
            "  FROM workflow_a_control.client_account"
            " WHERE client_id = %s FOR UPDATE",
            (params["client_id"],),
        )
        locked_client = [dict(row) for row in cur.fetchall()]
        if len(locked_client) != 1 or \
                locked_client[0]["trips_pagination_mode"] != "strict_meta":
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "the client mode changed before the insert",
                EXIT_WRITE_CONFLICT,
            )
        if (
            locked_schedule[0]["client_id"] != params["client_id"]
            or locked_schedule[0]["dataset_name"] != params["dataset_name"]
        ):
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "the schedule identity changed before the insert",
                EXIT_WRITE_CONFLICT,
            )

        cur.execute(
            "SELECT count(*) AS n "
            "FROM workflow_a_control.client_schedule_run_history "
            "WHERE schedule_id = %s AND status = 'RUNNING'",
            (schedule_id,),
        )
        if int((cur.fetchone() or {})["n"]):
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "a RUNNING history row appeared before the insert",
                EXIT_WRITE_CONFLICT,
            )

        recovery = target_recovery_counts(
            cur,
            schedule_id=schedule_id,
            client_id=params["client_id"],
        )
        if recovery["recovery_rows"]:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "a recovery run appeared for this target before the insert",
                EXIT_WRITE_CONFLICT,
            )

        try:
            affected = _insert_initial_coverage_row(cur, params)
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                "another transaction inserted the coverage row first; not "
                "retried",
                EXIT_WRITE_CONFLICT,
            ) from exc
        except psycopg.Error as exc:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                f"the coverage insert was rejected: {exc.__class__.__name__}",
                EXIT_WRITE_CONFLICT,
            ) from exc

        if affected != 1:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_WRITE_CONFLICT",
                f"the insert affected {affected} rows; exactly 1 is required",
                EXIT_WRITE_CONFLICT,
            )

        stored_rows = _read_back(
            cur,
            client_id=params["client_id"],
            dataset_name=params["dataset_name"],
        )
        if len(stored_rows) != 1:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED",
                f"{len(stored_rows)} coverage rows are visible after the insert",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )
        mismatches = _verify_stored_row(stored_rows[0], params)
        if mismatches:
            conn.rollback()
            raise _refuse(
                "BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED",
                "stored values differ from the approved inputs: "
                f"{', '.join(sorted(mismatches))}",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )
        stored = stored_rows[0]

    conn.commit()
    return stored


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first Telematics trips coverage bootstrap. Inserts exactly "
            "one initial READY coverage row from explicit operator-approved "
            "bounds and reviewed audit evidence."
        ),
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument(
        "--coverage-start-ts", required=True,
        help="A — explicit reviewed lower bound. Never inferred or moved.",
    )
    parser.add_argument(
        "--covered-through-ts", required=True,
        help="W — explicit reviewed upper bound. Never inferred or moved.",
    )
    parser.add_argument("--seeded-by", required=True)
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
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
    return parser


def _plan(params: dict, *, bundle: dict, observation: dict, dry_run: bool) -> dict:
    return {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "client_code": params["client_code"],
        "target_client_code": params["client_code"],
        "target_pagination_mode": observation["target_pagination_mode"],
        "target_coverage_row_count": observation["target_coverage_row_count"],
        "target_recovery_row_count": observation["target_recovery_row_count"],
        "target_active_recovery_count":
            observation["target_active_recovery_count"],
        "target_running_history_row_count":
            observation["target_running_history_row_count"],
        "recovery_table_present": observation["recovery_table_present"],
        "non_target_compatibility_client_count":
            observation["non_target_compatibility_client_count"],
        "non_target_compatibility_client_codes":
            list(observation["non_target_compatibility_client_codes"]),
        "client_id": params["client_id"],
        "dataset_name": params["dataset_name"],
        "schedule_id": params["schedule_id"],
        "coverage_start_ts": iso_utc(params["coverage_start_ts"]),
        "covered_through_ts": iso_utc(params["covered_through_ts"]),
        "bootstrap_status": params["bootstrap_status"],
        "bootstrap_evidence_ref": params["bootstrap_evidence_ref"],
        "seeded_at": iso_utc(params["seeded_at"]),
        "seeded_by": params["seeded_by"],
        "covered_through_source": params["covered_through_source"],
        "last_gap_detected_ts": None,
        "evidence_bundle_sha256": bundle["bundle_sha256"],
        "audit_classification": bundle["audit_classification"],
        "rows_to_insert": 1,
        "rows_to_update": 0,
        "rows_to_delete": 0,
        "client_mode_change": None,
        "client_mode_changes": 0,
        "schedule_change": None,
        "schedule_changes": 0,
        "history_rows_created": 0,
        "history_mutations": 0,
        "recovery_rows_created": 0,
        "recovery_mutations": 0,
        "non_target_coverage_rows_touched": 0,
        "provider_requests": 0,
        "subprocesses_launched": 0,
    }


def run(args) -> tuple[int, dict]:
    dry_run = not args.execute
    client_code = _safe_token(args.client_code, label="--client-code")
    dataset_name = _safe_token(args.dataset, label="--dataset")
    seeded_by = _safe_token(args.seeded_by, label="--seeded-by")
    approval_ref = _safe_token(args.approval_ref, label="--approval-ref")
    declared_hash = str(args.evidence_sha256 or "").strip().lower()
    if not SHA256_RE.match(declared_hash):
        raise _refuse(
            "BOOTSTRAP_REFUSED_PARAMETER",
            "--evidence-sha256 must be a lowercase 64-character SHA-256",
            EXIT_INVALID_PARAMETERS,
        )
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    expected_environment = str(args.expected_environment or "").strip()
    if not expected_environment:
        raise _refuse(
            "BOOTSTRAP_REFUSED_PARAMETER",
            "--expected-environment is required",
            EXIT_INVALID_PARAMETERS,
        )

    if not dry_run:
        confirmed = str(args.confirm_client_code or "").strip()
        if not confirmed:
            raise _refuse(
                "BOOTSTRAP_REFUSED_CONFIRMATION",
                "--execute additionally requires --confirm-client-code",
                EXIT_INVALID_PARAMETERS,
            )
        if confirmed != client_code:
            raise _refuse(
                "BOOTSTRAP_REFUSED_CONFIRMATION",
                "--confirm-client-code does not match --client-code",
                EXIT_INVALID_PARAMETERS,
            )

    coverage_start_ts = parse_bound(
        args.coverage_start_ts, label="--coverage-start-ts"
    )
    covered_through_ts = parse_bound(
        args.covered_through_ts, label="--covered-through-ts"
    )

    bundle = load_bundle(Path(args.evidence_file))

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT date_trunc('second', now()) AS db_now")
            db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)

            evidence_hash = verify_bundle(
                bundle=bundle,
                declared_sha256=declared_hash,
                client_code=client_code,
                dataset_name=dataset_name,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
                now_utc=db_now,
            )
            validate_selected_interval(
                bundle=bundle,
                coverage_start_ts=coverage_start_ts,
                covered_through_ts=covered_through_ts,
                now_utc=db_now,
            )
            state = preflight(
                cur,
                bundle=bundle,
                client_code=client_code,
                dataset_name=dataset_name,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    # Every identity column written below comes from the rows `preflight`
    # resolved and verified, not from the CLI strings that merely selected them.
    schedule = state["schedule"]
    client = state["client"]
    params = {
        "schedule_id": schedule["schedule_id"],
        "client_id": client["client_id"],
        "client_code": client["client_code"],
        "dataset_name": schedule["dataset_name"],
        "coverage_start_ts": coverage_start_ts,
        "covered_through_ts": covered_through_ts,
        "bootstrap_status": INITIAL_BOOTSTRAP_STATUS,
        "bootstrap_evidence_ref": build_evidence_ref(
            evidence_sha256=evidence_hash, approval_ref=approval_ref
        ),
        "seeded_at": state["db_now"],
        "seeded_by": seeded_by,
        "covered_through_source": INITIAL_COVERED_THROUGH_SOURCE,
        "updated_at": state["db_now"],
    }
    observation = {
        **state["observation"],
        "target_pagination_mode": str(client["trips_pagination_mode"]),
    }
    plan = _plan(params, bundle=bundle, observation=observation, dry_run=dry_run)

    if dry_run:
        plan["would_insert"] = True
        plan["database_writes_performed"] = 0
        return EXIT_OK, plan

    import psycopg
    from psycopg.rows import dict_row

    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        stored = execute_bootstrap(
            write_conn, schedule_id=params["schedule_id"], params=params
        )
    except BootstrapRefused:
        try:
            write_conn.rollback()
        except Exception:
            pass
        raise
    finally:
        write_conn.close()

    plan["database_writes_performed"] = 1
    plan["affected_row_count"] = 1
    plan["transaction_result"] = "COMMITTED"
    plan["stored_row"] = {
        "schedule_id": stored["schedule_id"],
        "client_id": stored["client_id"],
        "client_code": stored["client_code"],
        "dataset_name": stored["dataset_name"],
        "coverage_start_ts": iso_utc(stored["coverage_start_ts"]),
        "covered_through_ts": iso_utc(stored["covered_through_ts"]),
        "bootstrap_status": stored["bootstrap_status"],
        "bootstrap_evidence_ref": stored["bootstrap_evidence_ref"],
        "seeded_at": iso_utc(stored["seeded_at"]),
        "seeded_by": stored["seeded_by"],
        "covered_through_source": stored["covered_through_source"],
        "last_gap_detected_ts": iso_utc(stored["last_gap_detected_ts"]),
        "updated_at": iso_utc(stored["updated_at"]),
    }
    return EXIT_OK, plan


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, plan = run(args)
    except BootstrapRefused as exc:
        print(f"BOOTSTRAP_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(
            f"BOOTSTRAP_FAILED {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(plan, sort_keys=True, indent=2, default=str))
    print(plan["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
