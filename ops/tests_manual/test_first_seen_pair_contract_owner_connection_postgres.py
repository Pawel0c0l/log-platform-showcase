#!/usr/bin/env python3
"""The CONTRACT closure runs its DDL as the table OWNER, and refuses otherwise.

WHY THIS SUITE EXISTS.
    A production closure attempt failed with

        InsufficientPrivilege: must be owner of table client_trips

    after every readiness gate had passed. The tool opened ONE session per
    client, as that client's registry `client_db_user`, and used it for both the
    readiness read and the `ALTER TABLE` DDL. On this fleet the client roles are
    least-privilege runtime users and `public.client_trips` is owned by the
    platform role, so the DDL was unreachable through that identity.

    The failure was atomic — `_swap_constraints` puts DROP and ADD in one
    transaction — so nothing was half-applied. But no existing test could have
    caught it: `test_first_seen_pair_contract_closure_postgres.py` connects with
    a single DSN whose role owns everything, which is precisely the arrangement
    production does not have. A suite that cannot distinguish the two identities
    cannot prove anything about which one runs the DDL.

    So this suite builds the production arrangement explicitly: an owner role
    that owns the table, and a separate non-owner client role that does not. It
    is the difference between the two roles that makes the assertions meaningful.

WHAT IT PROVES.
     1. readiness still operates through the per-client runtime identity;
     2. mutation runs on the owner-capable connection;
     3. the underprivileged client role can never be used for the DDL;
     4. an owner-identity mismatch fails CLOSED, before any ALTER;
     5. check-only opens no owner connection and mutates nothing;
     6. OPEN -> CLOSED succeeds when owner and client roles are distinct;
     7. a failed swap stays atomic;
     8. the ledger is written only after a validated physical CLOSED state.

    Protected-G6 ordering and the G7 requirement are argument- and lock-level
    concerns owned by `run()`; they are covered by the activation-race and
    rollback-envelope suites and are deliberately not re-implemented here.

DISPOSABLE ONLY. Provisions its own PostgreSQL 16 container and removes it on
every exit path. It never reads a configured DSN and therefore cannot be pointed
at production by accident.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.close_telematics_first_seen_pair_contract import (  # noqa: E402
    ACTION_CLOSE,
    ACTION_NONE,
    EXPAND_CONSTRAINT,
    LEDGER_ENTRY,
    STRICT_CONSTRAINT,
    STRICT_DEFINITION,
    ContractRefused,
    _assert_owns_client_trips,
    _client_owner_conn,
    _client_owner_dsn,
    close_contract,
    plan_action,
    readiness,
)
from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)

OWNER_ROLE = "owner_role"
CLIENT_ROLE = "client_runtime_role"
ROLE_PASSWORD = "disposable-not-a-secret"

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _connect(dsn: str, **kw):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(dsn, row_factory=dict_row, **kw)


def _role_dsn(port: int, database: str, role: str) -> str:
    return (
        f"host=127.0.0.1 port={port} dbname={database} "
        f"user={role} password={ROLE_PASSWORD}"
    )


def _client_row(port: int, database: str) -> Dict[str, Any]:
    """A registry row shaped exactly like `resolve_clients` returns."""
    return {
        "client_id": "00000000-0000-0000-0000-000000000001",
        "client_code": "DISPOSABLE01",
        "client_db_host": "127.0.0.1",
        "client_db_port": port,
        "client_db_name": database,
        "client_db_user": CLIENT_ROLE,
        "client_db_password_secret_ref": "unused-in-this-suite",
    }


def _provision(admin_dsn: str, database: str) -> None:
    """Build the PRODUCTION arrangement: owner owns the table, client does not."""
    with _connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for role in (OWNER_ROLE, CLIENT_ROLE):
                cur.execute(
                    f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'"
                )
            cur.execute(f'GRANT CONNECT ON DATABASE "{database}" TO "{OWNER_ROLE}"')
            cur.execute(f'GRANT CONNECT ON DATABASE "{database}" TO "{CLIENT_ROLE}"')
            # The owner is also the MIGRATION role — in production `ops/db_migrate.sh`
            # runs every client migration as this identity, so it necessarily holds
            # CREATE on schema public. `_record_ledger` relies on it: its first
            # statement is `CREATE TABLE IF NOT EXISTS public.schema_migrations`,
            # for the client that has never run a migration. Granting it here is
            # fixture FIDELITY, not fixture convenience — omit it and the suite
            # tests a role production does not have.
            cur.execute(f'GRANT CREATE ON SCHEMA public TO "{OWNER_ROLE}"')


def _build_open_state(admin_dsn: str, *, rows: int = 3) -> None:
    """A clean OPEN client_trips owned by OWNER_ROLE, readable by CLIENT_ROLE."""
    with _connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS public.client_trips")
            cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
            cur.execute(
                """
                CREATE TABLE public.client_trips (
                  id BIGSERIAL PRIMARY KEY,
                  first_seen_request_id TEXT,
                  first_seen_response_received_at_utc TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
                "CHECK (first_seen_response_received_at_utc IS NULL "
                "       OR first_seen_request_id IS NOT NULL) NOT VALID"
            )
            for n in range(rows):
                cur.execute(
                    "INSERT INTO public.client_trips"
                    "(first_seen_request_id, first_seen_response_received_at_utc)"
                    " VALUES (%s, now())",
                    (f"req-{n}",),
                )
            cur.execute(
                """
                CREATE TABLE public.schema_migrations (
                  filename TEXT PRIMARY KEY,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES "
                "('047_client_trips_first_seen_request_id.sql'),"
                "('048_client_trips_first_seen_response_received_at.sql')"
            )
            # OWNER owns both; the client role gets only what a runtime user has.
            cur.execute(f'ALTER TABLE public.client_trips OWNER TO "{OWNER_ROLE}"')
            cur.execute(f'ALTER TABLE public.schema_migrations OWNER TO "{OWNER_ROLE}"')
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{CLIENT_ROLE}"')
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON public.client_trips "
                f'TO "{CLIENT_ROLE}"'
            )
            cur.execute(
                f'GRANT SELECT ON public.schema_migrations TO "{CLIENT_ROLE}"'
            )


def _catalog(admin_dsn: str) -> Dict[str, Any]:
    with _connect(admin_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT conname, convalidated, pg_get_constraintdef(oid) AS def
                  FROM pg_constraint
                 WHERE conrelid = 'public.client_trips'::regclass
                   AND contype = 'c'
                """
            )
            constraints = {r["conname"]: r for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) AS n FROM public.schema_migrations WHERE filename=%s",
                (LEDGER_ENTRY,),
            )
            ledger = int(cur.fetchone()["n"])
    return {"constraints": constraints, "ledger": ledger}


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------

def run_suite(dsn: str, info: Dict[str, Any]) -> None:
    port = int(info["port"])
    database = str(info["database"])
    admin = dsn
    client = _client_row(port, database)

    _provision(admin, database)
    _build_open_state(admin)

    owner_dsn = _role_dsn(port, database, OWNER_ROLE)
    client_dsn = _role_dsn(port, database, CLIENT_ROLE)

    # --- 1. readiness still works through the per-client runtime identity ----
    with _connect(client_dsn) as cconn:
        with cconn.cursor() as ccur:
            state = readiness(ccur)
        cconn.rollback()
    _check(
        "readiness operates through the per-client runtime identity",
        state["ready"] is True and state["contract_state"] == "OPEN",
        f"ready={state['ready']} state={state['contract_state']}",
    )
    _check(
        "readiness read the ledger through the client role",
        state["ledger_recorded"] is False,
    )

    # --- 3. the client role genuinely cannot perform the DDL ----------------
    # The premise of the whole separation. If this ever stops raising, the
    # fixture has stopped reproducing production and every later assertion is
    # measuring nothing.
    ddl_refused = False
    detail = ""
    try:
        with _connect(client_dsn) as cconn:
            with cconn.cursor() as ccur:
                ccur.execute(
                    f"ALTER TABLE public.client_trips DROP CONSTRAINT "
                    f"IF EXISTS {EXPAND_CONSTRAINT}"
                )
            cconn.commit()
    except Exception as exc:  # noqa: BLE001 - the type is the assertion
        ddl_refused = "must be owner" in str(exc) or "permission denied" in str(exc)
        detail = f"{type(exc).__name__}: {exc}"
    _check(
        "the client runtime role cannot run the closure DDL",
        ddl_refused,
        detail or "the client role was allowed to ALTER the table",
    )

    # --- 4. owner-identity mismatch fails CLOSED, before any ALTER ----------
    refused_code = None
    try:
        with _connect(client_dsn) as cconn:
            _assert_owns_client_trips(cconn, client)
    except ContractRefused as exc:
        refused_code = str(exc)
    _check(
        "a non-owner session is refused before any DDL",
        refused_code is not None
        and "OWNER_CONNECTION_NOT_TABLE_OWNER" in str(refused_code),
        f"got {refused_code!r}",
    )
    after_mismatch = _catalog(admin)
    _check(
        "the refused mismatch changed nothing",
        EXPAND_CONSTRAINT in after_mismatch["constraints"]
        and STRICT_CONSTRAINT not in after_mismatch["constraints"]
        and after_mismatch["ledger"] == 0,
    )

    # --- 2. the owner connection resolves to the owner ----------------------
    os.environ["POSTGRES_USER"] = OWNER_ROLE
    os.environ["POSTGRES_PASSWORD"] = ROLE_PASSWORD
    built = _client_owner_dsn(client)
    _check(
        "the owner DSN is built from POSTGRES_USER, not the registry user",
        f"user={OWNER_ROLE}" in built and CLIENT_ROLE not in built,
        built.split("password=")[0],
    )
    owner_conn, identity = _client_owner_conn(client)
    _check(
        "the owner connection proves its identity server-side",
        identity["owner_session_user"] == OWNER_ROLE
        and identity["client_trips_owner"] == OWNER_ROLE,
        str(identity),
    )

    # --- 5. check-only plans no action, so no owner connection is opened ----
    plan_read_only = plan_action(state, execute=False)
    _check(
        "check-only plans ACTION_NONE and therefore opens no owner session",
        plan_read_only["action"] == ACTION_NONE
        and plan_read_only["result"] == "READY_NOT_EXECUTED",
        str(plan_read_only),
    )

    # --- 6. OPEN -> CLOSED through the owner session ------------------------
    plan_execute = plan_action(state, execute=True)
    _check(
        "an execute plan on a ready OPEN client is ACTION_CLOSE",
        plan_execute["action"] == ACTION_CLOSE,
        str(plan_execute),
    )
    with owner_conn:
        result = close_contract(owner_conn)
    closed = _catalog(admin)
    strict = closed["constraints"].get(STRICT_CONSTRAINT)
    _check(
        "the strict constraint exists after closure",
        strict is not None,
    )
    _check(
        "the strict constraint carries the canonical definition",
        strict is not None and str(strict["def"]).strip() == STRICT_DEFINITION,
        "" if strict is None else str(strict["def"]),
    )
    _check(
        "the strict constraint is VALIDATED",
        strict is not None and bool(strict["convalidated"]) is True,
    )
    _check(
        "the obsolete EXPAND constraint is gone",
        EXPAND_CONSTRAINT not in closed["constraints"],
    )
    # --- 8. ledger only after a validated physical CLOSED state -------------
    _check(
        "the closure ledger is recorded exactly once",
        closed["ledger"] == 1,
        f"ledger={closed['ledger']}",
    )
    _check(
        "close_contract reported the validated strict constraint",
        result.get("strict_constraint_validated") is True
        and result.get("ledger_entry") == LEDGER_ENTRY,
        str(result),
    )

    # --- readiness through the client role now reports CLOSED ---------------
    with _connect(client_dsn) as cconn:
        with cconn.cursor() as ccur:
            after = readiness(ccur)
        cconn.rollback()
    _check(
        "the client identity observes the CLOSED state it cannot itself create",
        after["contract_state"] == "CLOSED" and after["ledger_recorded"] is True,
        f"state={after['contract_state']} ledger={after['ledger_recorded']}",
    )

    # --- 7. a failed swap is atomic ----------------------------------------
    # Rebuild OPEN, then make the swap fail on its second statement by having a
    # strict constraint of the same name already present. The DROP in the same
    # transaction must not survive.
    _build_open_state(admin)
    with _connect(admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
                "CHECK ((first_seen_request_id IS NULL) "
                "       = (first_seen_response_received_at_utc IS NULL)) NOT VALID"
            )
    swap_failed = False
    owner_conn2, _ = _client_owner_conn(client)
    try:
        with owner_conn2:
            close_contract(owner_conn2)
    except Exception:  # noqa: BLE001 - the duplicate name is expected to raise
        swap_failed = True
    atomic = _catalog(admin)
    _check(
        "a swap that fails mid-transaction raises",
        swap_failed,
    )
    _check(
        "a failed swap leaves the EXPAND constraint in place (atomic rollback)",
        EXPAND_CONSTRAINT in atomic["constraints"],
        f"constraints={sorted(atomic['constraints'])}",
    )
    _check(
        "a failed swap records no ledger entry",
        atomic["ledger"] == 0,
    )


def test_argument_gates_refuse_before_any_connection() -> None:
    """G7 and its siblings still refuse, and refuse BEFORE any I/O.

    These run with no database at all — deliberately. Every one of these gates
    is reached before `_load_dotenv()`, before the DSN is built and before any
    connection is opened, so a suite that needed a live server to prove them
    would be proving something weaker than the code guarantees. Pointing the
    environment at an unreachable port makes that concrete: if any of these
    checks moved after the connect, the test would fail with a connection error
    instead of the expected refusal.
    """
    from ops.close_telematics_first_seen_pair_contract import build_parser, run

    os.environ["POSTGRES_HOST"] = "127.0.0.1"
    os.environ["POSTGRES_PORT"] = "1"  # nothing listens here
    os.environ["POSTGRES_DB"] = "unreachable"
    os.environ["POSTGRES_USER"] = "unreachable"
    os.environ["POSTGRES_PASSWORD"] = "unreachable"

    base = [
        "--execute",
        "--expected-environment", "production",
        "--expected-platform-uuid", "52517750-7438-4558-8490-2736ae4cc629",
    ]
    cases = [
        ("APPROVAL_REF_REQUIRED", base + ["--confirm", "CLOSE_CONTRACT",
                                          "--rollback-window-closed"]),
        ("CONFIRMATION_MISMATCH", base + ["--approval-ref", "T-1",
                                          "--rollback-window-closed"]),
        ("ROLLBACK_WINDOW_STILL_OPEN", base + ["--approval-ref", "T-1",
                                               "--confirm", "CLOSE_CONTRACT"]),
    ]
    for expected_code, argv in cases:
        got = None
        try:
            run(build_parser().parse_args(argv))
        except ContractRefused as exc:
            got = exc.code
        except Exception as exc:  # noqa: BLE001 - anything else is the failure
            got = f"{type(exc).__name__}: {exc}"
        _check(
            f"{expected_code} refuses before any connection is opened",
            got == expected_code,
            f"got {got!r}",
        )


def main() -> int:
    saved = {
        k: os.environ.get(k)
        for k in (
            "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST",
            "POSTGRES_PORT", "POSTGRES_DB",
        )
    }
    try:
        test_argument_gates_refuse_before_any_connection()
        with disposable_postgres(label="mlag-owner") as (dsn, info):
            print(
                f"disposable PostgreSQL {info['server_version']} "
                f"on 127.0.0.1:{info['port']} db={info['database']}"
            )
            run_suite(dsn, info)
    except DisposablePostgresUnavailable as exc:
        print(f"SKIP: {exc}")
        return 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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
