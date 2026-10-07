#!/usr/bin/env python3
"""Retrospectively terminalize ONE abandoned historical `public.runs` row.

WHAT THIS IS FOR.
    A run whose process stopped before it reached its own finalization stays
    `RUNNING` forever. Nothing reaps `public.runs`: the Workflow A dispatcher's
    stale sweep owns `workflow_a_control.client_schedule_run_history`, and
    `ops/execution_watchdog.py` is expectation-driven, so an ad hoc manual run
    is never one of its subjects.

    The only existing writer is `PATCH /runs/{run_id}`, which always stamps
    `ended_at = now()`. That is correct for a live job finalizing itself and
    wrong for an operator cleaning up months later: it would record a
    multi-week execution that never happened.

    This tool is the narrow, auditable alternative. It terminalizes exactly one
    named run, with a status the operator states explicitly and a historical
    `ended_at` the operator supplies, and it records durable provenance so a
    future reader can tell an administrative outcome from an observed one.

WHAT THIS IS NOT.
    * Not a bulk cleaner. There is no "reconcile all stale runs" path, by
      construction: `--run-id` takes exactly one value.
    * Not a replay or retry. It changes the ledger, never the work. A run whose
      business effects must be redone needs a separate, separately authorized
      job invocation.
    * Not a replacement for live finalization. `run_context` and
      `PATCH /runs/{run_id}` keep their existing one-way CAS contract (P1-I)
      unchanged; this tool never relaxes it.
    * Not self-authorizing. Running it with `--execute` against production is a
      production data mutation and requires explicit current authorization
      (AGENTS.md §7).

SAFETY MODEL.
    Dry run is the default. `--execute` is required to write, and even then the
    mutation is fail-closed:

      * the UPDATE's own WHERE clause demands `status = 'RUNNING'` AND
        `ended_at IS NULL`, so a row that reached a terminal state — or that
        another writer moved between preflight and execution — loses the CAS
        and is left exactly as it was;
      * the ledger UPDATE and the provenance INSERT share one transaction, so a
        reconciled run always has evidence and an evidenced run is always
        reconciled;
      * `ops_control.run_reconciliation.run_id` is a primary key, so a second
        reconciliation of the same run is refused by the database;
      * every validation failure aborts before any statement is issued, and a
        failed mutation is never retried automatically.

USAGE.
    Inspect a candidate (read-only, suggests a defensible ended_at):

      PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_historical_run.py inspect \\
          --run-id 612faf1d-917a-4f04-8a01-6f27d5057b90

    Dry run (no write):

      PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_historical_run.py reconcile \\
          --run-id 612faf1d-917a-4f04-8a01-6f27d5057b90 \\
          --status FAILED \\
          --ended-at '2026-07-10T01:21:21.260753+02:00' \\
          --actor 'owner' \\
          --reason 'process interrupted before finalization; superseded by 49b404e4' \\
          --approval-ref 'OPS-2026-08-18-run-ledger'

    Execute (production data mutation — separate authorization required):

      ... same command ... --execute
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The only terminal statuses a retrospective reconciliation may assert.
#:
#: `SUCCESS` is deliberately absent. Reconciliation records that a run did not
#: reach its own terminalization; claiming it succeeded would fabricate a
#: business outcome nobody observed, which is the exact failure mode P1-I
#: exists to prevent. `api.main.ALLOWED_RUN_STATUSES` lists more values, and
#: that is not a licence to write them here — a constant listing a value is not
#: a contract endorsing it.
RECONCILABLE_STATUSES = ("FAILED", "CANCELED")

#: The precondition a row must still satisfy to be reconcilable. Anything else
#: — including a row someone terminalized while the operator was reading the
#: dry run — fails closed.
RECONCILABLE_PRECONDITION = "status = 'RUNNING' AND ended_at IS NULL"

MAX_REASON = 500
MAX_ACTOR = 200
MAX_APPROVAL_REF = 200
MAX_EVIDENCE_REF = 500


class ReconciliationError(RuntimeError):
    """Fail-closed refusal carrying a stable classification for the operator."""

    def __init__(self, classification: str, message: str, details: dict | None = None):
        self.classification = classification
        self.details = details or {}
        super().__init__(message)


# ---------------------------------------------------------------------------
# Pure validation. Every one of these runs before a connection is opened, so a
# malformed invocation can never reach the database at all.
# ---------------------------------------------------------------------------

def parse_run_id(raw: str) -> str:
    """Exactly one syntactically valid UUID. No globs, no lists, no 'all'."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        raise ReconciliationError(
            "INVALID_RUN_ID",
            f"Not a valid run_id UUID: {raw!r}",
        )


def parse_status(raw: str) -> str:
    """The operator states the terminal status; nothing infers it.

    Age does not imply FAILED and silence does not imply CANCELED. The two mean
    different things to whoever reads the ledger next, so the choice stays with
    the person who has the evidence.
    """
    status = (raw or "").strip().upper()
    if status not in RECONCILABLE_STATUSES:
        raise ReconciliationError(
            "UNSUPPORTED_STATUS",
            f"Unsupported reconciliation status {raw!r}; "
            f"choose explicitly from {', '.join(RECONCILABLE_STATUSES)}",
            {"allowed": list(RECONCILABLE_STATUSES)},
        )
    return status


def parse_ended_at(raw: str) -> datetime:
    """A timezone-aware historical timestamp.

    A naive timestamp is refused rather than localized: guessing the zone of a
    months-old execution is precisely the kind of quiet assumption this tool
    exists to avoid.
    """
    text = (raw or "").strip()
    if not text:
        raise ReconciliationError("INVALID_ENDED_AT", "--ended-at is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ReconciliationError(
            "INVALID_ENDED_AT",
            f"--ended-at is not an ISO-8601 timestamp: {raw!r}",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReconciliationError(
            "INVALID_ENDED_AT",
            f"--ended-at must carry an explicit timezone offset: {raw!r}",
        )
    return parsed


def require_text(value: str | None, *, field: str, maximum: int, required: bool = True):
    text = (value or "").strip()
    if not text:
        if required:
            raise ReconciliationError(
                "MISSING_PROVENANCE",
                f"{field} is required: a reconciliation without stated provenance "
                f"is indistinguishable from an unexplained ledger edit",
            )
        return None
    if len(text) > maximum:
        raise ReconciliationError(
            "INVALID_PROVENANCE",
            f"{field} exceeds {maximum} characters",
        )
    return text


def validate_timestamps(
    *, started_at: datetime, ended_at: datetime, now: datetime
) -> None:
    """The two bounds that make a supplied `ended_at` defensible.

    Mirrors `ck_run_reconciliation_time_order` so the operator gets a precise
    refusal instead of a constraint-violation traceback. The database keeps its
    own copy because tooling is not the only thing that could ever write here.
    """
    if ended_at < started_at:
        raise ReconciliationError(
            "ENDED_AT_BEFORE_STARTED_AT",
            f"--ended-at {ended_at.isoformat()} precedes the run's "
            f"started_at {started_at.isoformat()}",
            {"started_at": started_at.isoformat(), "ended_at": ended_at.isoformat()},
        )
    if ended_at > now:
        raise ReconciliationError(
            "ENDED_AT_IN_FUTURE",
            f"--ended-at {ended_at.isoformat()} is in the future "
            f"(now {now.isoformat()})",
            {"now": now.isoformat(), "ended_at": ended_at.isoformat()},
        )


def repository_head() -> str:
    """The commit the reconciliation was performed from, for the audit row."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        raise ReconciliationError(
            "REPOSITORY_HEAD_UNAVAILABLE",
            f"Cannot resolve repository HEAD for the audit record: {exc}",
        )
    if not re.fullmatch(r"[0-9a-f]{40}", out):
        raise ReconciliationError(
            "REPOSITORY_HEAD_UNAVAILABLE",
            f"Repository HEAD is not a 40-character SHA: {out!r}",
        )
    return out


# ---------------------------------------------------------------------------
# Database access. Read paths and the single write path are separate functions
# so the read paths can be reused by `inspect` without any chance of mutating.
# ---------------------------------------------------------------------------

def platform_db_conn():
    """Connection to the platform database, built like `api.main.db_conn`."""
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return psycopg.connect(dsn, row_factory=dict_row)


def load_run(conn, run_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, source, trigger, status, started_at, ended_at "
            "FROM public.runs WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ReconciliationError("RUN_NOT_FOUND", f"No run {run_id} in public.runs")
    return dict(row)


def load_existing_reconciliation(conn, run_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, reconciled_status, historical_ended_at, reconciled_at, "
            "       actor, reason, approval_ref, repository_head, evidence_ref "
            "  FROM ops_control.run_reconciliation WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def load_last_log_at(conn, run_id: str):
    """The last thing the run is known to have done.

    Offered by `inspect` as a *suggestion* only. It is the most defensible
    lower bound the platform holds for when execution stopped, but the operator
    still has to decide and pass it explicitly — a suggestion that could be
    accepted by default would be a default, and defaults are what this tool
    refuses to have.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(ts) AS last_ts FROM public.logs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return (row or {}).get("last_ts")


def assert_reconcilable(run: dict, existing: dict | None) -> None:
    """Everything that must hold before a write is even proposed."""
    if existing is not None:
        raise ReconciliationError(
            "ALREADY_RECONCILED",
            f"Run {run['run_id']} was already reconciled at "
            f"{existing['reconciled_at']} to {existing['reconciled_status']}",
            {"existing": _jsonable(existing)},
        )
    if run["status"] != "RUNNING" or run["ended_at"] is not None:
        raise ReconciliationError(
            "RUN_NOT_RECONCILABLE",
            f"Run {run['run_id']} is not reconcilable: expected "
            f"{RECONCILABLE_PRECONDITION}, found status={run['status']!r} "
            f"ended_at={run['ended_at']}",
            {"status": run["status"], "ended_at": _jsonable(run["ended_at"])},
        )


def execute_reconciliation(conn, *, plan: dict) -> dict:
    """The one and only write path.

    The ledger UPDATE and the provenance INSERT are one transaction. The
    UPDATE's WHERE clause is the compare-and-set: it re-asserts the precondition
    at mutation time, so a row another writer settled between preflight and here
    simply returns no row and the whole transaction is rolled back.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.runs
                   SET status = %s, ended_at = %s
                 WHERE run_id = %s
                   AND {RECONCILABLE_PRECONDITION}
                RETURNING run_id, status, started_at, ended_at
                """,
                (plan["status"], plan["ended_at"], plan["run_id"]),
            )
            updated = cur.fetchone()
            if updated is None:
                raise ReconciliationError(
                    "CAS_LOST",
                    f"Run {plan['run_id']} no longer satisfies "
                    f"{RECONCILABLE_PRECONDITION}; nothing was written",
                )

            cur.execute(
                """
                INSERT INTO ops_control.run_reconciliation
                    (run_id, run_source, run_started_at, reconciled_status,
                     historical_ended_at, actor, reason, approval_ref,
                     repository_head, evidence_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING run_id, reconciled_status, historical_ended_at,
                          reconciled_at, actor, reason, approval_ref,
                          repository_head, evidence_ref
                """,
                (
                    plan["run_id"], plan["run_source"], plan["run_started_at"],
                    plan["status"], plan["ended_at"], plan["actor"], plan["reason"],
                    plan["approval_ref"], plan["repository_head"], plan["evidence_ref"],
                ),
            )
            evidence = cur.fetchone()

    return {"run": _jsonable(dict(updated)), "reconciliation": _jsonable(dict(evidence))}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _emit(payload: dict) -> None:
    print(json.dumps(_jsonable(payload), indent=2, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_inspect(args) -> int:
    run_id = parse_run_id(args.run_id)
    with platform_db_conn() as conn:
        run = load_run(conn, run_id)
        existing = load_existing_reconciliation(conn, run_id)
        last_log_at = load_last_log_at(conn, run_id)

    reconcilable = existing is None and run["status"] == "RUNNING" and run["ended_at"] is None
    _emit({
        "action": "inspect",
        "run": _jsonable(run),
        "already_reconciled": existing is not None,
        "existing_reconciliation": _jsonable(existing) if existing else None,
        "reconcilable": reconcilable,
        "precondition": RECONCILABLE_PRECONDITION,
        "last_log_at": _jsonable(last_log_at),
        "suggested_ended_at": _jsonable(last_log_at),
        "suggested_ended_at_note": (
            "The last log this run emitted. The most defensible lower bound the "
            "platform holds for when execution stopped. It is a suggestion, not a "
            "default: pass it explicitly via --ended-at if you accept it."
        ),
        "allowed_statuses": list(RECONCILABLE_STATUSES),
    })
    return 0


def cmd_reconcile(args) -> int:
    # Pure validation first: nothing below opens a connection.
    run_id = parse_run_id(args.run_id)
    status = parse_status(args.status)
    ended_at = parse_ended_at(args.ended_at)
    actor = require_text(args.actor, field="--actor", maximum=MAX_ACTOR)
    reason = require_text(args.reason, field="--reason", maximum=MAX_REASON)
    approval_ref = require_text(
        args.approval_ref, field="--approval-ref", maximum=MAX_APPROVAL_REF
    )
    evidence_ref = require_text(
        args.evidence_ref, field="--evidence-ref",
        maximum=MAX_EVIDENCE_REF, required=False,
    )
    head = repository_head()
    now = datetime.now(timezone.utc)

    with platform_db_conn() as conn:
        run = load_run(conn, run_id)
        existing = load_existing_reconciliation(conn, run_id)
        assert_reconcilable(run, existing)
        validate_timestamps(started_at=run["started_at"], ended_at=ended_at, now=now)

        plan = {
            "run_id": run_id,
            "run_source": run["source"],
            "run_started_at": run["started_at"],
            "status": status,
            "ended_at": ended_at,
            "actor": actor,
            "reason": reason,
            "approval_ref": approval_ref,
            "repository_head": head,
            "evidence_ref": evidence_ref,
        }

        proposal = {
            "action": "reconcile",
            "executed": False,
            "run_id": run_id,
            "current": {
                "status": run["status"],
                "started_at": _jsonable(run["started_at"]),
                "ended_at": _jsonable(run["ended_at"]),
                "source": run["source"],
                "trigger": run["trigger"],
            },
            "proposed": {
                "status": status,
                "ended_at": _jsonable(ended_at),
            },
            "provenance": {
                "actor": actor,
                "reason": reason,
                "approval_ref": approval_ref,
                "repository_head": head,
                "evidence_ref": evidence_ref,
            },
            "preconditions_pass": True,
            "precondition": RECONCILABLE_PRECONDITION,
            "mutation": (
                f"UPDATE public.runs SET status='{status}', "
                f"ended_at='{ended_at.isoformat()}' "
                f"WHERE run_id='{run_id}' AND {RECONCILABLE_PRECONDITION}"
                " ;  INSERT INTO ops_control.run_reconciliation (...) VALUES (...)"
            ),
            "ended_at_is_historical_not_now": ended_at != now,
        }

        if not args.execute:
            proposal["note"] = (
                "DRY RUN — nothing was written. Re-run with --execute to apply. "
                "Executing against production is a production data mutation and "
                "requires explicit current authorization."
            )
            _emit(proposal)
            return 0

        result = execute_reconciliation(conn, plan=plan)

    proposal["executed"] = True
    proposal["result"] = result
    _emit(proposal)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser(
        "inspect", help="read-only view of one candidate run and its provenance state"
    )
    inspect.add_argument("--run-id", required=True, help="exactly one run_id UUID")
    inspect.set_defaults(func=cmd_inspect)

    reconcile = sub.add_parser(
        "reconcile",
        help="terminalize exactly one abandoned run (dry run unless --execute)",
    )
    reconcile.add_argument("--run-id", required=True, help="exactly one run_id UUID")
    reconcile.add_argument(
        "--status", required=True,
        help=f"explicit terminal status, one of {', '.join(RECONCILABLE_STATUSES)}",
    )
    reconcile.add_argument(
        "--ended-at", required=True,
        help="historical ISO-8601 end of execution WITH timezone offset; never now()",
    )
    reconcile.add_argument("--actor", required=True, help="who is performing this")
    reconcile.add_argument("--reason", required=True, help="why this disposition")
    reconcile.add_argument(
        "--approval-ref", required=True, help="the authorization this acts under"
    )
    reconcile.add_argument(
        "--evidence-ref", default=None,
        help="optional pointer at the investigation that justified the disposition",
    )
    reconcile.add_argument(
        "--execute", action="store_true",
        help="actually write; without it the command is a dry run",
    )
    reconcile.set_defaults(func=cmd_reconcile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconciliationError as exc:
        print(
            json.dumps(
                {
                    "classification": exc.classification,
                    "error": str(exc),
                    "details": _jsonable(exc.details),
                    "executed": False,
                },
                indent=2, sort_keys=True, default=str,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
