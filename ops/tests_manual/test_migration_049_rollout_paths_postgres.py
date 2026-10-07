#!/usr/bin/env python3
"""Both client-business rollout paths must land on a schema activation accepts.

Run:
    ECO_DASHBOARD_049_ROLLOUT_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5433/postgres \\
      python3 ops/tests_manual/test_migration_049_rollout_paths_postgres.py

WHY THIS EXISTS SEPARATELY FROM THE PHYSICAL PREFLIGHT SUITE.
    `test_migration_049_physical_preflight_postgres.py` proves the gate refuses a
    malformed 049. That is only half of a release gate being correct: a gate
    nothing can satisfy is as much a defect as a gate everything satisfies. The
    physical requirement is only right if the schema the repository's OWN
    rollout paths produce passes it — from a fresh onboarding and from an
    existing pre-049 client migrated forward — and if the runtime role can then
    actually use the ledger those paths built.

    Both paths are exercised through the repository's own declarations and
    tooling rather than a local copy: the onboarding DDL and grant lists are
    IMPORTED from `scripts/onboard_workflow_a_client.py`, and the existing-client
    path runs `scripts/apply_client_business_migrations.py` as a subprocess, the
    same way an operator would.

DESTRUCTIVE. Creates and drops its own databases and one runtime role in the
instance the DSN names, and refuses any DSN that is not loopback. It performs no
persistent migration: every database it touches is one it created. No production
database, no release activation, no e-mail, no provider and no Cloudflare
resource is involved anywhere in this file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# REPOSITORY `.env` ISOLATION — installed BEFORE the first repository import
#
# THE DEFECT THIS CLOSES. `scripts/onboard_workflow_a_client.py` used to load
# `.env` AT IMPORT TIME, from the current working directory. This suite imports
# that module for its DDL and grant declarations, so running it from the
# repository root read the host's real operator credentials into this process —
# provider keys, database passwords, SMTP and Cloudflare material. Nothing was
# printed or transmitted, and that is not the standard: a deterministic schema
# test must run on synthetic loopback configuration only, so that it cannot
# reach real infrastructure even by accident.
#
# TWO HALVES, because there are two paths. In-process, the dotenv load now
# happens in the script's `main()`, and the guard below PROVES no repository
# `.env` is opened by anything this file imports — it is evidence, not just an
# assumption about the fix. Out of process, the migration runner is a program
# and legitimately loads `.env` when an operator runs it, so the subprocess is
# given `LOG_PLATFORM_NO_DOTENV=1` and a minimal synthetic environment instead
# of a copy of this one.
# ---------------------------------------------------------------------------

REPOSITORY_DOTENV = ROOT / ".env"

#: Every attempt to open the repository `.env`, recorded rather than blocked so
#: the assertion can name what did it.
_DOTENV_OPENS: List[str] = []


def _install_dotenv_guard() -> None:
    import builtins
    import io

    def _guarded(original):
        def wrapper(file, *args, **kwargs):
            try:
                candidate = Path(os.fspath(file)).resolve()
                if candidate == REPOSITORY_DOTENV.resolve():
                    _DOTENV_OPENS.append(str(candidate))
            except (TypeError, ValueError, OSError):
                pass
            return original(file, *args, **kwargs)
        return wrapper

    builtins.open = _guarded(builtins.open)
    if io.open is not builtins.open:
        io.open = _guarded(io.open)


_install_dotenv_guard()

from ops.release_schema_preflight import (  # noqa: E402
    SchemaPreflightError,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from scripts.onboard_workflow_a_client import (  # noqa: E402
    CLIENT_BUSINESS_DDL_FILES,
    CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED,
)

ENV = "ECO_DASHBOARD_049_ROLLOUT_TEST_DSN"
MIGRATION_FILE = "049_eco_dashboard_delivery_operation.sql"
#: 049 is applied shared history on every existing client, so the reviewed
#: external-mailer contract arrives as a forward migration. Both files are part
#: of ONE rollout: a path that installs 049 without 050 lands on a schema the
#: approved runtime cannot use, so both paths are exercised through the chain.
MIGRATION_050 = "050_eco_dashboard_external_mailer_ownership.sql"
ECO_DASHBOARD_MIGRATIONS = (MIGRATION_FILE, MIGRATION_050)
#: The requirement the release gate keys on. It is anchored on 050 because that
#: is the migration whose presence makes the reviewed contract true.
REQUIREMENT_MIGRATION = MIGRATION_050
TABLE = "public.eco_dashboard_delivery_operation"

PLATFORM_DB = "eco049_rollout_platform"
NEW_CLIENT_DB = "eco049_rollout_new"
OLD_CLIENT_DB = "eco049_rollout_existing"
NEW_CLIENT_ID = "ef683c71-ee62-4b01-8534-d52c510c423f"
OLD_CLIENT_ID = "70fb2ffa-a8ae-4e85-8df2-3f17a105430b"
RUNTIME_ROLE = "eco049_rollout_runtime"
RUNTIME_PASSWORD = "disposable-rollout"

#: Exactly what `apply_grants` gives the runtime role on this relation.
RUNTIME_PRIVILEGES = ("SELECT", "INSERT", "UPDATE")

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


def _connect_as_runtime(dbname: str):
    import psycopg

    p = _parts()
    return psycopg.connect(
        f"postgresql://{RUNTIME_ROLE}:{RUNTIME_PASSWORD}@{p['host']}:{p['port']}/{dbname}"
    )


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
        for name in (PLATFORM_DB, NEW_CLIENT_DB, OLD_CLIENT_DB):
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'DROP ROLE IF EXISTS "{RUNTIME_ROLE}"')


def _ensure_runtime_role() -> None:
    with _admin() as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{RUNTIME_ROLE}"')
        conn.execute(
            f'CREATE ROLE "{RUNTIME_ROLE}" LOGIN PASSWORD \'{RUNTIME_PASSWORD}\''
        )


# ---------------------------------------------------------------------------
# Platform control plane
# ---------------------------------------------------------------------------

def _build_platform(clients: List[Dict[str, str]]) -> None:
    p = _parts()
    with _connect(PLATFORM_DB) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS workflow_a_control")
        conn.execute(
            """
            CREATE TABLE workflow_a_control.client_account (
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


# ---------------------------------------------------------------------------
# Path 1 — a NEW client, through onboarding's own declarations
# ---------------------------------------------------------------------------

def _onboard(db_name: str) -> None:
    """Onboarding's client-business half, using the script's own lists.

    `apply_client_ddl` then `apply_grants`, in that order — which is what makes
    049's own grant block a no-op on this path and why the relation has to be
    named in the onboarding grant list as well. Both facts are exercised here
    rather than asserted from the source.
    """
    with _connect(db_name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        for ddl in CLIENT_BUSINESS_DDL_FILES:
            conn.execute(ddl.read_text(encoding="utf-8"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for filename in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED:
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (filename,),
            )
        conn.commit()
    _grant_runtime(db_name)


def _grant_runtime(db_name: str) -> None:
    """The onboarding grant, narrowed to the relation under test."""
    with _admin() as conn:
        conn.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{RUNTIME_ROLE}"')
    with _connect(db_name) as conn:
        conn.execute(f'GRANT USAGE ON SCHEMA public TO "{RUNTIME_ROLE}"')
        conn.execute(
            f'GRANT {", ".join(RUNTIME_PRIVILEGES)} ON TABLE {TABLE} '
            f'TO "{RUNTIME_ROLE}"'
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Path 2 — an EXISTING pre-049 client, through the real migration runner
# ---------------------------------------------------------------------------

def _build_pre_049_client(db_name: str) -> None:
    """Everything onboarding installs EXCEPT 049, recorded as applied.

    This is the shape of a client that predates the Driver Eco Dashboard
    release: complete, healthy, and missing exactly one migration.
    """
    with _connect(db_name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        for ddl in CLIENT_BUSINESS_DDL_FILES:
            if ddl.name in ECO_DASHBOARD_MIGRATIONS:
                continue
            conn.execute(ddl.read_text(encoding="utf-8"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for filename in CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED:
            if filename in ECO_DASHBOARD_MIGRATIONS:
                continue
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (filename,),
            )
        conn.commit()
    # The runtime role's grant on `client_trips` is what 049's own grant block
    # mirrors from, and on THIS path the block is not a no-op: it is how an
    # existing client's runtime role reaches the new relation at all.
    with _admin() as conn:
        conn.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{RUNTIME_ROLE}"')
    with _connect(db_name) as conn:
        conn.execute(f'GRANT USAGE ON SCHEMA public TO "{RUNTIME_ROLE}"')
        conn.execute(
            f'GRANT SELECT, INSERT, UPDATE ON TABLE public.client_trips '
            f'TO "{RUNTIME_ROLE}"'
        )
        conn.commit()


def _synthetic_environment() -> Dict[str, str]:
    """The COMPLETE environment the migration runner is given. Nothing inherited.

    Built from scratch rather than copied from this process, so no SMTP,
    provider, Cloudflare or production database variable that happens to be
    exported can reach the child. `LOG_PLATFORM_NO_DOTENV` stops it loading the
    repository `.env` as well — the runner is a program and loading it is
    correct when an operator runs it, which is precisely why the test has to
    say otherwise explicitly.
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
        "POSTGRES_DB": PLATFORM_DB,
    }


#: Run in a child with the runner's exact environment. Installs the same
#: `open` guard, imports the migration runner and calls its dotenv loader
#: DIRECTLY, then reports whether the repository `.env` was opened. This is the
#: half the in-process guard structurally cannot see, and it proves the
#: isolation rather than assuming the flag is honoured. It reads no value from
#: any environment file: the only thing it ever prints is a path it refused.
_CHILD_DOTENV_PROBE = """
import builtins, os, sys
from pathlib import Path
ROOT = Path(sys.argv[1])
TARGET = (ROOT / ".env").resolve()
opened = []
_original = builtins.open
def _guarded(file, *a, **kw):
    try:
        if Path(os.fspath(file)).resolve() == TARGET:
            opened.append(str(TARGET))
    except (TypeError, ValueError, OSError):
        pass
    return _original(file, *a, **kw)
builtins.open = _guarded
sys.path.insert(0, str(ROOT))
import scripts.apply_client_business_migrations as runner
import scripts.onboard_workflow_a_client as onboarding
after_import = list(opened)
runner._load_dotenv_if_present()
onboarding._load_dotenv_if_present()
print("IMPORT_OPENED=" + str(bool(after_import)))
print("LOADER_OPENED=" + str(bool(opened)))
print("TARGET_EXISTS=" + str(TARGET.exists()))
"""


#: Run in a child whose working directory contains a SYNTHETIC `.env`. Proves
#: the operator-facing behaviour was preserved: moving the load into `main()`
#: must not stop a real invocation from resolving configuration from the file
#: it has always used.
_OPERATOR_DOTENV_PROBE = """
import os, sys
from pathlib import Path
ROOT = Path(sys.argv[1])
sys.path.insert(0, str(ROOT))
import scripts.onboard_workflow_a_client as onboarding
print("BEFORE=" + repr(os.environ.get("ECO049_SYNTHETIC_PROBE")))
onboarding._load_dotenv_if_present()
print("AFTER=" + repr(os.environ.get("ECO049_SYNTHETIC_PROBE")))
"""


def _operator_dotenv_probe() -> None:
    """Isolating the test must not have broken the real onboarding workflow."""
    with tempfile.TemporaryDirectory(prefix="eco049-operator-env-") as scratch:
        (Path(scratch) / ".env").write_text(
            "ECO049_SYNTHETIC_PROBE=synthetic-not-a-secret\n", encoding="utf-8"
        )
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(ROOT),
        }
        completed = subprocess.run(
            [sys.executable, "-c", _OPERATOR_DOTENV_PROBE, str(ROOT)],
            capture_output=True, text=True, env=environment, cwd=scratch,
            timeout=120,
        )
    out = completed.stdout
    _check("importing onboarding still has NO environment side effect",
           "BEFORE=None" in out,
           f"stdout={out!r} stderr={completed.stderr[-400:]!r}")
    _check("but executing its entrypoint loader still resolves the operator's "
           "`.env`",
           "AFTER='synthetic-not-a-secret'" in out,
           f"stdout={out!r} stderr={completed.stderr[-400:]!r}")


def _child_dotenv_probe() -> None:
    """Prove the out-of-process half: neither import nor loader reads `.env`."""
    with tempfile.TemporaryDirectory(prefix="eco049-dotenv-probe-") as scratch:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_DOTENV_PROBE, str(ROOT)],
            capture_output=True, text=True, env=_synthetic_environment(),
            cwd=scratch, timeout=120,
        )
    out = completed.stdout
    _check("the migration runner's own environment opens no repository `.env`",
           completed.returncode == 0
           and "IMPORT_OPENED=False" in out and "LOADER_OPENED=False" in out,
           f"rc={completed.returncode} stdout={out!r} stderr={completed.stderr[-400:]!r}")
    _check("(and the probe was looking at a `.env` that really exists)",
           "TARGET_EXISTS=True" in out, out)


def _run_migration_runner(client_id: str) -> subprocess.CompletedProcess:
    # `cwd` is a scratch directory, not the repository: a tool that resolved
    # configuration relative to the working directory must not find the
    # repository's by being invoked from inside it.
    with tempfile.TemporaryDirectory(prefix="eco049-runner-cwd-") as scratch:
        return subprocess.run(
            [sys.executable,
             str(ROOT / "scripts" / "apply_client_business_migrations.py"),
             "--client-id", client_id,
             *[arg for name in ECO_DASHBOARD_MIGRATIONS
               for arg in ("--migration", name)],
             "--apply"],
            capture_output=True, text=True, env=_synthetic_environment(),
            cwd=scratch, timeout=300,
        )


# ---------------------------------------------------------------------------
# Assertions shared by both paths
# ---------------------------------------------------------------------------

def _ledger_recorded(db_name: str, filename: str = MIGRATION_FILE) -> bool:
    with _connect(db_name) as conn:
        row = conn.execute(
            "SELECT 1 FROM public.schema_migrations WHERE filename = %s",
            (filename,),
        ).fetchone()
    return row is not None


def _chain_recorded(db_name: str) -> bool:
    return all(_ledger_recorded(db_name, name)
               for name in ECO_DASHBOARD_MIGRATIONS)


def _runtime_ledger_probe(path: str, db_name: str) -> None:
    """The publisher's own first three statements, as the RUNTIME role.

    Onboarding and migration both claim to leave a database the job can use;
    this is the only thing that establishes it. The privileges deliberately
    withheld are checked in the same connection, because a path that
    over-granted would also "work".
    """
    import psycopg

    operation_id = ("R" * 8) + path[:8].upper().ljust(8, "X")
    with _connect_as_runtime(db_name) as conn:
        try:
            conn.execute(
                f"""INSERT INTO {TABLE} (
                        operation_id, client_id, identity_key, period_type,
                        period_start_date, period_end_date, send_scope,
                        subject_ref, payload_digest, recipient_identity,
                        recipient_email, state
                    ) VALUES (%s, gen_random_uuid(), 'DRIVER-1', 'weekly',
                              DATE '2026-07-01', DATE '2026-07-08', 'normal',
                              'subject-1', %s, 'rcpt_1',
                              'driver@example.invalid', 'PREPARED')""",
                (operation_id, "a" * 64),
            )
            row = conn.execute(
                f"SELECT state, provider_name FROM {TABLE} WHERE operation_id = %s",
                (operation_id,),
            ).fetchone()
            conn.execute(
                f"UPDATE {TABLE} SET attempt_count = attempt_count + 1, "
                "updated_at = now() WHERE operation_id = %s",
                (operation_id,),
            )
            conn.commit()
            _check(f"[{path}] the runtime role can INSERT, SELECT and UPDATE the ledger",
                   row is not None and row[0] == "PREPARED" and row[1] is None,
                   str(row))
        except Exception as exc:  # pragma: no cover - a failing path must say why
            conn.rollback()
            _check(f"[{path}] the runtime role can INSERT, SELECT and UPDATE the ledger",
                   False, f"{type(exc).__name__}: {exc}")

        for verb, statement in (
            ("DELETE", f"DELETE FROM {TABLE}"),
            ("TRUNCATE", f"TRUNCATE {TABLE}"),
            ("DDL", f"ALTER TABLE {TABLE} ADD COLUMN probe_column TEXT"),
        ):
            try:
                conn.execute(statement)
                conn.rollback()
                _check(f"[{path}] the runtime role holds no {verb} privilege",
                       False, "the statement was accepted")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                _check(f"[{path}] the runtime role holds no {verb} privilege", True)
            except Exception as exc:
                conn.rollback()
                _check(f"[{path}] the runtime role holds no {verb} privilege",
                       False, f"{type(exc).__name__}: {exc}")


def _release_tree(tmp_root: Path) -> Path:
    """A release declaring only the 049 requirement, carried through VERBATIM.

    The platform requirements and 047/048 are outside what this two-path fixture
    builds; narrowing the 049 requirement itself would be narrowing the very
    declaration under test.
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


def _physical_preflight(label: str, tree: Path) -> None:
    try:
        report = verify_schema_prerequisites(
            release_tree=tree,
            release_id="eco049-rollout",
            platform_conn_factory=lambda: _connect(PLATFORM_DB),
            client_conn_factory=lambda client: _connect(client.db_name),
        )
    except SchemaPreflightError as exc:
        _check(label, False, f"{exc.code}: {exc.detail}")
        return
    _check(label,
           bool(report.client_checks)
           and all(not c["physical_defects"] for c in report.client_checks),
           str(report.client_checks))


# ---------------------------------------------------------------------------

def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable loopback PostgreSQL DSN.")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)

    print("=" * 78)
    print("MIGRATION 049 — BOTH ROLLOUT PATHS SATISFY THE PHYSICAL REQUIREMENT")
    print("=" * 78)

    print("\n### ENVIRONMENT ISOLATION — synthetic configuration only")
    _check("importing the repository's onboarding module opened no `.env`",
           not _DOTENV_OPENS, f"opened: {_DOTENV_OPENS}")
    # The guard can only see this process, so the fact that a real `.env`
    # EXISTS is recorded: on a host without one the assertion above would be
    # vacuously true and would stop meaning anything.
    _check("(the repository does carry a real `.env`, so the guard is not "
           "vacuous)",
           REPOSITORY_DOTENV.exists(),
           "no repository .env on this host; the isolation proof is weaker here")
    synthetic = _synthetic_environment()
    secretish = sorted(
        name for name in synthetic
        if any(token in name.upper() for token in
               ("SMTP", "CLOUDFLARE", "R2_", "API_KEY", "DB_KEY", "SECRET",
                "TOKEN", "PASSWORD"))
    )
    _check("the only credential the runner is given is the disposable "
           "instance's own",
           secretish == ["POSTGRES_PASSWORD"], str(secretish))
    _check("and that credential comes from the loopback test DSN, not from "
           "this process's environment",
           synthetic["POSTGRES_PASSWORD"] == str(_parts()["password"]))
    process_secrets = {
        name for name in os.environ
        if any(token in name.upper() for token in
               ("SMTP", "CLOUDFLARE", "R2_", "API_KEY", "DB_KEY", "SECRET",
                "TOKEN", "PASSWORD"))
    }
    leaked = sorted((process_secrets & set(synthetic)) - {"POSTGRES_PASSWORD"})
    _check("no other secret variable of this process reaches the child",
           not leaked, str(leaked))
    _check("and its environment is exactly the synthetic set",
           set(_synthetic_environment()) == {
               "PATH", "PYTHONPATH", "LOG_PLATFORM_NO_DOTENV", "POSTGRES_HOST",
               "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
               "POSTGRES_DB"},
           str(sorted(_synthetic_environment())))

    try:
        _recreate_databases(PLATFORM_DB, NEW_CLIENT_DB, OLD_CLIENT_DB)
        _ensure_runtime_role()

        print("\n### NEW CLIENT — repository-standard onboarding")
        _onboard(NEW_CLIENT_DB)
        _build_platform([{
            "client_id": NEW_CLIENT_ID, "client_code": "ECONEW001",
            "client_name": "Eco 049 new client", "db_name": NEW_CLIENT_DB,
        }])
        _check("onboarding records the whole 049 + 050 chain in the client "
               "ledger",
               _chain_recorded(NEW_CLIENT_DB))
        _runtime_ledger_probe("new", NEW_CLIENT_DB)
        with tempfile.TemporaryDirectory(prefix="eco049-rollout-") as tmp:
            _physical_preflight(
                "a freshly onboarded client satisfies the reviewed physical "
                "requirement",
                _release_tree(Path(tmp)),
            )

        print("\n### EXISTING CLIENT — repository-standard migration runner")
        _build_pre_049_client(OLD_CLIENT_DB)
        _recreate_databases(PLATFORM_DB)
        _build_platform([{
            "client_id": OLD_CLIENT_ID, "client_code": "ECOOLD001",
            "client_name": "Eco 049 existing client", "db_name": OLD_CLIENT_DB,
        }])
        _check("the pre-049 client does not carry the relation yet",
               not _ledger_recorded(OLD_CLIENT_DB)
               and not _ledger_recorded(OLD_CLIENT_DB, MIGRATION_050))
        with tempfile.TemporaryDirectory(prefix="eco049-rollout-pre-") as tmp:
            tree = _release_tree(Path(tmp))
            try:
                verify_schema_prerequisites(
                    release_tree=tree, release_id="eco049-rollout-pre",
                    platform_conn_factory=lambda: _connect(PLATFORM_DB),
                    client_conn_factory=lambda client: _connect(client.db_name),
                )
                _check("activation is blocked while the chain is absent", False,
                       "the gate PASSED")
            except SchemaPreflightError as exc:
                _check("activation is blocked while the chain is absent",
                       exc.code == "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING",
                       f"{exc.code}: {exc.detail}")

        completed = _run_migration_runner(OLD_CLIENT_ID)
        # The runner reads the fleet from POSTGRES_* — which this suite points
        # at its own disposable platform database. Asserted explicitly, because
        # a runner that had fallen back to a `.env` fleet would be operating on
        # databases this suite does not own, and "it worked" would be the worst
        # possible way to discover that.
        _check("nothing this process did opened the repository `.env`",
               not _DOTENV_OPENS, f"opened: {_DOTENV_OPENS}")
        _child_dotenv_probe()
        _operator_dotenv_probe()
        _check("the runner matched exactly the one disposable client",
               "Matched 1 enabled client" in completed.stdout
               and OLD_CLIENT_DB in completed.stdout,
               completed.stdout[-1500:])
        _check("the migration runner applies the 049 + 050 chain to the "
               "existing client",
               completed.returncode == 0
               and all(name in completed.stdout
                       for name in ECO_DASHBOARD_MIGRATIONS),
               f"rc={completed.returncode}\n{completed.stdout[-1500:]}"
               f"\n{completed.stderr[-800:]}")
        _check("and records both files in the client ledger",
               _chain_recorded(OLD_CLIENT_DB))
        _runtime_ledger_probe("existing", OLD_CLIENT_DB)
        with tempfile.TemporaryDirectory(prefix="eco049-rollout-post-") as tmp:
            _physical_preflight(
                "the migrated existing client satisfies the reviewed physical "
                "requirement",
                _release_tree(Path(tmp)),
            )
    finally:
        _teardown()

    print("\n" + "=" * 78)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("BOTH ROLLOUT PATHS PRODUCE A SCHEMA ACTIVATION ACCEPTS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
