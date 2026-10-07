#!/usr/bin/env python3
"""Dry-run-first enrichment of EXPAND-era first-seen timestamps (M-LAG Phase C).

Specification of record:
  docs/21_telematics_delivery_lag_trace.md §4 (provenance states), §11 (the
    expand-contract rollout), §12 (this tool's exact contract)
  db/client_business/048_client_trips_first_seen_response_received_at.sql
  db/migrations/061_workflow_a_provider_request_log.sql (the only permitted
    source of the instant)

WHAT PROBLEM THIS SOLVES.
    Migration 047 shipped before this milestone, so the deployed M4-era writer has
    been populating `client_trips.first_seen_request_id` without the matching
    instant. After the EXPAND migration those rows are in the
    PROVENANCE_TIMESTAMP_PENDING state: the observation genuinely happened and is
    recorded in the platform database, but the instant is not on the trip row
    where the metric and the CONTRACT need it.

    This tool copies it — and ONLY it, and only from one place.

THE ONLY PERMITTED SOURCE.
    `workflow_a_control.provider_request_log.response_received_at_utc` of the row
    whose `request_id` EQUALS the trip's `first_seen_request_id`. Exact identity
    match, nothing else. There is deliberately no fallback to:

      * `synced_at` or `sync_run_id` — last-touched, the defect M4 removed;
      * the trip's own timestamps;
      * another request from the same run, sub-window or page;
      * the nearest request in time;
      * any job, projection or finalization time.

    A candidate whose request row cannot be resolved is REPORTED UNRESOLVED and
    left untouched. Imputing a value would fabricate the evidence the whole
    milestone exists to make trustworthy, and it would do so permanently.

NOT A DISTRIBUTED TRANSACTION, AND IT DOES NOT PRETEND TO BE.
    The trips are in a per-client business database and the evidence is in the
    platform database `logdb`. There is no XA, no two-phase commit, and none is
    introduced. What makes that safe here is the shape of the operation rather
    than a transaction:

      * the platform side is READ-ONLY — a `REPEATABLE READ READ ONLY`
        transaction, so nothing on that side can be left half-done;
      * the client side writes are individually conditional and idempotent;
      * a partial run leaves a strictly smaller candidate set and re-running
        continues from wherever it stopped.

    So the operation is *resumable*, which is the property that actually matters,
    and it is not described as atomic across the two databases.

RACE SAFETY.
    Ingestion keeps running while this executes, and the new M-LAG writer may
    create a complete pair for a row this tool selected moments earlier. Every
    UPDATE therefore carries a compare-and-swap predicate::

        WHERE client_id = %s
          AND provider_trip_id = %s
          AND first_seen_request_id = %s      -- unchanged since selection
          AND first_seen_response_received_at_utc IS NULL   -- still pending

    A row that changed under us matches zero rows and is counted as `raced`, not
    overwritten and not treated as an error. Because `first_seen_request_id` is
    immutable by contract, the third predicate can only fail if the row was
    replaced entirely, which is itself worth reporting.

IDEMPOTENCY.
    A second run finds no candidates and writes nothing. A row that already has
    an instant is never rewritten — it is not even selected.

EVIDENCE RETENTION DEADLINE.
    `api/platform_prune.py` deletes `provider_request_log` rows past a fixed
    180-day horizon. A pending row whose request has been pruned is
    PERMANENTLY unenrichable, and this tool will report it as
    `unresolved_evidence_expired` rather than inventing anything. Historical
    enrichment must therefore run WELL INSIDE that window; the report prints the
    oldest candidate's age so the margin is visible.

Typical use — dry-run first, always::

    PYTHONPATH="$PWD" python3 ops/enrich_telematics_first_seen_timestamps.py \\
        --client-code ALPHA00001 \\
        --expected-environment production \\
        --expected-platform-uuid <uuid>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.timezone_utils import set_pg_session_timezone  # noqa: E402
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402
from ops.audit_telematics_coverage_bootstrap import (  # noqa: E402
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
#: A write ran but some candidates could not be resolved. The writes that did
#: happen are correct and committed; the run is simply not a closure.
EXIT_UNRESOLVED_REMAIN = 8

CLIENT_MIGRATION = "048_client_trips_first_seen_response_received_at.sql"
CLIENT_MIGRATION_COLUMN = "first_seen_response_received_at_utc"
PLATFORM_MIGRATION = "061_workflow_a_provider_request_log.sql"

#: Matches `api/platform_prune.py`'s fixed horizon for `provider_request_log`.
#: Restated rather than imported so this tool has no dependency on the prune
#: module; the M-LAG suite asserts the two agree.
PROVIDER_REQUEST_LOG_RETENTION_DAYS = 180

#: Bounded operator-facing sample of unresolved rows. Identities only —
#: `provider_trip_id` and the request UUID — never payload, never credentials.
UNRESOLVED_SAMPLE_LIMIT = 25

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")


class EnrichmentRefused(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> EnrichmentRefused:
    return EnrichmentRefused(code, message, exit_code)


def _safe_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_TOKEN_RE.match(text):
        raise _refuse(
            "INVALID_PARAMETER",
            f"{label} must match {SAFE_TOKEN_RE.pattern}",
            EXIT_INVALID_PARAMETERS,
        )
    return text


def _require_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise _refuse(
            "DEPENDENCY_MISSING", "psycopg is required", EXIT_RUNTIME_FAILURE
        ) from exc
    return psycopg, dict_row


# ---------------------------------------------------------------------------
# The candidate set — the one definition, used by this tool and the readiness gate
# ---------------------------------------------------------------------------

#: PROVENANCE_TIMESTAMP_PENDING: identity present, instant absent. Rows that
#: already have an instant are not selected, which is what makes a re-run a no-op
#: rather than a rewrite.
CANDIDATE_SQL = """
    SELECT provider_trip_id,
           first_seen_request_id::text AS first_seen_request_id
      FROM public.client_trips
     WHERE first_seen_request_id IS NOT NULL
       AND first_seen_response_received_at_utc IS NULL
     ORDER BY provider_trip_id
"""

#: The compare-and-swap write. See RACE SAFETY in the module docstring.
ENRICH_SQL = """
    UPDATE public.client_trips
       SET first_seen_response_received_at_utc = %(received_at)s
     WHERE client_id = %(client_id)s
       AND provider_trip_id = %(provider_trip_id)s
       AND first_seen_request_id = %(request_id)s
       AND first_seen_response_received_at_utc IS NULL
"""

#: Rows whose instant exists without an identity. Structurally impossible from
#: the EXPAND migration onward, so a non-zero count means either a pre-EXPAND
#: write or a constraint that was dropped. Reported, never repaired here.
ORPHAN_INSTANT_SQL = """
    SELECT count(*) AS n
      FROM public.client_trips
     WHERE first_seen_response_received_at_utc IS NOT NULL
       AND first_seen_request_id IS NULL
"""


def resolve_instants(cur, *, request_ids: Sequence[str]) -> Dict[str, datetime]:
    """request_id -> response_received_at_utc, by EXACT identity.

    Read-only. A request_id absent from the result is unresolved and stays that
    way; there is no second query with a looser predicate.
    """
    if not request_ids:
        return {}
    cur.execute(
        """
        SELECT request_id::text AS request_id, response_received_at_utc
          FROM workflow_a_control.provider_request_log
         WHERE request_id = ANY(%s)
           AND response_received_at_utc IS NOT NULL
        """,
        (list(request_ids),),
    )
    return {
        str(r["request_id"]): r["response_received_at_utc"]
        for r in cur.fetchall()
    }


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _client_conn(client: Dict[str, Any], *, read_only: bool):
    psycopg, _ = _require_psycopg()
    dsn = (
        f"host={client['client_db_host']} "
        f"port={client['client_db_port']} "
        f"dbname={client['client_db_name']} "
        f"user={client['client_db_user']} "
        f"password={resolve_secret(client['client_db_password_secret_ref'])}"
    )
    conn = set_pg_session_timezone(psycopg.connect(dsn))
    if read_only:
        # The server itself refuses a write on a dry run, so "dry run" is not a
        # property of this code being careful.
        conn.execute("SET TRANSACTION READ ONLY")
    return conn


def resolve_clients(cur, *, client_code: Optional[str]) -> List[Dict[str, Any]]:
    sql = """
        SELECT client_id::text AS client_id, client_code,
               client_db_host, client_db_port, client_db_name,
               client_db_user, client_db_password_secret_ref
          FROM workflow_a_control.client_account
         WHERE enabled = true
    """
    params: List[Any] = []
    if client_code:
        sql += " AND client_code = %s"
        params.append(client_code)
    sql += " ORDER BY client_code"
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    if client_code and not rows:
        raise _refuse("CLIENT_NOT_FOUND", f"no enabled client {client_code!r}")
    return rows


def verify_platform_prerequisite(cur) -> None:
    cur.execute("SELECT filename FROM public.schema_migrations ORDER BY filename")
    applied = {str(r["filename"]) for r in cur.fetchall()}
    if PLATFORM_MIGRATION not in applied:
        raise _refuse(
            "PLATFORM_EVIDENCE_ABSENT",
            f"{PLATFORM_MIGRATION} is not applied; there is no evidence to "
            "enrich from",
        )


def assert_client_expanded(cur) -> None:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'client_trips'
           AND column_name = %s
        """,
        (CLIENT_MIGRATION_COLUMN,),
    )
    if cur.fetchone() is None:
        raise _refuse(
            "CLIENT_NOT_EXPANDED",
            f"public.client_trips.{CLIENT_MIGRATION_COLUMN} is absent; apply "
            f"{CLIENT_MIGRATION} (EXPAND) to this client first",
        )


# ---------------------------------------------------------------------------
# Per-client work
# ---------------------------------------------------------------------------

def enrich_client(
    pcur, client: Dict[str, Any], *, dry_run: bool, now_utc: datetime,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "client_code": client["client_code"],
        "client_id": client["client_id"],
        "candidates": 0,
        "resolved": 0,
        "enriched": 0,
        "raced": 0,
        "unresolved": 0,
        "unresolved_sample": [],
        "orphan_instants": 0,
        "oldest_candidate_request_age_days": None,
        "evidence_deadline_utc": None,
    }

    with _client_conn(client, read_only=True) as rconn:
        with rconn.cursor(row_factory=_require_psycopg()[1]) as rcur:
            assert_client_expanded(rcur)
            rcur.execute(CANDIDATE_SQL)
            candidates = [dict(r) for r in rcur.fetchall()]
            rcur.execute(ORPHAN_INSTANT_SQL)
            summary["orphan_instants"] = int(rcur.fetchone()["n"])

    summary["candidates"] = len(candidates)
    if not candidates:
        return summary

    request_ids = sorted({c["first_seen_request_id"] for c in candidates})
    instants = resolve_instants(pcur, request_ids=request_ids)
    summary["resolved"] = len(instants)

    unresolved = [c for c in candidates if c["first_seen_request_id"] not in instants]
    summary["unresolved"] = len(unresolved)
    summary["unresolved_sample"] = [
        {
            "provider_trip_id": int(c["provider_trip_id"]),
            "first_seen_request_id": c["first_seen_request_id"],
        }
        for c in unresolved[:UNRESOLVED_SAMPLE_LIMIT]
    ]
    if len(unresolved) > UNRESOLVED_SAMPLE_LIMIT:
        summary["unresolved_sample_truncated_at"] = UNRESOLVED_SAMPLE_LIMIT

    if instants:
        oldest = min(instants.values())
        age = now_utc - oldest.astimezone(timezone.utc)
        summary["oldest_candidate_request_age_days"] = round(
            age.total_seconds() / 86400.0, 2
        )
        summary["evidence_deadline_utc"] = (
            oldest.astimezone(timezone.utc)
            + timedelta(days=PROVIDER_REQUEST_LOG_RETENTION_DAYS)
        ).isoformat()

    if dry_run:
        return summary

    resolvable = [
        c for c in candidates if c["first_seen_request_id"] in instants
    ]
    with _client_conn(client, read_only=False) as wconn:
        with wconn.cursor() as wcur:
            enriched = 0
            raced = 0
            for c in resolvable:
                wcur.execute(
                    ENRICH_SQL,
                    {
                        "received_at": instants[c["first_seen_request_id"]],
                        "client_id": client["client_id"],
                        "provider_trip_id": c["provider_trip_id"],
                        "request_id": c["first_seen_request_id"],
                    },
                )
                if wcur.rowcount == 1:
                    enriched += 1
                elif wcur.rowcount == 0:
                    # The row changed between selection and write: the new writer
                    # filled the pair, or the row was replaced. Not an error and
                    # not overwritten.
                    raced += 1
                else:  # pragma: no cover - the PK makes this impossible
                    wconn.rollback()
                    raise _refuse(
                        "ENRICHMENT_AFFECTED_MULTIPLE_ROWS",
                        f"one CAS update affected {wcur.rowcount} rows for "
                        f"provider_trip_id={c['provider_trip_id']}; the trip "
                        "primary key is not what we think it is",
                        EXIT_RUNTIME_FAILURE,
                    )
        wconn.commit()
    summary["enriched"] = enriched
    summary["raced"] = raced
    return summary


def run(args) -> Tuple[int, Dict[str, Any]]:
    client_code = (
        _safe_token(args.client_code, label="--client-code")
        if args.client_code else None
    )
    dry_run = not bool(args.execute)
    if not dry_run:
        if not args.approval_ref:
            raise _refuse(
                "APPROVAL_REF_REQUIRED",
                "--approval-ref is required for a write",
                EXIT_INVALID_PARAMETERS,
            )
        _safe_token(args.approval_ref, label="--approval-ref")
        if str(args.confirm or "") != "ENRICH":
            raise _refuse(
                "CONFIRMATION_MISMATCH",
                "--confirm ENRICH is required for a write",
                EXIT_INVALID_PARAMETERS,
            )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    psycopg, dict_row = _require_psycopg()
    now_utc = datetime.now(timezone.utc)

    report: Dict[str, Any] = {
        "mode": "DRY_RUN" if dry_run else "EXECUTE",
        "approval_ref": args.approval_ref,
        "provider_request_log_retention_days": PROVIDER_REQUEST_LOG_RETENTION_DAYS,
        "clients": [],
    }

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as pconn:
        # The platform side is read-only for the whole run, enforced by the
        # server. REPEATABLE READ so every client resolves against one snapshot
        # of the evidence.
        pconn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with pconn.cursor() as pcur:
            report["platform_identity"] = verify_platform_identity(
                pcur,
                expected_environment=str(args.expected_environment),
                expected_platform_uuid=expected_uuid,
            )
            verify_platform_prerequisite(pcur)
            for client in resolve_clients(pcur, client_code=client_code):
                report["clients"].append(
                    enrich_client(
                        pcur, client, dry_run=dry_run, now_utc=now_utc,
                    )
                )
        pconn.rollback()

    totals = {
        key: sum(c[key] for c in report["clients"])
        for key in ("candidates", "resolved", "enriched", "raced", "unresolved",
                    "orphan_instants")
    }
    report["totals"] = totals
    report["closure_ready"] = (
        totals["unresolved"] == 0
        and totals["orphan_instants"] == 0
        and (dry_run or totals["candidates"] == totals["enriched"] + totals["raced"])
    )

    if totals["unresolved"] or totals["orphan_instants"]:
        # Fail closed on the CLASSIFICATION, not on the writes. Whatever was
        # enriched is correct and committed; the run is simply not a closure and
        # must not be reported as one.
        return EXIT_UNRESOLVED_REMAIN, report
    return EXIT_OK, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first enrichment of client_trips."
            "first_seen_response_received_at_utc from the EXACT matching "
            "workflow_a_control.provider_request_log row. Never imputes."
        ),
    )
    parser.add_argument("--client-code", help="Restrict to one client.")
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument("--approval-ref")
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform the enrichment. Without it the tool reports DRY_RUN only.",
    )
    parser.add_argument(
        "--confirm", help="Must be the literal ENRICH when --execute is used.",
    )
    parser.add_argument("--dsn")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, report = run(args)
    except EnrichmentRefused as exc:
        print(f"ENRICHMENT_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(
            f"ENRICHMENT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(report, sort_keys=True, indent=2, default=str))
    print(report["mode"])
    if exit_code == EXIT_UNRESOLVED_REMAIN:
        print(
            "UNRESOLVED_REMAIN — enrichment is not closed; see "
            "unresolved_sample and orphan_instants",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
