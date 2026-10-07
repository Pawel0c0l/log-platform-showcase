#!/usr/bin/env python3
"""Dry-run-first zero-state cold-start coverage baseline writer.

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §5.2/§5.2.1 (bounded coverage
    interval and the fail-closed preconditions), §13.6 (the ordered gates)
  docs/14_telematics_trips_compatibility_implementation_plan.md §4.1–§4.6, §6.1
  docs/15_telematics_coverage_mutation_contract.md (C6/C11 own every later mutation)
  db/migrations/057_workflow_a_trips_coverage_state.sql (the physical contract)
  ops/audit_telematics_cold_start.py (the evidence contract this consumes)

WHAT THIS IS.
    A second, deliberately separate coverage `INSERT` surface for the one case
    the historical C10 writer cannot serve: a client that has never run. It
    inserts a **zero-width initialization baseline** — ``A == W == the approved
    first managed instant`` — and nothing else.

WHY IT IS SEPARATE FROM `ops/bootstrap_telematics_trips_coverage.py`.
    The historical writer's contract is evidence-based: a reviewed inventory of
    proven intervals, an operator-selected ``[A, W]`` inside that evidence, and
    a refusal whenever an unresolved interval intersects the claim. Widening it
    to accept "no evidence at all" would turn its central guarantee into a
    special case, and the special case would then be reachable for clients that
    *do* have history. Two narrow writers that each refuse the other's evidence
    are safer than one permissive writer:

    * this tool accepts only ``telematics-cold-start-audit/1`` bundles classified
      ``COLD_START_ZERO_STATE_CONFIRMED``, so a historical C10 bundle is
      refused on its `bundle_version`;
    * the historical writer accepts only ``telematics-coverage-bootstrap-audit/1``
      bundles, so a cold-start bundle is refused there by the same mechanism,
      with no change to that module.

WHY ``A == W`` IS NOT A FALSE CLAIM.
    ``[A, W]`` is a closed interval of *verified* coverage. With ``A == W`` it
    is degenerate: it spans zero elapsed time and therefore asserts that no
    period of any duration has been covered. It cannot overstate history,
    because there is no history inside a zero-width interval to overstate. Its
    only function is to give the schedule a monotone anchor from which the
    single authorized recovery starts — the recovery must begin exactly at
    ``W``, so the union of the baseline and the recovered interval is
    ``[T, E]`` with no one-second hole at the join. The row is deliberately
    **not** reporting-ready: ``bootstrap_status = 'READY'`` is coverage-state
    vocabulary meaning "this bounded claim is usable", never "this client has
    data".

HARD GUARANTEES.
    * dry-run is the default; a write requires ``--execute``, a second
      deliberate ``--confirm-client-code``, and an explicit
      ``--confirm-schedule-disabled``;
    * exactly one `INSERT`; no `UPDATE`, no `DELETE`, no `ON CONFLICT`, no
      repair path;
    * the client mode, the schedule (including its `enabled` flag), history,
      recovery rows and all client-business data are untouched;
    * zero provider requests, zero subprocesses;
    * every zero-state condition is re-validated inside the write transaction,
      under row locks scoped to the one target.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/bootstrap_telematics_cold_start_coverage.py \\
        --client-code ECHO00001 --dataset trips_sync \\
        --evidence-file /var/lib/log-platform/cold-start/ECHO00001.json \\
        --evidence-sha256 <64 hex> \\
        --expected-schedule-id 60c80b85-f294-4a00-8e09-b6a3688af443 \\
        --confirm-schedule-disabled \\
        --coverage-start-ts 2026-07-01T00:00:00Z \\
        --initial-covered-through-ts 2026-07-01T00:00:00Z \\
        --seeded-by operator@example.invalid \\
        --approval-ref TELEMATICS-COLD-START-ECHO00001-1 \\
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

from ops.audit_telematics_cold_start import (  # noqa: E402
    CLASSIFICATION_ZERO_STATE_CONFIRMED,
    COLD_START_BUNDLE_VERSION,
    COLD_START_SEMANTICS_VERSION,
    DEFAULT_DATASET,
    MIGRATION_CEILING,
    bundle_sha256,
    evaluate_zero_state,
)
from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
    canonical_uuid,
    iso_utc,
    open_read_only_connection,
    parse_iso_utc,
    platform_dsn_from_env,
    _load_dotenv,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
EXIT_WRITE_CONFLICT = 6
EXIT_POSTWRITE_VERIFICATION_FAILED = 7

# The approved initial state, identical in shape to the historical bootstrap:
# migration 057 requires `READY` to carry both bounds, a non-empty evidence
# reference and seed metadata, and `bootstrap` is the only honest provenance
# for a watermark that no execution has moved.
INITIAL_BOOTSTRAP_STATUS = "READY"
INITIAL_COVERED_THROUGH_SOURCE = "bootstrap"

STRICT_META = "strict_meta"

# Distinct from `telematics-coverage-bootstrap/1`. The recovery tool's cold-start
# gate requires this exact prefix, so a historically bootstrapped row can never
# be recovered through the disabled-schedule path and vice versa.
COLD_START_EVIDENCE_REF_PREFIX = "telematics-cold-start-bootstrap/1"

# Cold-start evidence ages exactly like historical evidence: a stale bundle may
# no longer describe the schedule it was produced from.
MAX_EVIDENCE_AGE_SECONDS = 14 * 86_400

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Semantically load-bearing configuration. A change to any of these between the
# evidence and the write invalidates the attested zero state.
BOUND_SCHEDULE_PARAMETERS = (
    "enabled",
    "frequency",
    "day_of_week",
    "day_of_month",
    "day_of_month_last",
    "run_time",
    "timezone",
    "lookback_days",
)
BOUND_CLIENT_PARAMETERS = (
    "trips_stabilization_delay_seconds",
    "trips_overlap_seconds",
    "trips_max_recovery_span_seconds",
)


class ColdStartBootstrapRefused(RuntimeError):
    """Stable, sanitized refusal. Carries no secret and no raw evidence body."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(
    code: str, message: str, exit_code: int = EXIT_REFUSED
) -> ColdStartBootstrapRefused:
    return ColdStartBootstrapRefused(code, message, exit_code)


# ---------------------------------------------------------------------------
# Operator input
# ---------------------------------------------------------------------------

def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be 1-200 characters of [A-Za-z0-9._:@/+-]",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def parse_bound(raw: str, *, label: str) -> datetime:
    """Parse an operator-approved bound as an aware whole-second UTC instant."""
    try:
        parsed = parse_iso_utc(raw, label=label)
    except Exception as exc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be a timezone-aware ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.microsecond != 0:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            f"{label} must be a whole-second instant; it is never rounded here",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed


def build_evidence_ref(
    *, evidence_sha256: str, approval_ref: str, managed_start_iso: str
) -> str:
    """A safe reference that also records what kind of baseline this row is.

    The stored value identifies the reviewed zero-state bundle by hash, the
    approval by ticket, and the desired managed start instant. It carries no
    bundle body, no counts and no client data. The `cold-start` prefix is what
    the C11 disabled-schedule gate matches on, so the row's provenance is not a
    matter of documentation discipline.
    """
    return (
        f"{COLD_START_EVIDENCE_REF_PREFIX}:sha256={evidence_sha256}"
        f":approval={approval_ref}:managed-start={managed_start_iso}"
    )


# ---------------------------------------------------------------------------
# Evidence verification
# ---------------------------------------------------------------------------

def load_bundle(path: Path) -> Dict[str, Any]:
    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
    except OSError as exc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            f"the evidence bundle could not be read: {exc.strerror}",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    try:
        bundle = json.loads(raw)
    except ValueError as exc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the evidence bundle is not valid JSON",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if not isinstance(bundle, dict):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the evidence bundle must be a JSON object",
            EXIT_INVALID_PARAMETERS,
        )
    return bundle


def verify_bundle(
    *,
    bundle: Dict[str, Any],
    declared_sha256: str,
    client_code: str,
    dataset_name: str,
    expected_schedule_id: str,
    expected_environment: str,
    expected_platform_uuid: str,
    now_utc: datetime,
) -> str:
    """Validate schema, hash, identity, classification and age. Returns the hash."""
    if bundle.get("bundle_version") != COLD_START_BUNDLE_VERSION:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            f"unsupported bundle_version {bundle.get('bundle_version')!r}; this "
            "writer accepts only zero-state cold-start evidence, never a "
            "historical C10 inventory bundle",
        )
    if bundle.get("cold_start_semantics_version") != COLD_START_SEMANTICS_VERSION:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle was produced under a different cold-start semantics "
            "contract; baseline meaning may differ",
        )

    stored_hash = str(bundle.get("bundle_sha256") or "")
    if not SHA256_RE.match(stored_hash):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no canonical lowercase SHA-256",
        )
    if bundle_sha256(bundle) != stored_hash:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle content does not match its own recorded SHA-256",
        )
    if declared_sha256 != stored_hash:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "--evidence-sha256 does not match the bundle hash",
        )

    for field, expected in (
        ("environment_name", expected_environment),
        ("platform_uuid", expected_platform_uuid),
        ("client_code", client_code),
        ("dataset_name", dataset_name),
        ("schedule_id", expected_schedule_id),
    ):
        if str(bundle.get(field) or "") != str(expected):
            raise _refuse(
                "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
                f"bundle {field} does not match the requested target",
            )
    if bundle.get("migration_ceiling") != MIGRATION_CEILING:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle was produced against a different migration ceiling",
        )
    if str(bundle.get("audit_classification")) != CLASSIFICATION_ZERO_STATE_CONFIRMED:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            f"audit_classification {bundle.get('audit_classification')!r} "
            "cannot support a cold-start baseline",
        )
    if bundle.get("schedule_enabled") is not False:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle does not attest a disabled authoritative schedule",
        )
    if str(bundle.get("client_mode")) != STRICT_META:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            f"the bundle attests client_mode {bundle.get('client_mode')!r}, "
            f"expected {STRICT_META}",
        )

    counts = bundle.get("zero_state_counts")
    if not isinstance(counts, dict):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no zero_state_counts object",
        )
    for key in (
        "coverage_rows", "recovery_rows", "active_recovery_rows",
        "schedule_history_rows", "running_schedule_history_rows",
        "platform_business_runs", "running_platform_business_runs",
        "client_trips_rows",
    ):
        if counts.get(key) != 0:
            raise _refuse(
                "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
                f"the bundle reports {key} = {counts.get(key)!r}; a cold start "
                "requires zero",
            )

    generated_raw = str(bundle.get("generated_at_utc") or "")
    try:
        generated_at = parse_iso_utc(generated_raw, label="generated_at_utc")
    except Exception as exc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no valid generated_at_utc",
        ) from exc
    age_seconds = (now_utc - generated_at).total_seconds()
    if age_seconds < 0:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle claims to have been generated in the future",
        )
    if age_seconds > MAX_EVIDENCE_AGE_SECONDS:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            f"the bundle is {int(age_seconds // 86400)} days old; the bounded "
            f"maximum is {MAX_EVIDENCE_AGE_SECONDS // 86400} days",
        )

    for field in ("client_id", "schedule_id"):
        canonical_uuid(bundle.get(field), label=f"bundle {field}")
    return stored_hash


def validate_baseline(
    *,
    bundle: Dict[str, Any],
    coverage_start_ts: datetime,
    covered_through_ts: datetime,
    now_utc: datetime,
) -> datetime:
    """Enforce the zero-width baseline contract and return the managed start.

    Three separate refusals, none of which is ever repaired by moving a bound:

    * ``A != W`` — a cold start has verified nothing, so any positive-width
      interval would be a claim about a period nobody proved;
    * ``A`` different from the attested ``desired_managed_start_ts`` — the
      baseline is bound to the reviewed evidence, not to a fresh CLI value;
    * ``W`` in the future — the runtime coverage gate treats a future watermark
      as malformed state, so a row that would be rejected is never created.
    """
    if coverage_start_ts != covered_through_ts:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_BASELINE",
            "--coverage-start-ts and --initial-covered-through-ts must be the "
            "same instant. A cold-start baseline is a zero-width initialization "
            "anchor; a positive-width interval would claim verified coverage "
            "of a period that has never been fetched",
        )
    attested_raw = str(bundle.get("desired_managed_start_ts") or "")
    try:
        attested = parse_iso_utc(attested_raw, label="desired_managed_start_ts")
    except Exception as exc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            "the bundle carries no valid desired_managed_start_ts",
        ) from exc
    if attested != coverage_start_ts:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_BASELINE",
            "the requested baseline instant differs from the managed start "
            "attested by the reviewed evidence; this tool never moves it",
        )
    if covered_through_ts > now_utc:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_BASELINE",
            "the baseline instant is in the future; the runtime gate refuses a "
            "future covered_through_ts as malformed coverage state",
        )
    return attested


# ---------------------------------------------------------------------------
# Fresh preflight
# ---------------------------------------------------------------------------

def _bound_parameters(schedule: Dict[str, Any], client: Dict[str, Any]) -> Dict[str, Any]:
    values = {name: schedule[name] for name in BOUND_SCHEDULE_PARAMETERS}
    values.update({name: client[name] for name in BOUND_CLIENT_PARAMETERS})
    return values


def preflight(
    cur,
    *,
    bundle: Dict[str, Any],
    client_code: str,
    dataset_name: str,
    expected_schedule_id: str,
    expected_environment: str,
    expected_platform_uuid: str,
) -> Dict[str, Any]:
    """Re-verify every zero-state precondition against live state, read-only."""
    try:
        state = evaluate_zero_state(
            cur,
            client_code=client_code,
            dataset_name=dataset_name,
            expected_schedule_id=expected_schedule_id,
            expected_environment=expected_environment,
            expected_platform_uuid=expected_platform_uuid,
        )
    except Exception as exc:
        # The zero-state evaluator raises its own stable codes; re-raising them
        # verbatim keeps one vocabulary for one condition.
        raise _map_zero_state_error(exc) from exc

    client = state["client"]
    schedule = state["schedule"]
    if str(client["client_id"]) != str(bundle["client_id"]):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved client_id differs from the evidence bundle",
        )
    if str(schedule["schedule_id"]) != str(bundle["schedule_id"]):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved authoritative schedule differs from the bundle",
        )
    if str(client["client_code"]) != client_code:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved client_code differs from the requested target",
        )
    if str(schedule["dataset_name"]) != dataset_name:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PREFLIGHT",
            "the resolved schedule dataset differs from the requested target",
        )

    observed = _bound_parameters(schedule, client)
    recorded = bundle.get("schedule_parameters") or {}
    drifted = [
        name for name in (*BOUND_SCHEDULE_PARAMETERS, *BOUND_CLIENT_PARAMETERS)
        if str(recorded.get(name)) != str(observed[name])
    ]
    if drifted:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PREFLIGHT",
            "schedule/client configuration changed since the audit: "
            f"{', '.join(sorted(drifted))}",
        )
    return state


def _map_zero_state_error(exc: Exception) -> Exception:
    from ops.audit_telematics_cold_start import ColdStartAuditError

    if isinstance(exc, ColdStartAuditError):
        # `str(exc)` already begins with the code; keep one copy of it.
        detail = str(exc).split(": ", 1)[-1]
        return _refuse(exc.code, detail, EXIT_REFUSED)
    return exc


# ---------------------------------------------------------------------------
# The single approved cold-start coverage INSERT
# ---------------------------------------------------------------------------

def _insert_baseline_row(cur, params: Dict[str, Any]) -> int:
    """Insert exactly one zero-width `READY` baseline row and return the count.

    A plain `INSERT ... VALUES`: no conflict clause, no upsert, no update clause
    and no repair. `last_gap_detected_ts` is deliberately absent from the column
    list — a fresh baseline has detected no gap.
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


def _read_back(cur, *, client_id: str, dataset_name: str) -> list:
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


def _verify_stored_row(stored: Dict[str, Any], params: Dict[str, Any]) -> list:
    mismatches = []
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
    if stored.get("coverage_start_ts") != stored.get("covered_through_ts"):
        mismatches.append("zero_width_baseline")
    return mismatches


def execute_cold_start_bootstrap(
    conn,
    *,
    params: Dict[str, Any],
    client_code: str,
    dataset_name: str,
    expected_schedule_id: str,
    expected_environment: str,
    expected_platform_uuid: str,
    bundle: Dict[str, Any],
) -> Dict[str, Any]:
    """One explicit transaction: lock, re-check the zero state, insert, verify."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "COLD_START_BOOTSTRAP_RUNTIME_FAILURE", "psycopg is required",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    schedule_id = params["schedule_id"]
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        # Lock exactly the two target rows whose values authorize this write.
        cur.execute(
            "SELECT client_id::text AS client_id, client_code,"
            " trips_pagination_mode"
            "  FROM workflow_a_control.client_account"
            " WHERE client_id = %s FOR UPDATE",
            (params["client_id"],),
        )
        locked_client = [dict(row) for row in cur.fetchall()]
        if len(locked_client) != 1 or \
                str(locked_client[0]["trips_pagination_mode"]) != STRICT_META:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                "the client disappeared or its mode changed before the insert",
                EXIT_WRITE_CONFLICT,
            )
        cur.execute(
            "SELECT schedule_id::text AS schedule_id,"
            " client_id::text AS client_id, dataset_name, enabled"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s FOR UPDATE",
            (schedule_id,),
        )
        locked_schedule = [dict(row) for row in cur.fetchall()]
        if len(locked_schedule) != 1:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                "the authoritative schedule disappeared before the insert",
                EXIT_WRITE_CONFLICT,
            )
        locked = locked_schedule[0]
        if bool(locked["enabled"]):
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                "the schedule was enabled before the insert; a cold-start "
                "baseline is only ever seeded for a disabled schedule",
                EXIT_WRITE_CONFLICT,
            )
        if str(locked["client_id"]) != str(params["client_id"]) or \
                str(locked["dataset_name"]) != str(params["dataset_name"]):
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                "the schedule identity changed before the insert",
                EXIT_WRITE_CONFLICT,
            )

        # Every zero-state condition, re-evaluated under the locks.
        try:
            evaluate_zero_state(
                cur,
                client_code=client_code,
                dataset_name=dataset_name,
                expected_schedule_id=expected_schedule_id,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_platform_uuid,
            )
        except Exception as exc:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                f"the zero state no longer holds before the insert: {exc}",
                EXIT_WRITE_CONFLICT,
            ) from exc

        try:
            affected = _insert_baseline_row(cur, params)
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                "another transaction inserted the coverage row first; not "
                "retried",
                EXIT_WRITE_CONFLICT,
            ) from exc
        except psycopg.Error as exc:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                f"the coverage insert was rejected: {exc.__class__.__name__}",
                EXIT_WRITE_CONFLICT,
            ) from exc

        if affected != 1:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
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
                "COLD_START_BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED",
                f"{len(stored_rows)} coverage rows are visible after the insert",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )
        mismatches = _verify_stored_row(stored_rows[0], params)
        if mismatches:
            conn.rollback()
            raise _refuse(
                "COLD_START_BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED",
                "stored values differ from the approved inputs: "
                f"{', '.join(sorted(mismatches))}",
                EXIT_POSTWRITE_VERIFICATION_FAILED,
            )
        stored = stored_rows[0]

    conn.commit()
    return stored


def independent_fingerprint(
    dsn: str, *, client_id: str, dataset_name: str,
) -> Dict[str, Any]:
    """Re-read the stored row on a fresh read-only connection and fingerprint it.

    Independent of the write connection on purpose: the fingerprint the operator
    carries into the mode change, the recovery and the activation must come from
    a transaction that could not have seen uncommitted state.
    """
    from jobs.api.telematics.coverage_finalization import coverage_fingerprint

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            rows = _read_back(
                cur, client_id=client_id, dataset_name=dataset_name,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    if len(rows) != 1:
        raise _refuse(
            "COLD_START_BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED",
            f"{len(rows)} coverage rows are visible on an independent read",
            EXIT_POSTWRITE_VERIFICATION_FAILED,
        )
    return {"row": rows[0], "fingerprint": coverage_fingerprint(rows[0])}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first zero-state cold-start coverage baseline writer. "
            "Inserts exactly one zero-width READY baseline row from reviewed "
            "cold-start evidence."
        ),
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--expected-schedule-id", required=True)
    parser.add_argument(
        "--confirm-schedule-disabled", action="store_true",
        help="Explicit confirmation that the authoritative schedule is expected "
             "to be disabled for the whole of this operation.",
    )
    parser.add_argument(
        "--coverage-start-ts", required=True,
        help="A — the approved first managed instant. Never inferred or moved.",
    )
    parser.add_argument(
        "--initial-covered-through-ts", required=True,
        help="W — must equal A. The baseline is zero-width by contract.",
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


def _plan(
    params: Dict[str, Any],
    *,
    bundle: Dict[str, Any],
    state: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "path": "COLD_START_ZERO_STATE",
        "cold_start_semantics_version": COLD_START_SEMANTICS_VERSION,
        "client_code": params["client_code"],
        "client_id": params["client_id"],
        "dataset_name": params["dataset_name"],
        "schedule_id": params["schedule_id"],
        "schedule_enabled": bool(state["schedule"]["enabled"]),
        "target_pagination_mode": str(state["client"]["trips_pagination_mode"]),
        "target_coverage_row_count": state["coverage_rows"],
        "target_recovery_row_count": state["recovery"]["total"],
        "target_schedule_history_row_count": state["history"]["total"],
        "target_platform_business_run_count": state["platform_runs"]["total"],
        "target_client_trips_row_count": bundle["zero_state_counts"][
            "client_trips_rows"
        ],
        "coverage_start_ts": iso_utc(params["coverage_start_ts"]),
        "covered_through_ts": iso_utc(params["covered_through_ts"]),
        "covered_interval_seconds": 0,
        "baseline_kind": "ZERO_WIDTH_INITIALIZATION",
        "reporting_ready": False,
        "bootstrap_status": params["bootstrap_status"],
        "bootstrap_evidence_ref": params["bootstrap_evidence_ref"],
        "seeded_at": iso_utc(params["seeded_at"]),
        "seeded_by": params["seeded_by"],
        "covered_through_source": params["covered_through_source"],
        "last_gap_detected_ts": None,
        "evidence_bundle_sha256": bundle["bundle_sha256"],
        "audit_classification": bundle["audit_classification"],
        "desired_managed_start_ts": bundle["desired_managed_start_ts"],
        "latest_safe_shifted_cutoff_recovery_boundary_ts": bundle[
            "latest_safe_shifted_cutoff_recovery_boundary_ts"
        ],
        "rows_to_insert": 1,
        "rows_to_update": 0,
        "rows_to_delete": 0,
        "client_mode_change": None,
        "client_mode_changes": 0,
        "schedule_change": None,
        "schedule_changes": 0,
        "schedule_enabled_changes": 0,
        "history_rows_created": 0,
        "history_mutations": 0,
        "recovery_rows_created": 0,
        "recovery_mutations": 0,
        "non_target_coverage_rows_touched": 0,
        "client_business_writes": 0,
        "provider_requests": 0,
        "subprocesses_launched": 0,
    }


def run(args) -> tuple:
    dry_run = not args.execute
    client_code = _safe_token(args.client_code, label="--client-code")
    dataset_name = _safe_token(args.dataset, label="--dataset")
    seeded_by = _safe_token(args.seeded_by, label="--seeded-by")
    approval_ref = _safe_token(args.approval_ref, label="--approval-ref")
    declared_hash = str(args.evidence_sha256 or "").strip().lower()
    if not SHA256_RE.match(declared_hash):
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            "--evidence-sha256 must be a lowercase 64-character SHA-256",
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
            "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            "--expected-environment is required",
            EXIT_INVALID_PARAMETERS,
        )
    if not args.confirm_schedule_disabled:
        raise _refuse(
            "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
            "--confirm-schedule-disabled is required; the cold-start path "
            "operates only on a schedule the operator has confirmed stays "
            "disabled until a verified recovery",
            EXIT_INVALID_PARAMETERS,
        )

    if not dry_run:
        confirmed = str(args.confirm_client_code or "").strip()
        if not confirmed:
            raise _refuse(
                "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
                "--execute additionally requires --confirm-client-code",
                EXIT_INVALID_PARAMETERS,
            )
        if confirmed != client_code:
            raise _refuse(
                "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
                "--confirm-client-code does not match --client-code",
                EXIT_INVALID_PARAMETERS,
            )

    coverage_start_ts = parse_bound(
        args.coverage_start_ts, label="--coverage-start-ts"
    )
    covered_through_ts = parse_bound(
        args.initial_covered_through_ts, label="--initial-covered-through-ts"
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
                expected_schedule_id=expected_schedule_id,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
                now_utc=db_now,
            )
            managed_start = validate_baseline(
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
                expected_schedule_id=expected_schedule_id,
                expected_environment=expected_environment,
                expected_platform_uuid=expected_uuid,
            )
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

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
            evidence_sha256=evidence_hash,
            approval_ref=approval_ref,
            managed_start_iso=iso_utc(managed_start),
        ),
        "seeded_at": state["db_now"],
        "seeded_by": seeded_by,
        "covered_through_source": INITIAL_COVERED_THROUGH_SOURCE,
        "updated_at": state["db_now"],
    }
    plan = _plan(params, bundle=bundle, state=state, dry_run=dry_run)

    if dry_run:
        plan["would_insert"] = True
        plan["database_writes_performed"] = 0
        return EXIT_OK, plan

    import psycopg
    from psycopg.rows import dict_row

    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        stored = execute_cold_start_bootstrap(
            write_conn,
            params=params,
            client_code=client_code,
            dataset_name=dataset_name,
            expected_schedule_id=expected_schedule_id,
            expected_environment=expected_environment,
            expected_platform_uuid=expected_uuid,
            bundle=bundle,
        )
    except ColdStartBootstrapRefused:
        try:
            write_conn.rollback()
        except Exception:
            pass
        raise
    finally:
        write_conn.close()

    verified = independent_fingerprint(
        dsn,
        client_id=params["client_id"],
        dataset_name=params["dataset_name"],
    )
    plan["database_writes_performed"] = 1
    plan["affected_row_count"] = 1
    plan["transaction_result"] = "COMMITTED"
    plan["baseline_coverage_fingerprint"] = verified["fingerprint"]
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


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, plan = run(args)
    except ColdStartBootstrapRefused as exc:
        print(f"COLD_START_BOOTSTRAP_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(
            f"COLD_START_BOOTSTRAP_FAILED {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(plan, sort_keys=True, indent=2, default=str))
    print(plan["mode"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
