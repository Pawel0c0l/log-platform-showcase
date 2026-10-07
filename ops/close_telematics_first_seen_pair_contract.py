#!/usr/bin/env python3
"""Dry-run-first CONTRACT closure for the first-seen pair (M-LAG Phase D).

Specification of record:
  docs/21_telematics_delivery_lag_trace.md §11 (the expand-contract rollout and its
    compatibility matrix), §13 (this tool's readiness gate)
  db/client_business/048_client_trips_first_seen_response_received_at.sql (EXPAND)
  ops/enrich_telematics_first_seen_timestamps.py (Phase C, which must run first)

WHAT THIS DOES.
    Replaces the EXPAND-era one-directional constraint

        ck_client_trips_first_seen_instant_needs_request
            (instant IS NULL OR request_id IS NOT NULL)

    with the strict bidirectional pairing invariant

        ck_client_trips_first_seen_pairing
            (request_id IS NULL) = (instant IS NULL)

    and validates it, so from then on the database itself guarantees that a
    first-seen event is either wholly present or wholly absent.

WHY THIS IS A TOOL AND NOT A MIGRATION FILE. Three independent reasons, each
sufficient on its own:

  1. `scripts/apply_client_business_migrations.py` applies EVERY pending file in
     `db/client_business/` automatically. A `049_..._contract.sql` would therefore
     be applied by the ordinary migration run — very possibly before the M-LAG
     release is active and long before historical enrichment has happened. That
     is precisely the premature enforcement this phase must not allow.
  2. A *gated* migration file that refused would be worse, not better: that
     applier returns on the first failure, so a permanently-refusing file would
     block every later client migration behind it.
  3. The correct lock behaviour is impossible in that runner. It executes a whole
     file inside ONE transaction, so a `VALIDATE CONSTRAINT` scan would run while
     holding the ACCESS EXCLUSIVE lock taken by the first DDL statement. This
     tool owns its transactions and can therefore add the constraint `NOT VALID`
     in one short transaction and `VALIDATE` it in a second, where the scan takes
     only SHARE UPDATE EXCLUSIVE and does not block readers or writers.

WHAT CLOSING THE CONTRACT COSTS, STATED PLAINLY.
    It intentionally ENDS ROLLBACK COMPATIBILITY WITH THE M4-ERA WRITER. That
    writer sets `first_seen_request_id` and cannot set the instant, so under the
    strict constraint every one of its provenance-bearing inserts would be
    rejected. Do not run this while a rollback to the pre-M-LAG release is still
    a live option. There is no way to have both.

THE CONTRACT STATE — classified before any gate is applied.
    OPEN                          the exact EXPAND constraint, strict absent.
    STRICT_PRESENT_NOT_VALIDATED  the exact strict constraint, `convalidated`
                                  false. An INTERRUPTED closure, NOT a closed
                                  one — see RESUMABILITY below.
    CLOSED                        the exact strict constraint, validated.
    INVALID                       anything else: a wrong definition under either
                                  name, neither constraint present, or BOTH
                                  present. Refused; this tool never guesses
                                  which DDL would repair a shape it did not
                                  create.

    Both definitions are compared against canonical `pg_get_constraintdef`
    output, and validation status is read from `pg_constraint.convalidated`
    rather than inferred from the printed text.

THE READINESS GATE — every condition is checked, and any failure refuses.
    G1  the EXPAND column exists;
    G2  zero PROVENANCE_TIMESTAMP_PENDING rows (identity without instant);
    G3  zero orphan instants (instant without identity);
    G4  the EXPAND constraint is present WITH ITS EXACT DEFINITION, i.e. this
        database really went through the expand phase rather than arriving in
        some other shape or carrying a same-named constraint that means
        something else;
    G5  the strict constraint is not already installed;
    G6  the MATERIALIZED one-step rollback envelope is PROVEN, from the
        AUTHORITATIVE release inventory itself. `current` and `previous` must
        both resolve canonically INSIDE that inventory's `releases/` directory
        — no `..` escape, no external target, no dangling id — both must pass
        `ops.release_boundary.verify_release`, which recomputes each release
        from its own bytes against its manifest, commit and source-tree digest,
        they must be DISTINCT identities, and BOTH must declare the first-seen
        pair capability and the EXACT canonical EXPAND and validated CONTRACT
        constraint definitions as two distinct alternatives. Closing the
        contract makes every release without that capability unactivatable, so
        an unproven envelope means the rollback target dies with the closure.

        G6 is a DECLARATION proof, not a live-schema one: it asks whether those
        two releases could run on either side of the transition. Whether the
        live database is ready to be transitioned is G1–G5 and the state
        machine.

        The result that authorizes `--execute` is evaluated WHILE THIS PROCESS
        HOLDS `SCHEMA_TRANSITION_LOCK_KEY` — the same key every supported
        pointer mutation takes across its swap — and it stays authoritative for
        as long as the lock is held, which is through the last ledger insert.
        An earlier, unprotected snapshot may refuse early but never authorizes:
        an activation or rollback can move both pointers a moment after it.

        Under `--expected-environment production` the inventory is the
        configured production release root and nothing else; `--release-root`
        cannot substitute a directory an operator built.

        Independent review established that the envelope cannot exist yet: the
        first bridge activation leaves `previous` legacy, and the envelope is
        reached only once a SECOND distinct bridge-compatible release has been
        activated. The tool therefore gates and waits, and

    G7  the operator states, with `--rollback-window-closed`, that M4-writer
        rollback is intentionally no longer required. This one cannot be derived
        from the database and is therefore demanded explicitly rather than
        assumed from a clean G2/G3 — a brand-new client with no trips would
        otherwise pass every automatic check.

    G1–G5 are per-client and apply to a fresh OPEN -> CLOSED closure. A resume
    needs G1–G3 only: the EXPAND constraint is legitimately gone by then, so
    requiring G4/G5 would make the unfinished state unfinishable. G6 and G7 are
    fleet-wide preconditions of `--execute`, checked once, INSIDE the transition
    coordination and BEFORE any client DDL — including before a resume, because
    a resume also leaves the fleet in a state no legacy release can run
    against.

WHY G6 REPLACED AN OPERATOR ASSERTION.
    `--rollback-window-closed` alone asserted the envelope; it could not
    establish it. Independent review checked the real pointers — `current =
    18dcda87f16d`, `previous = 20a01b4358b8` — and found neither carries the
    pair-contract capability, so the closure would have been permitted while
    destroying the one-step rollback it claimed to have accounted for. G6 now
    reads the answer out of the release layout. G7 survives as what it always
    genuinely was: an irreversibility acknowledgement, not evidence.

SERIALISATION AGAINST RELEASE ACTIVATION.
    The strict constraint narrows which releases may run at all, and that
    narrowing is invisible to the platform database, so an activation that
    checked the fleet a moment earlier could still swap a legacy release into
    `current` after this tool committed. Both tools therefore take
    `SCHEMA_TRANSITION_LOCK_KEY` in the platform database; this one holds it
    across the whole client mutation sequence. Whichever acquires it first, the
    other observes its committed outcome instead of a stale one.

    THE EVIDENCE LIVES INSIDE THE SAME REGION. G6 is evaluated after the lock is
    acquired, so the pointers it reads are the committed ones and cannot move
    again until the closure is finished with them. Order, in full:

      resolve the authoritative inventory -> observational G6 (may refuse, never
      authorizes) -> read-only platform identity and fleet enumeration ->
      acquire SCHEMA_TRANSITION_LOCK_KEY -> PROTECTED G6 -> refuse or proceed ->
      client DDL, validation and ledger for every client -> release the lock.

    No client database lock is taken while the platform coordination lock is
    being acquired, and the activation side takes the same key first as well, so
    the two acquisition orders are identical and cannot deadlock.

RESUMABILITY — the defect this closes.
    The closure spans three transactions (swap / validate / ledger) because that
    is what makes it lock-safe, so it can be interrupted between them. The
    earlier gate treated STRICT-CONSTRAINT-PRESENT as sufficient evidence of
    completion and reported ALREADY_CLOSED, which meant an interruption between
    the swap and the validate left the constraint permanently unvalidated and
    the ledger permanently unwritten, with every rerun agreeing there was
    nothing to do.

    Now only CLOSED *with the ledger entry* is ALREADY_CLOSED. An unfinished
    closure is reported as RESUME_REQUIRED (or LEDGER_REPAIR_REQUIRED) with exit
    9, and `--execute` finishes it through the SAME code path a fresh closure
    uses. Both remaining steps are idempotent — `VALIDATE CONSTRAINT` on a
    validated constraint is a no-op, the ledger insert is `ON CONFLICT DO
    NOTHING` — so a rerun converges, never duplicates a constraint, and never
    drops a correct validated one.

`--check-only` runs the gate and reports, changing nothing. That is also the
pre-contract readiness report referenced by docs/21 §13.

Typical use::

    # readiness report, changes nothing
    PYTHONPATH="$PWD" python3 ops/close_telematics_first_seen_pair_contract.py \\
        --client-code ALPHA00001 --check-only \\
        --expected-environment production --expected-platform-uuid <uuid>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from ops.manage_release import DEFAULT_RELEASE_ROOT  # noqa: E402
from ops.release_schema_preflight import (  # noqa: E402
    FIRST_SEEN_EXPAND_DEFINITION,
    FIRST_SEEN_STRICT_DEFINITION,
    SchemaPreflightError,
    rollback_envelope_status,
    schema_transition_lock,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_REFUSED = 4
EXIT_RUNTIME_FAILURE = 5
#: The gate ran and the client is NOT ready. Distinct from a parameter refusal so
#: an operator can tell "you asked wrongly" from "the fleet is not there yet".
EXIT_NOT_READY = 9

EXPAND_CONSTRAINT = "ck_client_trips_first_seen_instant_needs_request"
STRICT_CONSTRAINT = "ck_client_trips_first_seen_pairing"
COLUMN = "first_seen_response_received_at_utc"

#: Canonical `pg_get_constraintdef` bodies, with any trailing `NOT VALID` marker
#: removed — validation status is read from `pg_constraint.convalidated`, never
#: inferred from the printed definition. PostgreSQL reprints these from the
#: parsed catalog entry rather than from the migration's source text, so an
#: exact comparison is robust and a same-named constraint carrying a different
#: expression cannot pass as either state.
#:
#: DEFINED ONCE, in `ops/release_schema_preflight.py`, and imported here. G6
#: verifies that a release DECLARES exactly these; this tool verifies that a
#: client database CARRIES exactly these. Two independently maintained copies
#: of the bridge contract could disagree about what the transition is, which is
#: precisely the drift both checks exist to catch.
EXPAND_DEFINITION = FIRST_SEEN_EXPAND_DEFINITION
STRICT_DEFINITION = FIRST_SEEN_STRICT_DEFINITION

#: The four states this tool distinguishes. The distinction that matters is
#: STRICT_PRESENT_NOT_VALIDATED, which the first implementation folded into
#: "already closed": an interruption between the `ADD CONSTRAINT ... NOT VALID`
#: transaction and the `VALIDATE` transaction left the strict constraint
#: enforcing new writes but never proven against existing rows, and a rerun
#: reported ALREADY_CLOSED without finishing the job.
STATE_OPEN = "OPEN"
STATE_STRICT_NOT_VALIDATED = "STRICT_PRESENT_NOT_VALIDATED"
STATE_CLOSED = "CLOSED"
STATE_INVALID = "INVALID"

#: Synthetic ledger entry, so the closure is visible to
#: `scripts/apply_client_business_migrations.py`, to the release preflight and to
#: any operator reading `schema_migrations`. It is NOT a file in
#: `db/client_business/`, and must never become one — see the module docstring.
LEDGER_ENTRY = "049_client_trips_first_seen_pair_contract.tool"

SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,200}$")


class ContractRefused(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str, exit_code: int = EXIT_REFUSED) -> ContractRefused:
    return ContractRefused(code, message, exit_code)


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
# The readiness gate
# ---------------------------------------------------------------------------

READINESS_SQL = """
    SELECT
      (SELECT count(*) FROM information_schema.columns
        WHERE table_schema='public' AND table_name='client_trips'
          AND column_name=%(column)s)                      AS column_present,
      (SELECT count(*) FROM public.client_trips
        WHERE first_seen_request_id IS NOT NULL
          AND first_seen_response_received_at_utc IS NULL)  AS pending_rows,
      (SELECT count(*) FROM public.client_trips
        WHERE first_seen_response_received_at_utc IS NOT NULL
          AND first_seen_request_id IS NULL)                AS orphan_instants,
      (SELECT count(*) FROM public.client_trips
        WHERE first_seen_request_id IS NOT NULL
          AND first_seen_response_received_at_utc IS NOT NULL) AS complete_pairs,
      (SELECT count(*) FROM public.client_trips)            AS total_rows,
      (SELECT count(*) FROM pg_constraint
        WHERE conname=%(expand)s
          AND conrelid='public.client_trips'::regclass)     AS expand_constraint,
      (SELECT count(*) FROM pg_constraint
        WHERE conname=%(strict)s
          AND conrelid='public.client_trips'::regclass)     AS strict_constraint,
      (SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname=%(expand)s
          AND conrelid='public.client_trips'::regclass)     AS expand_definition,
      (SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname=%(strict)s
          AND conrelid='public.client_trips'::regclass)     AS strict_definition,
      (SELECT convalidated FROM pg_constraint
        WHERE conname=%(strict)s
          AND conrelid='public.client_trips'::regclass)     AS strict_validated
"""

#: Asked separately, and only once the relation is known to exist: a client
#: database that has never run a migration has no ledger table at all, and
#: naming it in the main statement would make the readiness report fail to parse
#: rather than report "not recorded".
LEDGER_SQL = "SELECT count(*) AS n FROM public.schema_migrations WHERE filename=%s"


def ledger_recorded(cur, entry: str = LEDGER_ENTRY) -> bool:
    """Is the synthetic closure entry recorded? An absent ledger means 'no'."""
    cur.execute("SELECT to_regclass('public.schema_migrations') AS relation")
    row = cur.fetchone()
    relation = row["relation"] if isinstance(row, dict) else row[0]
    if relation is None:
        return False
    cur.execute(LEDGER_SQL, (entry,))
    row = cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0]) == 1


def _definition_body(definition: Optional[str]) -> Optional[str]:
    """Canonical constraint text with the `NOT VALID` marker and whitespace removed.

    Validation status is a catalog boolean and is read as one; folding it into
    the definition string is what would let "present but unvalidated" masquerade
    as a different constraint rather than as a different STATE of the same one.
    """
    if definition is None:
        return None
    text = " ".join(str(definition).split())
    if text.upper().endswith(" NOT VALID"):
        text = text[: -len(" NOT VALID")].rstrip()
    return text


def contract_state(row: Dict[str, Any]) -> str:
    """Classify the pairing contract. Anything not proven safe is INVALID.

    OPEN                        the exact EXPAND constraint, strict absent.
    STRICT_PRESENT_NOT_VALIDATED the exact strict constraint, convalidated=false.
                                An INTERRUPTED closure: already enforcing every
                                new INSERT/UPDATE, never proven against existing
                                rows, and never the ledger. Resumable.
    CLOSED                      the exact strict constraint, convalidated=true.
    INVALID                     everything else — a wrong definition under either
                                name, neither constraint present, or BOTH present.
                                Both present is not a transition state this tool
                                can produce: the swap is one atomic catalog
                                transaction, so coexistence means something
                                outside this tool altered the schema.
    """
    expand = _definition_body(row.get("expand_definition"))
    strict = _definition_body(row.get("strict_definition"))
    if strict is None:
        if expand is None or expand != EXPAND_DEFINITION:
            return STATE_INVALID
        return STATE_OPEN
    if strict != STRICT_DEFINITION or expand is not None:
        return STATE_INVALID
    return STATE_CLOSED if bool(row.get("strict_validated")) else (
        STATE_STRICT_NOT_VALIDATED
    )


def readiness(cur) -> Dict[str, Any]:
    cur.execute(
        READINESS_SQL,
        {"column": COLUMN, "expand": EXPAND_CONSTRAINT, "strict": STRICT_CONSTRAINT},
    )
    row = dict(cur.fetchone())
    state = contract_state(row)
    gates = {
        "G1_expand_column_present": int(row["column_present"]) == 1,
        "G2_zero_pending_rows": int(row["pending_rows"]) == 0,
        "G3_zero_orphan_instants": int(row["orphan_instants"]) == 0,
        # G4 is now EXACT, not by name. A same-named constraint carrying a
        # different expression proves the database did NOT go through this
        # expand phase, which is precisely what G4 exists to establish.
        "G4_expand_constraint_exact": _definition_body(
            row.get("expand_definition")
        ) == EXPAND_DEFINITION,
        "G5_strict_constraint_absent": int(row["strict_constraint"]) == 0,
    }
    row["gates"] = gates
    row["contract_state"] = state
    row["ledger_recorded"] = ledger_recorded(cur)
    #: The data invariants the strict constraint asserts. Separated from the
    #: constraint-shape gates because the resume path needs exactly these: the
    #: EXPAND constraint is legitimately gone by then, so `ready` cannot be the
    #: precondition for finishing a closure that already started.
    row["data_clean"] = (
        gates["G1_expand_column_present"]
        and gates["G2_zero_pending_rows"]
        and gates["G3_zero_orphan_instants"]
    )
    row["ready"] = all(gates.values()) and state == STATE_OPEN
    return row


# ---------------------------------------------------------------------------
# The closure — two transactions, on purpose
# ---------------------------------------------------------------------------

def _swap_constraints(conn) -> None:
    """Transaction 1, catalog-only: drop the one-directional CHECK, add the strict one.

    Both are metadata operations, so the ACCESS EXCLUSIVE lock is momentary — and
    the strict constraint is ALREADY ENFORCED on every new INSERT/UPDATE from the
    moment this commits, which is what actually closes the contract for the
    writer. The two statements share one transaction on purpose: the table is
    never left with neither constraint, and never with both.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"ALTER TABLE public.client_trips "
            f"DROP CONSTRAINT IF EXISTS {EXPAND_CONSTRAINT}"
        )
        cur.execute(
            f"ALTER TABLE public.client_trips "
            f"ADD CONSTRAINT {STRICT_CONSTRAINT} CHECK ("
            "  (first_seen_request_id IS NULL)"
            "  = (first_seen_response_received_at_utc IS NULL)"
            ") NOT VALID"
        )
    conn.commit()


def _validate_strict(conn) -> None:
    """Transaction 2: prove the existing rows.

    The scan takes only SHARE UPDATE EXCLUSIVE, so readers and writers continue.
    Splitting it from transaction 1 is the entire reason this is a tool rather
    than a migration file — and it is also why an interruption can land between
    the two, which `_finish_closure` is built to resume rather than mistake for
    completion.

    `VALIDATE CONSTRAINT` on an already-validated constraint is a no-op in
    PostgreSQL, so re-running this is safe and never drops or re-adds anything.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"ALTER TABLE public.client_trips "
            f"VALIDATE CONSTRAINT {STRICT_CONSTRAINT}"
        )
    conn.commit()


def _record_ledger(conn) -> None:
    """Transaction 3: the synthetic ledger entry, proved before it is trusted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
              filename TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
            "ON CONFLICT (filename) DO NOTHING",
            (LEDGER_ENTRY,),
        )
        # Read back inside the same transaction: the ledger entry is what a later
        # operator will trust, so it is proved rather than assumed.
        cur.execute(
            "SELECT count(*) AS n FROM pg_constraint "
            "WHERE conname = %s AND conrelid = 'public.client_trips'::regclass "
            "  AND convalidated",
            (STRICT_CONSTRAINT,),
        )
        row = cur.fetchone()
        validated = int(row["n"] if isinstance(row, dict) else row[0]) == 1
        if not validated:
            conn.rollback()
            raise _refuse(
                "POSTWRITE_VERIFICATION_FAILED",
                "the strict constraint does not read back as validated",
                EXIT_RUNTIME_FAILURE,
            )
    conn.commit()


def _finish_closure(conn) -> Dict[str, Any]:
    """Everything from "the strict constraint exists" to "the closure is complete".

    Deliberately the SAME code path for a fresh closure and for resuming an
    interrupted one, because the target state is identical and a second
    implementation would be a second thing to get wrong. Both steps are
    idempotent — `VALIDATE` on a validated constraint is a no-op, the ledger
    insert is `ON CONFLICT DO NOTHING` — so re-entering here converges rather
    than repeating work, and nothing correct is ever dropped and re-created.
    """
    _validate_strict(conn)
    _record_ledger(conn)
    return {"ledger_entry": LEDGER_ENTRY, "strict_constraint_validated": True}


def close_contract(conn) -> Dict[str, Any]:
    """OPEN -> CLOSED. Swap the constraint, validate it, record the ledger."""
    _swap_constraints(conn)
    return _finish_closure(conn)


def resume_closure(conn) -> Dict[str, Any]:
    """STRICT_PRESENT_NOT_VALIDATED (or a missing ledger row) -> CLOSED.

    The strict constraint is already installed and already enforcing, so there is
    nothing to swap: resuming means finishing the validation and the ledger, and
    NOT re-adding a constraint that is already correct. A correct validated
    strict constraint is never dropped by this path.
    """
    return _finish_closure(conn)


#: What `plan_action` decides to do. `close` runs the full swap+validate+ledger
#: sequence; `resume` finishes an already-installed strict constraint and never
#: touches the constraint itself.
ACTION_NONE = "none"
ACTION_CLOSE = "close"
ACTION_RESUME = "resume"


def plan_action(state: Dict[str, Any], *, execute: bool) -> Dict[str, Any]:
    """Decide, from a readiness row alone, what this client needs.

    Separated from `run` so the state machine is deterministic and testable
    without a fleet, a platform identity or a secret resolver. It performs no
    I/O and makes no decision that depends on anything but `state`.

    Returns `result` (the reported outcome), `action` (what the caller must
    execute), `reason` (why, when the result alone is not self-explaining) and
    `counts_as` — `not_ready`, `resume_required` or None — which is what decides
    the process exit code.
    """
    contract = state.get("contract_state")

    if contract == STATE_INVALID:
        # Not proven safe: a wrong definition under either name, neither
        # constraint present, or both. Fail closed rather than guess which DDL
        # would repair a shape this tool did not create.
        return {"result": "NOT_READY", "action": ACTION_NONE,
                "reason": "CONTRACT_STATE_INVALID", "counts_as": "not_ready"}

    if contract == STATE_CLOSED:
        if state.get("ledger_recorded"):
            # The only genuinely closed state. Rerunning is a no-op here.
            return {"result": "ALREADY_CLOSED", "action": ACTION_NONE,
                    "reason": None, "counts_as": None}
        # Validated, but the third transaction never committed: the schema is
        # correct and only the ledger is behind.
        if execute:
            return {"result": "LEDGER_RECORDED", "action": ACTION_RESUME,
                    "reason": None, "counts_as": None}
        return {"result": "LEDGER_REPAIR_REQUIRED", "action": ACTION_NONE,
                "reason": None, "counts_as": "resume_required"}

    if contract == STATE_STRICT_NOT_VALIDATED:
        # An INTERRUPTED closure. The strict constraint already governs every
        # new write, so this is not "not ready" — it is unfinished, and the tool
        # owns finishing it. The data invariants are still required: VALIDATE
        # would fail against a violating row anyway, and refusing first says why
        # instead of surfacing a raw PostgreSQL error.
        if not state.get("data_clean"):
            return {"result": "NOT_READY", "action": ACTION_NONE,
                    "reason": "RESUME_BLOCKED_BY_DATA_VIOLATION",
                    "counts_as": "not_ready"}
        if execute:
            return {"result": "RESUMED_AND_CLOSED", "action": ACTION_RESUME,
                    "reason": None, "counts_as": None}
        return {"result": "RESUME_REQUIRED", "action": ACTION_NONE,
                "reason": None, "counts_as": "resume_required"}

    # STATE_OPEN from here.
    if not state.get("ready"):
        return {"result": "NOT_READY", "action": ACTION_NONE,
                "reason": None, "counts_as": "not_ready"}
    if not execute:
        return {"result": "READY_NOT_EXECUTED", "action": ACTION_NONE,
                "reason": None, "counts_as": None}
    return {"result": "CLOSED", "action": ACTION_CLOSE,
            "reason": None, "counts_as": None}


def _client_conn(client: Dict[str, Any]):
    """The READINESS / OBSERVATION session: the client's own runtime identity.

    This is the identity the client's application actually runs as, which is
    exactly what a readiness report should be measured through — a gate that
    inspects production as a more privileged role than the writer is a gate that
    can pass on state the writer cannot actually reach.
    """
    psycopg, _ = _require_psycopg()
    dsn = (
        f"host={client['client_db_host']} "
        f"port={client['client_db_port']} "
        f"dbname={client['client_db_name']} "
        f"user={client['client_db_user']} "
        f"password={resolve_secret(client['client_db_password_secret_ref'])}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


#: The owner-required DDL this tool performs. PostgreSQL grants none of these
#: through `GRANT`: `ALTER TABLE ... DROP/ADD/VALIDATE CONSTRAINT` is reserved to
#: the table OWNER, so no ACL repair can make the client runtime role able to run
#: them. Recorded here because the distinction is the whole reason two identities
#: exist below, and a future reader will otherwise try to fix it with a GRANT.
OWNER_REQUIRED_OPERATIONS = (
    "ALTER TABLE public.client_trips DROP CONSTRAINT",
    "ALTER TABLE public.client_trips ADD CONSTRAINT",
    "ALTER TABLE public.client_trips VALIDATE CONSTRAINT",
)


def _client_owner_dsn(client: Dict[str, Any]) -> str:
    """Client database, opened as the PLATFORM role from the environment.

    The same identity every other privileged client-database path in this
    repository already uses — `ops.release_schema_preflight._client_dsn`,
    `jobs.reports.stage3.permissions.admin_dsn` and
    `ops.provision_local_environment_identity._client_admin_conn` all resolve the
    client DSN's user from `POSTGRES_USER`. This is deliberately NOT a new
    credential, a new role or a new secret: it is the existing owner identity,
    reached the existing way.
    """
    return (
        f"host={client['client_db_host']} "
        f"port={client['client_db_port']} "
        f"dbname={client['client_db_name']} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _assert_owns_client_trips(conn, client: Dict[str, Any]) -> Dict[str, Any]:
    """Prove, SERVER-SIDE, that this session may perform the closure DDL.

    FAIL CLOSED. The check asks PostgreSQL who the session is and who owns the
    table, rather than trusting that `POSTGRES_USER` was set to the right thing:
    an environment variable is a claim, `pg_class.relowner` is the fact. A
    session that is not the owner is refused here, before any `ALTER TABLE`, so
    the failure is a clean refusal rather than a half-applied swap.

    There is deliberately NO fallback to the client runtime role. Falling back
    would reintroduce exactly the failure this separation exists to prevent, and
    would do it silently.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_user                        AS session_user_name,
                   pg_get_userbyid(c.relowner)         AS table_owner,
                   pg_has_role(current_user, c.relowner, 'USAGE') AS owns
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname = 'client_trips'
            """
        )
        row = cur.fetchone()
    if row is None:
        raise _refuse(
            "OWNER_CONNECTION_TABLE_ABSENT",
            f"public.client_trips is absent on {client['client_db_name']!r}",
            EXIT_RUNTIME_FAILURE,
        )
    session_user_name = str(row["session_user_name"] if isinstance(row, dict) else row[0])
    table_owner = str(row["table_owner"] if isinstance(row, dict) else row[1])
    owns = bool(row["owns"] if isinstance(row, dict) else row[2])
    if not owns:
        raise _refuse(
            "OWNER_CONNECTION_NOT_TABLE_OWNER",
            f"the closure session is {session_user_name!r} but "
            f"public.client_trips on {client['client_db_name']!r} is owned by "
            f"{table_owner!r}; {OWNER_REQUIRED_OPERATIONS[0]} and its siblings "
            "require ownership and cannot be granted. Refusing rather than "
            "attempting DDL that would fail mid-transition.",
            EXIT_RUNTIME_FAILURE,
        )
    return {
        "owner_session_user": session_user_name,
        "client_trips_owner": table_owner,
    }


def _client_owner_conn(client: Dict[str, Any]):
    """The MUTATION session: owner-capable, used ONLY for the closure DDL.

    Opened per client, immediately proved to own the table, and closed as soon as
    that client's transition is done. Readiness never runs through it.
    """
    psycopg, dict_row = _require_psycopg()
    conn = set_pg_session_timezone(
        psycopg.connect(_client_owner_dsn(client), row_factory=dict_row)
    )
    try:
        identity = _assert_owns_client_trips(conn, client)
    except BaseException:
        conn.close()
        raise
    return conn, identity


#: The environment name under which an alternate release root is never
#: inspectable and never authoritative, whatever else is passed.
PRODUCTION_ENVIRONMENT = "production"

#: The two authorities G6 can carry, and the only one `--execute` accepts.
G6_OBSERVATIONAL = "OBSERVATIONAL"
G6_PROTECTED = "PROTECTED"


def _same_path(left: Path, right: Path) -> bool:
    """Path identity by resolved filesystem location, not by spelling."""
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def authoritative_release_root(args, *, execute: bool) -> Path:
    """The release inventory G6 is allowed to read, or a refusal.

    THE HOLE THIS CLOSES. `--release-root` was an ordinary path argument, so the
    second independent review satisfied G6 by pointing it at a synthetic
    directory it had built itself. That made G6 prove only that SOME filesystem
    structure looked bridge-like — not that PRODUCTION's `current` and
    `previous` are real releases carrying the transition declaration. An
    operator could therefore have closed the CONTRACT against evidence that
    described nothing production runs.

    So the override is now bounded, in the same shape `ops/cutover_execute.py`
    already uses for its rehearsal roots:

      * production identity (`--expected-environment production`) accepts ONLY
        `ops.manage_release.DEFAULT_RELEASE_ROOT`, in every mode. There is no
        flag that relaxes this;
      * any other identity may point elsewhere for a read-only report, and may
        do so under `--execute` only with the explicit
        `--allow-non-production-release-root`, which exists so the deterministic
        suites can drive the real closure against their own temporary
        inventories without ever making a synthetic root authoritative.

    The returned path is absolute and resolved, because `verify_release`
    requires an absolute root and pointer canonicalisation is meaningless
    against a relative one.
    """
    supplied = Path(str(args.release_root)).expanduser()
    if not supplied.is_absolute():
        raise _refuse(
            "RELEASE_ROOT_NOT_ABSOLUTE",
            f"--release-root must be absolute; got {supplied}",
            EXIT_INVALID_PARAMETERS,
        )
    authoritative = Path(DEFAULT_RELEASE_ROOT)
    if _same_path(supplied, authoritative):
        return supplied

    environment = str(args.expected_environment)
    if environment == PRODUCTION_ENVIRONMENT:
        raise _refuse(
            "RELEASE_ROOT_NOT_AUTHORITATIVE",
            f"--release-root {supplied} is not the authoritative production "
            f"release inventory {authoritative}. G6 proves that the releases "
            "PRODUCTION would run on both sides of this transition are real and "
            "bridge-compatible; an arbitrary directory cannot stand in for that, "
            "and no flag overrides this under --expected-environment "
            f"{PRODUCTION_ENVIRONMENT}.",
            EXIT_INVALID_PARAMETERS,
        )
    if execute and not bool(
        getattr(args, "allow_non_production_release_root", False)
    ):
        raise _refuse(
            "RELEASE_ROOT_NOT_AUTHORITATIVE",
            f"--release-root {supplied} is not {authoritative}, so --execute "
            "requires the explicit --allow-non-production-release-root. That "
            "flag is for controlled non-production use only and is refused "
            f"under --expected-environment {PRODUCTION_ENVIRONMENT}.",
            EXIT_INVALID_PARAMETERS,
        )
    return supplied


def rollback_envelope(
    release_root: Path,
    *,
    source_repo: Optional[Path] = None,
    authority: str = G6_OBSERVATIONAL,
) -> Dict[str, Any]:
    """G6. Read the one-step rollback envelope out of the release layout.

    Never an operator assertion, never inferred from repository HEAD or from a
    test requirements object, and never satisfied by a directory that merely
    looks like a release layout.
    `ops/release_schema_preflight.rollback_envelope_status` resolves `current`
    and `previous` canonically inside the inventory's own `releases/`
    directory, verifies each target with `ops.release_boundary.verify_release`
    — manifest, commit binding and on-disk bytes — and proves each declares the
    pair-contract capability and the EXACT canonical EXPAND and validated
    CONTRACT states as distinct alternatives. Two aliases for one release are
    refused, because re-activating a release is a no-op that moves no pointer.

    `authority` labels what the result may be used for, and is carried into the
    report so a read-only diagnostic can never be mistaken for a durable
    authorization:

      OBSERVATIONAL  a snapshot. Reports the envelope, and may REFUSE early on
                     it, but may never authorize `--execute`: a supported
                     activation or rollback can move both pointers a moment
                     after it is taken.
      PROTECTED      evaluated while this process holds
                     `SCHEMA_TRANSITION_LOCK_KEY`, which is the same key every
                     pointer mutation takes across its swap. Only this result
                     authorizes CONTRACT DDL, and it stays true for as long as
                     the lock is held.

    Returns the status verbatim so the check-only report shows the exact release
    ids and per-release defects. It exposes no secret: release ids, commits,
    pointer paths and declared requirements only.
    """
    try:
        status = rollback_envelope_status(
            Path(release_root), source_repo=source_repo,
        )
    except SchemaPreflightError as exc:  # pragma: no cover - defensive
        status = {
            "release_root": str(release_root), "ready": False,
            "reasons": [f"envelope_check_failed:{exc.code}"],
            "current": None, "previous": None,
        }
    status["authority"] = authority
    return status


def _envelope_refusal(envelope: Dict[str, Any], *, protected: bool) -> ContractRefused:
    """One refusal text for both G6 gates, differing only in what proved it."""
    current = (envelope.get("current") or {}).get("release_id")
    previous = (envelope.get("previous") or {}).get("release_id")
    where = (
        "re-evaluated under SCHEMA_TRANSITION_LOCK_KEY"
        if protected else
        "observed before the transition lock was taken"
    )
    return _refuse(
        "ROLLBACK_ENVELOPE_NOT_MATERIALIZED",
        f"G6 ({where}): the one-step rollback envelope is not proven. current="
        f"{current!r} previous={previous!r}; "
        + "; ".join(str(r) for r in envelope.get("reasons", []))
        + ". Closing the CONTRACT refuses every release without the "
        "first-seen pair capability, so both pointers must already be "
        "distinct, verified, materialized, bridge-compatible releases in the "
        "authoritative release inventory. Activate a SECOND distinct "
        "bridge-compatible release and re-run; nothing has been changed.",
        EXIT_NOT_READY,
    )


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


def run(args) -> Tuple[int, Dict[str, Any]]:
    client_code = (
        _safe_token(args.client_code, label="--client-code")
        if args.client_code else None
    )
    check_only = bool(args.check_only)
    execute = bool(args.execute) and not check_only
    if execute:
        if not args.approval_ref:
            raise _refuse(
                "APPROVAL_REF_REQUIRED",
                "--approval-ref is required to close the contract",
                EXIT_INVALID_PARAMETERS,
            )
        _safe_token(args.approval_ref, label="--approval-ref")
        if str(args.confirm or "") != "CLOSE_CONTRACT":
            raise _refuse(
                "CONFIRMATION_MISMATCH",
                "--confirm CLOSE_CONTRACT is required",
                EXIT_INVALID_PARAMETERS,
            )
        if not args.rollback_window_closed:
            raise _refuse(
                "ROLLBACK_WINDOW_STILL_OPEN",
                "G7: --rollback-window-closed is required. Closing this contract "
                "makes the M4-era writer unable to insert provenance-bearing "
                "rows, so rolling back to the pre-M-LAG release stops being "
                "safe. That decision cannot be read out of the database and is "
                "not inferred from a clean fleet.",
                EXIT_INVALID_PARAMETERS,
            )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    psycopg, dict_row = _require_psycopg()

    # THE AUTHORITATIVE INVENTORY, resolved before any lock is taken and before
    # any database is opened. Everything G6 goes on to prove is a property of
    # THIS root, so establishing which root may be read is the first step, not a
    # late validation of an argument already acted on.
    release_root = authoritative_release_root(args, execute=execute)
    source_repo = Path(
        getattr(args, "source_repo", None) or REPO_ROOT
    ).expanduser()

    # An OBSERVATIONAL G6 — the check-only report is exactly where an operator
    # should learn that the envelope is not there yet, with the release ids that
    # say why, and an execute that is already hopeless should say so before
    # queueing behind a live activation's lock. It reads the release layout
    # only, so it is safe in every mode. It can REFUSE. It cannot AUTHORIZE:
    # see the protected re-evaluation below.
    envelope = rollback_envelope(
        release_root, source_repo=source_repo, authority=G6_OBSERVATIONAL,
    )

    report: Dict[str, Any] = {
        "mode": "CHECK_ONLY" if check_only else ("EXECUTE" if execute else "DRY_RUN"),
        "approval_ref": args.approval_ref,
        "rollback_window_closed": bool(args.rollback_window_closed),
        "release_root": str(release_root),
        "release_root_authoritative": _same_path(
            release_root, Path(DEFAULT_RELEASE_ROOT)
        ),
        "source_repo": str(source_repo),
        "G6_rollback_envelope": envelope,
        "G6_authority": envelope.get("authority"),
        "clients": [],
    }

    if execute and not envelope.get("ready"):
        # Fail fast, before the lock and before any database connection. This is
        # a cheap refusal on a snapshot, never an authorization from one.
        raise _envelope_refusal(envelope, protected=False)

    not_ready = 0
    resume_required = 0

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as pconn:
        pconn.execute("SET TRANSACTION READ ONLY")
        with pconn.cursor() as pcur:
            report["platform_identity"] = verify_platform_identity(
                pcur,
                expected_environment=str(args.expected_environment),
                expected_platform_uuid=expected_uuid,
            )
            clients = resolve_clients(pcur, client_code=client_code)
        pconn.rollback()

    def _process_clients() -> None:
        nonlocal not_ready, resume_required
        for client in clients:
            entry: Dict[str, Any] = {
                "client_code": client["client_code"],
                "client_id": client["client_id"],
            }
            with _client_conn(client) as cconn:
                with cconn.cursor(row_factory=dict_row) as ccur:
                    state = readiness(ccur)
                cconn.rollback()
                entry.update({k: v for k, v in state.items() if k != "gates"})
                entry["gates"] = state["gates"]

                plan = plan_action(state, execute=execute)
                entry["result"] = plan["result"]
                if plan["reason"]:
                    entry["reason"] = plan["reason"]
                if plan["counts_as"] == "not_ready":
                    not_ready += 1
                elif plan["counts_as"] == "resume_required":
                    resume_required += 1

            # THE MUTATION RUNS ON A SECOND, OWNER-CAPABLE SESSION, and only
            # after the readiness session above has been closed. The two are
            # separated because they need different authority and must not be
            # able to borrow each other's: readiness is measured through the
            # client's own runtime role, while `ALTER TABLE ... CONSTRAINT` is
            # owner-only in PostgreSQL and cannot be granted to that role. The
            # decision of WHAT to do is still made entirely from the readiness
            # state — `plan_action` is unchanged and still sees only the client
            # session's row — so the owner session executes a plan, it does not
            # get to re-decide it under different privileges.
            if plan["action"] in (ACTION_CLOSE, ACTION_RESUME):
                owner_conn, owner_identity = _client_owner_conn(client)
                entry.update(owner_identity)
                with owner_conn:
                    if plan["action"] == ACTION_CLOSE:
                        entry.update(close_contract(owner_conn))
                    else:
                        entry.update(resume_closure(owner_conn))
            report["clients"].append(entry)

    if execute:
        # Held across EVERY client, from before the AUTHORITATIVE G6 evaluation
        # to after the last ledger insert. A release activation, a rollback and
        # a cutover all take the same key transaction-scoped across their
        # pointer swap, so:
        #
        #   * a pointer mutation that has already committed is visible to the
        #     protected G6 below, however stale the pre-lock snapshot was;
        #   * a pointer mutation that has not yet committed cannot commit while
        #     this closure holds the lock, so the envelope G6 accepted stays
        #     true for the whole interval in which CONTRACT DDL can advance.
        #
        # THE DEFECT THIS ORDERING CLOSES. The gate used to run entirely before
        # the lock, so a supported activation could invalidate the envelope in
        # the window between the snapshot and the acquisition, and the closure
        # would proceed on evidence that had already stopped being true.
        #
        # Partial fleet progress remains safe: the lock is about ordering
        # against activation, not about atomicity across clients, and each
        # client's own closure stays independently resumable.
        try:
            with schema_transition_lock(
                platform_conn_factory=lambda: psycopg.connect(dsn),
            ):
                report["schema_transition_lock_held"] = True
                protected = rollback_envelope(
                    release_root, source_repo=source_repo,
                    authority=G6_PROTECTED,
                )
                report["G6_rollback_envelope"] = protected
                report["G6_authority"] = protected.get("authority")
                if not protected.get("ready"):
                    # FAIL CLOSED, INSIDE THE COORDINATION AND BEFORE ANY CLIENT
                    # DDL. `--rollback-window-closed` does not override this and
                    # is not asked to: it acknowledges irreversibility, while
                    # this establishes that the one-step rollback target
                    # survives.
                    raise _envelope_refusal(protected, protected=True)
                _process_clients()
        except SchemaPreflightError as exc:
            raise _refuse(
                "SCHEMA_TRANSITION_LOCK_UNAVAILABLE",
                f"{exc.code}: {exc.detail}. A release activation is most likely "
                "in flight; nothing has been changed.",
                EXIT_NOT_READY,
            ) from exc
    else:
        # Read-only modes take no lock: they mutate nothing, so there is nothing
        # to serialise, and blocking a readiness report behind an activation
        # would be a cost with no safety return.
        report["schema_transition_lock_held"] = False
        _process_clients()

    report["not_ready_clients"] = not_ready
    report["resume_required_clients"] = resume_required
    if not_ready or resume_required:
        return EXIT_NOT_READY, report
    return EXIT_OK, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Readiness gate and CONTRACT closure for the client_trips first-seen "
            "pair. Closing it intentionally ends M4-writer rollback "
            "compatibility."
        ),
    )
    parser.add_argument("--client-code", help="Restrict to one client.")
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-platform-uuid", required=True)
    parser.add_argument(
        "--check-only", action="store_true",
        help="Report the readiness gate and change nothing. Never writes.",
    )
    parser.add_argument("--approval-ref")
    parser.add_argument(
        "--execute", action="store_true",
        help="Close the contract. Requires --confirm and "
             "--rollback-window-closed.",
    )
    parser.add_argument(
        "--confirm", help="Must be the literal CLOSE_CONTRACT.",
    )
    parser.add_argument(
        "--rollback-window-closed", action="store_true",
        help="G7. Acknowledges that closing this contract is irreversible for "
             "M4-writer rollback. An acknowledgement only: G6 separately PROVES "
             "the materialized rollback envelope and this flag cannot substitute "
             "for it.",
    )
    parser.add_argument(
        "--release-root", default=str(DEFAULT_RELEASE_ROOT),
        help="G6. The AUTHORITATIVE release inventory whose 'current' and "
             "'previous' pointers must both resolve canonically to distinct, "
             "VERIFIED materialized releases carrying the exact bridge "
             "declaration before the contract may be closed. Under "
             "--expected-environment production only the default is accepted.",
    )
    parser.add_argument(
        "--allow-non-production-release-root", action="store_true",
        help="Controlled non-production use only. Permits --execute against a "
             "release root that is not the authoritative one. Refused under "
             "--expected-environment production, where no flag relaxes the "
             "authoritative inventory.",
    )
    parser.add_argument(
        "--source-repo", default=str(REPO_ROOT),
        help="Repository the release manifests are cross-checked against when "
             "Git can still reach their commits. An unreachable commit is "
             "reported, not fatal: manifest and content verification stand on "
             "the release's own bytes.",
    )
    parser.add_argument("--dsn")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, report = run(args)
    except ContractRefused as exc:
        print(f"CONTRACT_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(f"CONTRACT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE

    print(json.dumps(report, sort_keys=True, indent=2, default=str))
    print(report["mode"])
    envelope = report.get("G6_rollback_envelope") or {}
    if not envelope.get("ready"):
        current = (envelope.get("current") or {}).get("release_id")
        previous = (envelope.get("previous") or {}).get("release_id")
        print(
            f"G6 ROLLBACK ENVELOPE NOT PROVEN ({envelope.get('authority')}) — "
            f"current={current} previous={previous}: "
            + "; ".join(str(r) for r in envelope.get("reasons", []))
            + ". --execute is refused until a SECOND distinct bridge-compatible "
            "release has been activated, leaving both pointers bridge-capable.",
            file=sys.stderr,
        )
    elif envelope.get("authority") != G6_PROTECTED:
        # A READY report that was never taken under the transition lock is a
        # DIAGNOSTIC, not an authorization, and must not read like one: a
        # supported activation can move both pointers a moment after it.
        print(
            "G6 READY, OBSERVATIONAL ONLY — this snapshot was taken without "
            "SCHEMA_TRANSITION_LOCK_KEY and does not authorize anything. "
            "--execute re-evaluates the envelope under that lock and decides "
            "on the protected result.",
            file=sys.stderr,
        )
    if exit_code == EXIT_NOT_READY:
        if report.get("not_ready_clients"):
            print(
                f"NOT_READY — {report['not_ready_clients']} client(s) fail the "
                "gate; run ops/enrich_telematics_first_seen_timestamps.py first, "
                "and inspect any client reported CONTRACT_STATE_INVALID by hand",
                file=sys.stderr,
            )
        if report.get("resume_required_clients"):
            print(
                f"RESUME_REQUIRED — {report['resume_required_clients']} client(s) "
                "carry an UNFINISHED closure (strict constraint installed but "
                "not validated, or ledger entry missing). Re-run with --execute "
                "to finish it; nothing is dropped or re-created.",
                file=sys.stderr,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
