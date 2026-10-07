#!/usr/bin/env python3
"""S13 persistence against a real PostgreSQL 16 (approved stage S13).

WHAT THIS PROVES THAT THE RENDERING SUITE CANNOT.
    ``test_portal_server_preferences_and_saved_views.py`` runs the response and
    canonicalization code against stubbed persistence, so it can prove what the
    server DOES with a stored object but not that the statements themselves are
    owner-scoped, that the constraints hold, or that the migration applies
    cleanly. This suite executes migration 066 for real and drives the actual
    `api/main.py` persistence functions against the database it created.

DESTRUCTIVE, AND TASK-OWNED. The suite does not accept a DSN. It STARTS its own
`postgres:16` container on a free loopback port, verifies the server really is
major version 16, creates a database of its own inside it, and removes the
container in a `finally` — so every destructive statement it issues lands in an
instance that did not exist a second earlier and will not exist a second later.

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \\
        ops/tests_manual/test_portal_s13_persistence_postgres.py

The previous shape took `PORTAL_S13_TEST_DSN` and reset the database it named,
which meant an operator's exported DSN decided which `public` schema got
dropped. That environment variable is deliberately gone: there is now no input
that can point this suite at a database it did not create.

Without a usable Docker daemon or a local `postgres:16` image the suite prints
``LIVE_MIGRATION_TEST_NOT_AVAILABLE`` and exits 0. It is evidence when a
disposable instance can be created and is never a reason to touch a real one.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION = REPO_ROOT / "db" / "migrations" / "066_portal_account_preferences_and_saved_views.sql"
# The correction. `ops/db_migrate.sh` applies each FILE once, so the authorized
# structure is established by the SEQUENCE 066 then 067, and every structural
# assertion below is made after both have run.
CORRECTION_MIGRATION = REPO_ROOT / "db" / "migrations" / "067_portal_saved_object_structural_integrity.sql"

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DATASET_B = "dddddddd-dddd-dddd-dddd-dddddddddddd"

# Relations this suite creates as prerequisites, plus the three 066 adds. Used
# to prove no unrelated relation is touched.
PREREQUISITE_TABLES = {"artifact_users", "portal_clients", "portal_database_datasets", "unrelated_keepsake"}
S13_TABLES = {"portal_user_preferences", "portal_database_saved_views", "portal_database_column_sets"}


def _install_fastapi_stubs() -> None:
    """FastAPI and the S6 crypto dependency are stubbed; `psycopg` is REAL.

    The disposable-database interpreter carries `psycopg` but not
    `cryptography`, which `api/row_reference.py` imports for the S6 opaque row
    reference. S13 neither reads nor writes a row reference — one of the things
    this suite asserts is that no persisted object ever contains one — so the
    module is satisfied with an inert stand-in rather than made a prerequisite
    of running the persistence tests.
    """
    crypto = types.ModuleType("cryptography")
    exceptions = types.ModuleType("cryptography.exceptions")

    class _InvalidTag(Exception):
        pass

    exceptions.InvalidTag = _InvalidTag
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    hashes = types.ModuleType("cryptography.hazmat.primitives.hashes")
    hashes.SHA256 = object
    ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")
    aead = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")
    aead.AESGCM = object
    kdf = types.ModuleType("cryptography.hazmat.primitives.kdf")
    hkdf = types.ModuleType("cryptography.hazmat.primitives.kdf.hkdf")
    hkdf.HKDF = object
    for name, module in (
        ("cryptography", crypto),
        ("cryptography.exceptions", exceptions),
        ("cryptography.hazmat", hazmat),
        ("cryptography.hazmat.primitives", primitives),
        ("cryptography.hazmat.primitives.hashes", hashes),
        ("cryptography.hazmat.primitives.ciphers", ciphers),
        ("cryptography.hazmat.primitives.ciphers.aead", aead),
        ("cryptography.hazmat.primitives.kdf", kdf),
        ("cryptography.hazmat.primitives.kdf.hkdf", hkdf),
    ):
        sys.modules.setdefault(name, module)

    class _App:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return lambda fn: fn

        post = patch = delete = on_event = get

    class _HTMLResponse:
        def __init__(self, content, status_code=200, headers=None, media_type=None):
            self.body = str(content).encode("utf-8")
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type or "text/html"

    class _StreamingResponse:
        def __init__(self, body, media_type=None, headers=None):
            self.body = body
            self.media_type = media_type
            self.headers = headers or {}
            self.status_code = 200

    class _HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _identity_default(default=None, *a, **k):
        return default

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = _identity_default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = _identity_default
    fastapi.Form = _identity_default
    fastapi.Query = _identity_default
    fastapi.Body = _identity_default
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = _HTMLResponse
    responses.StreamingResponse = _StreamingResponse
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: None
    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)


_install_fastapi_stubs()

try:  # noqa: E402
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - interpreter without the driver
    # A repository-wide sweep runs every suite with the default interpreter,
    # which does not carry `psycopg`. That is the same "no disposable instance
    # available" situation and must read as such, not as a failure that hides a
    # real one.
    print("LIVE_MIGRATION_TEST_NOT_AVAILABLE: psycopg is unavailable; run this suite with .venv/bin/python")
    raise SystemExit(0)

from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

import api.main as api_main  # noqa: E402

DSN = ""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def run(sql: str, values=None):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return []


def _reset_schema() -> None:
    run("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    run("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    run(
        """
        CREATE TABLE artifact_users (
          user_id UUID PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          is_active BOOLEAN NOT NULL DEFAULT true
        );
        CREATE TABLE portal_clients (client_code TEXT PRIMARY KEY);
        CREATE TABLE portal_database_datasets (
          dataset_id UUID PRIMARY KEY,
          client_code TEXT NOT NULL REFERENCES portal_clients(client_code),
          dataset_name TEXT NOT NULL
        );
        -- A relation 066 must not touch, so "no unrelated damage" is testable
        -- rather than asserted.
        CREATE TABLE unrelated_keepsake (id INT PRIMARY KEY, note TEXT NOT NULL);
        """
    )
    run("INSERT INTO artifact_users (user_id, username) VALUES (%s, 'alice'), (%s, 'bob')", (ALICE, BOB))
    run("INSERT INTO portal_clients VALUES ('ACME_01')")
    run(
        "INSERT INTO portal_database_datasets (dataset_id, client_code, dataset_name) "
        "VALUES (%s, 'ACME_01', 'Trips'), (%s, 'ACME_01', 'Events')",
        (DATASET_A, DATASET_B),
    )
    run("INSERT INTO unrelated_keepsake VALUES (1, 'do not touch')")


def _apply_migration(path=MIGRATION) -> None:
    """Apply one migration file exactly as `ops/db_migrate.sh` would."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))


def _apply_migration_sequence() -> None:
    """The authorized LOCAL sequence: 066, then the 067 correction."""
    _apply_migration(MIGRATION)
    _apply_migration(CORRECTION_MIGRATION)


def _apply_correction_expecting_failure() -> tuple[str, str]:
    """`(sqlstate, message)` for a correction that must refuse to succeed."""
    try:
        _apply_migration(CORRECTION_MIGRATION)
    except psycopg.errors.Error as exc:
        return str(getattr(exc, "sqlstate", "") or ""), str(exc)
    raise AssertionError("migration 067 accepted a schema it must have refused")


def _tables() -> set[str]:
    return {
        row["table_name"]
        for row in run("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    }


def _expect_error(sql: str, values=None) -> str:
    try:
        run(sql, values)
    except psycopg.errors.Error as exc:
        return str(getattr(exc, "sqlstate", "") or "")
    raise AssertionError(f"statement was expected to fail: {sql[:120]}")


# ===========================================================================
# 1. Migration
# ===========================================================================
def test_migration_applies_cleanly_and_preserves_existing_users() -> None:
    _reset_schema()
    before = _tables()
    assert before == PREREQUISITE_TABLES, before
    _apply_migration_sequence()
    after = _tables()
    assert after == PREREQUISITE_TABLES | S13_TABLES, after

    # Existing accounts survive untouched and are already valid: no preference
    # row exists, which IS the AUTO default.
    users = run("SELECT user_id, username, is_active FROM artifact_users ORDER BY username")
    assert [u["username"] for u in users] == ["alice", "bob"], users
    assert run("SELECT count(*) AS n FROM portal_user_preferences")[0]["n"] == 0
    assert run("SELECT note FROM unrelated_keepsake")[0]["note"] == "do not touch"
    print("PASS: migration 066 applies cleanly, adds exactly three relations and preserves existing rows")


def test_migration_is_re_executable() -> None:
    _apply_migration_sequence()
    _apply_migration_sequence()
    counts = run(
        """
        SELECT table_name, count(*) AS n
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        GROUP BY table_name ORDER BY table_name
        """,
        (sorted(S13_TABLES),),
    )
    assert {row["table_name"]: row["n"] for row in counts} == {
        "portal_database_column_sets": 7,
        "portal_database_saved_views": 7,
        "portal_user_preferences": 4,
    }, counts
    print("PASS: re-executing the 066+067 sequence converges on the same structure and adds nothing")


def test_structure_constraints_and_indexes() -> None:
    columns = {
        (row["table_name"], row["column_name"]): (row["data_type"], row["is_nullable"], row["column_default"])
        for row in run(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (sorted(S13_TABLES),),
        )
    }
    assert columns[("portal_user_preferences", "theme")][:2] == ("text", "NO")
    assert "'auto'" in (columns[("portal_user_preferences", "theme")][2] or "")
    for table, payload in (
        ("portal_database_saved_views", "view_state_json"),
        ("portal_database_column_sets", "layout_state_json"),
    ):
        assert columns[(table, payload)][:2] == ("jsonb", "NO"), (table, columns[(table, payload)])
        assert columns[(table, "owner_user_id")][:2] == ("uuid", "NO")
        assert columns[(table, "dataset_id")][:2] == ("uuid", "NO")

    foreign_keys = {
        (row["table_name"], row["column_name"], row["foreign_table"], row["delete_rule"])
        for row in run(
            """
            SELECT tc.table_name, kcu.column_name, ccu.table_name AS foreign_table, rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
            JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
            JOIN information_schema.referential_constraints rc ON rc.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = ANY(%s)
            """,
            (sorted(S13_TABLES),),
        )
    }
    for table in S13_TABLES:
        assert (table, "user_id" if table == "portal_user_preferences" else "owner_user_id",
                "artifact_users", "CASCADE") in foreign_keys, (table, foreign_keys)
    for table in ("portal_database_saved_views", "portal_database_column_sets"):
        assert (table, "dataset_id", "portal_database_datasets", "CASCADE") in foreign_keys, table

    indexes = {row["indexname"] for row in run(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s)",
        (sorted(S13_TABLES),),
    )}
    assert "idx_portal_database_saved_views_owner" in indexes, indexes
    assert "idx_portal_database_column_sets_owner" in indexes, indexes
    assert "portal_database_saved_views_owner_dataset_name_key" in indexes, indexes
    assert "portal_database_column_sets_owner_dataset_name_key" in indexes, indexes
    print("PASS: columns, foreign keys, cascade rules and the owner+dataset indexes are as designed")


def test_the_database_enforces_the_theme_enum_names_and_payload_bounds() -> None:
    assert _expect_error(
        "INSERT INTO portal_user_preferences (user_id, theme) VALUES (%s, 'sepia')", (ALICE,)
    ) == "23514"
    run("INSERT INTO portal_user_preferences (user_id, theme) VALUES (%s, 'dark')", (ALICE,))
    run("DELETE FROM portal_user_preferences")

    good = json.dumps({"state_version": 1, "filters": []})
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, '', %s::jsonb)", (ALICE, DATASET_A, good)
    ) == "23514", "an empty name must be refused"
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, %s, %s::jsonb)", (ALICE, DATASET_A, "x" * 81, good)
    ) == "23514", "an over-long name must be refused"
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'x', %s::jsonb)", (ALICE, DATASET_A, json.dumps([1, 2, 3]))
    ) == "23514", "a non-object payload must be refused"
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'x', %s::jsonb)", (ALICE, DATASET_A, json.dumps({"v": "x" * 9000}))
    ) == "23514", "an oversized payload must be refused"
    # And the bound is BYTES: a payload well under 8192 characters but over 8192
    # UTF-8 bytes is refused too. Under the code-point bound 066 shipped with,
    # this row was storable and the application's own limit agreed with it.
    multibyte = json.dumps({"v": "\u017c" * 4200}, ensure_ascii=False)
    assert len(multibyte) < 8192 < len(multibyte.encode("utf-8")), (len(multibyte), len(multibyte.encode("utf-8")))
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'x', %s::jsonb)", (ALICE, DATASET_A, multibyte)
    ) == "23514", "the payload bound must be measured in UTF-8 bytes"
    # Foreign keys are real: neither a stranger nor a phantom dataset is storable.
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'x', %s::jsonb)",
        ("99999999-9999-9999-9999-999999999999", DATASET_A, good)
    ) == "23503"
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'x', %s::jsonb)",
        (ALICE, "99999999-9999-9999-9999-999999999999", good)
    ) == "23503"
    print("PASS: the theme enum, the name bounds, the payload shape/size and both foreign keys are enforced")


# ===========================================================================
# 2. The application's own statements, against the real schema
# ===========================================================================
def _document(**overrides):
    document = {
        "state_version": 1,
        "filters": [{"column_name": "driver_name", "operator": "contains", "value": "Kowalski"}],
        "sort": "trip_date", "direction": "desc", "page": 1, "limit": 100,
        "layout": {"cols": ["driver_name"], "colorder": [], "colw": {}, "colpin": None},
    }
    document.update(overrides)
    return json.dumps(document)


def test_theme_read_and_write_round_trip_and_default_to_auto() -> None:
    run("DELETE FROM portal_user_preferences")
    assert api_main._get_portal_user_theme(ALICE) == ("auto", True)
    assert api_main._set_portal_user_theme(ALICE, "dark") is True
    assert api_main._get_portal_user_theme(ALICE) == ("dark", True)
    # Account isolation is the row, not a convention.
    assert api_main._get_portal_user_theme(BOB) == ("auto", True)
    assert api_main._set_portal_user_theme(BOB, "light") is True
    assert api_main._get_portal_user_theme(ALICE) == ("dark", True)
    # Re-choosing AUTO is a value, not a delete-and-guess.
    assert api_main._set_portal_user_theme(ALICE, "auto") is True
    assert api_main._get_portal_user_theme(ALICE) == ("auto", True)
    assert run("SELECT count(*) AS n FROM portal_user_preferences")[0]["n"] == 2
    # An unknown value never reaches the database.
    assert api_main._set_portal_user_theme(ALICE, "sepia") is False
    assert api_main._get_portal_user_theme(ALICE) == ("auto", True)
    print("PASS: theme read/write round-trips per account, defaults to AUTO and refuses an unknown value")


def test_saved_object_crud_is_owner_scoped_in_the_statement() -> None:
    run("DELETE FROM portal_database_saved_views")
    run("DELETE FROM portal_database_column_sets")
    alice_view, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Trasy", state_json=_document()
    )
    assert outcome == "created" and alice_view
    bob_view, outcome = api_main._create_portal_saved_view(
        owner_user_id=BOB, dataset_id=DATASET_A, view_name="Trasy", state_json=_document()
    )
    assert outcome == "created" and bob_view, "the same name is free for a different account"

    # Read: Bob's view is invisible to Alice by every route into it.
    assert api_main._get_portal_saved_view_for_user(bob_view, ALICE) is None
    assert api_main._get_portal_saved_view_for_user(bob_view, ALICE, DATASET_A) is None
    assert api_main._get_portal_saved_view_for_user(alice_view, ALICE, DATASET_B) is None
    listed = api_main._list_portal_saved_views_for_user(ALICE, DATASET_A)
    assert [v["saved_view_id"] for v in listed] == [alice_view], listed

    # Write: rename, overwrite and delete all miss.
    assert api_main._update_portal_saved_view(
        saved_view_id=bob_view, owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Przejęty"
    ) == "not_found"
    assert api_main._update_portal_saved_view(
        saved_view_id=bob_view, owner_user_id=ALICE, dataset_id=DATASET_A,
        state_json=_document(sort="depot")
    ) == "not_found"
    assert api_main._delete_portal_saved_view(saved_view_id=bob_view, owner_user_id=ALICE) == "not_found"
    still = run("SELECT view_name, owner_user_id FROM portal_database_saved_views WHERE saved_view_id = %s", (bob_view,))
    assert still[0]["view_name"] == "Trasy" and str(still[0]["owner_user_id"]) == BOB

    # The owner's own operations work.
    assert api_main._update_portal_saved_view(
        saved_view_id=alice_view, owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Trasy 2"
    ) == "updated"
    assert api_main._delete_portal_saved_view(saved_view_id=alice_view, owner_user_id=ALICE) == "deleted"
    assert api_main._get_portal_saved_view_for_user(alice_view, ALICE) is None

    # And a non-uuid never reaches the driver.
    for hostile in ("' OR 1=1 --", "../x", "", "not-a-uuid"):
        assert api_main._get_portal_saved_view_for_user(hostile, ALICE) is None, hostile
        assert api_main._delete_portal_saved_view(saved_view_id=hostile, owner_user_id=ALICE) == "not_found", hostile
    print("PASS: saved-view CRUD is owner-scoped in the statement; an IDOR probe mutates and reveals nothing")


def test_column_set_crud_is_owner_scoped_in_the_statement() -> None:
    layout = json.dumps({"state_version": 1, "cols": ["driver_name"], "colorder": [], "colw": {}, "colpin": []})
    alice_set, outcome = api_main._create_portal_column_set(
        owner_user_id=ALICE, dataset_id=DATASET_A, set_name="Wąski", state_json=layout
    )
    assert outcome == "created"
    bob_set, outcome = api_main._create_portal_column_set(
        owner_user_id=BOB, dataset_id=DATASET_A, set_name="Wąski", state_json=layout
    )
    assert outcome == "created"
    assert api_main._get_portal_column_set_for_user(bob_set, ALICE) is None
    assert api_main._get_portal_column_set_for_user(bob_set, ALICE, DATASET_A) is None
    assert api_main._delete_portal_column_set(column_set_id=bob_set, owner_user_id=ALICE) == "not_found"
    assert [s["column_set_id"] for s in api_main._list_portal_column_sets_for_user(ALICE, DATASET_A)] == [alice_set]
    assert api_main._delete_portal_column_set(column_set_id=alice_set, owner_user_id=ALICE) == "deleted"
    assert run("SELECT count(*) AS n FROM portal_database_column_sets")[0]["n"] == 1
    print("PASS: column-set CRUD is owner-scoped in the statement")


def test_duplicate_names_are_refused_on_create_and_on_rename() -> None:
    run("DELETE FROM portal_database_saved_views")
    first, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Trasy", state_json=_document()
    )
    assert outcome == "created"
    second, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Trasy", state_json=_document()
    )
    assert (second, outcome) == (None, "duplicate"), (second, outcome)
    # Case is significant: no surprising case-insensitive policy.
    third, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_A, view_name="trasy", state_json=_document()
    )
    assert outcome == "created", outcome
    # A rename onto an existing name is the same refusal, resolved from SQLSTATE.
    assert api_main._update_portal_saved_view(
        saved_view_id=third, owner_user_id=ALICE, dataset_id=DATASET_A, view_name="Trasy"
    ) == "duplicate"
    assert run("SELECT view_name FROM portal_database_saved_views WHERE saved_view_id = %s", (third,))[0]["view_name"] == "trasy"
    # And the same name is free on another dataset.
    _id, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_B, view_name="Trasy", state_json=_document()
    )
    assert outcome == "created", outcome
    print("PASS: duplicate names refuse deterministically on create and on rename, per owner per dataset")


def test_the_per_dataset_object_limit_is_enforced() -> None:
    run("DELETE FROM portal_database_saved_views")
    limit = api_main.PORTAL_SAVED_OBJECTS_PER_DATASET_MAX
    for index in range(limit):
        _id, outcome = api_main._create_portal_saved_view(
            owner_user_id=ALICE, dataset_id=DATASET_A, view_name=f"W{index}", state_json=_document()
        )
        assert outcome == "created", (index, outcome)
    _id, outcome = api_main._create_portal_saved_view(
        owner_user_id=ALICE, dataset_id=DATASET_A, view_name="W-over", state_json=_document()
    )
    assert (_id, outcome) == (None, "limit"), (_id, outcome)
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == limit
    print("PASS: the bounded per-account per-dataset object count is enforced with a refusal, not a truncation")


def test_deleting_an_account_or_a_dataset_cascades_and_touches_nothing_else() -> None:
    run("DELETE FROM portal_database_saved_views")
    run("DELETE FROM portal_database_column_sets")
    run("DELETE FROM portal_user_preferences")
    api_main._set_portal_user_theme(BOB, "dark")
    api_main._create_portal_saved_view(owner_user_id=BOB, dataset_id=DATASET_A, view_name="B1", state_json=_document())
    api_main._create_portal_saved_view(owner_user_id=ALICE, dataset_id=DATASET_A, view_name="A1", state_json=_document())
    api_main._create_portal_saved_view(owner_user_id=ALICE, dataset_id=DATASET_B, view_name="A2", state_json=_document())

    run("DELETE FROM portal_database_datasets WHERE dataset_id = %s", (DATASET_B,))
    remaining = {row["view_name"] for row in run("SELECT view_name FROM portal_database_saved_views")}
    assert remaining == {"B1", "A1"}, remaining

    run("DELETE FROM artifact_users WHERE user_id = %s", (BOB,))
    remaining = {row["view_name"] for row in run("SELECT view_name FROM portal_database_saved_views")}
    assert remaining == {"A1"}, remaining
    assert run("SELECT count(*) AS n FROM portal_user_preferences")[0]["n"] == 0
    assert run("SELECT note FROM unrelated_keepsake")[0]["note"] == "do not touch"
    assert api_main._get_portal_user_theme(ALICE) == ("auto", True)
    print("PASS: a deleted account or dataset cascades to its own objects only, and nothing unrelated changes")


def test_an_absent_schema_degrades_instead_of_raising() -> None:
    """The migration-absent rollout case, executed rather than argued."""
    run("DROP TABLE portal_database_saved_views, portal_database_column_sets, portal_user_preferences")
    assert api_main._portal_preferences_schema_available() is False
    assert api_main._get_portal_user_theme(ALICE) == ("auto", False)
    assert api_main._set_portal_user_theme(ALICE, "dark") is False
    assert api_main._list_portal_saved_views_for_user(ALICE, DATASET_A) == []
    assert api_main._list_portal_column_sets_for_user(ALICE, DATASET_A) == []
    assert api_main._get_portal_saved_view_for_user("11111111-1111-1111-1111-111111111111", ALICE) is None
    assert api_main._delete_portal_saved_view(
        saved_view_id="11111111-1111-1111-1111-111111111111", owner_user_id=ALICE
    ) == "failed"
    _apply_migration_sequence()
    assert api_main._portal_preferences_schema_available() is True
    print("PASS: with the S13 relations absent every entry point degrades; nothing raises a raw PostgreSQL error")



# ===========================================================================
# 3. S13 review correction — structural post-conditions and partial schemas
#
# 066 alone uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
# That is the repository's additive convention, and it also means a PRE-EXISTING
# same-named relation with the wrong structure is silently accepted: the
# statement is skipped, the migration reports success, and the application's
# column-name probe declared S13 ready against a schema that cannot hold its
# invariants. These tests execute that state rather than argue about it.
# ===========================================================================
CONSTRAINTS_BY_TABLE = {
    "portal_user_preferences": {
        "portal_user_preferences_pkey",
        "portal_user_preferences_user_id_fkey",
        "portal_user_preferences_theme_check",
    },
    "portal_database_saved_views": {
        "portal_database_saved_views_pkey",
        "portal_database_saved_views_owner_user_id_fkey",
        "portal_database_saved_views_dataset_id_fkey",
        "portal_database_saved_views_name_check",
        "portal_database_saved_views_state_object_check",
        "portal_database_saved_views_state_bound_check",
        "portal_database_saved_views_owner_dataset_name_key",
    },
    "portal_database_column_sets": {
        "portal_database_column_sets_pkey",
        "portal_database_column_sets_owner_user_id_fkey",
        "portal_database_column_sets_dataset_id_fkey",
        "portal_database_column_sets_name_check",
        "portal_database_column_sets_state_object_check",
        "portal_database_column_sets_state_bound_check",
        "portal_database_column_sets_owner_dataset_name_key",
    },
}


def _constraints(table: str) -> set[str]:
    return {
        row["conname"]
        for row in run(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s)", (f"public.{table}",)
        )
    }


def _constraint_definition(table: str, name: str) -> str:
    rows = run(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = to_regclass(%s) AND conname = %s",
        (f"public.{table}", name),
    )
    return str(rows[0]["def"]) if rows else ""


def _assert_authorized_structure() -> None:
    """Every post-condition the S13 sequence must guarantee, in one place."""
    for table, expected in CONSTRAINTS_BY_TABLE.items():
        assert expected.issubset(_constraints(table)), (table, expected - _constraints(table))
    # Cascade on every foreign key: an object whose owner or dataset is gone can
    # never be scoped by an authorization query again.
    non_cascading = run(
        """
        SELECT conname FROM pg_constraint
         WHERE contype = 'f' AND confdeltype <> 'c'
           AND conrelid = ANY (ARRAY[
             to_regclass('public.portal_user_preferences'),
             to_regclass('public.portal_database_saved_views'),
             to_regclass('public.portal_database_column_sets')]::oid[])
        """
    )
    assert non_cascading == [], non_cascading
    # The payload bound is stated in BYTES on both relations.
    for table, column in (("portal_database_saved_views", "view_state_json"),
                          ("portal_database_column_sets", "layout_state_json")):
        definition = _constraint_definition(table, f"{table}_state_bound_check")
        assert "octet_length" in definition, (table, definition)
        assert "8192" in definition, (table, definition)
        assert "char_length" not in definition, (table, definition)
    # The listing indexes exist under their authorized names.
    indexes = {row["indexname"] for row in run(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s)",
        (sorted(S13_TABLES),),
    )}
    assert {"idx_portal_database_saved_views_owner", "idx_portal_database_column_sets_owner"}.issubset(indexes), indexes
    # The theme enum, the defaults and the payload type.
    assert "auto" in _constraint_definition("portal_user_preferences", "portal_user_preferences_theme_check")
    types = {
        (row["table_name"], row["column_name"]): row["data_type"]
        for row in run(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (sorted(S13_TABLES),),
        )
    }
    assert types[("portal_database_saved_views", "view_state_json")] == "jsonb", types
    assert types[("portal_database_column_sets", "layout_state_json")] == "jsonb", types


def test_the_local_sequence_establishes_the_exact_authorized_structure() -> None:
    _reset_schema()
    _apply_migration_sequence()
    _assert_authorized_structure()
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
    assert api_main._portal_preferences_schema_available() is True
    print("PASS: 066 then 067 establishes every required S13 post-condition, byte bound included")


def test_the_correction_refuses_to_stand_in_for_the_base_migration() -> None:
    _reset_schema()
    state, message = _apply_correction_expecting_failure()
    assert state == "42P01", (state, message)
    assert "066" in message, message
    assert S13_TABLES.isdisjoint(_tables()), _tables()
    print("PASS: 067 refuses to run without 066 rather than half-creating the schema")


def _prerequisites_plus(malformed_sql: str) -> None:
    _reset_schema()
    run(malformed_sql)


# Each case: a same-named relation that 066 will SKIP, and what the corrected
# sequence must do about it. `repair` means the authorized structure is reached
# additively; `refuse` means the migration fails deterministically.
MALFORMED_CASES: list[tuple[str, str, str]] = [
    (
        "saved views without the owner foreign key",
        """
        CREATE TABLE portal_database_saved_views (
          saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
          view_name TEXT NOT NULL,
          view_state_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "repair",
    ),
    (
        "saved views without any CHECK or uniqueness",
        """
        CREATE TABLE portal_database_saved_views (
          saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
          view_name TEXT NOT NULL,
          view_state_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "repair",
    ),
    (
        "saved views whose foreign keys do not cascade",
        """
        CREATE TABLE portal_database_saved_views (
          saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id),
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id),
          view_name TEXT NOT NULL,
          view_state_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "repair",
    ),
    (
        "saved views missing a column entirely",
        """
        CREATE TABLE portal_database_saved_views (
          saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
          view_name TEXT NOT NULL,
          view_state_json JSONB NOT NULL
        );
        """,
        "repair",
    ),
    (
        "the listing index defined on the wrong columns",
        """
        CREATE TABLE portal_database_saved_views (
          saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
          view_name TEXT NOT NULL,
          view_state_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_portal_database_saved_views_owner ON portal_database_saved_views (view_name);
        """,
        "repair",
    ),
    (
        "preferences without the theme constraint and with a wrong default",
        """
        CREATE TABLE portal_user_preferences (
          user_id UUID PRIMARY KEY REFERENCES artifact_users(user_id) ON DELETE CASCADE,
          theme TEXT NOT NULL DEFAULT 'sepia',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "repair",
    ),
    (
        "a payload column of the wrong type",
        """
        CREATE TABLE portal_database_column_sets (
          column_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
          dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
          set_name TEXT NOT NULL,
          layout_state_json TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "refuse",
    ),
    (
        "an owner column of the wrong type",
        """
        CREATE TABLE portal_database_column_sets (
          column_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id TEXT NOT NULL,
          dataset_id UUID NOT NULL,
          set_name TEXT NOT NULL,
          layout_state_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        "refuse",
    ),
]


def test_the_base_migration_alone_accepts_a_malformed_same_named_relation() -> None:
    """The reviewed defect, executed. 066 succeeds and leaves the schema wrong.

    This is why the correction exists, and it is also why the runtime probe
    could not be a list of column names: every column is present here.
    """
    _prerequisites_plus(MALFORMED_CASES[0][1])
    _apply_migration(MIGRATION)  # succeeds, and skips the malformed relation
    assert "portal_database_saved_views" in _tables()
    assert "portal_database_saved_views_owner_user_id_fkey" not in _constraints("portal_database_saved_views")
    # Ownership is not enforced at all: a stranger's id is storable.
    run(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'ghost', %s::jsonb)",
        ("99999999-9999-9999-9999-999999999999", DATASET_A, _document()),
    )
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == 1
    # And the corrected probe refuses to call this ready.
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE
    assert api_main._portal_preferences_schema_available() is False
    print("PASS: 066 alone accepts a malformed same-named relation; the corrected probe calls it INCOMPATIBLE")


def test_a_partial_or_malformed_schema_is_repaired_or_refused_never_accepted() -> None:
    repaired = refused = 0
    for label, malformed_sql, expectation in MALFORMED_CASES:
        _prerequisites_plus(malformed_sql)
        _apply_migration(MIGRATION)
        if expectation == "repair":
            _apply_migration(CORRECTION_MIGRATION)
            _assert_authorized_structure()
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY, label
            repaired += 1
        else:
            state, message = _apply_correction_expecting_failure()
            assert state in ("42804", "42703"), (label, state, message)
            assert "S13 schema integrity" in message, (label, message)
            # And nothing is left claiming to be ready.
            assert api_main._portal_s13_schema_state() != api_main.PORTAL_S13_SCHEMA_READY, label
            assert api_main._portal_preferences_schema_available() is False, label
            refused += 1
    assert (repaired, refused) == (6, 2), (repaired, refused)
    print(f"PASS: {repaired} malformed schemas are repaired to the exact structure and {refused} fail deterministically")


def test_data_that_violates_a_corrected_rule_fails_the_migration() -> None:
    """A repair is not granted over data that breaks the rule it introduces."""
    _reset_schema()
    _apply_migration(MIGRATION)
    # 066's code-point bound admits this row; 067's byte bound must not, and it
    # must say so instead of installing an unenforced constraint.
    oversized = json.dumps({"v": "ż" * 4200}, ensure_ascii=False)
    assert len(oversized) < 8192 < len(oversized.encode("utf-8"))
    run(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'big', %s::jsonb)",
        (ALICE, DATASET_A, oversized),
    )
    state, message = _apply_correction_expecting_failure()
    assert state == "23514", (state, message)
    assert "portal_database_saved_views_state_bound_check" in message, message
    # The row is still there: the failed migration destroyed nothing.
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == 1
    print("PASS: a payload that breaks the corrected byte bound fails the migration instead of weakening it")


# ===========================================================================
# 4. S13 review correction — the object cap is atomic
# ===========================================================================
def _fill_to(count: int) -> None:
    run("DELETE FROM portal_database_saved_views")
    run("DELETE FROM portal_database_column_sets")
    for index in range(count):
        assert api_main._create_portal_saved_view(
            owner_user_id=ALICE, dataset_id=DATASET_A, view_name=f"W{index}", state_json=_document()
        )[1] == "created", index
        assert api_main._create_portal_column_set(
            owner_user_id=ALICE, dataset_id=DATASET_A, set_name=f"Z{index}", state_json=_layout()
        )[1] == "created", index


def _layout() -> str:
    return json.dumps({"state_version": 1, "cols": ["driver_name"], "colorder": [], "colw": {}, "colpin": []})


def test_the_count_then_insert_race_is_real_without_the_lock() -> None:
    """The reviewed defect, reproduced as an interleaving rather than a claim.

    Two READ COMMITTED transactions both read 49, both insert, both commit. The
    counts they read were each true when they were read; the cap is a constraint
    over a SET of rows, which no snapshot read can enforce.
    """
    limit = api_main.PORTAL_SAVED_OBJECTS_PER_DATASET_MAX
    _fill_to(limit - 1)
    first, second = connect(), connect()
    try:
        first.autocommit = False
        second.autocommit = False
        with first.cursor() as a, second.cursor() as b:
            a.execute("SELECT count(*) AS n FROM portal_database_saved_views WHERE owner_user_id = %s "
                      "AND dataset_id = %s", (ALICE, DATASET_A))
            b.execute("SELECT count(*) AS n FROM portal_database_saved_views WHERE owner_user_id = %s "
                      "AND dataset_id = %s", (ALICE, DATASET_A))
            assert a.fetchone()["n"] == limit - 1
            assert b.fetchone()["n"] == limit - 1
            a.execute("INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, "
                      "view_state_json) VALUES (%s, %s, 'race-a', %s::jsonb)", (ALICE, DATASET_A, _document()))
            b.execute("INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, "
                      "view_state_json) VALUES (%s, %s, 'race-b', %s::jsonb)", (ALICE, DATASET_A, _document()))
        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == limit + 1
    print("PASS: an unguarded count-then-insert really does commit a 51st object; the cap needs a lock")


def _concurrent_creates(kind: str, *, workers: int) -> list[str]:
    """Fire `workers` creates at once through the REAL application functions."""
    barrier = threading.Barrier(workers)
    outcomes: list[str] = [""] * workers

    def attempt(index: int) -> None:
        barrier.wait(timeout=60)
        if kind == "saved_view":
            _id, outcome = api_main._create_portal_saved_view(
                owner_user_id=ALICE, dataset_id=DATASET_A,
                view_name=f"concurrent-{index}", state_json=_document(),
            )
        else:
            _id, outcome = api_main._create_portal_column_set(
                owner_user_id=ALICE, dataset_id=DATASET_A,
                set_name=f"concurrent-{index}", state_json=_layout(),
            )
        outcomes[index] = outcome

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    for thread in threads:
        assert not thread.is_alive(), "a concurrent create did not finish"
    return outcomes


def test_concurrent_creates_cannot_commit_the_fifty_first_object() -> None:
    """The product decision stays 50 per owner per dataset. Only atomicity changed."""
    limit = api_main.PORTAL_SAVED_OBJECTS_PER_DATASET_MAX
    assert limit == 50, limit
    for kind, table, scope_column in (
        ("saved_view", "portal_database_saved_views", "view_name"),
        ("column_set", "portal_database_column_sets", "set_name"),
    ):
        _fill_to(limit - 1)
        outcomes = _concurrent_creates(kind, workers=8)
        assert outcomes.count("created") == 1, (kind, outcomes)
        assert outcomes.count("limit") == 7, (kind, outcomes)
        total = run(
            f"SELECT count(*) AS n FROM {table} WHERE owner_user_id = %s AND dataset_id = %s",
            (ALICE, DATASET_A),
        )[0]["n"]
        assert total == limit, (kind, total)
        # A different dataset in the same scope is untouched: the lock is per
        # owner AND per dataset, not a global gate.
        assert api_main._create_portal_saved_view(
            owner_user_id=ALICE, dataset_id=DATASET_B, view_name="elsewhere", state_json=_document()
        )[1] == "created", kind
        # And a different owner has their own allowance.
        assert api_main._create_portal_saved_view(
            owner_user_id=BOB, dataset_id=DATASET_A, view_name="bob", state_json=_document()
        )[1] == "created", kind
        run("DELETE FROM portal_database_saved_views WHERE owner_user_id = %s", (BOB,))
    print("PASS: 8 concurrent creates from 49 commit exactly one object; the cap holds at 50 for both types")


def test_the_quota_lock_does_not_break_rename_or_update() -> None:
    limit = api_main.PORTAL_SAVED_OBJECTS_PER_DATASET_MAX
    _fill_to(limit)
    # At the cap, an UPDATE is still allowed: the quota bounds creation, not
    # editing what already exists.
    existing = api_main._list_portal_saved_views_for_user(ALICE, DATASET_A)[0]
    assert api_main._update_portal_saved_view(
        saved_view_id=existing["saved_view_id"], owner_user_id=ALICE, dataset_id=DATASET_A,
        view_name="Przemianowany", state_json=_document(sort="depot"),
    ) == "updated"
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == limit
    # And once one is deleted, exactly one create fits again.
    assert api_main._delete_portal_saved_view(
        saved_view_id=existing["saved_view_id"], owner_user_id=ALICE
    ) == "deleted"
    outcomes = _concurrent_creates("saved_view", workers=4)
    assert outcomes.count("created") == 1, outcomes
    assert run("SELECT count(*) AS n FROM portal_database_saved_views")[0]["n"] == limit
    print("PASS: the quota bounds creation only; rename and update stay available at the cap")


# ===========================================================================
# 5. S13 review correction — mixed-version states, executed
# ===========================================================================
def test_the_probe_separates_absence_incompatibility_and_a_database_fault() -> None:
    _reset_schema()
    # Truly absent: the documented pre-S13 rollout tolerance.
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_ABSENT
    assert api_main._get_portal_user_theme_state(ALICE) == ("auto", api_main.PORTAL_THEME_SCOPE_LOCAL)
    assert api_main._set_portal_user_theme(ALICE, "dark") is False
    assert api_main._list_portal_saved_views_for_user(ALICE, DATASET_A) == []

    # Correct: full S13.
    _apply_migration_sequence()
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
    assert api_main._set_portal_user_theme(ALICE, "dark") is True
    assert api_main._get_portal_user_theme_state(ALICE) == ("dark", api_main.PORTAL_THEME_SCOPE_SERVER)

    # Partial: one relation gone. NOT pre-S13, and not silently degraded to it.
    run("DROP TABLE portal_database_column_sets")
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE
    assert api_main._portal_preferences_schema_available() is False
    # The preferences relation still answers, so the account remains the theme
    # authority; the SAVED-OBJECT surfaces are what the partial schema removes.
    assert api_main._get_portal_user_theme_state(ALICE) == ("dark", api_main.PORTAL_THEME_SCOPE_SERVER)

    # A permission failure is an operational failure, never "migration absent".
    _apply_migration_sequence()
    run("DROP ROLE IF EXISTS s13_restricted")
    run("CREATE ROLE s13_restricted LOGIN PASSWORD 'x'")
    run("GRANT USAGE ON SCHEMA public TO s13_restricted")
    run("REVOKE ALL ON portal_user_preferences FROM PUBLIC, s13_restricted")
    restricted_dsn = DSN.replace("postgres:disposable@", "s13_restricted:x@")
    previous = api_main.db_conn
    try:
        api_main.db_conn = lambda: psycopg.connect(restricted_dsn, row_factory=dict_row)
        theme, scope = api_main._get_portal_user_theme_state(ALICE)
        assert (theme, scope) == ("auto", api_main.PORTAL_THEME_SCOPE_UNAVAILABLE), (theme, scope)
        assert api_main._set_portal_user_theme(ALICE, "light") is False
        # The SCHEMA is correct — the catalog says so and the catalog is
        # readable to every role — so the probe reports it correctly. What is
        # broken is this role's access, and that is exactly the distinction the
        # correction exists to keep.
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
    finally:
        api_main.db_conn = previous
        run("DROP OWNED BY s13_restricted")
        run("DROP ROLE IF EXISTS s13_restricted")

    # A role that cannot even reach the schema gets `42P01` from PostgreSQL —
    # the same SQLSTATE a genuinely missing relation produces. The absence is
    # therefore CONFIRMED against `pg_catalog` before the pre-S13 tolerance
    # applies, so a locked-out role is not read as a pre-S13 database.
    run("DROP ROLE IF EXISTS s13_locked_out")
    run("CREATE ROLE s13_locked_out LOGIN PASSWORD 'x'")
    run("REVOKE USAGE ON SCHEMA public FROM s13_locked_out")
    run("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    locked_dsn = DSN.replace("postgres:disposable@", "s13_locked_out:x@")
    previous = api_main.db_conn
    try:
        api_main.db_conn = lambda: psycopg.connect(locked_dsn, row_factory=dict_row)
        theme, scope = api_main._get_portal_user_theme_state(ALICE)
        assert (theme, scope) == ("auto", api_main.PORTAL_THEME_SCOPE_UNAVAILABLE), (theme, scope)
    finally:
        api_main.db_conn = previous
        run("GRANT USAGE ON SCHEMA public TO PUBLIC")
        run("DROP OWNED BY s13_locked_out")
        run("DROP ROLE IF EXISTS s13_locked_out")

    # And a database that cannot answer at all is an error, not an absence.
    unreachable = DSN.replace("127.0.0.1", "127.0.0.1").rsplit("/", 1)[0] + "/no_such_database"
    previous = api_main.db_conn
    try:
        api_main.db_conn = lambda: psycopg.connect(unreachable, row_factory=dict_row, connect_timeout=5)
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_ERROR
        assert api_main._get_portal_user_theme_state(ALICE) == (
            "auto", api_main.PORTAL_THEME_SCOPE_UNAVAILABLE
        )
    finally:
        api_main.db_conn = previous
    print("PASS: absent, correct, partial, permission-denied and unreachable are distinct outcomes")



# ===========================================================================
# S13 FINAL CLOSURE — runtime readiness verifies DEFINITIONS, not names.
#
# Migration-time integrity was already settled. What was not is the RUNTIME
# probe: it asked whether a constraint with the expected NAME existed. A
# database whose `..._owner_user_id_fkey` points at the wrong relation, whose
# `..._theme_check` accepts a theme the renderer has no tokens for, whose
# `..._owner_dataset_name_key` covers the wrong columns or whose
# `..._state_bound_check` counts code points instead of bytes carries all the
# right names and none of the rules S13's authorization and integrity story
# depends on — and was classified `ready`.
#
# Each case below starts from a CORRECT 066+067 schema and replaces exactly one
# rule with a same-named wrong one. The probe the reviewed commit shipped is
# loaded out of Git and run against the same database, so "this was accepted
# before" is executed rather than asserted.
# ===========================================================================
# The LAST INDEPENDENTLY REVIEWED candidate. Its probe already compared
# definitions rather than names, and it still classified an INVERTED CHECK as
# `ready`, because it decided a CHECK by the fragments its text contains. The
# whole reviewed block — its expected definitions, its matcher and its probe —
# is loaded out of Git and run against the same database, so "this was accepted
# before" is executed rather than asserted.
REVIEWED_COMMIT = "fe1a38d461868b466266f7d9bb722318703e89e3"


def _schema_probe_from_commit(commit: str):
    """`_portal_s13_schema_state` and ITS OWN contract, as `commit` shipped it.

    The expected definitions are taken from that commit too. Injecting the
    CURRENT contract would silently upgrade the old probe and make the RED side
    of this evidence unobservable.

    The three application bounds the older blocks GENERATE their expected
    expressions from — the theme vocabulary, the name limit and the payload
    limit — are passed in, because they live above the extracted block and are
    unchanged between the commits compared here. They decide the LITERALS the
    old probe expects, never the mechanism by which it decides an expression, so
    supplying them does not soften the probe being reproduced.
    """
    import re as _re
    import subprocess
    from typing import Any as _Any

    result = subprocess.run(
        ["git", "show", f"{commit}:api/main.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return None
    source = result.stdout
    start = source.index("PORTAL_S13_REQUIRED_COLUMN_DEFINITIONS: dict")
    end = source.index("\ndef _portal_preferences_schema_available", start)
    namespace = {
        "db_conn": lambda: api_main.db_conn(),
        "re": _re,
        "Any": _Any,
        "PORTAL_THEME_MODES": api_main.PORTAL_THEME_MODES,
        "PORTAL_SAVED_OBJECT_NAME_MAX_CHARS": api_main.PORTAL_SAVED_OBJECT_NAME_MAX_CHARS,
        "PORTAL_SAVED_OBJECT_STATE_MAX_BYTES": api_main.PORTAL_SAVED_OBJECT_STATE_MAX_BYTES,
    }
    exec(compile(source[start:end], f"<{commit[:7]}>", "exec"), namespace)  # noqa: S102
    return namespace["_portal_s13_schema_state"]


def _reviewed_schema_probe():
    return _schema_probe_from_commit(REVIEWED_COMMIT)


# `(label, SQL that keeps every authorized NAME and breaks exactly one RULE,
#   whether the LAST REVIEWED probe called the result `ready`)`.
#
# The third field is the RED side. `True` means `fe1a38d` accepted the wrong
# schema and this suite proves it by running that probe; `False` means the
# reviewed probe already refused this one and only the GREEN assertion applies.
SAME_NAME_WRONG_DEFINITION_CASES = [
    ("a foreign key pointing at the wrong relation", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_owner_user_id_fkey;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_owner_user_id_fkey
       FOREIGN KEY (owner_user_id) REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE;
     """),
    ("a foreign key on the wrong column", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_owner_user_id_fkey;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_owner_user_id_fkey
       FOREIGN KEY (dataset_id) REFERENCES artifact_users(user_id) ON DELETE CASCADE;
     """),
    ("a foreign key that does not cascade", False, """
     ALTER TABLE portal_user_preferences
       DROP CONSTRAINT portal_user_preferences_user_id_fkey;
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_user_id_fkey
       FOREIGN KEY (user_id) REFERENCES artifact_users(user_id) ON DELETE RESTRICT;
     """),
    ("a theme check that accepts a value the renderer cannot express", False, """
     ALTER TABLE portal_user_preferences DROP CONSTRAINT portal_user_preferences_theme_check;
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_theme_check
       CHECK (theme IN ('auto', 'light', 'dark', 'solarized'));
     """),
    ("uniqueness over the wrong columns", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_owner_dataset_name_key;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_owner_dataset_name_key
       UNIQUE (owner_user_id, view_name);
     """),
    ("a payload bound counted in code points, not bytes", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_state_bound_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_bound_check
       CHECK (char_length(layout_state_json::text) <= 8192);
     """),
    ("a payload shape rule covering the wrong column", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_state_object_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_object_check
       CHECK (view_name <> '');
     """),
    ("a constraint that exists but was never validated", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_name_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_name_check
       CHECK (view_name <> '' AND char_length(view_name) <= 80) NOT VALID;
     """),
    ("the expected index name on the wrong column", False, """
     DROP INDEX idx_portal_database_saved_views_owner;
     CREATE INDEX idx_portal_database_saved_views_owner
       ON portal_database_saved_views (view_name);
     """),
    ("the expected index name in descending order", False, """
     DROP INDEX idx_portal_database_column_sets_owner;
     CREATE INDEX idx_portal_database_column_sets_owner
       ON portal_database_column_sets (owner_user_id, dataset_id, set_name DESC);
     """),
    ("a generated identifier default that is gone", False, """
     ALTER TABLE portal_database_saved_views ALTER COLUMN saved_view_id DROP DEFAULT;
     """),
    ("a payload column that is no longer jsonb", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_state_object_check;
     ALTER TABLE portal_database_column_sets
       ALTER COLUMN layout_state_json TYPE TEXT USING layout_state_json::text;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_object_check
       CHECK (jsonb_typeof(layout_state_json::jsonb) = 'object');
     """),
    ("a nullable owner column", False, """
     ALTER TABLE portal_database_saved_views ALTER COLUMN owner_user_id DROP NOT NULL;
     """),
    # -----------------------------------------------------------------
    # THE DEFECT THIS CORRECTION CLOSES: same names, same tokens, same
    # literals, same functions, same numbers — INVERTED meaning. Every case
    # in this block was `ready` under a probe that decided a CHECK by what
    # its text contained.
    # -----------------------------------------------------------------
    ("a theme check that admits everything EXCEPT the approved themes", True, """
     ALTER TABLE portal_user_preferences DROP CONSTRAINT portal_user_preferences_theme_check;
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_theme_check
       CHECK (theme NOT IN ('auto', 'light', 'dark'));
     """),
    ("a theme check missing one approved value", False, """
     ALTER TABLE portal_user_preferences DROP CONSTRAINT portal_user_preferences_theme_check;
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_theme_check
       CHECK (theme IN ('auto', 'light'));
     """),
    ("a theme check whose value differs only in case", False, """
     ALTER TABLE portal_user_preferences DROP CONSTRAINT portal_user_preferences_theme_check;
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_theme_check
       CHECK (theme IN ('auto', 'light', 'DARK'));
     """),
    ("a payload shape rule requiring a NON-object", True, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_state_object_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_object_check
       CHECK (jsonb_typeof(view_state_json) <> 'object');
     """),
    ("a payload shape rule demanding a JSON array", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_state_object_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_object_check
       CHECK (jsonb_typeof(layout_state_json) = 'array');
     """),
    ("a payload bound requiring AT LEAST 8192 bytes", True, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_state_bound_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_bound_check
       CHECK (octet_length(view_state_json::text) >= 8192);
     """),
    ("a payload bound requiring MORE than 8192 bytes", True, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_state_bound_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_bound_check
       CHECK (octet_length(layout_state_json::text) > 8192);
     """),
    ("a payload bound that excludes the authorized 8192th byte", True, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_state_bound_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_bound_check
       CHECK (octet_length(view_state_json::text) < 8192);
     """),
    ("a payload bound counted in code points on the OTHER relation", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_state_bound_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_bound_check
       CHECK (char_length(view_state_json::text) <= 8192);
     """),
    ("a payload bound at a different size", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_state_bound_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_bound_check
       CHECK (octet_length(layout_state_json::text) <= 16384);
     """),
    ("a name bound with the comparison inverted", True, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_name_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_name_check
       CHECK (view_name <> '' AND char_length(view_name) >= 80);
     """),
    ("a name bound that no longer refuses an empty name", True, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_name_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_name_check
       CHECK (char_length(set_name) <= 80);
     """),
    ("a name bound at a different length", False, """
     ALTER TABLE portal_database_column_sets
       DROP CONSTRAINT portal_database_column_sets_name_check;
     ALTER TABLE portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_name_check
       CHECK (set_name <> '' AND char_length(set_name) <= 40);
     """),
    ("a name bound measured in bytes instead of characters", False, """
     ALTER TABLE portal_database_saved_views
       DROP CONSTRAINT portal_database_saved_views_name_check;
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_name_check
       CHECK (view_name <> '' AND octet_length(view_name) <= 80);
     """),
    # -----------------------------------------------------------------
    # A rule the authorized migration never created is as decisive as a
    # missing one: it decides what the application's own statements do.
    # -----------------------------------------------------------------
    ("an unauthorized uniqueness rule under another name", True, """
     ALTER TABLE portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_one_per_owner UNIQUE (owner_user_id);
     """),
    ("an unauthorized value rule under another name", True, """
     ALTER TABLE portal_user_preferences
       ADD CONSTRAINT portal_user_preferences_no_dark_check CHECK (theme <> 'dark');
     """),
    ("the listing index restricted to some of the rows", True, """
     DROP INDEX idx_portal_database_saved_views_owner;
     CREATE INDEX idx_portal_database_saved_views_owner
       ON portal_database_saved_views (owner_user_id, dataset_id, view_name)
       WHERE view_name <> 'excluded';
     """),
    ("a theme default the enum would refuse", False, """
     ALTER TABLE portal_user_preferences ALTER COLUMN theme SET DEFAULT 'light';
     """),
]


def test_the_authorized_check_contract_is_the_migrations_own_deparsed_output() -> None:
    """The expected CHECK expressions are not text a person composed.

    Every one of them is compared against `pg_get_expr(conbin, conrelid)` for
    the constraint migrations 066 then 067 actually created, so the contract in
    `api/main.py` IS PostgreSQL's canonical rendering of the authorized rule. If
    a later edit changed the application's bound, its name limit or its theme
    vocabulary without the migration following, this fails here rather than
    silently accepting a database that disagrees with the server.
    """
    _reset_schema()
    _apply_migration_sequence()
    checked = 0
    for table, expected in api_main.PORTAL_S13_REQUIRED_CONSTRAINT_DEFINITIONS.items():
        for name, spec in expected.items():
            if spec.get("type") != "c":
                continue
            rows = run(
                "SELECT pg_get_expr(conbin, conrelid) AS e FROM pg_constraint "
                "WHERE conrelid = to_regclass(%s) AND conname = %s",
                (f"public.{table}", name),
            )
            assert rows, (table, name)
            assert rows[0]["e"] == spec["expression"], (table, name, rows[0]["e"], spec["expression"])
            checked += 1
    # And the expression the contract names really is the `<= 8192 UTF-8 bytes`
    # rule at its exact boundary, measured on the encoded document.
    at_bound = json.dumps({"v": "x" * (8192 - len(json.dumps({"v": ""})))})
    assert len(at_bound.encode("utf-8")) == 8192, len(at_bound.encode("utf-8"))
    run(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'edge', %s::jsonb)", (ALICE, DATASET_A, at_bound),
    )
    over_bound = json.dumps({"v": "x" * (8193 - len(json.dumps({"v": ""})))})
    assert len(over_bound.encode("utf-8")) == 8193
    assert _expect_error(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'over', %s::jsonb)", (ALICE, DATASET_A, over_bound),
    ) == "23514"
    run("DELETE FROM portal_database_saved_views")
    print(f"PASS: all {checked} authorized CHECK expressions are the migrations' own deparsed output, "
          f"and the payload rule admits exactly 8192 UTF-8 bytes")


def test_same_named_but_wrong_schema_definitions_are_incompatible() -> None:
    reviewed = _reviewed_schema_probe()
    accepted_by_the_reviewed_probe = 0
    expected_red: list[str] = []
    for label, baseline_ready, mutation in SAME_NAME_WRONG_DEFINITION_CASES:
        _reset_schema()
        _apply_migration_sequence()
        # The starting point really is the authorized structure.
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY, label
        run(mutation)
        # Every authorized NAME is still present — which is all a probe that
        # counted names ever asked for.
        for table, expected in CONSTRAINTS_BY_TABLE.items():
            assert expected.issubset(_constraints(table)), (label, table)
        state = api_main._portal_s13_schema_state()
        assert state == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE, (label, state)
        assert api_main._portal_preferences_schema_available() is False, label
        if reviewed is None:
            continue
        reviewed_state = reviewed()
        if reviewed_state == api_main.PORTAL_S13_SCHEMA_READY:
            accepted_by_the_reviewed_probe += 1
        if baseline_ready:
            # The RED side, executed: the last reviewed candidate really did
            # call this wrong schema ready.
            assert reviewed_state == api_main.PORTAL_S13_SCHEMA_READY, (label, reviewed_state)
            expected_red.append(label)
    if reviewed is None:
        print("NOTE: the reviewed commit is not reachable; the RED side was not executed")
    else:
        assert len(expected_red) == sum(1 for case in SAME_NAME_WRONG_DEFINITION_CASES if case[1])
        assert len(expected_red) >= 10, expected_red
    # And the authorized structure is still READY, so the stricter probe has not
    # simply learned to refuse everything.
    _reset_schema()
    _apply_migration_sequence()
    assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
    assert api_main._portal_preferences_schema_available() is True
    print(
        f"PASS: {len(SAME_NAME_WRONG_DEFINITION_CASES)} same-named wrong definitions are INCOMPATIBLE "
        f"({accepted_by_the_reviewed_probe} of them were `ready` on {REVIEWED_COMMIT[:7]}, "
        f"{len(expected_red)} of those semantically inverted)"
    )



# ===========================================================================
# S13 EXPRESSION IDENTITY — the deparsed text names a function, it does not
# identify one.
#
# The candidate reviewed at `358145bc` compared each CHECK and each column
# default against the WHOLE canonical expression PostgreSQL deparses it into,
# which settled every question about the SHAPE of a rule — its operator, its
# direction, its literals, its unit. It did not settle which OBJECT computes it.
#
# `pg_get_expr` renders through the CALLER's `search_path`: a function prints
# unqualified exactly when the caller's path makes THAT function the visible
# one. So a hand-written `evil.octet_length(text)` that returns `1` for every
# input, installed under the authorized constraint name, deparses to the exact
# string the contract expects as soon as the probe connects with
# `search_path = evil, pg_catalog` — and the probe answered `ready` for a
# database whose 8 KiB payload bound does not exist. The same trick works on
# `jsonb_typeof`, on `char_length` and on the `gen_random_uuid()` default.
#
# Every case below is executed twice against the SAME physical database: once
# through the probe `358145bc` shipped (the RED side, loaded out of Git) and
# once through the current one, under a matrix of caller `search_path`
# settings. The decisive property is not that any single answer is
# `incompatible` — it is that the answer does not MOVE when only the caller's
# session setting changes.
# ===========================================================================
IDENTITY_REVIEWED_COMMIT = "358145bc73e83ae3e4720f62e1948532bb90f8ad"

# `None` is the connection's own default. The rest are the paths an operator, a
# pooler, a `role`-level `SET` or an attacker with `CREATE` on one schema can
# put a probe connection into.
SEARCH_PATH_MATRIX = (
    None,
    "evil, pg_catalog",
    "pg_catalog",
    "public, pg_catalog",
    '"$user", public',
)


def _connection_factory(search_path):
    """`db_conn`, but the connection it hands back carries `search_path`."""

    class _Conn:
        def __init__(self):
            self._inner = psycopg.connect(DSN, row_factory=dict_row)
            if search_path is not None:
                with self._inner.cursor() as cur:
                    cur.execute(f"SET search_path = {search_path}")

        def __enter__(self):
            return self._inner

        def __exit__(self, *a):
            self._inner.close()
            return False

    return _Conn


def _state_under(probe, search_path) -> str:
    previous = api_main.db_conn
    try:
        api_main.db_conn = _connection_factory(search_path)
        return probe()
    finally:
        api_main.db_conn = previous


def _states_across_the_matrix(probe) -> dict:
    return {search_path: _state_under(probe, search_path) for search_path in SEARCH_PATH_MATRIX}


# `(label, SQL that shadows ONE pg_catalog identity in schema `evil` and
#   rebuilds every S13 rule that used it, so that under `evil, pg_catalog`
#   NOTHING in the schema deparses schema-qualified)`.
#
# Each case drops `evil` first: a shadow left over from a previous case would
# make an unrelated authorized rule print qualified, and the reviewed probe
# would refuse the schema for the wrong reason — masking the very acceptance
# this proves.
SHADOWED_IDENTITY_CASES = [
    ("the payload bound computed by `evil.octet_length(text)`, which returns 1", """
     DROP SCHEMA IF EXISTS evil CASCADE;
     CREATE SCHEMA evil;
     CREATE FUNCTION evil.octet_length(text) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;
     ALTER TABLE portal_database_saved_views DROP CONSTRAINT portal_database_saved_views_state_bound_check;
     ALTER TABLE portal_database_column_sets DROP CONSTRAINT portal_database_column_sets_state_bound_check;
     SET search_path = evil, pg_catalog;
     ALTER TABLE public.portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_bound_check
       CHECK (octet_length((view_state_json)::text) <= 8192);
     ALTER TABLE public.portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_bound_check
       CHECK (octet_length((layout_state_json)::text) <= 8192);
     """),
    ("the object-shape rule computed by `evil.jsonb_typeof(jsonb)`, which always says 'object'", """
     DROP SCHEMA IF EXISTS evil CASCADE;
     CREATE SCHEMA evil;
     CREATE FUNCTION evil.jsonb_typeof(jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'object'::text $$;
     ALTER TABLE portal_database_saved_views DROP CONSTRAINT portal_database_saved_views_state_object_check;
     ALTER TABLE portal_database_column_sets DROP CONSTRAINT portal_database_column_sets_state_object_check;
     SET search_path = evil, pg_catalog;
     ALTER TABLE public.portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_state_object_check
       CHECK (jsonb_typeof(view_state_json) = 'object'::text);
     ALTER TABLE public.portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_state_object_check
       CHECK (jsonb_typeof(layout_state_json) = 'object'::text);
     """),
    ("the name bound computed by `evil.char_length(text)`, which returns 1", """
     DROP SCHEMA IF EXISTS evil CASCADE;
     CREATE SCHEMA evil;
     CREATE FUNCTION evil.char_length(text) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;
     ALTER TABLE portal_database_saved_views DROP CONSTRAINT portal_database_saved_views_name_check;
     ALTER TABLE portal_database_column_sets DROP CONSTRAINT portal_database_column_sets_name_check;
     SET search_path = evil, pg_catalog;
     ALTER TABLE public.portal_database_saved_views
       ADD CONSTRAINT portal_database_saved_views_name_check
       CHECK ((view_name <> ''::text) AND (char_length(view_name) <= 80));
     ALTER TABLE public.portal_database_column_sets
       ADD CONSTRAINT portal_database_column_sets_name_check
       CHECK ((set_name <> ''::text) AND (char_length(set_name) <= 80));
     """),
    ("the row identity issued by `evil.gen_random_uuid()`, which returns one constant", """
     DROP SCHEMA IF EXISTS evil CASCADE;
     CREATE SCHEMA evil;
     CREATE FUNCTION evil.gen_random_uuid() RETURNS uuid LANGUAGE sql VOLATILE
       AS $$ SELECT '00000000-0000-0000-0000-000000000009'::uuid $$;
     SET search_path = evil, pg_catalog;
     ALTER TABLE public.portal_database_saved_views ALTER COLUMN saved_view_id SET DEFAULT gen_random_uuid();
     ALTER TABLE public.portal_database_column_sets ALTER COLUMN column_set_id SET DEFAULT gen_random_uuid();
     """),
]


def test_a_shadowed_function_identity_is_incompatible_under_every_search_path() -> None:
    """The RED→GREEN evidence, executed on one physical database per case.

    `358145bc` is loaded out of Git and run FIRST, so "the reviewed candidate
    called this schema ready" is a result rather than a claim. Then the current
    probe answers the same database, five times, under five caller paths.
    """
    reviewed = _schema_probe_from_commit(IDENTITY_REVIEWED_COMMIT)
    assert reviewed is not None, "the reviewed candidate must be reachable for the RED side"
    red = []
    for label, mutation in SHADOWED_IDENTITY_CASES:
        _reset_schema()
        _apply_migration_sequence()
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY, label
        run(mutation)
        # Every authorized NAME survives the shadow, which is all a probe
        # comparing names ever asked for.
        for table, expected in CONSTRAINTS_BY_TABLE.items():
            assert expected.issubset(_constraints(table)), (label, table)

        # RED. Under the hostile path the reviewed probe reads the shadowed
        # function's own name and accepts a rule it does not enforce.
        reviewed_states = _states_across_the_matrix(reviewed)
        assert reviewed_states["evil, pg_catalog"] == api_main.PORTAL_S13_SCHEMA_READY, (label, reviewed_states)
        red.append(label)

        # GREEN. Same database, same five paths, one answer.
        current_states = _states_across_the_matrix(api_main._portal_s13_schema_state)
        assert set(current_states.values()) == {api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE}, (label, current_states)
        assert api_main._portal_preferences_schema_available() is False, label
    run("DROP SCHEMA IF EXISTS evil CASCADE")
    assert len(red) == len(SHADOWED_IDENTITY_CASES)
    print(f"PASS: {len(red)} shadowed pg_catalog identities are INCOMPATIBLE under every caller search_path "
          f"(all {len(red)} were `ready` on {IDENTITY_REVIEWED_COMMIT[:8]} under `evil, pg_catalog`)")


def test_the_shadowed_payload_bound_really_does_not_hold() -> None:
    """The shadowed schema is not merely different — it enforces nothing.

    Without this, "identity matters" is an argument about catalogs. With it, the
    database accepts a document twice the authorized size through a constraint
    whose deparsed text is byte-for-byte the authorized rule.
    """
    _reset_schema()
    _apply_migration_sequence()
    run(SHADOWED_IDENTITY_CASES[0][1])
    oversized = json.dumps({"v": "x" * 20000})
    assert len(oversized.encode("utf-8")) > 2 * api_main.PORTAL_SAVED_OBJECT_STATE_MAX_BYTES
    run(
        "INSERT INTO portal_database_saved_views (owner_user_id, dataset_id, view_name, view_state_json) "
        "VALUES (%s, %s, 'shadowed', %s::jsonb)", (ALICE, DATASET_A, oversized),
    )
    stored = run("SELECT octet_length(view_state_json::text) AS n FROM portal_database_saved_views")
    assert stored and stored[0]["n"] > 2 * api_main.PORTAL_SAVED_OBJECT_STATE_MAX_BYTES, stored
    # And that is the rule whose text the reviewed probe accepted.
    with connect() as hostile:
        with hostile.cursor() as cur:
            cur.execute("SET search_path = evil, pg_catalog")
            cur.execute(
                "SELECT pg_get_expr(conbin, conrelid) AS e FROM pg_constraint "
                "WHERE conname = 'portal_database_saved_views_state_bound_check'"
            )
            deparsed = cur.fetchone()["e"]
    assert deparsed == api_main.PORTAL_S13_REQUIRED_CONSTRAINT_DEFINITIONS[
        "portal_database_saved_views"]["portal_database_saved_views_state_bound_check"]["expression"], deparsed
    run("DELETE FROM portal_database_saved_views")
    run("DROP SCHEMA IF EXISTS evil CASCADE")
    print(f"PASS: the shadowed bound stored {stored[0]['n']} bytes through a constraint that deparses, under "
          "`evil, pg_catalog`, to the authorized 8192-byte rule verbatim")


def test_the_authorized_schema_is_ready_under_every_search_path() -> None:
    """The strictness has a floor: the caller's path cannot make 066+067 wrong.

    `358145bc` fails this in the OTHER direction. `pgcrypto` puts a
    `public.gen_random_uuid()` beside the `pg_catalog` one, so a connection with
    `search_path = public, pg_catalog` makes the authorized default deparse as
    `pg_catalog.gen_random_uuid()` and the reviewed probe calls the schema the
    migrations just produced `incompatible`.
    """
    _reset_schema()
    _apply_migration_sequence()
    current_states = _states_across_the_matrix(api_main._portal_s13_schema_state)
    assert set(current_states.values()) == {api_main.PORTAL_S13_SCHEMA_READY}, current_states
    reviewed = _schema_probe_from_commit(IDENTITY_REVIEWED_COMMIT)
    reviewed_states = _states_across_the_matrix(reviewed) if reviewed else {}
    flipped = sorted(
        str(path) for path, state in reviewed_states.items()
        if state != api_main.PORTAL_S13_SCHEMA_READY
    )
    assert flipped == ["public, pg_catalog"], reviewed_states
    print(f"PASS: the authorized 066+067 schema is READY under all {len(SEARCH_PATH_MATRIX)} caller search_paths "
          f"({IDENTITY_REVIEWED_COMMIT[:8]} called it {reviewed_states['public, pg_catalog']} under "
          "`public, pg_catalog`)")


def test_the_probe_does_not_leak_its_search_path_to_a_reused_connection() -> None:
    """The pinned deparse context is transaction-local, not session state.

    The probe is driven twice over ONE connection that is NOT closed between
    calls — the shape a pool hands out — and the caller's own `search_path` is
    read back from that same connection afterwards.
    """
    _reset_schema()
    _apply_migration_sequence()
    shared = psycopg.connect(DSN, row_factory=dict_row)
    try:
        with shared.cursor() as cur:
            cur.execute("SET search_path = public, pg_catalog")
        shared.commit()

        class _Pooled:
            """`with db_conn()` that RETURNS the connection instead of closing it."""

            def __init__(self):
                self._inner = shared

            def __enter__(self):
                return self._inner

            def __exit__(self, *a):
                self._inner.rollback()
                return False

        previous = api_main.db_conn
        try:
            api_main.db_conn = _Pooled
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
            with shared.cursor() as cur:
                cur.execute("SHOW search_path")
                between = cur.fetchone()["search_path"]
            shared.rollback()
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
        finally:
            api_main.db_conn = previous
        with shared.cursor() as cur:
            cur.execute("SHOW search_path")
            after = cur.fetchone()["search_path"]
    finally:
        shared.close()
    assert between == "public, pg_catalog", between
    assert after == "public, pg_catalog", after
    print("PASS: the probe's pinned `search_path` is transaction-local and the reused connection keeps its own")


def test_the_authorized_expressions_depend_on_no_object_outside_pg_catalog() -> None:
    """The identity read returns NOTHING for the schema the migrations produce.

    This is the other half of the mechanism's correctness: a check that fired on
    the authorized structure would be a permanent `incompatible`, not a control.
    """
    _reset_schema()
    _apply_migration_sequence()
    rows = run(
        """
        SELECT c.relname AS t, con.conname AS n,
               pg_describe_object(d.refclassid, d.refobjid, d.refobjsubid) AS o
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_depend d ON d.classid = 'pg_constraint'::regclass AND d.objid = con.oid
        WHERE c.relname = ANY(%s) AND con.contype = 'c'
          AND d.refclassid = ANY(%s::regclass[])
        UNION ALL
        SELECT c.relname, a.attname,
               pg_describe_object(d.refclassid, d.refobjid, d.refobjsubid)
        FROM pg_attrdef ad
        JOIN pg_class c ON c.oid = ad.adrelid
        JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
        JOIN pg_depend d ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid
        WHERE c.relname = ANY(%s) AND d.refclassid = ANY(%s::regclass[])
        """,
        (
            sorted(S13_TABLES), list(api_main.PORTAL_S13_IDENTITY_BEARING_DEPENDENCY_CLASSES),
            sorted(S13_TABLES), list(api_main.PORTAL_S13_IDENTITY_BEARING_DEPENDENCY_CLASSES),
        ),
    )
    assert rows == [], rows
    print("PASS: every authorized S13 CHECK and default is computed by pinned pg_catalog objects alone")


def test_the_runtime_readiness_probe_stays_bounded() -> None:
    """One `set_config` and four catalog reads, scoped to the three relations.

    The fourth read is the expression-identity one; it is bounded by the same
    relation list as the other three and returns nothing on a correct schema.
    """
    _reset_schema()
    _apply_migration_sequence()
    statements: list[str] = []
    previous = api_main.db_conn

    class _Cursor:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._inner.close()
            return False

        def execute(self, sql, values=None):
            statements.append(str(sql))
            return self._inner.execute(sql, values)

        def fetchall(self):
            return self._inner.fetchall()

        def fetchone(self):
            return self._inner.fetchone()

    class _Conn:
        def __init__(self):
            self._inner = psycopg.connect(DSN, row_factory=dict_row)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._inner.close()
            return False

        def cursor(self, *a, **k):
            return _Cursor(self._inner.cursor(*a, **k))

    try:
        api_main.db_conn = _Conn
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
    finally:
        api_main.db_conn = previous
    assert len(statements) == 5, statements
    # The pinned deparse context is set once, transaction-locally.
    assert statements[0] == "SELECT set_config('search_path', %s, true)", statements[0]
    for statement in statements[1:]:
        assert "c.relname = ANY(%s)" in statement, statement
        assert "n.nspname = 'public'" in statement, statement
        # Catalog metadata only: the probe never reads a stored row.
        for relation in ("portal_user_preferences ", "FROM portal_database_saved_views"):
            assert relation not in statement, statement
    print("PASS: runtime readiness is one transaction-local `set_config` plus four catalog reads "
          "scoped to the three S13 relations")


def _run_all() -> None:
    test_migration_applies_cleanly_and_preserves_existing_users()
    test_migration_is_re_executable()
    test_structure_constraints_and_indexes()
    test_the_database_enforces_the_theme_enum_names_and_payload_bounds()

    test_theme_read_and_write_round_trip_and_default_to_auto()
    test_saved_object_crud_is_owner_scoped_in_the_statement()
    test_column_set_crud_is_owner_scoped_in_the_statement()
    test_duplicate_names_are_refused_on_create_and_on_rename()
    test_the_per_dataset_object_limit_is_enforced()
    test_deleting_an_account_or_a_dataset_cascades_and_touches_nothing_else()
    test_an_absent_schema_degrades_instead_of_raising()

    # S13 review correction.
    test_the_local_sequence_establishes_the_exact_authorized_structure()
    test_the_correction_refuses_to_stand_in_for_the_base_migration()
    test_the_base_migration_alone_accepts_a_malformed_same_named_relation()
    test_a_partial_or_malformed_schema_is_repaired_or_refused_never_accepted()
    test_data_that_violates_a_corrected_rule_fails_the_migration()

    _reset_schema()
    _apply_migration_sequence()
    test_the_count_then_insert_race_is_real_without_the_lock()
    test_concurrent_creates_cannot_commit_the_fifty_first_object()
    test_the_quota_lock_does_not_break_rename_or_update()

    test_the_probe_separates_absence_incompatibility_and_a_database_fault()
    test_the_authorized_check_contract_is_the_migrations_own_deparsed_output()
    test_same_named_but_wrong_schema_definitions_are_incompatible()

    # S13 expression-identity correction.
    test_the_authorized_expressions_depend_on_no_object_outside_pg_catalog()
    test_the_authorized_schema_is_ready_under_every_search_path()
    test_a_shadowed_function_identity_is_incompatible_under_every_search_path()
    test_the_shadowed_payload_bound_really_does_not_hold()
    test_the_probe_does_not_leak_its_search_path_to_a_reused_connection()

    test_the_runtime_readiness_probe_stays_bounded()


def main() -> None:
    """Own the instance, prove it is PostgreSQL 16, run, and always clean up."""
    global DSN
    try:
        instance = disposable_postgres(label="s13")
    except DisposablePostgresUnavailable as exc:  # pragma: no cover - defensive
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return
    try:
        with instance as (dsn, info):
            # The guard still runs, even though the DSN is one this process just
            # created: a suite this destructive states its target rather than
            # trusting its own construction.
            require_loopback_dsn_or_exit(dsn, label="disposable S13 instance")
            assert info["server_version_num"] // 10000 == 16, info
            assert "logdb" not in dsn.lower(), "refusing a production-like database name"
            DSN = dsn
            print(f"POSTGRES_16_VERIFIED: {info['server_version']} "
                  f"(container {info['container']}, database {info['database']})")
            # Every `api/main.py` persistence function reaches the database
            # through `db_conn`, so pointing that at the disposable instance is
            # what makes this suite exercise the real statements rather than a
            # copy of them.
            api_main.db_conn = connect
            _run_all()
    except DisposablePostgresUnavailable as exc:
        print(f"LIVE_MIGRATION_TEST_NOT_AVAILABLE: {exc}")
        return

    print("\nDISPOSABLE_POSTGRES_MIGRATION_TEST_PASS")


if __name__ == "__main__":
    main()
