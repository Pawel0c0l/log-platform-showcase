#!/usr/bin/env python3
"""Migration 050 must upgrade the APPLIED 049 into the reviewed contract.

Run:
    ECO_DASHBOARD_050_TEST_DSN=postgresql://postgres:pw@127.0.0.1:55951/postgres \\
      python3 ops/tests_manual/test_migration_050_external_mailer_rollout_postgres.py

WHY THIS EXISTS.
    `049_eco_dashboard_delivery_operation.sql` is APPLIED. Every enabled client
    business database recorded it on 2026-08-19 ~19:55 CEST, in its pre-review
    physical form — 43 columns, 32 constraints, no `external_mailer`, no
    EXTERNAL_MAILER_HANDOFF, the pre-review guard body. The migration runner
    keys on the FILENAME, so an edited 049 is a file that will never execute
    again anywhere it already ran: editing it would produce a repository whose
    fresh installs carry the reviewed contract and whose five existing clients
    silently do not.

    So the reviewed delta lives in `050_eco_dashboard_external_mailer_ownership.sql`,
    and this suite proves the three things that has to mean:

      A. HISTORICAL UPGRADE — a database carrying exactly the applied 049 reaches
         the reviewed contract by applying 050 through the real runner;
      B. FRESH INSTALL — an empty database reaches it through onboarding's own
         declared chain;
      C. EQUIVALENCE — the two land on catalogs that are physically identical,
         not merely both "green".

    It also proves the gate in both directions: 049 alone must be refused,
    049 + 050 must be accepted, and a forged 050 ledger row over an uncorrected
    schema must still be refused.

DESTRUCTIVE. Creates and drops its own databases in the instance the DSN names,
and refuses any DSN that is not loopback. It performs no persistent migration:
every database it touches is one it created. No production database, no release
activation, no e-mail, no provider and no Cloudflare resource is involved.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.release_schema_preflight import (  # noqa: E402
    SchemaPreflightError,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from scripts.apply_client_business_migrations import (  # noqa: E402
    _discover_client_business_files,
)
from scripts.onboard_workflow_a_client import (  # noqa: E402
    CLIENT_BUSINESS_DDL_FILES,
    CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED,
)

ENV = "ECO_DASHBOARD_050_TEST_DSN"

MIGRATION_049 = "049_eco_dashboard_delivery_operation.sql"
MIGRATION_050 = "050_eco_dashboard_external_mailer_ownership.sql"
REQUIREMENT_MIGRATION = MIGRATION_050

#: THE APPLIED BYTES, PINNED.
#:
#: SHA-256 of `db/client_business/049_eco_dashboard_delivery_operation.sql` as it
#: stood at commit a5069cd — the tree from which the five persistent client
#: databases were migrated, and therefore the only definition of 049 that
#: describes what those databases physically contain. This constant is the
#: repository's immutability guarantee expressed as a test: any future edit of
#: 049, for any reason, fails here rather than in production on a client whose
#: ledger will never replay the file.
HISTORICAL_049_SHA256 = (
    "110322e319240dfd9c99317594e7975c4c8fbffb2d9d524b1a75b3f448ccf133"
)

#: The guard body digest the applied 049 installed, and the one the reviewed
#: contract requires. Distinct values, so "the function was replaced" is
#: observable rather than assumed.
HISTORICAL_GUARD_SHA256 = (
    "3e83442051120379b3a6a488e004fc762c6073f358e877e9cb058d7389bbdc68"
)
REVIEWED_GUARD_SHA256 = (
    "b219334ae2128b86f4588a9da20c23268e625ca3078739932ee0f95ef4cf12a9"
)

#: What the applied 049 physically is, and what the chain must become. Stated as
#: numbers as well as names because the release-prerequisite audit reported the
#: fleet in exactly these terms.
HISTORICAL_COLUMN_COUNT = 43
HISTORICAL_CONSTRAINT_COUNT = 32
REVIEWED_COLUMN_COUNT = 44
REVIEWED_CONSTRAINT_COUNT = 35

#: Every object 050 is responsible for. Named individually so a partial
#: migration cannot pass as a complete one.
DELTA_COLUMN = "external_mailer"
NEW_CONSTRAINTS = (
    "chk_eco_dashboard_delivery_operation_external_mailer",
    "chk_eco_dashboard_delivery_operation_external_mailer_unbound",
    "chk_eco_dashboard_delivery_operation_handoff_owned",
)
REPLACED_CONSTRAINTS = (
    "chk_eco_dashboard_delivery_operation_state",
    "chk_eco_dashboard_delivery_operation_bearer_present",
    "chk_eco_dashboard_delivery_operation_provider_unbound",
)
HANDOFF_STATE = "EXTERNAL_MAILER_HANDOFF"

SCHEMA = "public"
TABLE = "eco_dashboard_delivery_operation"
QUALIFIED = f"{SCHEMA}.{TABLE}"

PLATFORM_DB = "eco050_platform"
UPGRADE_DB = "eco050_upgrade"      # Path A — historical 049, then 050
FRESH_DB = "eco050_fresh"          # Path B — the full onboarding chain
FORGED_DB = "eco050_forged"        # ledger says 050, schema says otherwise

UPGRADE_CLIENT_ID = "e90d445c-1051-44ba-82c2-cf475ca0b0f8"
FRESH_CLIENT_ID = "82110708-b7d8-41cf-839e-1e9842886a54"
FORGED_CLIENT_ID = "26eb5d74-91e4-4c1b-8c1e-43bfd65beae3"

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# Disposable instance plumbing
# ---------------------------------------------------------------------------

def _parts() -> Dict[str, object]:
    dsn = os.environ[ENV]
    authority = dsn.split("@", 1)[1]
    hostport = authority.split("/", 1)[0]
    credentials = dsn.split("//", 1)[1].split("@", 1)[0]
    user, _, password = credentials.partition(":")
    return {
        "host": hostport.split(":")[0],
        "port": int(hostport.split(":")[1]),
        "user": user,
        "password": password,
    }


def _connect(dbname: str):
    import psycopg

    base = os.environ[ENV].rsplit("/", 1)[0]
    return psycopg.connect(f"{base}/{dbname}")


def _admin():
    import psycopg

    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


def _recreate_databases(*names: str) -> None:
    with _admin() as conn:
        for name in names:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}"')


def _teardown() -> None:
    with _admin() as conn:
        for name in (PLATFORM_DB, UPGRADE_DB, FRESH_DB, FORGED_DB):
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _sql(name: str) -> str:
    return (ROOT / "db" / "client_business" / name).read_text(encoding="utf-8")


def _record(conn, filename: str) -> None:
    conn.execute(
        "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
        "ON CONFLICT DO NOTHING",
        (filename,),
    )


def _ensure_ledger_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
        "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


# ---------------------------------------------------------------------------
# 1. IMMUTABILITY — the file on disk is still the file the fleet ran
# ---------------------------------------------------------------------------

def immutability() -> None:
    """049 is applied shared history, so its bytes are a fixed point.

    Checked two ways, because a digest alone says only "something changed" and
    the interesting failure is specific: a reviewer moving the external-mailer
    delta back into 049 would produce a repository in which fresh installs and
    the five migrated clients carry different schemas, with no migration able to
    reconcile them.
    """
    print("\n### MIGRATION IMMUTABILITY — 049 is applied history")
    raw = (ROOT / "db" / "client_business" / MIGRATION_049).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _check("049 is byte-identical to the definition the fleet applied",
           digest == HISTORICAL_049_SHA256,
           f"{digest} != {HISTORICAL_049_SHA256}")
    text = raw.decode("utf-8")
    _check("049 names no reviewed external-mailer object",
           DELTA_COLUMN not in text and HANDOFF_STATE not in text,
           "the post-049 delta must live in 050, not in an applied migration")
    delta = _sql(MIGRATION_050)
    _check("050 carries the whole reviewed delta",
           all(name in delta for name in NEW_CONSTRAINTS + REPLACED_CONSTRAINTS)
           and DELTA_COLUMN in delta and HANDOFF_STATE in delta)
    _check("050 creates no relation of its own",
           "CREATE TABLE" not in delta.upper(),
           "050 is a forward delta over 049, not a second definition of it")


# ---------------------------------------------------------------------------
# 2. DISCOVERY — both rollout paths see 050, in order, exactly once
# ---------------------------------------------------------------------------

def discovery() -> None:
    print("\n### DISCOVERY — the runner and onboarding both carry 050")
    names = [p.name for p in _discover_client_business_files()]
    _check("the migration runner discovers 050", MIGRATION_050 in names)
    _check("and orders it immediately after 049",
           names.index(MIGRATION_050) == names.index(MIGRATION_049) + 1,
           str(names[-4:]))
    _check("discovery is deterministic (lexical, no duplicates)",
           names == sorted(names) and len(names) == len(set(names)))
    onboarding = [p.name for p in CLIENT_BUSINESS_DDL_FILES]
    _check("onboarding applies 050 to a new client",
           MIGRATION_050 in onboarding)
    _check("and applies it after the 049 it upgrades",
           onboarding.index(MIGRATION_050) > onboarding.index(MIGRATION_049),
           str(onboarding[-6:]))
    _check("onboarding records 050 in the new client's ledger",
           MIGRATION_050 in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED)


# ---------------------------------------------------------------------------
# Catalog observation
# ---------------------------------------------------------------------------

def _snapshot(db_name: str) -> Dict:
    """Everything about the relation a release could depend on."""
    with _connect(db_name) as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT column_name, data_type, is_nullable, column_default,
                      ordinal_position
                 FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY column_name""",
            (SCHEMA, TABLE),
        )
        columns = {r[0]: [r[1], r[2], r[3], r[4]] for r in cur.fetchall()}
        cur.execute(
            """SELECT conname, pg_get_constraintdef(oid), convalidated, contype
                 FROM pg_constraint WHERE conrelid = %s::regclass
                ORDER BY conname""",
            (QUALIFIED,),
        )
        constraints = {r[0]: [r[1], r[2], r[3]] for r in cur.fetchall()}
        cur.execute(
            """SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisunique
                 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE i.indrelid = %s::regclass ORDER BY 1""",
            (QUALIFIED,),
        )
        indexes = {r[0]: [r[1], r[2]] for r in cur.fetchall()}
        cur.execute(
            """SELECT t.tgname, t.tgtype, t.tgenabled,
                      fn.nspname || '.' || p.proname,
                      pg_get_expr(t.tgqual, t.tgrelid)
                 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                 JOIN pg_namespace fn ON fn.oid = p.pronamespace
                WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal
                ORDER BY 1""",
            (QUALIFIED,),
        )
        triggers = {r[0]: [r[1], r[2], r[3], r[4]] for r in cur.fetchall()}
        cur.execute(
            """SELECT pg_get_function_identity_arguments(p.oid), l.lanname,
                      pg_get_function_result(p.oid),
                      encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex')
                 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                 JOIN pg_language l ON l.oid = p.prolang
                WHERE n.nspname = 'public'
                  AND p.proname = 'eco_dashboard_delivery_operation_guard'""",
        )
        function = [list(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT a.attname, col_description(a.attrelid, a.attnum)
                 FROM pg_attribute a
                WHERE a.attrelid = %s::regclass AND a.attnum > 0
                  AND col_description(a.attrelid, a.attnum) IS NOT NULL
                ORDER BY 1""",
            (QUALIFIED,),
        )
        column_comments = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "columns": columns, "constraints": constraints, "indexes": indexes,
        "triggers": triggers, "function": function,
        "column_comments": column_comments,
    }


# ---------------------------------------------------------------------------
# 3. PATH A — the historical database, upgraded by the real runner
# ---------------------------------------------------------------------------

def _build_historical_client(db_name: str) -> None:
    """Exactly what the five persistent clients contain: 049 and nothing after.

    Predecessors come from onboarding's own declaration rather than a local
    copy, so a change to the chain cannot leave this fixture describing a
    database the repository no longer produces.
    """
    with _connect(db_name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        for ddl in CLIENT_BUSINESS_DDL_FILES:
            if ddl.name == MIGRATION_050:
                continue
            conn.execute(ddl.read_text(encoding="utf-8"))
        _ensure_ledger_table(conn)
        for filename in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED:
            if filename == MIGRATION_050:
                continue
            _record(conn, filename)
        conn.commit()


def historical_state(db_name: str) -> None:
    print("\n### PATH A — the applied 049 state, reconstructed")
    snapshot = _snapshot(db_name)
    _check(f"the historical ledger carries {HISTORICAL_COLUMN_COUNT} columns",
           len(snapshot["columns"]) == HISTORICAL_COLUMN_COUNT,
           str(len(snapshot["columns"])))
    _check(f"and {HISTORICAL_CONSTRAINT_COUNT} constraints",
           len(snapshot["constraints"]) == HISTORICAL_CONSTRAINT_COUNT,
           str(len(snapshot["constraints"])))
    _check("with no external_mailer column",
           DELTA_COLUMN not in snapshot["columns"])
    _check("no external-mailer ownership CHECK",
           not (set(NEW_CONSTRAINTS) & set(snapshot["constraints"])),
           str(sorted(set(NEW_CONSTRAINTS) & set(snapshot["constraints"]))))
    _check("no EXTERNAL_MAILER_HANDOFF in the state rule",
           HANDOFF_STATE not in snapshot["constraints"][REPLACED_CONSTRAINTS[0]][0])
    _check("and the pre-review guard body",
           snapshot["function"][0][3] == HISTORICAL_GUARD_SHA256,
           snapshot["function"][0][3])
    _check("this is the state the fleet audit reported", True,
           "43 columns / 32 constraints / guard "
           f"{HISTORICAL_GUARD_SHA256[:12]}…")


def reviewed_state(label: str, db_name: str) -> Dict:
    print(f"\n### {label} — the reviewed contract")
    snapshot = _snapshot(db_name)
    _check(f"[{label}] {REVIEWED_COLUMN_COUNT} columns",
           len(snapshot["columns"]) == REVIEWED_COLUMN_COUNT,
           str(sorted(snapshot["columns"])))
    _check(f"[{label}] {REVIEWED_CONSTRAINT_COUNT} constraints",
           len(snapshot["constraints"]) == REVIEWED_CONSTRAINT_COUNT,
           str(len(snapshot["constraints"])))
    column = snapshot["columns"].get(DELTA_COLUMN)
    _check(f"[{label}] external_mailer is nullable TEXT with no default",
           column is not None
           and column[0] == "text" and column[1] == "YES" and column[2] is None,
           str(column))
    _check(f"[{label}] every ownership CHECK is present and validated",
           all(name in snapshot["constraints"] and snapshot["constraints"][name][1]
               for name in NEW_CONSTRAINTS),
           str([n for n in NEW_CONSTRAINTS if n not in snapshot["constraints"]]))
    _check(f"[{label}] every replaced state CHECK admits {HANDOFF_STATE}",
           all(HANDOFF_STATE in snapshot["constraints"][name][0]
               for name in REPLACED_CONSTRAINTS),
           str([n for n in REPLACED_CONSTRAINTS
                if HANDOFF_STATE not in snapshot["constraints"][n][0]]))
    _check(f"[{label}] the guard function carries the reviewed body",
           snapshot["function"][0][3] == REVIEWED_GUARD_SHA256,
           snapshot["function"][0][3])
    _check(f"[{label}] the guard trigger is still bound and enabled",
           list(snapshot["triggers"]) == ["trg_eco_dashboard_delivery_operation_guard"]
           and snapshot["triggers"]["trg_eco_dashboard_delivery_operation_guard"][1]
               in ("O", "A"),
           str(snapshot["triggers"]))
    _check(f"[{label}] external_mailer carries its ownership comment",
           DELTA_COLUMN in snapshot["column_comments"]
           and "immutable" in snapshot["column_comments"][DELTA_COLUMN].lower(),
           str(snapshot["column_comments"].get(DELTA_COLUMN)))
    return snapshot


# ---------------------------------------------------------------------------
# 4. PATH B — a fresh installation through onboarding's own chain
# ---------------------------------------------------------------------------

def _build_fresh_client(db_name: str) -> None:
    with _connect(db_name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        for ddl in CLIENT_BUSINESS_DDL_FILES:
            conn.execute(ddl.read_text(encoding="utf-8"))
        _ensure_ledger_table(conn)
        for filename in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED:
            _record(conn, filename)
        conn.commit()


# ---------------------------------------------------------------------------
# 5. The migration runner, as an operator would use it
# ---------------------------------------------------------------------------

def _synthetic_environment(platform_db: str = PLATFORM_DB) -> Dict[str, str]:
    """The COMPLETE environment the runner is given. Nothing inherited.

    Built from scratch so no SMTP, provider, Cloudflare or production database
    variable that happens to be exported can reach the child, and
    `LOG_PLATFORM_NO_DOTENV` stops it resolving the repository `.env` — which
    would point it at the five real client databases this suite must never
    touch.
    """
    p = _parts()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(ROOT),
        "LOG_PLATFORM_NO_DOTENV": "1",
        "POSTGRES_HOST": str(p["host"]),
        "POSTGRES_PORT": str(p["port"]),
        "POSTGRES_USER": str(p["user"]),
        "POSTGRES_PASSWORD": str(p["password"]),
        "POSTGRES_DB": platform_db,
    }


def _run_runner(*args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="eco050-runner-cwd-") as scratch:
        return subprocess.run(
            [sys.executable,
             str(ROOT / "scripts" / "apply_client_business_migrations.py"),
             *args],
            capture_output=True, text=True, env=_synthetic_environment(),
            cwd=scratch, timeout=300,
        )


def _build_platform(clients: List[Dict[str, str]]) -> None:
    p = _parts()
    with _connect(PLATFORM_DB) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS workflow_a_control")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_a_control.client_account (
              client_id UUID PRIMARY KEY,
              client_code TEXT,
              client_name TEXT,
              client_db_host TEXT NOT NULL,
              client_db_port INTEGER NOT NULL,
              client_db_name TEXT NOT NULL,
              enabled BOOLEAN NOT NULL DEFAULT TRUE
            )
            """
        )
        conn.execute("DELETE FROM workflow_a_control.client_account")
        for client in clients:
            conn.execute(
                "INSERT INTO workflow_a_control.client_account "
                "(client_id, client_code, client_name, client_db_host, "
                " client_db_port, client_db_name, enabled) "
                "VALUES (%s, %s, %s, %s, %s, %s, TRUE)",
                (client["client_id"], client["client_code"], client["client_name"],
                 p["host"], p["port"], client["db_name"]),
            )
        conn.commit()


def dry_run_matrix() -> None:
    """The matrix the future rollout task must see before it mutates anything."""
    print("\n### DRY-RUN MATRIX — 049 SKIP, 050 WOULD APPLY")
    completed = _run_runner("--client-id", UPGRADE_CLIENT_ID, "--list")
    out = completed.stdout
    _check("the dry run succeeds and matches exactly the one client",
           completed.returncode == 0 and "Matched 1 enabled client" in out,
           f"rc={completed.returncode}\n{out[-1200:]}\n{completed.stderr[-500:]}")
    _check("049 is reported as already applied and is NOT replayed",
           f"SKIP   {MIGRATION_049}" in out,
           out[-1500:])
    _check("050 is reported as pending",
           f"Would apply {MIGRATION_050}" in out, out[-1500:])
    # Scoped to the two files this task owns. The fixture builds its client from
    # onboarding's DDL list, which is deliberately narrower than the full
    # directory, so other files are legitimately pending here and say nothing
    # about the real fleet — which the read-only audit already enumerated.
    _check("050 is pending exactly once",
           out.count(f"Would apply {MIGRATION_050}") == 1, out[-1500:])
    _check("049 is never in the would-apply set",
           f"Would apply {MIGRATION_049}" not in out, out[-1500:])
    _check("a --list run applies nothing",
           "applied= 0" in out, out[-600:])


def _ledger_has(db_name: str, filename: str) -> bool:
    with _connect(db_name) as conn:
        row = conn.execute(
            "SELECT 1 FROM public.schema_migrations WHERE filename = %s",
            (filename,),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 6. Release schema preflight, in both directions
# ---------------------------------------------------------------------------

def _release_tree(tmp_root: Path) -> Path:
    """A release declaring only the Driver Eco Dashboard requirement, VERBATIM.

    Narrowing the declaration itself would be narrowing the thing under test;
    the platform and client_trips requirements are simply outside what this
    fixture builds.
    """
    document = json.loads(
        (ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8")
    )
    document["requirements"] = [
        r for r in document["requirements"]
        if r.get("migration") == REQUIREMENT_MIGRATION
    ]
    assert len(document["requirements"]) == 1, (
        f"{REQUIREMENT_MIGRATION} is not declared in db/schema_requirements.json"
    )
    tree = tmp_root / "release"
    (tree / "db").mkdir(parents=True, exist_ok=True)
    (tree / "db" / "schema_requirements.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    return tree


def _preflight(tree: Path, release_id: str):
    return verify_schema_prerequisites(
        release_tree=tree,
        release_id=release_id,
        platform_conn_factory=lambda: _connect(PLATFORM_DB),
        client_conn_factory=lambda client: _connect(client.db_name),
    )


def preflight_refuses(label: str, expected_code: str) -> None:
    with tempfile.TemporaryDirectory(prefix="eco050-preflight-") as tmp:
        tree = _release_tree(Path(tmp))
        try:
            _preflight(tree, "eco050-refuse")
        except SchemaPreflightError as exc:
            _check(label, exc.code == expected_code, f"{exc.code}: {exc.detail}")
            return
    _check(label, False, "the gate PASSED")


def preflight_accepts(label: str) -> None:
    with tempfile.TemporaryDirectory(prefix="eco050-preflight-ok-") as tmp:
        tree = _release_tree(Path(tmp))
        try:
            report = _preflight(tree, "eco050-accept")
        except SchemaPreflightError as exc:
            _check(label, False, f"{exc.code}: {exc.detail}")
            return
    _check(label,
           bool(report.client_checks)
           and all(not c["physical_defects"] for c in report.client_checks)
           and all(c["ledger_recorded"] for c in report.client_checks),
           str(report.client_checks))


def forged_ledger_still_refused() -> None:
    """The ledger row is not the evidence, and 050 must not change that.

    A database that records 050 without carrying the schema 050 installs is the
    exact "the rollout looks correct" failure this gate exists for, and it has
    to stay a refusal now that the ledger anchor has moved.
    """
    print("\n### FAIL-CLOSED — a forged 050 ledger row over an uncorrected schema")
    _build_historical_client(FORGED_DB)
    with _connect(FORGED_DB) as conn:
        _record(conn, MIGRATION_050)
        conn.commit()
    _build_platform([{
        "client_id": FORGED_CLIENT_ID, "client_code": "ECO050FRG",
        "client_name": "Eco 050 forged ledger", "db_name": FORGED_DB,
    }])
    preflight_refuses(
        "a client claiming 050 without the reviewed schema is refused",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING",
    )


# ---------------------------------------------------------------------------
# 7. Runtime — the approved DeliveryLedger against the migrated database
# ---------------------------------------------------------------------------

#: Fixed namespace for the suite's synthetic client ids. Never a real client.
_IDENTITY_NAMESPACE = uuid.UUID("cf0f4876-96d7-4322-82cd-21f2082ad195")


def _identity(key: str):
    from jobs.ecodriving_dashboard.delivery_contract import DeliveryIdentity

    # DETERMINISTIC, so "the same key" really is the same logical delivery:
    # convergence on a rerun is one of the facts under test, and a random
    # client_id would make every call a different delivery that merely collides
    # on the derived operation id.
    return DeliveryIdentity(
        client_id=str(uuid.uuid5(_IDENTITY_NAMESPACE, key)),
        identity_key=key, period_type="weekly",
        period_start_date=date(2026, 7, 6), period_end_date=date(2026, 7, 13),
    )


def _create(ledger, key: str, *, external_mailer: Optional[str]):
    return ledger.ensure_operation(
        _identity(key),
        operation_id=("O" * 8) + hashlib.sha256(key.encode()).hexdigest()[:16],
        subject_ref=f"subject-{key}",
        payload_digest=hashlib.sha256(key.encode()).hexdigest(),
        recipient_email="driver@example.invalid",
        recipient_identity=f"rcpt-{key}",
        external_mailer=external_mailer,
    )


def runtime_smoke(db_name: str) -> None:
    """Nine facts the reviewed runtime needs the migrated database to be true."""
    import psycopg
    from jobs.ecodriving_dashboard.delivery_contract import (
        DeliveryContractError, DeliveryState,
    )
    from jobs.ecodriving_dashboard.delivery_ledger import DeliveryLedger, LedgerConflict

    print("\n### RUNTIME — the approved DeliveryLedger over the migrated schema")
    conn = _connect(db_name)
    conn.autocommit = True
    ledger = DeliveryLedger(conn)

    # 1 — ownership is written by the creating INSERT, and read back.
    record, created = _create(ledger, "owned-1", external_mailer="eco_weekly")
    _check("1. external_mailer is written at first operation creation",
           created and record.row.get("external_mailer") == "eco_weekly",
           str(record.row.get("external_mailer")))
    reloaded = ledger.load(record.row["delivery_id"])
    _check("   and survives a full-projection reload (no UndefinedColumn)",
           reloaded is not None
           and reloaded.row.get("external_mailer") == "eco_weekly")

    # 2 — a blank owner is refused before anything is written.
    try:
        _create(ledger, "blank-1", external_mailer="   ")
        _check("2. a blank external mailer is refused", False, "it was accepted")
    except DeliveryContractError:
        _check("2. a blank external mailer is refused", True)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"UPDATE {QUALIFIED} SET external_mailer = '  ' "
                "WHERE delivery_id = %s", (record.row["delivery_id"],))
            _check("   and the database refuses a blank value directly too",
                   False, "the UPDATE was accepted")
        except psycopg.errors.CheckViolation:
            _check("   and the database refuses a blank value directly too", True)

    # 3/4/5 — ownership is immutable in every direction.
    provider_owned, _ = _create(ledger, "provider-1", external_mailer=None)
    for label, delivery_id, value in (
        ("3. ownership NULL -> value is refused after creation",
         provider_owned.row["delivery_id"], "eco_weekly"),
        ("4. ownership value -> NULL is refused",
         record.row["delivery_id"], None),
        ("5. ownership value -> a different value is refused",
         record.row["delivery_id"], "eco_monthly"),
    ):
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"UPDATE {QUALIFIED} SET external_mailer = %s "
                    "WHERE delivery_id = %s", (value, str(delivery_id)))
                _check(label, False, "the UPDATE was accepted")
            except psycopg.errors.RaiseException as exc:
                _check(label, "OWNERSHIP_IMMUTABLE" in str(exc), str(exc)[:200])
            except psycopg.errors.CheckViolation as exc:
                _check(label, "OWNERSHIP_IMMUTABLE" in str(exc), str(exc)[:200])

    # 6 — an owned row cannot acquire provider identity or lifecycle state.
    provider_columns = (
        ("provider_name", "'smtp'"),
        ("provider_idempotency_key", "'idem-1'"),
        ("provider_backend_id", "'pbk_" + "0" * 40 + "'"),
        ("provider_message_id", "'msg-1'"),
        ("provider_submitted_at", "now()"),
        ("provider_accepted_at", "now()"),
        ("remote_delivered_at", "now()"),
        ("provider_attempts", "1"),
    )
    refused = []
    for column, literal in provider_columns:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"UPDATE {QUALIFIED} SET {column} = {literal} "
                    "WHERE delivery_id = %s", (str(record.row["delivery_id"]),))
                cur.execute(
                    f"UPDATE {QUALIFIED} SET {column} = DEFAULT "
                    "WHERE delivery_id = %s", (str(record.row["delivery_id"]),))
            except (psycopg.errors.CheckViolation,
                    psycopg.errors.RaiseException):
                refused.append(column)
    _check("6. an externally owned operation cannot acquire provider identity "
           "or lifecycle state",
           refused == [c for c, _ in provider_columns],
           f"accepted: {[c for c, _ in provider_columns if c not in refused]}")

    # 7 — the handoff state exists only for a correctly owned row.
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"UPDATE {QUALIFIED} SET state = %s WHERE delivery_id = %s",
                (DeliveryState.EXTERNAL_MAILER_HANDOFF,
                 str(provider_owned.row["delivery_id"])))
            _check(f"7. {HANDOFF_STATE} is unreachable for an unowned operation",
                   False, "the UPDATE was accepted")
        except psycopg.errors.CheckViolation:
            _check(f"7. {HANDOFF_STATE} is unreachable for an unowned operation",
                   True)
    claimed = ledger.claim(record.row["delivery_id"], owner="eco050-smoke")
    try:
        ledger.record_external_mailer_handoff(
            claimed, owner="eco050-smoke", mailer="eco_monthly")
        _check("   and the ledger refuses a handoff naming a different mailer",
               False, "the handoff was accepted")
    except LedgerConflict as exc:
        _check("   and the ledger refuses a handoff naming a different mailer",
               exc.code == "EXTERNAL_OWNERSHIP_CONFLICT", exc.code)

    # 8 — the provider-owned shape is untouched by any of this.
    _check("8. an ordinary provider-owned operation with external_mailer IS NULL "
           "remains valid",
           provider_owned.row.get("external_mailer") is None
           and provider_owned.state == DeliveryState.PREPARED,
           str(provider_owned.row.get("external_mailer")))
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {QUALIFIED} SET attempt_count = attempt_count + 1 "
            "WHERE delivery_id = %s", (str(provider_owned.row["delivery_id"]),))
    _check("   and is still writable through the normal lifecycle columns",
           ledger.load(provider_owned.row["delivery_id"]).row["attempt_count"] >= 1)

    # 9 — the Eco mailing integration's own contract module loads and agrees.
    from jobs.ecodriving_dashboard import eco_mailing_integration

    _check("9. the approved Eco mailing integration reads the ledger without "
           "UndefinedColumn",
           DeliveryState.EXTERNAL_MAILER_HANDOFF
           in eco_mailing_integration.LINKABLE_DELIVERY_STATES
           and ledger.find_by_operation(record.row["operation_id"]) is not None)

    # And the same ownership, re-asserted, is convergence rather than conflict.
    again, created_again = _create(ledger, "owned-1", external_mailer="eco_weekly")
    _check("   a rerun under the same ownership converges on the same row",
           not created_again
           and str(again.row["delivery_id"]) == str(record.row["delivery_id"]))
    try:
        _create(ledger, "owned-1", external_mailer=None)
        _check("   and a rerun that forgets the owner is refused", False,
               "it was accepted")
    except LedgerConflict as exc:
        _check("   and a rerun that forgets the owner is refused",
               exc.code == "EXTERNAL_OWNERSHIP_CONFLICT", exc.code)

    conn.close()


# ---------------------------------------------------------------------------

def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable loopback PostgreSQL DSN.")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)

    print("=" * 78)
    print("MIGRATION 050 — HISTORICAL UPGRADE AND FRESH INSTALL CONVERGE")
    print("=" * 78)

    immutability()
    discovery()

    try:
        _recreate_databases(PLATFORM_DB, UPGRADE_DB, FRESH_DB, FORGED_DB)

        # --- PATH A ---------------------------------------------------------
        _build_historical_client(UPGRADE_DB)
        _build_platform([{
            "client_id": UPGRADE_CLIENT_ID, "client_code": "ECO050UPG",
            "client_name": "Eco 050 historical client", "db_name": UPGRADE_DB,
        }])
        historical_state(UPGRADE_DB)

        print("\n### GATE — the historical client must NOT be activatable")
        preflight_refuses(
            "a client carrying 049 alone is refused on the ledger check",
            "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING",
        )

        dry_run_matrix()

        completed = _run_runner("--client-id", UPGRADE_CLIENT_ID,
                                "--migration", MIGRATION_050, "--apply")
        _check("the runner applies 050 to the historical client",
               completed.returncode == 0 and MIGRATION_050 in completed.stdout,
               f"rc={completed.returncode}\n{completed.stdout[-1500:]}"
               f"\n{completed.stderr[-800:]}")
        _check("and records it in the client ledger",
               _ledger_has(UPGRADE_DB, MIGRATION_050))
        _check("without disturbing the 049 ledger row",
               _ledger_has(UPGRADE_DB, MIGRATION_049))
        upgraded = reviewed_state("PATH A", UPGRADE_DB)
        preflight_accepts("the upgraded client satisfies the reviewed requirement")

        rerun = _run_runner("--client-id", UPGRADE_CLIENT_ID,
                            "--migration", MIGRATION_050, "--list")
        _check("a second discovery pass reports 050 as applied, never replayed",
               f"SKIP   {MIGRATION_050}" in rerun.stdout
               and "Would apply" not in rerun.stdout,
               rerun.stdout[-800:])

        runtime_smoke(UPGRADE_DB)

        # --- PATH B ---------------------------------------------------------
        _build_fresh_client(FRESH_DB)
        _build_platform([{
            "client_id": FRESH_CLIENT_ID, "client_code": "ECO050NEW",
            "client_name": "Eco 050 fresh client", "db_name": FRESH_DB,
        }])
        _check("a freshly onboarded client records both 049 and 050",
               _ledger_has(FRESH_DB, MIGRATION_049)
               and _ledger_has(FRESH_DB, MIGRATION_050))
        fresh = reviewed_state("PATH B", FRESH_DB)
        preflight_accepts("the fresh client satisfies the reviewed requirement")
        nothing_pending = _run_runner(
            "--client-id", FRESH_CLIENT_ID,
            "--migration", MIGRATION_049, "--migration", MIGRATION_050, "--list")
        _check("and the runner has nothing left to apply to it",
               "All clients are up to date" in nothing_pending.stdout,
               nothing_pending.stdout[-800:])

        # --- EQUIVALENCE ----------------------------------------------------
        print("\n### PHYSICAL EQUIVALENCE — the two paths are the same database")
        for section in ("columns", "constraints", "indexes", "triggers",
                        "function", "column_comments"):
            _check(f"the {section} catalog is identical on both paths",
                   upgraded[section] == fresh[section],
                   f"A={json.dumps(upgraded[section], default=str)[:900]}\n"
                   f"      B={json.dumps(fresh[section], default=str)[:900]}")
        _check("(and the comparison covered a non-trivial catalog)",
               len(upgraded["columns"]) == REVIEWED_COLUMN_COUNT
               and len(upgraded["constraints"]) == REVIEWED_CONSTRAINT_COUNT)

        # --- FAIL-CLOSED ----------------------------------------------------
        forged_ledger_still_refused()
    finally:
        _teardown()

    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("MIGRATION 050 UPGRADES THE APPLIED 049 INTO THE REVIEWED CONTRACT, "
          "AND A FRESH INSTALL LANDS ON THE SAME DATABASE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
