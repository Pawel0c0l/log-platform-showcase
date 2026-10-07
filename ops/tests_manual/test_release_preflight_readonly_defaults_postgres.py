#!/usr/bin/env python3
"""The PRODUCTION preflight connections are read-only, proved on the real ones.

Run (self-provisioning; needs Docker and the local `postgres:16` image):
    python3 ops/tests_manual/test_release_preflight_readonly_defaults_postgres.py

Or against an already-running disposable instance:
    RELEASE_PREFLIGHT_READONLY_TEST_DSN=postgresql://u:p@127.0.0.1:5433/disposable \\
      python3 ops/tests_manual/test_release_preflight_readonly_defaults_postgres.py

THE DEFECT THIS CLOSES.
    `test_release_schema_preflight_postgres.py::test_B11_the_gate_mutates_nothing`
    proved the gate cannot write — on connections THE TEST built, with
    `default_transaction_read_only=on` passed by the test's own
    `_readonly_connect`. `default_platform_conn` and `default_client_conn`, the
    factories `manage_release.py activate` actually uses, called
    `psycopg.connect(dsn)` with no such option. So the invariant held for the
    injected path and for nothing else: every production release preflight ran
    on a session PostgreSQL would have allowed to write. Independent review
    classified it BLOCKING, and the reason it survived a passing suite is
    exactly that the suite never exercised the default factories.

WHAT IS PROVED HERE, AND WHY IT NEEDS A REAL SERVER.
    "The session is read-only" is a statement about PostgreSQL, not about
    Python. A unit test can prove which keyword argument was passed; only the
    server can prove what it does with it. So the factories under test are the
    real ones — no `_readonly_connect`, no injected stand-in — pointed at a
    disposable instance through the same `POSTGRES_*` environment a production
    activation reads, and the writes attempted against them are assembled at
    runtime so no literal-SQL scan could have caught them either.

    The last case is the load-bearing one: a preflight step is temporarily
    replaced by one that issues a runtime-assembled `UPDATE`, and
    `verify_schema_prerequisites` is then called with NO factory arguments at
    all. If the defaults were writable again, that mutation would succeed. It
    is run twice — once on the platform leg, once on a client leg — because the
    two connections are opened by different factories.

THE FENCE IS DELIBERATELY NOT INCLUDED. `activation_fence` and
`schema_transition_lock` take `default_fence_conn`, which is not read-only:
their contract is locking, not inspection. That separation is asserted here too,
because "make everything read-only" would have been the wrong correction.

DESTRUCTIVE, AND ONLY WITHIN ITS OWN INSTANCE. It creates and drops its own
databases. With no DSN supplied it starts a task-owned PostgreSQL 16 container
with a tmpfs data directory and removes it in `finally`. It applies no
repository migration, touches no persistent database, activates no release,
sends no e-mail and reads no credential.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops import release_schema_preflight as preflight  # noqa: E402
from ops.release_schema_preflight import (  # noqa: E402
    READ_ONLY_SESSION_OPTIONS,
    SCHEMA_REQUIREMENTS_RELPATH,
    AffectedClient,
    activation_fence,
    default_client_conn,
    default_fence_conn,
    default_platform_conn,
    enumerate_affected_clients,
    fleet_fingerprint,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

ENV = "RELEASE_PREFLIGHT_READONLY_TEST_DSN"

#: SQLSTATE 25006 — `read_only_sql_transaction`. The server's own refusal.
READ_ONLY_SQLSTATE = "25006"

PLATFORM_MIGRATION = "900_readonly_probe_platform.sql"
CLIENT_MIGRATION = "901_readonly_probe_client.sql"
PLATFORM_SCHEMA = "workflow_a_control"
PLATFORM_TABLE = "readonly_probe"
CLIENT_SCHEMA = "public"
CLIENT_TABLE = "readonly_probe_client"
CLIENT_DBS = ("ro_pf_client_0", "ro_pf_client_1")

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# 0. NO DATABASE — what the factories ask libpq for
# ---------------------------------------------------------------------------

def factory_parameters() -> None:
    """Which connection each factory builds, without connecting to anything.

    Cheap and genuinely necessary: it pins the SEPARATION. The server evidence
    below proves the inspection sessions are read-only, but only this shows that
    the fence factory is a distinct function that deliberately does not carry
    the option — which is the part a well-meaning future edit would collapse.
    """
    print("\n### FACTORY PARAMETERS — no database")
    recorded: List[Dict[str, Any]] = []

    class _FakeConn:
        pass

    class _FakePsycopg:
        @staticmethod
        def connect(conninfo, **kwargs):
            recorded.append({"conninfo": conninfo, "kwargs": kwargs})
            return _FakeConn()

    real = preflight._require_psycopg
    preflight._require_psycopg = lambda: _FakePsycopg  # type: ignore[assignment]
    try:
        default_platform_conn()
        default_client_conn(AffectedClient(
            client_id=str(uuid.uuid4()), client_code="AAA00001",
            client_name="AAA00001", db_host="127.0.0.1", db_port=5432,
            db_name="probe_db",
        ))
        default_fence_conn()
    finally:
        preflight._require_psycopg = real  # type: ignore[assignment]

    platform, client, fence = recorded
    _check("default_platform_conn passes the read-only startup option",
           platform["kwargs"].get("options") == READ_ONLY_SESSION_OPTIONS,
           str(platform["kwargs"]))
    _check("default_client_conn passes the read-only startup option",
           client["kwargs"].get("options") == READ_ONLY_SESSION_OPTIONS,
           str(client["kwargs"]))
    _check("the read-only option is default_transaction_read_only=on",
           READ_ONLY_SESSION_OPTIONS == "-c default_transaction_read_only=on",
           READ_ONLY_SESSION_OPTIONS)
    _check("default_client_conn targets the fleet-declared client database",
           "dbname=probe_db" in client["conninfo"], client["conninfo"])
    _check("default_fence_conn carries NO read-only option (locking session)",
           "options" not in fence["kwargs"], str(fence["kwargs"]))
    _check("the fence uses its own factory, not the inspection one",
           _default_of(activation_fence, "platform_conn_factory")
           is default_fence_conn)
    _check("schema_transition_lock uses the fence factory",
           _default_of(preflight.schema_transition_lock,
                       "platform_conn_factory") is default_fence_conn)
    _check("the fence still inspects client state read-only",
           _default_of(activation_fence, "client_conn_factory")
           is default_client_conn)
    _check("verify_schema_prerequisites defaults to the inspection factories",
           _default_of(verify_schema_prerequisites, "platform_conn_factory")
           is default_platform_conn
           and _default_of(verify_schema_prerequisites, "client_conn_factory")
           is default_client_conn)


def _default_of(func, parameter: str):
    """The DEFAULT bound to a keyword parameter, through any decorator."""
    import inspect
    target = inspect.unwrap(func)
    if hasattr(target, "__wrapped__"):
        target = target.__wrapped__
    return inspect.signature(target).parameters[parameter].default


# ---------------------------------------------------------------------------
# Fixture: a real fleet in the disposable instance
# ---------------------------------------------------------------------------

def _psycopg():
    import psycopg
    return psycopg


def _admin(dsn: str):
    return _psycopg().connect(dsn, autocommit=True)


def _writable(dsn: str):
    return _psycopg().connect(dsn)


def _export_production_environment(dsn: str) -> None:
    """Point the PRODUCTION factories at the disposable instance.

    The whole suite depends on this: `default_platform_conn` reads `POSTGRES_*`,
    so exporting them is what makes the real factory connect somewhere it is
    safe to be refused by. The loopback guard has already run on this DSN, and
    the instance is one this process created.
    """
    parts = urlsplit(dsn)
    os.environ["POSTGRES_HOST"] = parts.hostname or "127.0.0.1"
    os.environ["POSTGRES_PORT"] = str(parts.port or 5432)
    os.environ["POSTGRES_DB"] = (parts.path or "/").lstrip("/")
    os.environ["POSTGRES_USER"] = parts.username or ""
    os.environ["POSTGRES_PASSWORD"] = parts.password or ""


def build_fleet(dsn: str) -> None:
    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = int(parts.port or 5432)

    admin = _admin(dsn)
    for name in CLIENT_DBS:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()

    conn = _writable(dsn)
    conn.execute(f"DROP SCHEMA IF EXISTS {PLATFORM_SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {PLATFORM_SCHEMA}")
    conn.execute(
        f"""CREATE TABLE {PLATFORM_SCHEMA}.client_account (
              client_id UUID PRIMARY KEY,
              client_code TEXT,
              client_name TEXT,
              enabled BOOLEAN NOT NULL,
              client_db_host TEXT NOT NULL,
              client_db_port INT NOT NULL,
              client_db_name TEXT NOT NULL)"""
    )
    for index, name in enumerate(CLIENT_DBS):
        conn.execute(
            f"INSERT INTO {PLATFORM_SCHEMA}.client_account "
            "(client_id, client_code, client_name, enabled, client_db_host, "
            " client_db_port, client_db_name) VALUES (%s,%s,%s,true,%s,%s,%s)",
            (str(uuid.UUID(int=index + 1)), f"RO{index:06d}",
             f"RO{index:06d}", host, port, name),
        )
    # The platform side of the synthetic release requirement.
    conn.execute(
        f"""CREATE TABLE {PLATFORM_SCHEMA}.{PLATFORM_TABLE} (
              probe_id UUID PRIMARY KEY,
              label TEXT NOT NULL)"""
    )
    conn.execute(
        f"INSERT INTO {PLATFORM_SCHEMA}.{PLATFORM_TABLE} VALUES (%s, %s)",
        (str(uuid.UUID(int=99)), "before"),
    )
    _create_ledger(conn, PLATFORM_MIGRATION)
    conn.commit()
    conn.close()

    base = dsn.rsplit("/", 1)[0]
    for name in CLIENT_DBS:
        client_conn = _writable(f"{base}/{name}")
        client_conn.execute(
            f"""CREATE TABLE {CLIENT_SCHEMA}.{CLIENT_TABLE} (
                  probe_id UUID PRIMARY KEY,
                  label TEXT NOT NULL)"""
        )
        client_conn.execute(
            f"INSERT INTO {CLIENT_SCHEMA}.{CLIENT_TABLE} VALUES (%s, %s)",
            (str(uuid.UUID(int=99)), "before"),
        )
        _create_ledger(client_conn, CLIENT_MIGRATION)
        client_conn.commit()
        client_conn.close()


def _create_ledger(conn, migration: str) -> None:
    conn.execute(
        "CREATE TABLE public.schema_migrations ("
        "filename TEXT PRIMARY KEY, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    conn.execute(
        "INSERT INTO public.schema_migrations (filename) VALUES (%s)",
        (migration,),
    )


def write_release_tree(root: Path) -> Path:
    """A release declaring one platform and one client requirement, both true."""
    tree = root / "release"
    (tree / "db").mkdir(parents=True, exist_ok=True)
    document = {
        "version": preflight.SCHEMA_REQUIREMENTS_VERSION,
        "_comment": [
            "Synthetic. Exists so the DEFAULT factories are driven against "
            "both a platform and a client database in one preflight."
        ],
        "requirements": [
            {
                "migration": PLATFORM_MIGRATION,
                "scope": "platform",
                "milestone": "RO",
                "reason": "drives the default platform inspection connection",
                "relations": [{
                    "schema": PLATFORM_SCHEMA,
                    "table": PLATFORM_TABLE,
                    "columns": [{"name": "probe_id", "type": "uuid",
                                 "nullable": False}],
                }],
            },
            {
                "migration": CLIENT_MIGRATION,
                "scope": "client_business",
                "milestone": "RO",
                "reason": "drives the default client inspection connection",
                "relations": [{
                    "schema": CLIENT_SCHEMA,
                    "table": CLIENT_TABLE,
                    "columns": [{"name": "probe_id", "type": "uuid",
                                 "nullable": False}],
                }],
            },
        ],
    }
    (tree / SCHEMA_REQUIREMENTS_RELPATH).write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    return tree


# ---------------------------------------------------------------------------
# Helpers for the write attempts
# ---------------------------------------------------------------------------

def _sqlstate(exc: BaseException) -> Optional[str]:
    return getattr(exc, "sqlstate", None)


def _refused_by_server(conn, statement: str, params=None) -> Dict[str, Any]:
    """Execute and report how PostgreSQL answered. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
        return {"refused": False, "sqlstate": None, "error": None}
    except Exception as exc:  # noqa: BLE001 - the answer IS the result
        state = _sqlstate(exc)
        return {"refused": True, "sqlstate": state,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with contextlib.suppress(Exception):
            conn.rollback()


def _fleet_clients() -> List[AffectedClient]:
    conn = default_platform_conn()
    try:
        with conn.cursor() as cur:
            affected, _count = enumerate_affected_clients(cur)
        conn.rollback()
    finally:
        conn.close()
    return affected


def _session_read_only(conn) -> Dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        default_ro = str(cur.fetchone()[0])
        cur.execute("SHOW transaction_read_only")
        current_ro = str(cur.fetchone()[0])
    return {"default": default_ro, "current": current_ro}


# ---------------------------------------------------------------------------
# 1. The default sessions are read-only, at the server
# ---------------------------------------------------------------------------

def default_sessions_are_read_only() -> None:
    print("\n### DEFAULT SESSIONS — PostgreSQL reports read-only")
    conn = default_platform_conn()
    try:
        state = _session_read_only(conn)
        _check("default_platform_conn: default_transaction_read_only = on",
               state["default"] == "on", str(state))
        _check("default_platform_conn: the live transaction is read-only",
               state["current"] == "on", str(state))
        # An explicit transaction does not reset it: the option is a session
        # GUC applied at startup, and BEGIN inherits it.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SHOW transaction_read_only")
            in_txn = str(cur.fetchone()[0])
            cur.execute("ROLLBACK")
        _check("an explicit BEGIN inherits the read-only mode", in_txn == "on",
               in_txn)
    finally:
        conn.close()

    clients = _fleet_clients()
    _check("the fixture fleet enumerates both clients", len(clients) == 2,
           str([c.db_name for c in clients]))
    for client in clients:
        conn = default_client_conn(client)
        try:
            state = _session_read_only(conn)
            _check(f"default_client_conn({client.db_name}): read-only session",
                   state["default"] == "on" and state["current"] == "on",
                   str(state))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. Runtime-assembled writes are refused by PostgreSQL
# ---------------------------------------------------------------------------

def _mutations(schema: str, table: str) -> List[tuple]:
    """Every statement assembled at RUNTIME, never present as a literal.

    Split and rejoined on purpose: a scan over this file's source finds no
    `UPDATE`, `INSERT`, `DELETE`, `CREATE TABLE`, `ALTER TABLE` or `DROP TABLE`
    literal to match, so what the server refuses below is exactly the class of
    statement a literal scan cannot see.
    """
    qualified = f"{schema}.{table}"
    row = str(uuid.UUID(int=99))
    return [
        ("UPDATE",
         "UP" + "DATE " + qualified + " SET label = 'mutated' WHERE probe_id = "
         f"'{row}'"),
        ("INSERT",
         "INS" + "ERT INTO " + qualified + " (probe_id, label) VALUES "
         f"('{uuid.uuid4()}', 'mutated')"),
        ("DELETE",
         "DEL" + "ETE FROM " + qualified + f" WHERE probe_id = '{row}'"),
        ("DDL CREATE",
         "CRE" + "ATE TA" + "BLE " + schema + ".readonly_escape_" +
         uuid.uuid4().hex[:8] + " (id int)"),
        ("DDL ALTER",
         "AL" + "TER TA" + "BLE " + qualified + " ADD COL" + "UMN escape int"),
        ("DDL DROP",
         "DR" + "OP TA" + "BLE " + qualified),
        ("DDL TRUNCATE",
         "TRUN" + "CATE " + qualified),
    ]


def writes_are_refused() -> None:
    print("\n### RUNTIME-ASSEMBLED WRITES — refused by the server")
    conn = default_platform_conn()
    try:
        for kind, statement in _mutations(PLATFORM_SCHEMA, PLATFORM_TABLE):
            result = _refused_by_server(conn, statement)
            _check(f"platform session refuses {kind} (SQLSTATE 25006)",
                   result["refused"]
                   and result["sqlstate"] == READ_ONLY_SQLSTATE,
                   str(result))
    finally:
        conn.close()

    client = _fleet_clients()[0]
    conn = default_client_conn(client)
    try:
        for kind, statement in _mutations(CLIENT_SCHEMA, CLIENT_TABLE):
            result = _refused_by_server(conn, statement)
            _check(f"client session refuses {kind} (SQLSTATE 25006)",
                   result["refused"]
                   and result["sqlstate"] == READ_ONLY_SQLSTATE,
                   str(result))
    finally:
        conn.close()

    # And nothing was written by any of the attempts above.
    conn = default_platform_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT label FROM {PLATFORM_SCHEMA}.{PLATFORM_TABLE} "
                "WHERE probe_id = %s", (str(uuid.UUID(int=99)),))
            label = str(cur.fetchone()[0])
        conn.rollback()
    finally:
        conn.close()
    _check("the platform probe row is unchanged", label == "before", label)


def reads_still_work() -> None:
    """A read-only session is useless if it cannot do the gate's actual job."""
    print("\n### READS — ordinary SELECT and catalog inspection still work")
    conn = default_platform_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {PLATFORM_SCHEMA}.{PLATFORM_TABLE}")
            rows = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM pg_attribute a JOIN pg_class c "
                "ON c.oid = a.attrelid WHERE c.relname = %s AND a.attnum > 0",
                (PLATFORM_TABLE,))
            attributes = int(cur.fetchone()[0])
            cur.execute("SELECT to_regclass(%s)",
                        (f"{PLATFORM_SCHEMA}.{PLATFORM_TABLE}",))
            regclass = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    _check("a plain SELECT succeeds on the read-only session", rows == 1,
           str(rows))
    _check("catalog inspection succeeds on the read-only session",
           attributes >= 2, str(attributes))
    _check("to_regclass resolves on the read-only session",
           regclass is not None, str(regclass))


# ---------------------------------------------------------------------------
# 3. The DEFAULT preflight path, end to end
# ---------------------------------------------------------------------------

def default_path_verifies_the_fleet(tree: Path) -> None:
    """`verify_schema_prerequisites` with NO factory arguments at all."""
    print("\n### DEFAULT PREFLIGHT PATH — no injected factory anywhere")
    report = verify_schema_prerequisites(
        release_tree=tree, release_id="readonly-defaults",
    )
    _check("the default path reached the platform database",
           len(report.platform_checks) == 1
           and report.platform_checks[0]["ledger_recorded"] is True
           and report.platform_checks[0]["physical_defects"] == [],
           str(report.platform_checks))
    _check("the default path reached EVERY client database",
           len(report.client_checks) == 2
           and all(c["ledger_recorded"] is True
                   and c["physical_defects"] == []
                   for c in report.client_checks),
           str(report.client_checks))
    _check("the default path enumerated the whole fleet",
           report.affected_client_count == 2
           and report.enabled_account_count == 2,
           str(report.as_dict()["affected_client_count"]))


def default_path_cannot_write(tree: Path) -> None:
    """The recurrence detector: make the gate try to write, on the REAL defaults.

    A preflight step is replaced by one that assembles a mutating statement at
    runtime and executes it on whichever cursor the gate handed it. Run once for
    the platform leg and once for a client leg, because those cursors come from
    two different factories. If either default were writable again, the mutation
    would commit and the run would simply pass.
    """
    print("\n### RECURRENCE — a mutating preflight step is refused on defaults")
    real = preflight.ledger_has_migration
    calls: Dict[str, int] = {"n": 0}

    def _make_mutating(target_call: int, schema: str, table: str):
        def _mutating(cur, filename: str) -> bool:
            index = calls["n"]
            calls["n"] += 1
            if index == target_call:
                cur.execute(
                    "UP" + "DATE " + f"{schema}.{table}" +
                    " SET label = 'escaped'"
                )
            return real(cur, filename)
        return _mutating

    for label, target_call, schema, table in (
        ("platform", 0, PLATFORM_SCHEMA, PLATFORM_TABLE),
        ("client", 1, CLIENT_SCHEMA, CLIENT_TABLE),
    ):
        calls["n"] = 0
        preflight.ledger_has_migration = _make_mutating(  # type: ignore[assignment]
            target_call, schema, table)
        try:
            verify_schema_prerequisites(
                release_tree=tree, release_id="readonly-escape",
            )
            _check(f"a runtime-assembled UPDATE on the {label} leg is refused",
                   False, "the mutation was ACCEPTED by the default session")
        except Exception as exc:  # noqa: BLE001
            _check(f"a runtime-assembled UPDATE on the {label} leg is refused",
                   _sqlstate(exc) == READ_ONLY_SQLSTATE,
                   f"{type(exc).__name__}: {exc} (sqlstate={_sqlstate(exc)})")
        finally:
            preflight.ledger_has_migration = real  # type: ignore[assignment]

    # Nothing escaped: both probe rows still read `before`.
    conn = default_platform_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT label FROM {PLATFORM_SCHEMA}.{PLATFORM_TABLE}")
            platform_label = str(cur.fetchone()[0])
        conn.rollback()
    finally:
        conn.close()
    client = _fleet_clients()[0]
    conn = default_client_conn(client)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT label FROM {CLIENT_SCHEMA}.{CLIENT_TABLE}")
            client_label = str(cur.fetchone()[0])
        conn.rollback()
    finally:
        conn.close()
    _check("no platform row was mutated by the escape attempt",
           platform_label == "before", platform_label)
    _check("no client row was mutated by the escape attempt",
           client_label == "before", client_label)


# ---------------------------------------------------------------------------
# 4. The activation fence keeps its own, separate contract
# ---------------------------------------------------------------------------

def fence_contract_is_separate() -> None:
    print("\n### ACTIVATION FENCE — separate connection, unchanged contract")
    conn = default_fence_conn()
    try:
        state = _session_read_only(conn)
        _check("default_fence_conn is NOT a read-only session",
               state["default"] == "off", str(state))
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)",
                        (preflight.SCHEMA_TRANSITION_LOCK_KEY,))
            cur.fetchone()
            cur.execute(
                f"LOCK TABLE {preflight.FLEET_RELATION} IN SHARE MODE")
            locked = True
        conn.rollback()
    finally:
        conn.close()
    _check("the fence session can take its advisory and SHARE locks", locked)

    clients = _fleet_clients()
    expected = fleet_fingerprint(clients, len(clients))
    with activation_fence(expected_fingerprint=expected,
                          declared_capabilities=frozenset()) as observed:
        _check("the fence re-establishes the same fleet under its locks",
               observed == expected, f"{observed} != {expected}")
    # The fence rolled back and closed: nothing is left holding a lock.
    conn = _writable(os.environ[ENV])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid = %s",
                (preflight.SCHEMA_TRANSITION_LOCK_KEY % (2 ** 32),))
            advisory = int(cur.fetchone()[0])
        conn.rollback()
    finally:
        conn.close()
    _check("the fence left no advisory lock behind", advisory == 0,
           str(advisory))


# ---------------------------------------------------------------------------

def _run_all(dsn: str) -> None:
    os.environ[ENV] = dsn
    _export_production_environment(dsn)
    build_fleet(dsn)
    with tempfile.TemporaryDirectory(prefix="ro-preflight-") as tmp:
        tree = write_release_tree(Path(tmp))
        default_sessions_are_read_only()
        writes_are_refused()
        reads_still_work()
        default_path_verifies_the_fleet(tree)
        default_path_cannot_write(tree)
        fence_contract_is_separate()


def main() -> int:
    print("=" * 78)
    print("RELEASE SCHEMA PREFLIGHT — PRODUCTION DEFAULTS ARE READ-ONLY")
    print("=" * 78)

    factory_parameters()

    dsn = os.environ.get(ENV)
    if dsn:
        require_loopback_dsn_or_exit(dsn, label=ENV)
        if "logdb" in dsn.lower():
            raise RuntimeError("refusing a production-like DSN")
        _run_all(dsn)
    else:
        from ops.tests_manual.disposable_postgres import (
            DisposablePostgresUnavailable, disposable_postgres,
        )
        try:
            with disposable_postgres(label="ropf") as (provisioned, info):
                print(f"\ndisposable PostgreSQL {info['server_version']} "
                      f"on 127.0.0.1:{info['port']}")
                require_loopback_dsn_or_exit(provisioned, label="disposable")
                _run_all(provisioned)
        except DisposablePostgresUnavailable as exc:
            print(f"SKIP: no disposable PostgreSQL available ({exc}); "
                  f"set {ENV} to a loopback DSN instead.")
            return 1 if _failures else 0

    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("ALL READ-ONLY DEFAULT-FACTORY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
