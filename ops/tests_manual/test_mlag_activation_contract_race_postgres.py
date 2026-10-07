#!/usr/bin/env python3
"""Release activation and CONTRACT closure must be mutually exclusive.

THE DEFECT THIS SUITE EXISTS FOR.
    `verify_schema_prerequisites` reads every CLIENT business database for the
    narrowing states in `SCHEMA_STATE_GUARDS`, decides whether the release
    declares the matching capability, and closes its connections. Only then does
    `activation_fence` open, and the fence locked the PLATFORM fleet table —
    which the CONTRACT closure never writes. Independent review reproduced the
    consequence exactly (`CLIENT_CONTRACT_COMMITTED_INSIDE_ACTIVATION_FENCE=
    True`): a closure committed strict state inside the fence of a legacy
    release whose capability check had already passed, with the pointer swap
    still ahead of it.

    The invariant that broke is the one the whole guard exists for:

        A LEGACY REQUEST-ONLY RELEASE MUST NEVER BECOME `current` AFTER ANY
        CLIENT HAS ENTERED STRICT CONTRACT STATE.

WHAT IS TESTED, AND HOW IT IS MADE DETERMINISTIC.
    Both orderings of the real race, through the real
    `ops.release_boundary.activate_release` and the real
    `ops.close_telematics_first_seen_pair_contract.run` — not helper predicates.
    Determinism comes from two things, neither of which is a timing assumption:

      * a test seam that parks one participant at a known point (patching
        `_swap_pointer` / `_fleet_fence`, which changes no behaviour under
        test); and
      * observing REAL lock state in `pg_locks` to know the other participant
        has genuinely reached its wait, rather than sleeping and hoping.

DESTRUCTIVE. Creates and drops its own databases and temporary release roots.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import release_boundary  # noqa: E402
from ops.release_boundary import (  # noqa: E402
    activate_release, pointer_release_id, prepare_release,
)
from ops.release_schema_preflight import (  # noqa: E402
    SCHEMA_TRANSITION_LOCK_KEY, SchemaPreflightError,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_MLAG_RACE_TEST_DSN"
SECRET_ENV = "TELEMATICS_MLAG_RACE_TEST_SECRET"

EXPAND_CONSTRAINT = "ck_client_trips_first_seen_instant_needs_request"
STRICT_CONSTRAINT = "ck_client_trips_first_seen_pairing"
MODE = "data_invariants_v1"
ENVIRONMENT = "race-test"
PLATFORM_UUID = "643d6d3f-d680-496b-8d24-434b8b22fc78"

PLATFORM_MIGRATIONS = (
    "008_workflow_a_control_plane.sql", "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    # S15's PREREQUISITES, in real migration order. 068 declares foreign keys
    # onto `portal_clients` and `portal_database_datasets`, so a fixture that
    # jumped straight to it could not execute it at all. Neither is declared in
    # `db/schema_requirements.json`; both are applied only so the migration that
    # IS declared can run.
    "033_portal_client_access.sql",
    "035_portal_database_catalog.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "061_workflow_a_provider_request_log.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
    "063_workflow_a_trip_delivery_lag_daily.sql",
    # S15 Report Explorer persistence. `_repository_document()` narrows only the
    # `client_business` requirements, so every PLATFORM requirement the working
    # copy declares reaches the gate exactly as written — and Portal V1 added
    # these two. Without them this fixture's "fully migrated" platform stops at
    # 063 and every activation refuses with
    # RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING before a single race assertion
    # runs. That refusal is the gate WORKING; the stale fixture was the defect.
    "068_portal_generated_reports.sql",
    "069_portal_generated_reports_integrity.sql",
)

#: The bootstrap relations 068's foreign keys reach that NO migration creates.
#:
#: A real platform database is `api/main.py`'s `SCHEMA_SQL` bootstrap first and
#: `db/migrations/` on top of it, so `artifact_users` and `artifacts` already
#: exist when 033/035/068 run. This disposable database starts empty, so they are
#: created here, narrowed to the columns those migrations actually reach.
#: Nothing in `db/schema_requirements.json` declares either, so nothing is
#: asserted against them: they exist so the DECLARED migration can execute
#: instead of failing on a missing foreign-key target. No rows are seeded — a
#: foreign key needs its target RELATION, never a target row.
#:
#: Kept byte-identical in intent to the same block in
#: `test_release_schema_preflight_postgres.py`, which solved this first.
PORTAL_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS public.artifact_users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  is_admin BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.artifacts (
  artifact_id UUID PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL DEFAULT 'report',
  filename TEXT NOT NULL DEFAULT 'artifact.pdf',
  content_type TEXT NOT NULL DEFAULT 'application/pdf',
  size_bytes BIGINT NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  storage_backend TEXT NOT NULL DEFAULT 'minio',
  storage_key TEXT NOT NULL DEFAULT ''
);
"""
CLIENT_MIGRATION_M4 = "047_client_trips_first_seen_request_id.sql"
CLIENT_MIGRATION_MLAG = "048_client_trips_first_seen_response_received_at.sql"

CLIENTS = (
    ("bd7662a5-eeb4-4614-8720-d477abfcb227", "RAA00001", "mlagrace_a"),
    ("b454f82c-5857-4bab-8342-b7258e5cf7de", "RAB00001", "mlagrace_b"),
)

#: How long the suite is willing to wait for a REAL observable event (a lock
#: appearing in `pg_locks`, a thread finishing). Nothing about correctness
#: depends on the value: it bounds a failure, it never creates a pass.
DEADLINE_SECONDS = 60.0

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _base() -> str:
    return os.environ[ENV].rsplit("/", 1)[0]


def _platform_db_name() -> str:
    return os.environ[ENV].rsplit("/", 1)[1]


def _host() -> str:
    return os.environ[ENV].split("@")[1].split(":")[0]


def _port() -> int:
    return int(os.environ[ENV].split("@")[1].split(":")[1].split("/")[0])


def _connect(dbname: str):
    import psycopg
    return psycopg.connect(f"{_base()}/{dbname}")


def _admin():
    import psycopg
    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


EXPAND_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
    "CHECK (first_seen_response_received_at_utc IS NULL "
    "       OR first_seen_request_id IS NOT NULL) NOT VALID"
)


def set_expand_state() -> None:
    """Return every client business database to the pre-closure EXPAND state."""
    for _client_id, _code, dbname in CLIENTS:
        conn = _connect(dbname)
        conn.execute(f"ALTER TABLE public.client_trips "
                     f"DROP CONSTRAINT IF EXISTS {STRICT_CONSTRAINT}")
        conn.execute(f"ALTER TABLE public.client_trips "
                     f"DROP CONSTRAINT IF EXISTS {EXPAND_CONSTRAINT}")
        conn.execute(EXPAND_DDL)
        conn.execute("DELETE FROM public.schema_migrations WHERE filename LIKE %s",
                     ("049_client_trips_first_seen_pair_contract%",))
        conn.commit()
        conn.close()


def strict_constraint_state() -> Dict[str, Optional[bool]]:
    """Per client: None when the strict constraint is absent, else convalidated."""
    out: Dict[str, Optional[bool]] = {}
    for _client_id, code, dbname in CLIENTS:
        conn = _connect(dbname)
        row = conn.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = %s AND conrelid = 'public.client_trips'::regclass",
            (STRICT_CONSTRAINT,),
        ).fetchone()
        out[code] = None if row is None else bool(row[0])
        conn.rollback()
        conn.close()
    return out


def build_fleet() -> None:
    platform = _connect(_platform_db_name())
    platform.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    platform.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
    platform.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations "
        "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    platform.execute("DELETE FROM public.schema_migrations")
    # Before the migrations, never instead of them: the two relations below are
    # the `api/main.py` bootstrap a real platform database already has, and
    # 033/035/068 declare foreign keys onto them.
    platform.execute(PORTAL_BOOTSTRAP_SQL)
    for name in PLATFORM_MIGRATIONS:
        platform.execute((ROOT / "db/migrations" / name).read_text())
        platform.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (name,),
        )
    # The identity marker the closure tool verifies before it will run at all.
    platform.execute("CREATE SCHEMA ops_control")
    platform.execute(
        """CREATE TABLE ops_control.environment_identity (
             identity_key TEXT PRIMARY KEY,
             environment TEXT NOT NULL,
             database_identity_id UUID NOT NULL,
             database_role TEXT NOT NULL,
             database_name TEXT NOT NULL)"""
    )
    platform.execute(
        "INSERT INTO ops_control.environment_identity VALUES "
        "('primary', %s, %s, 'platform', %s)",
        (ENVIRONMENT, PLATFORM_UUID, _platform_db_name()),
    )
    for client_id, code, dbname in CLIENTS:
        platform.execute(
            """INSERT INTO workflow_a_control.client_account
              (client_id,client_code,client_name,provider_type,provider_base_url,
               provider_basic_auth_username,provider_basic_auth_password_secret_ref,
               client_db_host,client_db_port,client_db_name,client_db_user,
               client_db_password_secret_ref,client_db_schema,
               speed_trigger_filter_text,enabled,trips_pagination_mode)
              VALUES (%s,%s,%s,'telematics','https://example.invalid','u','REF',
                      %s,%s,%s,%s,%s,'public','speeding',true,%s)""",
            (client_id, code, code, _host(), _port(), dbname,
             _pg_user(), SECRET_ENV, MODE),
        )
        platform.execute(
            """INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
               run_time,timezone,lookback_days,overwrite_existing)
              VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',3,true)""",
            (str(uuid.uuid4()), client_id, code),
        )
    platform.commit()
    platform.close()

    admin = _admin()
    for _client_id, _code, dbname in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()

    for _client_id, _code, dbname in CLIENTS:
        conn = _connect(dbname)
        conn.execute(
            """CREATE TABLE public.client_trips (
                 client_id UUID NOT NULL,
                 provider_trip_id BIGINT NOT NULL,
                 synced_at TIMESTAMPTZ NULL,
                 PRIMARY KEY (client_id, provider_trip_id))"""
        )
        conn.execute(
            "CREATE TABLE public.schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.execute((ROOT / "db/client_business" / CLIENT_MIGRATION_M4).read_text())
        conn.execute((ROOT / "db/client_business" / CLIENT_MIGRATION_MLAG).read_text())
        for name in (CLIENT_MIGRATION_M4, CLIENT_MIGRATION_MLAG):
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)", (name,)
            )
        conn.commit()
        conn.close()


def _pg_user() -> str:
    return os.environ[ENV].split("//", 1)[1].split(":", 1)[0]


def _pg_password() -> str:
    return os.environ[ENV].split("//", 1)[1].split(":", 1)[1].split("@", 1)[0]


def export_default_connection_env() -> None:
    """Point the REAL `default_platform_conn`/`default_client_conn` at the fixture.

    This is what lets the suite drive `activate_release` unmodified: the fence
    it opens internally is the production one, connecting the production way.
    """
    os.environ["POSTGRES_HOST"] = _host()
    os.environ["POSTGRES_PORT"] = str(_port())
    os.environ["POSTGRES_DB"] = _platform_db_name()
    os.environ["POSTGRES_USER"] = _pg_user()
    os.environ["POSTGRES_PASSWORD"] = _pg_password()
    os.environ[SECRET_ENV] = _pg_password()


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _repository_document() -> Dict:
    """The repository declaration, narrowed to the migrations this fixture applies.

    The fixture materializes exactly `CLIENT_MIGRATION_M4` and
    `CLIENT_MIGRATION_MLAG` in each client business database, so a
    `client_business` requirement for any other migration would refuse every
    activation here for a reason that has nothing to do with the race under
    test. Narrowing keeps this suite independent of whichever other client
    migrations the working copy happens to declare; the pairing relation this
    suite is about is untouched.
    """
    document = json.loads((ROOT / "db/schema_requirements.json").read_text("utf-8"))
    applied = {CLIENT_MIGRATION_M4, CLIENT_MIGRATION_MLAG}
    document["requirements"] = [
        requirement for requirement in document["requirements"]
        if requirement.get("scope") != "client_business"
        or requirement.get("migration") in applied
    ]
    return document


def _legacy_document() -> Dict:
    """M4-era by PROPERTY: no capability, the pairing relation pinned to EXPAND."""
    document = _repository_document()
    document.pop("capabilities", None)
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            alternatives = relation.pop("constraint_alternatives", None)
            if alternatives:
                relation["constraints"] = alternatives[0]["constraints"]
    return document


def build_release_root(repo: Path, release_root: Path) -> Dict[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "race@test.invalid")
    _git(repo, "config", "user.name", "race-test")
    _git(repo, "config", "commit.gpgsign", "false")

    ids: Dict[str, str] = {}
    for name, document in (
        ("bridge-a", _repository_document()),
        ("bridge-b", _repository_document()),
        ("legacy-c", _legacy_document()),
    ):
        target = repo / "db" / "schema_requirements.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")
        (repo / "VARIANT").write_text(f"{name}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"variant {name}")
        commit = _git(repo, "rev-parse", "HEAD").strip()
        ids[name] = str(prepare_release(
            source_repo=repo, release_root=release_root, committish=commit,
        )["release_id"])
    return ids


# ---------------------------------------------------------------------------
# Real lock observation — no sleeping and hoping
# ---------------------------------------------------------------------------

def wait_for_blocked_transition_lock(deadline: float = DEADLINE_SECONDS) -> bool:
    """Block until a session is genuinely WAITING on the transition lock.

    Reads `pg_locks` for an ungranted advisory lock on exactly
    `SCHEMA_TRANSITION_LOCK_KEY`. That is observed database state: when it is
    true, the other participant has reached its wait and cannot have committed
    anything past it. Returns False if it never appears within `deadline`.
    """
    classid = (SCHEMA_TRANSITION_LOCK_KEY >> 32) & 0xFFFFFFFF
    objid = SCHEMA_TRANSITION_LOCK_KEY & 0xFFFFFFFF
    conn = _connect(_platform_db_name())
    try:
        limit = time.monotonic() + deadline
        while time.monotonic() < limit:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "  AND classid = %s AND objid = %s AND NOT granted",
                (classid, objid),
            ).fetchone()
            conn.rollback()
            if int(row[0]) > 0:
                return True
            time.sleep(0.02)
        return False
    finally:
        conn.close()


def wait_for(event: threading.Event, label: str) -> bool:
    if event.wait(DEADLINE_SECONDS):
        return True
    _check(f"{label} (waited {DEADLINE_SECONDS}s)", False)
    return False


# ---------------------------------------------------------------------------
# The closure, through its real entry point
# ---------------------------------------------------------------------------

#: The source repository the fixture's releases were cut from. Set once in
#: `main`, so `closure_args` can hand the closure the same repository the
#: release manifests are cross-checked against.
SOURCE_REPO: Optional[Path] = None


def closure_args(release_root: Path, *, execute: bool) -> argparse.Namespace:
    return argparse.Namespace(
        client_code=None,
        expected_environment=ENVIRONMENT,
        expected_platform_uuid=PLATFORM_UUID,
        check_only=not execute,
        approval_ref="RACE-TEST",
        execute=execute,
        confirm="CLOSE_CONTRACT",
        rollback_window_closed=True,
        release_root=str(release_root),
        dsn=os.environ[ENV],
        # An explicit, non-production trust boundary. `ENVIRONMENT` is not
        # "production", so this flag is the only thing that lets the real
        # closure drive a temporary inventory — and
        # `test_mlag_rollback_envelope.py` proves it buys nothing under a
        # production identity.
        allow_non_production_release_root=True,
        source_repo=str(SOURCE_REPO) if SOURCE_REPO else None,
    )


def run_closure(release_root: Path, *, execute: bool = True):
    from ops.close_telematics_first_seen_pair_contract import run
    return run(closure_args(release_root, execute=execute))


# ---------------------------------------------------------------------------
# 1 — ACTIVATION WINS THE COORDINATION FIRST
# ---------------------------------------------------------------------------

def test_activation_first(release_root: Path, repo: Path, ids: Dict[str, str]) -> None:
    """A CONTRACT closure cannot commit inside a live activation's window."""
    print("\n## test_activation_first")
    set_expand_state()

    in_fence = threading.Event()
    may_swap = threading.Event()
    real_swap = release_boundary._swap_pointer
    state_while_blocked: Dict[str, object] = {}

    def _parked_swap(pointer, relative_target):
        # The seam. `_swap_pointer` is called INSIDE the fence, so parking here
        # parks the activation at exactly the interval under test — with the
        # transition lock and the fleet lock both held.
        if not in_fence.is_set():
            in_fence.set()
            may_swap.wait(DEADLINE_SECONDS)
        return real_swap(pointer, relative_target)

    activation: Dict[str, object] = {}
    closure: Dict[str, object] = {}

    def _activate() -> None:
        try:
            activation["result"] = activate_release(
                release_root=release_root, release_id=ids["bridge-a"],
                source_repo=repo,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
            activation["error"] = exc

    def _close() -> None:
        try:
            closure["result"] = run_closure(release_root)
        except BaseException as exc:  # noqa: BLE001
            closure["error"] = exc

    release_boundary._swap_pointer = _parked_swap
    try:
        activator = threading.Thread(target=_activate, name="activation")
        activator.start()
        if not wait_for(in_fence, "the activation reached its fence"):
            may_swap.set()
            activator.join(DEADLINE_SECONDS)
            return

        closer = threading.Thread(target=_close, name="closure")
        closer.start()

        observed = wait_for_blocked_transition_lock()
        _check("the closure genuinely BLOCKS on the transition lock while the "
               "activation holds it (observed in pg_locks, not assumed)",
               observed)
        state_while_blocked.update(strict_constraint_state())
        _check("THE REVIEW'S REPRODUCTION IS CLOSED: no client committed strict "
               "CONTRACT state inside the activation's window",
               all(v is None for v in state_while_blocked.values()),
               str(state_while_blocked))

        may_swap.set()
        activator.join(DEADLINE_SECONDS)
        closer.join(DEADLINE_SECONDS)
    finally:
        release_boundary._swap_pointer = real_swap
        may_swap.set()

    _check("the activation completed without error", "error" not in activation,
           repr(activation.get("error")))
    _check("and it is internally consistent with the schema state it authorized: "
           "the pointer moved to the release it validated in EXPAND",
           pointer_release_id(release_root / "current") == ids["bridge-a"],
           str(pointer_release_id(release_root / "current")))
    _check("the closure then proceeded, once the lock was free",
           "error" not in closure, repr(closure.get("error")))
    after = strict_constraint_state()
    _check("and every client is now in validated strict CONTRACT state",
           all(v is True for v in after.values()), str(after))


# ---------------------------------------------------------------------------
# 2 — CONTRACT CLOSURE WINS THE COORDINATION FIRST
# ---------------------------------------------------------------------------

def test_closure_first(release_root: Path, repo: Path, ids: Dict[str, str]) -> None:
    """A stale pre-fence capability check must not authorize the swap.

    The legacy activation's UNFENCED preflight runs while the fleet is still in
    EXPAND, so it passes — exactly the stale observation the review exploited.
    The closure then commits. The activation must nonetheless refuse, and refuse
    BEFORE the pointer moves, because the decision that authorizes the swap is
    re-taken inside the fence under the transition lock.
    """
    print("\n## test_closure_first")
    set_expand_state()
    before = pointer_release_id(release_root / "current")

    preflight_done = threading.Event()
    closure_committed = threading.Event()
    real_fence = release_boundary._fleet_fence

    def _parked_fence(fingerprint, declared_capabilities=frozenset()):
        # Pure ordering seam: it delays ENTRY to the fence and then delegates to
        # the real one. Nothing about the fence's behaviour is altered.
        preflight_done.set()
        closure_committed.wait(DEADLINE_SECONDS)
        return real_fence(fingerprint, declared_capabilities)

    activation: Dict[str, object] = {}

    def _activate() -> None:
        try:
            activation["result"] = activate_release(
                release_root=release_root, release_id=ids["legacy-c"],
                source_repo=repo,
            )
        except BaseException as exc:  # noqa: BLE001
            activation["error"] = exc

    release_boundary._fleet_fence = _parked_fence
    try:
        activator = threading.Thread(target=_activate, name="legacy-activation")
        activator.start()
        if not wait_for(preflight_done,
                        "the legacy activation passed its unfenced preflight"):
            closure_committed.set()
            activator.join(DEADLINE_SECONDS)
            return
        _check("the legacy release's preflight PASSED against the EXPAND fleet — "
               "which is precisely the stale observation the fix must not trust",
               True)

        exit_code, _report = run_closure(release_root)
        _check("the CONTRACT closure completed while the activation was still "
               "short of its fence", exit_code == 0, f"exit={exit_code}")
        committed = strict_constraint_state()
        _check("strict CONTRACT state is committed on every client",
               all(v is True for v in committed.values()), str(committed))

        closure_committed.set()
        activator.join(DEADLINE_SECONDS)
    finally:
        release_boundary._fleet_fence = real_fence
        closure_committed.set()

    error = activation.get("error")
    _check("the legacy activation was REFUSED",
           isinstance(error, SchemaPreflightError), repr(error))
    _check("with the capability refusal, decided inside the fence",
           isinstance(error, SchemaPreflightError)
           and error.code == "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING",
           getattr(error, "code", repr(error)))
    _check("AND THE POINTER NEVER MOVED",
           pointer_release_id(release_root / "current") == before,
           f"before={before} after={pointer_release_id(release_root / 'current')}")
    _check("`previous` was not disturbed either",
           pointer_release_id(release_root / "previous") != ids["legacy-c"],
           str(pointer_release_id(release_root / "previous")))


# ---------------------------------------------------------------------------
# 3 / 4 — the same two releases, sequentially, against a CLOSED contract
# ---------------------------------------------------------------------------

def test_after_closure_legacy_is_refused_and_bridge_is_allowed(
    release_root: Path, repo: Path, ids: Dict[str, str],
) -> None:
    print("\n## test_after_closure_legacy_is_refused_and_bridge_is_allowed")
    state = strict_constraint_state()
    _check("the fleet is in validated strict CONTRACT state to begin with",
           all(v is True for v in state.values()), str(state))

    before = pointer_release_id(release_root / "current")
    refused = None
    try:
        activate_release(release_root=release_root, release_id=ids["legacy-c"],
                         source_repo=repo)
    except SchemaPreflightError as exc:
        refused = exc
    _check("a legacy release is refused after closure",
           refused is not None and refused.code
           == "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING",
           getattr(refused, "code", "it was NOT refused"))
    _check("and the pointer did not move",
           pointer_release_id(release_root / "current") == before)

    target = (ids["bridge-b"] if before != ids["bridge-b"] else ids["bridge-a"])
    result = activate_release(release_root=release_root, release_id=target,
                              source_repo=repo)
    _check("a bridge release remains activatable after closure",
           bool(result.get("changed")), str(result))
    _check("and the pointer moved to it",
           pointer_release_id(release_root / "current") == target)
    _check("so the one-step rollback target is itself bridge-compatible",
           pointer_release_id(release_root / "previous") in
           (ids["bridge-a"], ids["bridge-b"]),
           str(pointer_release_id(release_root / "previous")))


def test_closure_is_idempotent_and_resumable_under_the_lock(
    release_root: Path,
) -> None:
    """The coordination must not have broken resumability or idempotency."""
    print("\n## test_closure_is_idempotent_and_resumable_under_the_lock")
    exit_code, report = run_closure(release_root)
    _check("re-running the closure against a CLOSED fleet is a clean no-op",
           exit_code == 0, f"exit={exit_code}")
    _check("every client reports ALREADY_CLOSED",
           all(entry["result"] == "ALREADY_CLOSED"
               for entry in report["clients"]),
           str([e["result"] for e in report["clients"]]))
    _check("and the run held the transition lock",
           report.get("schema_transition_lock_held") is True, str(report.get(
               "schema_transition_lock_held")))

    # Interrupt one client between the swap and the validate, then resume.
    conn = _connect(CLIENTS[0][2])
    conn.execute(f"ALTER TABLE public.client_trips "
                 f"DROP CONSTRAINT {STRICT_CONSTRAINT}")
    conn.execute(
        f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
        "CHECK ((first_seen_request_id IS NULL) "
        "       = (first_seen_response_received_at_utc IS NULL)) NOT VALID"
    )
    conn.execute("DELETE FROM public.schema_migrations WHERE filename LIKE %s",
                 ("049_client_trips_first_seen_pair_contract%",))
    conn.commit()
    conn.close()

    state = strict_constraint_state()
    _check("one client is now in the INTERRUPTED strict-NOT-VALID state",
           state[CLIENTS[0][1]] is False and state[CLIENTS[1][1]] is True,
           str(state))

    exit_code, report = run_closure(release_root)
    _check("the interrupted client is resumed to completion", exit_code == 0,
           f"exit={exit_code}")
    state = strict_constraint_state()
    _check("and both clients are validated again",
           all(v is True for v in state.values()), str(state))


def test_read_only_modes_take_no_lock(release_root: Path) -> None:
    print("\n## test_read_only_modes_take_no_lock")
    _exit_code, report = run_closure(release_root, execute=False)
    _check("a check-only run takes no transition lock",
           report.get("schema_transition_lock_held") is False,
           str(report.get("schema_transition_lock_held")))
    _check("and still reports the G6 rollback envelope",
           report.get("G6_rollback_envelope", {}).get("ready") is True,
           str(report.get("G6_rollback_envelope", {}).get("reasons")))


# ---------------------------------------------------------------------------
# G6 MUST BE DECIDED INSIDE THE COORDINATION, NOT BEFORE IT
# ---------------------------------------------------------------------------
#
# The second independent review reproduced this exactly: G6 reported READY, a
# SUPPORTED release operation then moved `current`/`previous`, and the closure
# went on to acquire the transition lock and act on the snapshot it had taken
# before that mutation. The two tests below are the two halves of the fix — the
# stale snapshot must be overridden by a protected re-read, and once the
# protected read has been accepted, no pointer mutation may invalidate it.

def _repoint(release_root: Path, *, current: str, previous: str) -> None:
    """Set both pointers directly, to restore a known fixture state."""
    for role, release_id in (("current", current), ("previous", previous)):
        pointer = release_root / role
        if pointer.is_symlink() or pointer.exists():
            pointer.unlink()
        pointer.symlink_to(f"releases/{release_id}")


def test_a_pointer_change_before_the_lock_is_observed_by_g6(
    release_root: Path, repo: Path, ids: Dict[str, str],
) -> None:
    """THE REVIEW'S STALE-WINDOW REPRODUCTION, now a refusal.

    The seam is `schema_transition_lock` itself: wrapping it runs the mutation
    at precisely the interval under test — after the closure has taken its
    observational G6 snapshot and before it owns any coordination. Nothing about
    the closure's own ordering is changed by the wrapper.
    """
    print("\n## test_a_pointer_change_before_the_lock_is_observed_by_g6")
    import ops.close_telematics_first_seen_pair_contract as closure

    set_expand_state()
    _repoint(release_root, current=ids["bridge-b"], previous=ids["bridge-a"])
    snapshot = closure.rollback_envelope(release_root, source_repo=repo)
    _check("G6_SNAPSHOT_READY is True before the closure starts",
           snapshot["ready"] is True, str(snapshot["reasons"]))
    _check("and that snapshot is labelled OBSERVATIONAL, not an authorization",
           snapshot["authority"] == closure.G6_OBSERVATIONAL,
           str(snapshot.get("authority")))

    real_lock = closure.schema_transition_lock
    mutated: Dict[str, object] = {}

    def _mutate_then_lock(**kwargs):
        # Called at `with schema_transition_lock(...)`, BEFORE the context
        # manager body runs and therefore before the lock is acquired.
        if not mutated:
            activate_release(release_root=release_root,
                             release_id=ids["legacy-c"], source_repo=repo)
            mutated["current"] = pointer_release_id(release_root / "current")
            mutated["previous"] = pointer_release_id(release_root / "previous")
        return real_lock(**kwargs)

    closure.schema_transition_lock = _mutate_then_lock
    try:
        run_closure(release_root)
        _check("the closure refuses on the LIVE envelope, not the snapshot",
               False, "run() returned instead of refusing")
    except closure.ContractRefused as exc:
        _check("the closure refuses on the LIVE envelope, not the snapshot",
               exc.code == "ROLLBACK_ENVELOPE_NOT_MATERIALIZED", str(exc))
        _check("and says the refusal came from the protected re-evaluation",
               "SCHEMA_TRANSITION_LOCK_KEY" in str(exc), str(exc))
    except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
        _check("the closure refuses on the LIVE envelope, not the snapshot",
               False, f"{type(exc).__name__}: {exc}")
    finally:
        closure.schema_transition_lock = real_lock

    _check("the supported activation really did move the pointers first",
           mutated.get("current") == ids["legacy-c"], str(mutated))
    _check("and G7 did not bypass it: --rollback-window-closed was set for this "
           "very run and the protected G6 refused anyway",
           closure_args(release_root, execute=True).rollback_window_closed is True)
    state = strict_constraint_state()
    _check("and NO client entered strict CONTRACT state: the refusal happened "
           "before any client DDL",
           all(v is None for v in state.values()), str(state))

    _repoint(release_root, current=ids["bridge-b"], previous=ids["bridge-a"])


def test_a_pointer_mutation_cannot_invalidate_an_accepted_g6(
    release_root: Path, repo: Path, ids: Dict[str, str],
) -> None:
    """The inverse: once G6 is accepted under the lock, it stays true.

    The closure is parked immediately after its PROTECTED G6 returns, while it
    holds `SCHEMA_TRANSITION_LOCK_KEY`. A supported activation is then started
    and observed genuinely BLOCKING in `pg_locks` — so the envelope the closure
    accepted cannot move underneath it for as long as it matters.
    """
    print("\n## test_a_pointer_mutation_cannot_invalidate_an_accepted_g6")
    import ops.close_telematics_first_seen_pair_contract as closure

    set_expand_state()
    _repoint(release_root, current=ids["bridge-b"], previous=ids["bridge-a"])

    real_envelope = closure.rollback_envelope
    parked = threading.Event()
    may_proceed = threading.Event()
    accepted: Dict[str, object] = {}

    def _park_after_protected_g6(root, **kwargs):
        status = real_envelope(root, **kwargs)
        if (kwargs.get("authority") == closure.G6_PROTECTED
                and not parked.is_set()):
            accepted["status"] = status
            parked.set()
            may_proceed.wait(DEADLINE_SECONDS)
        return status

    closer_result: Dict[str, object] = {}
    activation_result: Dict[str, object] = {}

    def _close() -> None:
        try:
            closer_result["result"] = run_closure(release_root)
        except BaseException as exc:  # noqa: BLE001
            closer_result["error"] = exc

    def _activate() -> None:
        try:
            activation_result["result"] = activate_release(
                release_root=release_root, release_id=ids["legacy-c"],
                source_repo=repo,
            )
        except BaseException as exc:  # noqa: BLE001
            activation_result["error"] = exc

    closure.rollback_envelope = _park_after_protected_g6
    try:
        closer = threading.Thread(target=_close, name="closure")
        closer.start()
        if not wait_for(parked, "the closure reached its protected G6"):
            may_proceed.set()
            closer.join(DEADLINE_SECONDS)
            return

        _check("the protected G6 is READY and labelled PROTECTED",
               bool(accepted["status"]["ready"])
               and accepted["status"]["authority"] == closure.G6_PROTECTED,
               str(accepted["status"].get("authority")))

        activator = threading.Thread(target=_activate, name="activation")
        activator.start()
        blocked = wait_for_blocked_transition_lock()
        _check("a supported activation genuinely BLOCKS on the same key while "
               "the closure holds the accepted envelope (observed in pg_locks)",
               blocked)
        _check("so the pointers the protected G6 accepted have not moved",
               pointer_release_id(release_root / "current") == ids["bridge-b"]
               and pointer_release_id(release_root / "previous") == ids["bridge-a"],
               f"current={pointer_release_id(release_root / 'current')} "
               f"previous={pointer_release_id(release_root / 'previous')}")
        live = real_envelope(release_root, source_repo=repo)
        _check("and the envelope is still READY when re-read at this instant",
               live["ready"] is True, str(live["reasons"]))

        may_proceed.set()
        closer.join(DEADLINE_SECONDS)
        activator.join(DEADLINE_SECONDS)
    finally:
        closure.rollback_envelope = real_envelope
        may_proceed.set()

    _check("the closure completed without error", "error" not in closer_result,
           str(closer_result.get("error")))
    state = strict_constraint_state()
    _check("every client is in validated strict CONTRACT state",
           all(v is True for v in state.values()), str(state))
    _check("and the legacy activation that waited behind it is REFUSED by the "
           "state guard rather than swapped in",
           isinstance(activation_result.get("error"), BaseException),
           str(activation_result.get("result")))
    _check("current is still the bridge release",
           pointer_release_id(release_root / "current") == ids["bridge-b"],
           str(pointer_release_id(release_root / "current")))


# ---------------------------------------------------------------------------
# The safety invariant, stated as a structural property
# ---------------------------------------------------------------------------

def test_the_authoritative_check_precedes_the_yield() -> None:
    print("\n## test_the_authoritative_check_precedes_the_yield")
    source = (ROOT / "ops/release_schema_preflight.py").read_text()
    fence = source[source.index("def activation_fence("):]
    body = fence[:fence.index("\n    finally:")]
    _check("the fence takes the transition lock",
           "pg_advisory_xact_lock" in body)
    _check("the fence still takes the real fleet TABLE lock, not only an "
           "advisory one", "LOCK TABLE" in body and "IN SHARE MODE" in body)
    _check("the authoritative state-guard re-read happens inside the fence",
           body.index("check_schema_state_guards") < body.index("yield observed"))
    _check("the fingerprint comparison also precedes the yield",
           body.index("RELEASE_SCHEMA_FLEET_CHANGED_DURING_ACTIVATION")
           < body.index("yield observed"))

    boundary = (ROOT / "ops/release_boundary.py").read_text()
    _check("the pointer swap happens inside the fence block",
           boundary.index("with fence:")
           < boundary.index("_swap_pointer(layout.current"))
    _check("and the release's declared capabilities are handed to the fence",
           "declared_capabilities" in boundary)

    closure = (ROOT / "ops/close_telematics_first_seen_pair_contract.py").read_text()
    _check("the closure takes the SAME coordination primitive",
           "schema_transition_lock" in closure)
    _check("and holds it across the whole client mutation sequence",
           closure.index("with schema_transition_lock(")
           < closure.index("_process_clients()\n        except"))
    _check("the AUTHORITATIVE G6 is evaluated INSIDE the lock, not before it",
           closure.index("with schema_transition_lock(")
           < closure.index("authority=G6_PROTECTED"))
    _check("and the protected result gates the client transition",
           closure.index("authority=G6_PROTECTED")
           < closure.index("_envelope_refusal(protected, protected=True)")
           < closure.index("_process_clients()\n        except"))
    _check("while the pre-lock envelope can only refuse, never authorize",
           closure.index("authority=G6_OBSERVATIONAL")
           < closure.index("with schema_transition_lock(")
           and "_envelope_refusal(envelope, protected=False)" in closure)


def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    export_default_connection_env()
    build_fleet()

    with tempfile.TemporaryDirectory(prefix="mlag-race-") as tmp:
        base = Path(tmp).resolve()
        repo = base / "repo"
        release_root = base / "release-root"
        global SOURCE_REPO
        SOURCE_REPO = repo
        ids = build_release_root(repo, release_root)

        # The supported rollout, executed: bridge-A then bridge-B, leaving
        # current = bridge-B and previous = bridge-A. That is the only state in
        # which the closure tool's G6 gate permits `--execute` at all, so the
        # race tests below run against a genuinely closable fleet.
        set_expand_state()
        activate_release(release_root=release_root, release_id=ids["bridge-a"],
                         source_repo=repo)
        activate_release(release_root=release_root, release_id=ids["bridge-b"],
                         source_repo=repo)
        _check("setup: current = bridge-B",
               pointer_release_id(release_root / "current") == ids["bridge-b"])
        _check("setup: previous = bridge-A",
               pointer_release_id(release_root / "previous") == ids["bridge-a"])

        test_activation_first(release_root, repo, ids)
        test_closure_first(release_root, repo, ids)
        test_after_closure_legacy_is_refused_and_bridge_is_allowed(
            release_root, repo, ids)
        test_closure_is_idempotent_and_resumable_under_the_lock(release_root)
        test_read_only_modes_take_no_lock(release_root)
        test_a_pointer_change_before_the_lock_is_observed_by_g6(
            release_root, repo, ids)
        test_a_pointer_mutation_cannot_invalidate_an_accepted_g6(
            release_root, repo, ids)
        test_the_authoritative_check_precedes_the_yield()

        _chmod_writable(release_root)

    admin = _admin()
    for _client_id, _code, dbname in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.close()

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


def _chmod_writable(path: Path) -> None:
    for current, _dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in files:
            try:
                os.chmod(Path(current) / name, 0o644)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
