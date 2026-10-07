#!/usr/bin/env python3
"""The first-seen pair CONTRACT closure is resumable, exact, and idempotent.

THE DEFECT THIS CLOSES.
    `ops/close_telematics_first_seen_pair_contract.py` closes the contract across
    three transactions — swap, validate, ledger — because that is what keeps the
    `VALIDATE` scan off the `ACCESS EXCLUSIVE` lock. An interruption can
    therefore land between them, leaving `ck_client_trips_first_seen_pairing`
    installed but `convalidated = false`.

    The original gate treated "the strict constraint exists" as proof of
    completion (G5) and reported ALREADY_CLOSED for exactly that state. So an
    interrupted closure could never be finished by rerunning the tool: every
    subsequent run agreed there was nothing left to do, while the constraint
    stayed unvalidated and the ledger entry stayed unwritten. docs/21 §13
    recorded it as a known operational residual; this suite is the evidence it
    is closed.

WHY IT NEEDS A REAL DATABASE.
    Every assertion is about `pg_constraint` — canonical `pg_get_constraintdef`
    text and the `convalidated` boolean — and about whether `VALIDATE
    CONSTRAINT` actually scans and actually refuses a violating row. None of
    that can be faked; the whole point is that the catalog, not the tool's
    belief, is the authority.

DESTRUCTIVE. Creates and drops its own tables in the database the DSN names.
Refuses any DSN that is not loopback, and any DSN that looks like the platform
database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.close_telematics_first_seen_pair_contract import (  # noqa: E402
    ACTION_CLOSE,
    ACTION_NONE,
    ACTION_RESUME,
    EXPAND_CONSTRAINT,
    EXPAND_DEFINITION,
    LEDGER_ENTRY,
    STATE_CLOSED,
    STATE_INVALID,
    STATE_OPEN,
    STATE_STRICT_NOT_VALIDATED,
    STRICT_CONSTRAINT,
    STRICT_DEFINITION,
    _swap_constraints,
    close_contract,
    plan_action,
    readiness,
    resume_closure,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_MLAG_CONTRACT_TEST_DSN"

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ[ENV], row_factory=dict_row)


# ---------------------------------------------------------------------------
# Fixture — a client business table in one specific state
# ---------------------------------------------------------------------------

#: The EXPAND constraint exactly as `db/client_business/048_…sql` installs it.
EXPAND_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
    "CHECK (first_seen_response_received_at_utc IS NULL "
    "       OR first_seen_request_id IS NOT NULL) NOT VALID"
)
#: The strict CONTRACT constraint exactly as the closure tool installs it.
STRICT_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
    "CHECK ((first_seen_request_id IS NULL) "
    "       = (first_seen_response_received_at_utc IS NULL)) NOT VALID"
)


def build(
    *,
    state: str,
    rows: Optional[List[Dict[str, Any]]] = None,
    ledger: bool = False,
    ledger_table: bool = True,
):
    """A client_trips in `state`, populated before any constraint is installed.

    Rows are inserted FIRST, so a scenario can seed exactly the pre-existing
    violation an unvalidated constraint tolerates and a `VALIDATE` must reject.
    That ordering is the whole difference between "NOT VALID" and "invalid".
    """
    conn = _connect()
    conn.execute("DROP TABLE IF EXISTS public.client_trips")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.execute(
        """CREATE TABLE public.client_trips (
             client_id UUID NOT NULL,
             provider_trip_id BIGINT NOT NULL,
             first_seen_request_id UUID NULL,
             first_seen_response_received_at_utc TIMESTAMPTZ NULL,
             PRIMARY KEY (client_id, provider_trip_id))"""
    )
    if ledger_table:
        conn.execute(
            "CREATE TABLE public.schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    for i, row in enumerate(rows or []):
        conn.execute(
            "INSERT INTO public.client_trips "
            "(client_id, provider_trip_id, first_seen_request_id, "
            " first_seen_response_received_at_utc) "
            "VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', %s, %s, %s)",
            (i, row.get("request_id"), row.get("instant")),
        )

    if state == "EXPAND":
        conn.execute(EXPAND_DDL)
    elif state == "STRICT_NOT_VALID":
        conn.execute(STRICT_DDL)
    elif state == "STRICT_VALIDATED":
        conn.execute(STRICT_DDL)
        conn.execute(
            f"ALTER TABLE public.client_trips VALIDATE CONSTRAINT {STRICT_CONSTRAINT}"
        )
    elif state == "BOTH":
        conn.execute(EXPAND_DDL)
        conn.execute(STRICT_DDL)
    elif state == "EXPAND_WRONG_DEFINITION":
        # Same NAME, different meaning: this one forbids the pending state the
        # real EXPAND constraint exists to permit.
        conn.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
            "CHECK (first_seen_request_id IS NULL "
            "       OR first_seen_response_received_at_utc IS NOT NULL) NOT VALID"
        )
    elif state == "STRICT_WRONG_DEFINITION":
        # Same name, one-directional expression. A name-only gate accepts it and
        # then believes pairing is enforced when it is not.
        conn.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
            "CHECK (first_seen_request_id IS NOT NULL "
            "       OR first_seen_response_received_at_utc IS NULL) NOT VALID"
        )
    elif state == "NONE":
        pass
    else:  # pragma: no cover - a typo in a scenario must not pass silently
        raise AssertionError(f"unknown fixture state {state!r}")

    if ledger and ledger_table:
        conn.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
            (LEDGER_ENTRY,),
        )
    conn.commit()
    conn.close()


def _state() -> Dict[str, Any]:
    conn = _connect()
    with conn.cursor() as cur:
        row = readiness(cur)
    conn.rollback()
    conn.close()
    return row


def _catalog() -> Dict[str, Any]:
    conn = _connect()
    rows = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) AS def, convalidated "
        "  FROM pg_constraint "
        " WHERE conrelid = 'public.client_trips'::regclass AND contype = 'c' "
        " ORDER BY conname"
    ).fetchall()
    conn.rollback()
    conn.close()
    return {r["conname"]: (r["def"], r["convalidated"]) for r in rows}


COMPLETE = {"request_id": "b454f82c-5857-4bab-8342-b7258e5cf7de",
            "instant": "2026-01-01T00:00:00+00:00"}
NO_PROVENANCE: Dict[str, Any] = {"request_id": None, "instant": None}
PENDING = {"request_id": "f6222a11-06ee-4e4f-8b25-302a9d963cfa", "instant": None}
ORPHAN = {"request_id": None, "instant": "2026-01-01T00:00:00+00:00"}


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

def test_state_open() -> None:
    print("\n## test_state_open")
    build(state="EXPAND", rows=[COMPLETE, NO_PROVENANCE])
    state = _state()
    _check("EXPAND with the exact definition classifies as OPEN",
           state["contract_state"] == STATE_OPEN, str(state["contract_state"]))
    _check("G4 is satisfied by the exact EXPAND definition",
           state["gates"]["G4_expand_constraint_exact"])
    _check("the gate reports ready", state["ready"] is True)
    plan = plan_action(state, execute=False)
    _check("a dry run plans READY_NOT_EXECUTED and does nothing",
           plan["result"] == "READY_NOT_EXECUTED" and plan["action"] == ACTION_NONE,
           str(plan))
    plan = plan_action(state, execute=True)
    _check("an execute run plans the full closure",
           plan["result"] == "CLOSED" and plan["action"] == ACTION_CLOSE, str(plan))


def test_state_strict_not_validated_is_not_already_closed() -> None:
    print("\n## test_state_strict_not_validated_is_not_already_closed")
    build(state="STRICT_NOT_VALID", rows=[COMPLETE, NO_PROVENANCE])
    state = _state()
    _check("an unvalidated strict constraint is STRICT_PRESENT_NOT_VALIDATED",
           state["contract_state"] == STATE_STRICT_NOT_VALIDATED,
           str(state["contract_state"]))
    _check("it is NOT reported as closed",
           state["contract_state"] != STATE_CLOSED)
    plan = plan_action(state, execute=False)
    _check("a check-only run reports RESUME_REQUIRED, not ALREADY_CLOSED",
           plan["result"] == "RESUME_REQUIRED", str(plan))
    _check("and it counts toward the non-zero exit",
           plan["counts_as"] == "resume_required", str(plan))
    plan = plan_action(state, execute=True)
    _check("an execute run plans a RESUME, never a re-close",
           plan["result"] == "RESUMED_AND_CLOSED" and plan["action"] == ACTION_RESUME,
           str(plan))


def test_state_closed() -> None:
    print("\n## test_state_closed")
    build(state="STRICT_VALIDATED", rows=[COMPLETE], ledger=True)
    state = _state()
    _check("a validated strict constraint classifies as CLOSED",
           state["contract_state"] == STATE_CLOSED, str(state["contract_state"]))
    _check("the ledger entry is seen", state["ledger_recorded"] is True)
    plan = plan_action(state, execute=True)
    _check("CLOSED plus ledger is ALREADY_CLOSED and does nothing",
           plan["result"] == "ALREADY_CLOSED" and plan["action"] == ACTION_NONE,
           str(plan))


def test_state_closed_without_ledger_is_repaired_not_ignored() -> None:
    print("\n## test_state_closed_without_ledger_is_repaired_not_ignored")
    build(state="STRICT_VALIDATED", rows=[COMPLETE], ledger=False)
    state = _state()
    _check("the schema is closed but the ledger entry is missing",
           state["contract_state"] == STATE_CLOSED
           and state["ledger_recorded"] is False)
    plan = plan_action(state, execute=False)
    _check("a check-only run says the ledger needs repair",
           plan["result"] == "LEDGER_REPAIR_REQUIRED"
           and plan["counts_as"] == "resume_required", str(plan))

    conn = _connect()
    resume_closure(conn)
    conn.close()
    state = _state()
    _check("resuming writes the ledger entry without touching the constraint",
           state["ledger_recorded"] is True
           and state["contract_state"] == STATE_CLOSED)


def test_state_closed_without_a_ledger_table_at_all() -> None:
    print("\n## test_state_closed_without_a_ledger_table_at_all")
    # A client database that has never run a migration has no ledger table. The
    # readiness report must say "not recorded", not fail to parse.
    build(state="STRICT_VALIDATED", rows=[COMPLETE], ledger_table=False)
    state = _state()
    _check("an absent ledger table reads as 'not recorded'",
           state["ledger_recorded"] is False
           and state["contract_state"] == STATE_CLOSED, str(state["contract_state"]))
    conn = _connect()
    resume_closure(conn)
    conn.close()
    _check("and the resume creates it and records the entry",
           _state()["ledger_recorded"] is True)


def test_invalid_states_fail_closed() -> None:
    print("\n## test_invalid_states_fail_closed")
    for fixture, why in (
        ("NONE", "neither constraint present"),
        ("EXPAND_WRONG_DEFINITION", "EXPAND name with a different expression"),
        ("STRICT_WRONG_DEFINITION", "strict name with a different expression"),
        ("BOTH", "both constraints present, which the closure never produces"),
    ):
        build(state=fixture, rows=[COMPLETE])
        state = _state()
        _check(f"{why} -> INVALID",
               state["contract_state"] == STATE_INVALID,
               f"actual={state['contract_state']}")
        plan = plan_action(state, execute=True)
        _check(f"{why} -> refused, nothing executed",
               plan["result"] == "NOT_READY"
               and plan["action"] == ACTION_NONE
               and plan["reason"] == "CONTRACT_STATE_INVALID", str(plan))


# ---------------------------------------------------------------------------
# Data gates
# ---------------------------------------------------------------------------

def test_zero_violations_is_ready() -> None:
    print("\n## test_zero_violations_is_ready")
    build(state="EXPAND", rows=[COMPLETE, NO_PROVENANCE, COMPLETE])
    state = _state()
    _check("PENDING = 0 and ORPHAN = 0", state["pending_rows"] == 0
           and state["orphan_instants"] == 0)
    _check("the data invariants read clean", state["data_clean"] is True)
    _check("and the gate is ready", state["ready"] is True)


def test_existing_data_violation_blocks_closure() -> None:
    print("\n## test_existing_data_violation_blocks_closure")
    build(state="EXPAND", rows=[COMPLETE, PENDING])
    state = _state()
    _check("a PROVENANCE_TIMESTAMP_PENDING row is counted",
           state["pending_rows"] == 1)
    _check("G2 fails", state["gates"]["G2_zero_pending_rows"] is False)
    plan = plan_action(state, execute=True)
    _check("closure is refused while a pending row exists",
           plan["result"] == "NOT_READY" and plan["action"] == ACTION_NONE, str(plan))

    # And an orphan instant, the other direction. The EXPAND constraint is
    # NOT VALID so the row can pre-exist; that is exactly why G3 is checked.
    build(state="EXPAND", rows=[ORPHAN])
    state = _state()
    _check("an orphan instant is counted", state["orphan_instants"] == 1)
    _check("G3 fails", state["gates"]["G3_zero_orphan_instants"] is False)
    _check("closure is refused while an orphan instant exists",
           plan_action(state, execute=True)["result"] == "NOT_READY")


def test_resume_is_blocked_by_a_data_violation() -> None:
    print("\n## test_resume_is_blocked_by_a_data_violation")
    build(state="STRICT_NOT_VALID", rows=[PENDING])
    state = _state()
    _check("an interrupted closure over violating rows is not resumable",
           state["contract_state"] == STATE_STRICT_NOT_VALIDATED
           and state["data_clean"] is False)
    plan = plan_action(state, execute=True)
    _check("and it refuses with the reason, rather than failing in VALIDATE",
           plan["result"] == "NOT_READY"
           and plan["reason"] == "RESUME_BLOCKED_BY_DATA_VIOLATION", str(plan))

    # Prove the refusal is not merely cautious: VALIDATE genuinely fails here.
    conn = _connect()
    failed = False
    try:
        resume_closure(conn)
    except Exception:
        failed = True
        conn.rollback()
    conn.close()
    _check("VALIDATE really would have failed against the violating row", failed)


# ---------------------------------------------------------------------------
# Execution: closure, interruption, resume, idempotency
# ---------------------------------------------------------------------------

def test_open_to_closed() -> None:
    print("\n## test_open_to_closed")
    build(state="EXPAND", rows=[COMPLETE, NO_PROVENANCE])
    conn = _connect()
    result = close_contract(conn)
    conn.close()
    catalog = _catalog()
    _check("the EXPAND constraint is gone", EXPAND_CONSTRAINT not in catalog)
    _check("the strict constraint is installed with the exact definition",
           catalog.get(STRICT_CONSTRAINT, ("", False))[0] == STRICT_DEFINITION,
           str(catalog.get(STRICT_CONSTRAINT)))
    _check("and it is validated",
           catalog.get(STRICT_CONSTRAINT, ("", False))[1] is True)
    _check("the ledger entry is recorded",
           result["ledger_entry"] == LEDGER_ENTRY and _state()["ledger_recorded"])
    _check("the resulting state is CLOSED",
           _state()["contract_state"] == STATE_CLOSED)

    # The invariant the closure exists to install, proved by the database.
    conn = _connect()
    rejected = False
    try:
        conn.execute(
            "INSERT INTO public.client_trips "
            "(client_id, provider_trip_id, first_seen_request_id, "
            " first_seen_response_received_at_utc) "
            "VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 9001, "
            "        '44444444-4444-4444-8444-444444444444', NULL)"
        )
        conn.commit()
    except Exception:
        rejected = True
        conn.rollback()
    conn.close()
    _check("a request-only INSERT — the M4-era writer's shape — is rejected",
           rejected)


def test_interruption_between_swap_and_validate_is_resumable() -> None:
    print("\n## test_interruption_between_swap_and_validate_is_resumable")
    build(state="EXPAND", rows=[COMPLETE, NO_PROVENANCE])

    # Exactly the interruption: transaction 1 commits, the process dies.
    conn = _connect()
    _swap_constraints(conn)
    conn.close()

    catalog = _catalog()
    _check("the strict constraint exists but is NOT VALID",
           STRICT_CONSTRAINT in catalog
           and catalog[STRICT_CONSTRAINT][1] is False)
    state = _state()
    _check("the ledger entry was never written",
           state["ledger_recorded"] is False)
    _check("the interrupted state is recognised, not called closed",
           state["contract_state"] == STATE_STRICT_NOT_VALIDATED)
    _check("a rerun would NOT report ALREADY_CLOSED",
           plan_action(state, execute=True)["result"] != "ALREADY_CLOSED")

    # Even unvalidated, the constraint is already enforcing new writes — which
    # is why this state is safe to leave briefly and unsafe to leave forever.
    conn = _connect()
    rejected = False
    try:
        conn.execute(
            "INSERT INTO public.client_trips "
            "(client_id, provider_trip_id, first_seen_request_id, "
            " first_seen_response_received_at_utc) "
            "VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 9002, "
            "        '44444444-4444-4444-8444-444444444444', NULL)"
        )
        conn.commit()
    except Exception:
        rejected = True
        conn.rollback()
    conn.close()
    _check("a NOT VALID strict constraint already rejects a request-only INSERT",
           rejected)

    # The resume.
    conn = _connect()
    resume_closure(conn)
    conn.close()
    catalog = _catalog()
    _check("the resume validates the existing constraint",
           catalog[STRICT_CONSTRAINT][1] is True)
    _check("without changing its definition",
           catalog[STRICT_CONSTRAINT][0] == STRICT_DEFINITION)
    _check("and there is exactly one pairing constraint, not a duplicate",
           sum(1 for name in catalog if name == STRICT_CONSTRAINT) == 1
           and len([n for n in catalog if "first_seen" in n]) == 1,
           str(sorted(catalog)))
    state = _state()
    _check("the client has converged to CLOSED",
           state["contract_state"] == STATE_CLOSED and state["ledger_recorded"])


def test_rerun_is_an_idempotent_no_op() -> None:
    print("\n## test_rerun_is_an_idempotent_no_op")
    build(state="EXPAND", rows=[COMPLETE, NO_PROVENANCE])
    conn = _connect()
    close_contract(conn)
    conn.close()
    before = _catalog()
    before_state = _state()

    # A rerun plans nothing at all.
    plan = plan_action(before_state, execute=True)
    _check("a closed client plans no action on rerun",
           plan["result"] == "ALREADY_CLOSED" and plan["action"] == ACTION_NONE,
           str(plan))

    # And even if the resume path were entered, it converges rather than churns.
    conn = _connect()
    resume_closure(conn)
    resume_closure(conn)
    conn.close()
    _check("re-entering the resume path changes nothing in the catalog",
           _catalog() == before, f"before={before} after={_catalog()}")
    _check("and the ledger still holds exactly one entry",
           _state()["ledger_recorded"] is True)

    conn = _connect()
    ledger_rows = conn.execute(
        "SELECT count(*) AS n FROM public.schema_migrations WHERE filename = %s",
        (LEDGER_ENTRY,),
    ).fetchone()["n"]
    conn.rollback()
    conn.close()
    _check("exactly one ledger row, never a duplicate", ledger_rows == 1)


def test_definitions_match_the_migration_and_the_requirements_file() -> None:
    print("\n## test_definitions_match_the_migration_and_the_requirements_file")
    # The tool's expected definitions and the release requirements file must
    # describe the same two constraints. If they drift, the closure would leave
    # a database that the release preflight then refuses.
    import json
    document = json.loads(
        (ROOT / "db/schema_requirements.json").read_text(encoding="utf-8")
    )
    declared: Dict[str, Dict[str, Any]] = {}
    for requirement in document["requirements"]:
        for relation in requirement["relations"]:
            for group in relation.get("constraint_alternatives", []):
                for constraint in group["constraints"]:
                    declared[constraint["name"]] = constraint
    expand = declared.get(EXPAND_CONSTRAINT, {})
    strict = declared.get(STRICT_CONSTRAINT, {})
    _check("the requirements file declares the EXPAND state the tool expects",
           " ".join(str(expand.get("definition", "")).split())
           == f"{EXPAND_DEFINITION} NOT VALID"
           and expand.get("validated") is False,
           str(expand))
    _check("and the CONTRACT state the tool installs, requiring validation",
           " ".join(str(strict.get("definition", "")).split()) == STRICT_DEFINITION
           and strict.get("validated") is True,
           str(strict))

    # The migration file is the other authority for the EXPAND half.
    migration = (
        ROOT / "db/client_business"
        / "048_client_trips_first_seen_response_received_at.sql"
    ).read_text(encoding="utf-8")
    _check("migration 048 still installs the EXPAND constraint NOT VALID",
           EXPAND_CONSTRAINT in migration and "NOT VALID" in migration)
    _check("and still does not install strict pairing itself",
           STRICT_CONSTRAINT not in migration)


def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    test_state_open()
    test_state_strict_not_validated_is_not_already_closed()
    test_state_closed()
    test_state_closed_without_ledger_is_repaired_not_ignored()
    test_state_closed_without_a_ledger_table_at_all()
    test_invalid_states_fail_closed()
    test_zero_violations_is_ready()
    test_existing_data_violation_blocks_closure()
    test_resume_is_blocked_by_a_data_violation()
    test_open_to_closed()
    test_interruption_between_swap_and_validate_is_resumable()
    test_rerun_is_an_idempotent_no_op()
    test_definitions_match_the_migration_and_the_requirements_file()

    conn = _connect()
    conn.execute("DROP TABLE IF EXISTS public.client_trips")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.commit()
    conn.close()

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
