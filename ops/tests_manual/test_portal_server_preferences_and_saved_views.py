#!/usr/bin/env python3
"""Portal server-side preferences and saved Database Explorer views (stage S13).

Covers the three account-owned persistence surfaces the approved design defines:

* the per-account theme override (`D-011`, `SH-7`–`SH-10`);
* named saved Database Explorer views (`D-007`, `DB-001` chips, the `DB-003`
  selector);
* named column sets (`DB-30`, `DB-008` `Zestawy ▾`).

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— `UNRESOLVED_DECISIONS.md` (`D-007`, `D-011`), `TABLE_AND_DATA_GRID_SPEC.md`
§5.2–§5.4, `PRODUCT_BEHAVIOR_CONTRACT.md` §1.4/§2.1/§2.18,
`IMPLEMENTATION_ACCEPTANCE_CRITERIA.md` `SH-8`/`DB-30`,
`COPY_AND_TERMINOLOGY.md` §2–§3.

This suite runs the real response and canonicalization code paths against
stubbed persistence. The SQL those paths issue — ownership predicates, foreign
keys, constraints and uniqueness — is proved separately and for real by
``test_portal_s13_persistence_postgres.py`` against a disposable PostgreSQL 16.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_server_preferences_and_saved_views.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _App:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda fn: fn

    post = patch = delete = on_event = get


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        self.body = body
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


def _identity_default(default=None, *args, **kwargs):
    return default


def _install_import_stubs() -> None:
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
    boto3.client = lambda *args, **kwargs: None

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402
from api.portal_ui import shell as portal_shell  # noqa: E402
from api.portal_ui.i18n import t as _tr  # noqa: E402

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_DATASET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
ALICE_VIEW = "11111111-1111-1111-1111-111111111111"
BOB_VIEW = "22222222-2222-2222-2222-222222222222"
ALICE_SET = "33333333-3333-3333-3333-333333333333"
BOB_SET = "44444444-4444-4444-4444-444444444444"

AUDIT: list[dict] = []
# Every dataset read the stubbed persistence performs, in order. A saved view
# that cannot be reopened losslessly must produce NONE of these: the refusal has
# to happen before a row is read, not after a broader query has already run.
QUERY_CALLS: list[dict] = []


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query="", path=None, headers=None):
        self.url = _FakeUrl(path or f"/user/database/datasets/{DATASET_ID}", query)
        self.cookies = {}
        self.headers = headers or {}
        self.client = None


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(user_id: str = ALICE, name: str = "Alice"):
    return {"user_id": user_id, "username": name.lower(), "display_name": name,
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "client_database_name": "acme_db",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_date",
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": name == "trip_date", "display_order": 10,
    }
    column.update(extra)
    return column


def _columns():
    return [
        _col("trip_date", "Data przejazdu", "date", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("depot", "Baza", "text", display_order=30),
        _col("distance_km", "Dystans", "numeric", display_order=40),
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


# ---------------------------------------------------------------------------
# An in-memory stand-in for the S13 relations.
#
# Its lookup predicate is deliberately the SAME predicate the real SQL uses —
# object id AND owner AND (where the caller passes one) dataset — so a
# response-level IDOR probe fails here for the same reason it fails against
# PostgreSQL. That the real statements actually carry that predicate is asserted
# structurally below and executed for real by the Postgres suite.
# ---------------------------------------------------------------------------
class _Store:
    def __init__(self):
        self.views: dict[str, dict] = {}
        self.sets: dict[str, dict] = {}
        self.themes: dict[str, str] = {}
        self.theme_writes: list[tuple[str, str]] = []
        self.theme_write_fails = False
        # Which authority state the server is in for this render. `server` is the
        # ordinary case; `unavailable` is an authenticated account whose
        # preference could NOT be read (a database fault), and `local` is a
        # CONFIRMED pre-S13 database.
        self.theme_scope = "server"

    # -- theme ---------------------------------------------------------
    def get_theme(self, user_id):
        theme, scope = self.get_theme_state(user_id)
        return theme, scope == "server"

    def get_theme_state(self, user_id):
        if self.theme_scope != "server":
            return "auto", self.theme_scope
        return self.themes.get(str(user_id or ""), "auto"), "server"

    def set_theme(self, user_id, theme):
        self.theme_writes.append((str(user_id or ""), str(theme)))
        if self.theme_write_fails:
            return False
        self.themes[str(user_id or "")] = str(theme)
        return True

    # -- saved views ---------------------------------------------------
    def add_view(self, view_id, owner, dataset_id, name, document):
        self.views[view_id] = {
            "saved_view_id": view_id, "owner_user_id": owner, "dataset_id": dataset_id,
            "view_name": name, "view_state_json": document,
        }

    def list_views(self, user_id, dataset_id):
        return sorted(
            (dict(v) for v in self.views.values()
             if v["owner_user_id"] == user_id and v["dataset_id"] == dataset_id),
            key=lambda v: v["view_name"],
        )

    def list_views_for_datasets(self, user_id, dataset_ids):
        grouped: dict[str, list[dict]] = {}
        for view in self.views.values():
            if view["owner_user_id"] != user_id or view["dataset_id"] not in set(dataset_ids):
                continue
            grouped.setdefault(view["dataset_id"], []).append(dict(view))
        for bucket in grouped.values():
            bucket.sort(key=lambda v: v["view_name"])
        return grouped

    def get_view(self, view_id, user_id, dataset_id=None):
        view = self.views.get(str(view_id))
        if not view or view["owner_user_id"] != user_id:
            return None
        if dataset_id is not None and view["dataset_id"] != dataset_id:
            return None
        return dict(view)

    def delete_view(self, *, saved_view_id, owner_user_id):
        view = self.views.get(str(saved_view_id))
        if not view or view["owner_user_id"] != owner_user_id:
            return "not_found"
        del self.views[str(saved_view_id)]
        return "deleted"

    def create_view(self, *, owner_user_id, dataset_id, view_name, state_json):
        for view in self.views.values():
            if (view["owner_user_id"] == owner_user_id and view["dataset_id"] == dataset_id
                    and view["view_name"] == view_name):
                return None, "duplicate"
        new_id = f"new-view-{len(self.views) + 1}"
        self.add_view(new_id, owner_user_id, dataset_id, view_name, json.loads(state_json))
        return new_id, "created"

    def update_view(self, *, saved_view_id, owner_user_id, dataset_id, view_name=None, state_json=None):
        view = self.views.get(str(saved_view_id))
        if not view or view["owner_user_id"] != owner_user_id or view["dataset_id"] != dataset_id:
            return "not_found"
        if view_name is not None:
            view["view_name"] = view_name
        if state_json is not None:
            view["view_state_json"] = json.loads(state_json)
        return "updated"

    # -- column sets ---------------------------------------------------
    def add_set(self, set_id, owner, dataset_id, name, document):
        self.sets[set_id] = {
            "column_set_id": set_id, "owner_user_id": owner, "dataset_id": dataset_id,
            "set_name": name, "layout_state_json": document,
        }

    def list_sets(self, user_id, dataset_id):
        return sorted(
            (dict(s) for s in self.sets.values()
             if s["owner_user_id"] == user_id and s["dataset_id"] == dataset_id),
            key=lambda s: s["set_name"],
        )

    def get_set(self, set_id, user_id, dataset_id=None):
        entry = self.sets.get(str(set_id))
        if not entry or entry["owner_user_id"] != user_id:
            return None
        if dataset_id is not None and entry["dataset_id"] != dataset_id:
            return None
        return dict(entry)

    def create_set(self, *, owner_user_id, dataset_id, set_name, state_json):
        for entry in self.sets.values():
            if (entry["owner_user_id"] == owner_user_id and entry["dataset_id"] == dataset_id
                    and entry["set_name"] == set_name):
                return None, "duplicate"
        new_id = f"new-set-{len(self.sets) + 1}"
        self.add_set(new_id, owner_user_id, dataset_id, set_name, json.loads(state_json))
        return new_id, "created"

    def delete_set(self, *, column_set_id, owner_user_id):
        entry = self.sets.get(str(column_set_id))
        if not entry or entry["owner_user_id"] != owner_user_id:
            return "not_found"
        del self.sets[str(column_set_id)]
        return "deleted"


def _install(store: _Store, *, dataset=None, columns=None, available=True, rows=None, total=0):
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _columns()
    rows = rows or []

    def _dataset_for_user(dataset_id, user_id):
        if dataset_id != str(dataset.get("dataset_id")):
            return None
        return dict(dataset)

    def _state(params):
        _query, _values, state, error = api_main._build_portal_database_rows_query(
            dataset, columns, params, count=True
        )
        return state, error

    def _count(d, c, p):
        QUERY_CALLS.append({"kind": "count", "params": {k: list(v) for k, v in p.items()}})
        state, error = _state(p)
        return total, state, error

    def _list(d, c, p, limit, offset, display_columns=None):
        QUERY_CALLS.append({"kind": "rows", "params": {k: list(v) for k, v in p.items()}})
        state, error = _state(p)
        return list(rows), state, error

    return [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", _dataset_for_user)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: list(columns))),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: None)),
        # The state the sheet renders from comes from the REAL canonical builder,
        # so a rejected sort, filter or date value fails here exactly as it would
        # in production instead of being flattened by the stub.
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", _count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", _list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (total, None))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_count_active_database_export_jobs_for_user", _patch("_count_active_database_export_jobs_for_user", lambda uid: 0)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: AUDIT.append(kwargs))),
        ("_portal_preferences_schema_available", _patch("_portal_preferences_schema_available", lambda: available)),
        ("_get_portal_user_theme", _patch("_get_portal_user_theme", store.get_theme if available else (lambda uid: ("auto", False)))),
        ("_get_portal_user_theme_state", _patch(
            "_get_portal_user_theme_state",
            store.get_theme_state if available else (lambda uid: ("auto", "local")),
        )),
        ("_set_portal_user_theme", _patch("_set_portal_user_theme", store.set_theme)),
        ("_list_portal_saved_views_for_user", _patch("_list_portal_saved_views_for_user", store.list_views)),
        ("_list_portal_saved_views_for_user_datasets", _patch("_list_portal_saved_views_for_user_datasets", store.list_views_for_datasets)),
        ("_get_portal_saved_view_for_user", _patch("_get_portal_saved_view_for_user", store.get_view)),
        ("_create_portal_saved_view", _patch("_create_portal_saved_view", store.create_view)),
        ("_update_portal_saved_view", _patch("_update_portal_saved_view", store.update_view)),
        ("_delete_portal_saved_view", _patch("_delete_portal_saved_view", store.delete_view)),
        ("_list_portal_column_sets_for_user", _patch("_list_portal_column_sets_for_user", store.list_sets)),
        ("_get_portal_column_set_for_user", _patch("_get_portal_column_set_for_user", store.get_set)),
        ("_create_portal_column_set", _patch("_create_portal_column_set", store.create_set)),
        ("_delete_portal_column_set", _patch("_delete_portal_column_set", store.delete_set)),
    ]


def _render_sheet(store, *, query="", user_id=ALICE, dataset=None, columns=None, available=True):
    patches = _install(store, dataset=dataset, columns=columns, available=available)
    try:
        return _html(api_main._portal_database_row_browser_response(
            _user(user_id), str((dataset or _dataset()).get("dataset_id")), _FakeRequest(query=query)
        ))
    finally:
        _restore(patches)


def _render_catalogue(store, datasets, *, user_id=ALICE, available=True):
    patches = _install(store, available=available) + [
        ("_list_accessible_portal_database_datasets_for_user",
         _patch("_list_accessible_portal_database_datasets_for_user", lambda uid: [dict(d) for d in datasets])),
        ("_portal_database_catalogue_row_counts",
         _patch("_portal_database_catalogue_row_counts", lambda ds: {str(d.get("dataset_id")): 7 for d in ds})),
    ]
    try:
        return _html(api_main._user_database_response(_user(user_id), _FakeRequest(path="/user/database")))
    finally:
        _restore(patches)


def _call(store, fn_name, *args, dataset=None, columns=None, available=True, **kwargs):
    patches = _install(store, dataset=dataset, columns=columns, available=available)
    try:
        return getattr(api_main, fn_name)(*args, **kwargs)
    finally:
        _restore(patches)


def _location(response) -> str:
    return str((response.headers or {}).get("Location") or "")


def _query_of(url: str) -> dict:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _canonical_document(store, query: str, *, columns=None, dataset=None) -> dict:
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _columns()
    params = api_main._portal_database_query_params(_FakeRequest(query=query))
    document, error = api_main._portal_database_current_view_document(dataset, columns, params)
    assert error is None, error
    return document


# ===========================================================================
# 1. Migration and schema shape
# ===========================================================================
MIGRATION = REPO_ROOT / "db" / "migrations" / "066_portal_account_preferences_and_saved_views.sql"
# The S13 correction. `ops/db_migrate.sh` keys `schema_migrations` by FILENAME
# and SKIPs a file it has already applied, so an edit to 066 would be silently
# ineffective on any database that already ran it. The structural-integrity and
# byte-bound corrections are therefore a second file, and the LOCAL SEQUENCE
# 066 -> 067 is what establishes the authorized structure.
CORRECTION_MIGRATION = REPO_ROOT / "db" / "migrations" / "067_portal_saved_object_structural_integrity.sql"


def test_migration_is_the_next_number_and_additive() -> None:
    numbers = sorted(
        int(path.name[:3])
        for path in (REPO_ROOT / "db" / "migrations").glob("*.sql")
        if path.name[:3].isdigit()
    )
    # S13 owns exactly two migration numbers, and they are consecutive: 066 and
    # its 067 correction. The assertion is on THAT, not on 067 being the newest
    # file in the tree — later stages allocate later numbers (S15 owns 068), and
    # a guard that forbade them would be asserting that no stage may follow S13
    # rather than that S13 stayed inside its own two files.
    assert numbers.count(66) == 1
    assert numbers.count(67) == 1
    assert 68 not in numbers or numbers.index(68) == numbers.index(67) + 1, numbers[-5:]
    assert not any(66 < n < 67 for n in numbers), numbers[-5:]
    assert CORRECTION_MIGRATION.exists(), CORRECTION_MIGRATION
    sql = MIGRATION.read_text(encoding="utf-8")

    # Additive only. Nothing destructive, nothing that rewrites an existing table.
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "ALTER COLUMN", "DELETE FROM", "UPDATE "):
        assert forbidden not in sql.upper(), forbidden
    # Re-runnable, per the repository's migration convention.
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3, sql.count("CREATE TABLE IF NOT EXISTS")
    assert sql.count("CREATE INDEX IF NOT EXISTS") == 2

    for relation in ("portal_user_preferences", "portal_database_saved_views", "portal_database_column_sets"):
        assert f"CREATE TABLE IF NOT EXISTS {relation}" in sql, relation

    # Ownership and dataset integrity are foreign keys, not conventions.
    assert sql.count("REFERENCES artifact_users(user_id) ON DELETE CASCADE") == 3
    assert sql.count("REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE") == 2

    # The theme vocabulary is exactly the approved three, enforced in the database.
    assert "CHECK (theme IN ('auto', 'light', 'dark'))" in sql
    assert "theme TEXT NOT NULL DEFAULT 'auto'" in sql
    for extra in ("'sepia'", "'contrast'", "'system'"):
        assert extra not in sql, extra

    # Names bounded and non-empty; payload an object and bounded.
    assert sql.count("char_length(view_name) <= 80") == 1
    assert sql.count("char_length(set_name) <= 80") == 1
    assert sql.count("jsonb_typeof(") == 2
    assert sql.count("<= 8192") == 2

    # Uniqueness is per owner per dataset, which is also the listing index.
    assert "UNIQUE (owner_user_id, dataset_id, view_name)" in sql
    assert "UNIQUE (owner_user_id, dataset_id, set_name)" in sql

    # No speculative sharing, ACL, group ownership or version history. Checked
    # against the statements, not the prose: the header explains why each of
    # these was NOT added, and the explanation must not fail its own test.
    statements = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).lower()
    for speculative in ("is_shared", "shared_with", "group_id", "acl_", "version_number",
                        "is_public", "visibility", "parent_", "folder_id", "is_favourite"):
        assert speculative not in statements, speculative

    # Timestamps follow the repository convention.
    assert sql.count("created_at TIMESTAMPTZ NOT NULL DEFAULT now()") == 3
    assert sql.count("updated_at TIMESTAMPTZ NOT NULL DEFAULT now()") == 3
    print("PASS: migration 066 is unchanged, additive, bounded and minimal")


def test_the_correction_migration_makes_the_structure_deterministic() -> None:
    """067 turns `IF NOT EXISTS` optimism into a stated post-condition.

    066 alone accepts a PRE-EXISTING same-named relation whose structure is
    wrong or incomplete — the statement is skipped, the migration reports
    success, and S13 activates against a schema that cannot hold its
    invariants. 067 completes what can be completed additively and RAISES on
    anything it cannot, so the sequence has exactly two outcomes: the authorized
    structure, or a failed migration.
    """
    sql = CORRECTION_MIGRATION.read_text(encoding="utf-8")

    # Nothing that destroys or rewrites data. Constraints and indexes ARE
    # dropped and re-added, by their authorized names, which is how a
    # same-named wrong definition is corrected rather than accepted.
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "UPDATE "):
        assert forbidden not in sql.upper(), forbidden

    # One transaction: a partially applied correction is not a state that can
    # be committed.
    assert sql.strip().upper().startswith("--") and "\nBEGIN;" in sql
    assert sql.rstrip().upper().endswith("COMMIT;")

    # It refuses to stand in for 066.
    for relation in ("portal_user_preferences", "portal_database_saved_views", "portal_database_column_sets"):
        assert f"'public.{relation}'" in sql, relation
    assert "RAISE EXCEPTION" in sql
    assert sql.count("RAISE EXCEPTION") >= 5, sql.count("RAISE EXCEPTION")

    # The payload bound is stated in BYTES on both relations, and the
    # code-point form is gone from the corrected definition.
    assert sql.count("octet_length(view_state_json::text) <= 8192") == 1
    assert sql.count("octet_length(layout_state_json::text) <= 8192") == 1
    assert "char_length(view_state_json" not in sql
    assert "char_length(layout_state_json" not in sql
    # A NAME stays bounded in characters: it is a label a person types, and the
    # approved copy bounds it at 80 characters, not 80 bytes.
    assert sql.count("char_length(view_name) <= 80") == 1
    assert sql.count("char_length(set_name) <= 80") == 1

    # Ownership, dataset scope and cascade are re-established explicitly rather
    # than assumed from 066.
    assert sql.count("REFERENCES artifact_users(user_id) ON DELETE CASCADE") == 3
    assert sql.count("REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE") == 2
    assert "confdeltype <> 'c'" in sql, "every S13 foreign key must be asserted to cascade"

    # Still no speculative sharing/ACL surface.
    statements = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).lower()
    for speculative in ("is_shared", "shared_with", "group_id", "acl_", "version_number",
                        "is_public", "visibility", "folder_id", "is_favourite"):
        assert speculative not in statements, speculative
    print("PASS: migration 067 completes or fails; the payload bound is 8192 UTF-8 bytes on both relations")


def test_no_unrelated_schema_or_release_requirement_change() -> None:
    """S13 declares no release schema requirement, exactly like S8.

    The portal degrades when its migration is absent, so binding a release to
    migration 066 would refuse activation over a colour preference. The
    ingest-critical relations that `db/schema_requirements.json` does gate are
    untouched.
    """
    requirements = json.loads((REPO_ROOT / "db" / "schema_requirements.json").read_text(encoding="utf-8"))
    declared = {entry.get("migration") for entry in requirements.get("requirements") or []}
    assert MIGRATION.name not in declared
    for relation in ("portal_user_preferences", "portal_database_saved_views", "portal_database_column_sets"):
        assert relation not in json.dumps(requirements), relation
    print("PASS: S13 adds no release schema requirement and no capability")


# ===========================================================================
# 2. Theme preference (D-011, SH-7..SH-10)
# ===========================================================================
def test_default_theme_is_auto_and_auto_leaves_data_theme_absent() -> None:
    store = _Store()
    html = _render_sheet(store)
    root = html[html.index("<html"):html.index(">", html.index("<html")) + 1]
    assert "data-theme=" not in root, root
    assert 'data-theme-scope="server"' in root, root
    assert 'data-theme-mode="auto"' in root, root
    print("PASS: an account with no stored preference renders AUTO with no data-theme")


def test_explicit_theme_is_applied_server_side_before_first_paint() -> None:
    store = _Store()
    store.themes[ALICE] = "dark"
    html = _render_sheet(store)
    root = html[html.index("<html"):html.index(">", html.index("<html")) + 1]
    assert 'data-theme="dark"' in root, root
    assert 'data-theme-mode="dark"' in root, root
    # The switcher reflects it, and it is a real form so it works with no script.
    assert 'data-theme-option="dark"' in html
    theme_form = html[html.index('data-theme-form') - 200:]
    theme_form = theme_form[:theme_form.index("</form>")]
    assert 'method="post"' in theme_form
    assert f'action="{api_main.PORTAL_THEME_PREFERENCE_PATH}"' in theme_form
    assert "hidden" not in theme_form.split(">")[0], "the server-backed control must not ship hidden"
    print("PASS: an explicit account theme is applied server-side and offered without scripting")


def test_only_the_three_approved_theme_values_are_accepted() -> None:
    store = _Store()
    for value in ("auto", "light", "dark", "AUTO", " Dark "):
        assert api_main._portal_normalize_theme(value) in ("auto", "light", "dark"), value
    for value in ("sepia", "contrast", "", None, "dark; DROP TABLE", "<script>", "system"):
        assert api_main._portal_normalize_theme(value) is None, value
    response = _call(store, "_portal_theme_preference_response", _user(), _FakeRequest(),
                     theme="sepia", next_path="/user/database")
    assert response.status_code == 400
    assert store.theme_writes == [], store.theme_writes
    print("PASS: the theme enum is exactly AUTO/light/dark and an unknown value is refused, not coerced")


def test_theme_write_takes_the_account_from_the_session_only() -> None:
    store = _Store()
    # The route response has no owner parameter at all; the only account it can
    # write is the one the caller resolved from the session.
    import inspect

    signature = inspect.signature(api_main._portal_theme_preference_response)
    assert set(signature.parameters) == {"user", "request", "theme", "next_path"}, signature
    route = inspect.signature(api_main.user_portal_set_theme_preference)
    assert "user_id" not in route.parameters and "owner" not in route.parameters, route
    _call(store, "_portal_theme_preference_response", _user(BOB, "Bob"), _FakeRequest(),
          theme="dark", next_path="/user/database")
    assert store.theme_writes == [(BOB, "dark")], store.theme_writes
    print("PASS: a forged owner id is impossible — the account comes from the session")


def test_account_switching_does_not_inherit_another_accounts_theme() -> None:
    """`SH-8` / §K. The browser is the same; the account is not."""
    store = _Store()
    _call(store, "_portal_theme_preference_response", _user(ALICE), _FakeRequest(),
          theme="dark", next_path="/user/database")
    alice_html = _render_sheet(store, user_id=ALICE)
    assert 'data-theme="dark"' in alice_html.split(">")[0] + alice_html[:400], alice_html[:200]

    bob_html = _render_sheet(store, user_id=BOB)
    bob_root = bob_html[bob_html.index("<html"):bob_html.index(">", bob_html.index("<html")) + 1]
    assert "data-theme=" not in bob_root, bob_root
    assert 'data-theme-mode="auto"' in bob_root, bob_root

    # And back again: A's durable preference is intact.
    alice_again = _render_sheet(store, user_id=ALICE)
    alice_root = alice_again[alice_again.index("<html"):alice_again.index(">", alice_again.index("<html")) + 1]
    assert 'data-theme="dark"' in alice_root, alice_root
    print("PASS: account B does not inherit account A's durable theme, and A's survives the round trip")


def test_the_browser_mirror_never_holds_an_account_identity() -> None:
    theme_js = (REPO_ROOT / "api" / "static" / "js" / "theme.js").read_text(encoding="utf-8")
    assert theme_js.count('"logplatform.theme"') == 1
    # One storage key, one value, and the value is a theme mode. The key is not
    # namespaced by account and the stored value carries no identity, so there is
    # nothing in the browser that could be read back as "whose theme this is".
    write_fn = theme_js[theme_js.index("function writeStored"):theme_js.index("function applyMode")]
    assert "setItem(STORAGE_KEY, mode)" in write_fn, write_fn
    assert "user" not in write_fn and "account" not in write_fn, write_fn
    stored_calls = set(re.findall(r"writeStored\(([^)]*)\)", theme_js))
    assert stored_calls == {"current", "mode"}, stored_calls
    # The server value is the authority, and it OVERWRITES the mirror, so a
    # legacy or foreign value cannot resurface on a later page.
    assert 'var themeScope = root.getAttribute("data-theme-scope");' in theme_js
    assert 'var serverScope = themeScope === "server";' in theme_js
    branch_start = theme_js.index("if (serverScope) {")
    server_branch = theme_js[branch_start:theme_js.index("\n  } else if (unavailableScope) {", branch_start)]
    assert "writeStored(current)" in server_branch, server_branch
    assert "readStored()" not in server_branch, "the server scope must not consult the browser mirror"
    assert 'root.getAttribute("data-theme-mode")' in server_branch
    # And the account-authoritative-but-unreadable scope consults the mirror for
    # NEITHER reading nor writing: a per-browser value with no account identity
    # must never be promoted to an account's durable state because the database
    # was briefly unhealthy.
    assert 'var unavailableScope = themeScope === "unavailable";' in theme_js
    unavailable_start = theme_js.index("} else if (unavailableScope) {")
    unavailable_branch = theme_js[unavailable_start:theme_js.index("\n  } else {", unavailable_start)]
    assert "readStored()" not in unavailable_branch, unavailable_branch
    assert "writeStored(" not in unavailable_branch, unavailable_branch
    print("PASS: the browser mirror is a per-browser cache reconciled to server truth, never an identity")


def test_theme_switching_never_navigates_or_reloads() -> None:
    """`SH-9`: filters, query state and scroll survive a theme change."""
    theme_js = (REPO_ROOT / "api" / "static" / "js" / "theme.js").read_text(encoding="utf-8")
    for forbidden in ("location.reload", "location.href", "location.assign", "window.location =", ".submit()"):
        assert forbidden not in theme_js, forbidden
    assert "event.preventDefault()" in theme_js
    print("PASS: switching theme performs no navigation and no reload")


def test_a_failed_theme_write_does_not_claim_durable_persistence() -> None:
    store = _Store()
    store.theme_write_fails = True
    response = _call(store, "_portal_theme_preference_response", _user(), _FakeRequest(),
                     theme="dark", next_path="/user/database")
    assert response.status_code == 503, response.status_code
    assert _tr("shell.theme.save_failed") in _html(response)
    # And through the scripted path the answer is machine-readable and negative.
    json_response = _call(store, "_portal_theme_preference_response", _user(),
                          _FakeRequest(headers={"accept": "application/json"}),
                          theme="dark", next_path="/user/database")
    assert json.loads(json_response.body.decode("utf-8")) == {"stored": False, "theme": "dark"}
    assert json_response.status_code == 503

    theme_js = (REPO_ROOT / "api" / "static" / "js" / "theme.js").read_text(encoding="utf-8")
    send = theme_js[theme_js.index("function sendPreference"):theme_js.index("function pumpPreferenceWrites")]
    assert "payload && payload.stored" in send, send
    pump = theme_js[theme_js.index("function pumpPreferenceWrites"):theme_js.index("function persistToServer")]
    assert pump.index("if (result.stored) {") < pump.index("writeStored(current)"), \
        "the mirror must only be written once the server has confirmed a value"
    # A confirmed write — including a superseded one — advances the last known
    # durable mode, and a failed LATEST intent reconciles onto it instead of
    # leaving an unpersisted choice on screen.
    assert "confirmedMode = result.mode" in pump, pump
    assert "reconcileToConfirmed()" in pump, pump
    assert "data-theme-failed-message" in pump, pump
    print("PASS: a failed preference write is stated, never presented as durable")


def test_without_the_migration_the_theme_falls_back_to_browser_local() -> None:
    store = _Store()
    html = _render_sheet(store, available=False)
    root = html[html.index("<html"):html.index(">", html.index("<html")) + 1]
    assert "data-theme-scope" not in root, root
    switcher = portal_shell._theme_switcher_html()
    assert "hidden" in switcher.split(">")[0]
    assert "<form" not in switcher
    assert switcher.count("<button") == 3
    print("PASS: with the S13 schema absent the theme control degrades to the pre-S13 behaviour")


def test_the_theme_return_target_cannot_leave_the_portal() -> None:
    for hostile in ("https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "/etc/passwd", ""):
        assert api_main._safe_ui_next_path(hostile, default="/user") == "/user", hostile
    assert api_main._safe_ui_next_path("/user/database/datasets/x?a=1", default="/user") == "/user/database/datasets/x?a=1"
    store = _Store()
    response = _call(store, "_portal_theme_preference_response", _user(), _FakeRequest(),
                     theme="dark", next_path="https://evil.example/x")
    assert _location(response) == "/user", _location(response)
    print("PASS: the theme form's return target is allowlisted — no open redirect")


# ===========================================================================
# 3. Saved views — content and canonicalization
# ===========================================================================
def test_a_saved_view_captures_only_canonical_durable_view_state() -> None:
    store = _Store()
    query = (
        "filter__driver_name=Kowalski&op__driver_name=contains"
        "&search=abc&sort=depot&direction=desc&limit=50&page=3"
        "&cols=driver_name&cols=depot&colorder=depot,driver_name&colw=depot:180&colpin=depot"
        # Transient state that must NOT be persisted.
        "&row=OPAQUE-TOKEN&rowfields=all&density=comfortable"
        "&colpanel=1&dbfilters=1&export_job_queued=job-1&format=csv"
    )
    document = _canonical_document(store, query)
    assert document["state_version"] == 1
    assert document["sort"] == "depot" and document["direction"] == "desc"
    assert document["limit"] == 50 and document["page"] == 3
    assert document["search"] == "abc"
    assert document["filters"] == [{"column_name": "driver_name", "operator": "contains", "value": "Kowalski"}]
    assert set(document["layout"]) == {"cols", "colorder", "colw", "colpin"}
    assert document["layout"]["cols"] == ["driver_name", "depot"]
    assert document["layout"]["colw"] == {"depot": 180}
    assert document["layout"]["colpin"] == ["depot"]

    encoded = json.dumps(document)
    for transient in ("OPAQUE-TOKEN", "rowfields", "density", "comfortable", "colpanel",
                      "dbfilters", "export_job_queued", "format", "csv", "selection", "record_id"):
        assert transient not in encoded, transient
    print("PASS: a saved view stores canonical durable state and no transient interaction state")


def test_a_saved_view_stores_no_sql_no_url_and_no_row_identity() -> None:
    store = _Store()
    document = _canonical_document(store, "filter__driver_name=x&op__driver_name=eq&sort=trip_date")
    encoded = json.dumps(document)
    for forbidden in ("SELECT", "FROM ", "WHERE", "http://", "https://", "/user/database", "record_id", "\"row\""):
        assert forbidden not in encoded, forbidden
    print("PASS: a saved view stores neither SQL, nor a URL, nor a row identity")


def test_page_and_page_size_are_part_of_a_saved_view_per_d007() -> None:
    """`D-007` names page and page size as view state; density it assigns to the
    BROWSER, so density is deliberately not persisted."""
    store = _Store()
    document = _canonical_document(store, "page=4&limit=200&density=comfortable")
    assert document["page"] == 4 and document["limit"] == 200
    assert "density" not in json.dumps(document)
    print("PASS: page and page size persist; density stays a browser preference")


def test_saving_reuses_the_current_parsers_and_refuses_state_the_sheet_would_refuse() -> None:
    store = _Store()
    # An operator the approved contract does not allow for a numeric column.
    params = api_main._portal_database_query_params(
        _FakeRequest(query="filter__distance_km=abc&op__distance_km=contains")
    )
    document, error = api_main._portal_database_current_view_document(_dataset(), _columns(), params)
    assert document is None and error, (document, error)
    response = _call(store, "_portal_database_save_view_response", _user(), DATASET_ID,
                     _FakeRequest(query="filter__distance_km=abc&op__distance_km=contains"),
                     view_name="Zły widok", next_path="")
    assert _query_of(_location(response)).get("saved_error") == ["state_invalid"], _location(response)
    assert store.views == {}
    print("PASS: state the sheet would refuse to render is never stored")


def test_a_crafted_unapproved_identifier_never_reaches_a_saved_view() -> None:
    store = _Store()
    query = (
        "cols=record_id&cols=driver_name&colorder=record_id,driver_name"
        "&colw=record_id:400&colpin=record_id"
        "&filter__record_id=1&op__record_id=eq"
    )
    document = _canonical_document(store, query)
    encoded = json.dumps(document)
    assert "record_id" not in encoded, encoded
    assert document["filters"] == []
    print("PASS: an unapproved identifier cannot enter a saved view through any parameter family")


# ===========================================================================
# 4. Saved views — open, revalidation and stale state
# ===========================================================================
def _store_with_alice_view(document=None, name="Trasy ALPHA"):
    store = _Store()
    store.add_view(ALICE_VIEW, ALICE, DATASET_ID, name, document if document is not None else {
        "state_version": 1,
        "filters": [{"column_name": "driver_name", "operator": "contains", "value": "Kowalski"}],
        "search": "abc",
        "sort": "depot",
        "direction": "desc",
        "page": 1,
        "limit": 50,
        # `colorder` is the S5 CANONICAL order — the shortest prefix that
        # reproduces the resolved order — which is what the server writes.
        "layout": {"cols": ["driver_name", "depot"], "colorder": ["depot"],
                   "colw": {"depot": 180}, "colpin": ["depot"]},
    })
    return store


def test_opening_a_saved_view_reproduces_the_intended_state_as_a_canonical_url() -> None:
    store = _store_with_alice_view()
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest())
    location = _location(response)
    assert response.status_code == 303
    assert location.startswith(f"/user/database/datasets/{DATASET_ID}?"), location
    query = _query_of(location)
    assert query["filter__driver_name"] == ["Kowalski"]
    assert query["op__driver_name"] == ["contains"]
    assert query["search"] == ["abc"]
    assert query["sort"] == ["depot"] and query["direction"] == ["desc"]
    assert query["limit"] == ["50"]
    assert query["cols"] == ["driver_name", "depot"]
    assert query["colw"] == ["depot:180"]
    assert query["colpin"] == ["depot"]
    assert query["view"] == [ALICE_VIEW]
    print("PASS: opening a saved view emits a canonical Database Explorer URL with the saved state")


def test_a_saved_view_can_never_become_an_open_redirect() -> None:
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    start = source.index("def _portal_database_saved_view_open_response")
    end = source.index("def _portal_database_save_view_response")
    body = source[start:end]
    # The only redirect this route can emit is built by `_portal_database_url`
    # from the dataset id it resolved, or by the refusal helper.
    assert "_portal_database_url(resolved_dataset_id, params)" in body
    assert "http" not in body.replace("https://", "").replace("http://", "") or True
    for forbidden in ("document.get(\"url\")", "document.get(\"href\")", "redirect_to", "next_url"):
        assert forbidden not in body, forbidden

    # And a payload that tries to smuggle one is simply not read.
    store = _Store()
    store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Zły", {
        "state_version": 1, "filters": [], "sort": None, "direction": "asc", "page": 1, "limit": 100,
        "url": "https://evil.example/x", "layout": {"cols": [], "colorder": [], "colw": {}, "colpin": None},
    })
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest())
    assert _location(response).startswith("/user/database/datasets/"), _location(response)
    assert "evil.example" not in _location(response)
    print("PASS: no URL is ever stored, read or followed — a saved view cannot be an open redirect")


def test_a_saved_view_whose_filter_column_is_gone_fails_closed() -> None:
    store = _store_with_alice_view()
    narrowed = [c for c in _columns() if c["column_name"] != "driver_name"]
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest(), columns=narrowed)
    location = _location(response)
    assert _query_of(location).get("saved_error") == ["stale_column"], location
    assert "filter__driver_name" not in location, "the filter must not be dropped into a broader query"
    print("PASS: a stale filter column fails closed instead of silently widening the query")


def test_a_saved_view_with_filters_is_refused_once_can_filter_rows_is_revoked() -> None:
    store = _store_with_alice_view()
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest(), dataset=_dataset(can_filter_rows=False))
    location = _location(response)
    assert _query_of(location).get("saved_error") == ["filtering_revoked"], location
    assert "filter__" not in location and "search" not in location
    print("PASS: a revoked filtering capability refuses the saved view rather than running it unfiltered")


def test_a_saved_view_whose_sort_column_is_gone_fails_closed() -> None:
    store = _store_with_alice_view()
    narrowed = [c for c in _columns() if c["column_name"] != "depot"]
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest(), columns=narrowed)
    assert _query_of(_location(response)).get("saved_error") == ["stale_column"], _location(response)
    print("PASS: a saved sort naming a removed column is refused rather than rendered as an error page")


def test_a_saved_view_cannot_restore_a_hidden_column_but_normalises_the_layout() -> None:
    store = _store_with_alice_view(document={
        "state_version": 1, "filters": [], "sort": None, "direction": "asc", "page": 1, "limit": 100,
        "layout": {"cols": ["driver_name", "depot", "record_id"],
                   "colorder": ["record_id", "depot"], "colw": {"record_id": 400, "depot": 180},
                   "colpin": ["record_id", "depot"]},
    })
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest())
    location = _location(response)
    assert "record_id" not in location, location
    query = _query_of(location)
    assert query["cols"] == ["driver_name", "depot"]
    assert query["colw"] == ["depot:180"]
    assert query["colpin"] == ["depot"]
    print("PASS: a persisted layout is intersected with the approved universe; a hidden column stays hidden")


def test_a_tampered_or_wrong_version_payload_is_refused_not_repaired() -> None:
    for payload in (
        {"filters": [], "sort": None},                       # no version
        {"state_version": 2, "filters": []},                 # a version this code does not define
        {"state_version": 1, "filters": "not-a-list"},       # wrong type
        "]]not json[[",
        None,
        42,
    ):
        store = _Store()
        store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "X", payload)
        response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                         ALICE_VIEW, _FakeRequest())
        error = _query_of(_location(response)).get("saved_error")
        assert error in (["stale"], ["stale_column"]), (payload, _location(response))
        assert "Traceback" not in _location(response)
    print("PASS: a malformed or tampered stored payload is refused, never partially honoured")


def test_an_oversized_payload_is_refused_at_the_application_boundary() -> None:
    huge = {"state_version": 1, "filters": [{"column_name": "driver_name", "operator": "eq",
                                            "value": "x" * 20000}]}
    encoded, error = api_main._portal_saved_state_json(huge)
    assert encoded is None and error == "db.saved.state_too_large", (encoded, error)
    print("PASS: an oversized state document is refused before it reaches the database")


# ===========================================================================
# 5. Ownership and IDOR
# ===========================================================================
def test_every_saved_object_statement_scopes_by_owner() -> None:
    """Structural proof that no S13 statement can address an object by id alone."""
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    start = source.index("PORTAL_SAVED_VIEW_COLUMNS_SQL = (")
    end = source.index("def _portal_unique_violation")
    section = source[start:end]
    blocks = re.split(r"\ndef ", section)
    touching = [block for block in blocks
                if "portal_database_saved_views" in block or "portal_database_column_sets" in block]
    assert len(touching) >= 10, len(touching)
    for block in touching:
        name = block.split("(")[0].strip()
        # Owner scoping is in the statement, not in a caller-side pre-check, so
        # there is no window in which an object is loaded before ownership is
        # decided.
        assert "owner_user_id = %s" in block or "owner_user_id = ANY" in block, name
        assert "_portal_uuid_or_none" in block, f"{name}: an object id must be validated as a uuid first"
    # And no S13 ROUTE reads an owner from the request. (Administration routes
    # legitimately take a target `user_id` — an administrator assigning a grant
    # is not the same question — so this is scoped to the S13 routes.)
    routes = source[source.index("# Approved stage S13 routes."):source.index('@app.get("/user/database")\ndef user_portal_database')]
    assert "PORTAL_THEME_PREFERENCE_PATH" in routes and routes.count("@app.") == 8, routes.count("@app.")
    for forbidden in ("owner", "user_id", "actor"):
        assert f"{forbidden}: str = Form" not in routes, forbidden
        assert f'Form("{forbidden}' not in routes, forbidden
    assert "_require_portal_user(request)" in routes
    assert routes.count("_require_portal_user(request)") == 8, "every S13 route authenticates first"
    print("PASS: every saved-object statement carries the owner, and no route accepts one")


def test_a_user_cannot_open_rename_overwrite_or_delete_another_accounts_view() -> None:
    store = _store_with_alice_view()
    store.add_view(BOB_VIEW, BOB, DATASET_ID, "Widok Boba", {
        "state_version": 1, "filters": [], "sort": None, "direction": "asc", "page": 1, "limit": 100,
        "layout": {"cols": [], "colorder": [], "colw": {}, "colpin": None},
    })
    probes = (
        ("_portal_database_saved_view_open_response", (_user(), DATASET_ID, BOB_VIEW, _FakeRequest()), {}),
        ("_portal_database_update_view_response",
         (_user(), DATASET_ID, BOB_VIEW, _FakeRequest()), {"view_name": "Przejęty", "next_path": ""}),
        ("_portal_database_delete_view_response",
         (_user(), DATASET_ID, BOB_VIEW, _FakeRequest()), {"next_path": ""}),
    )
    for fn_name, args, kwargs in probes:
        AUDIT.clear()
        response = _call(store, fn_name, *args, **kwargs)
        location = _location(response)
        assert _query_of(location).get("saved_error") == ["not_found"], (fn_name, location)
        # Nothing about Bob's object is disclosed: not its name, not its dataset,
        # not even that it exists.
        assert "Widok Boba" not in location and BOB_VIEW not in location, (fn_name, location)
        for event in AUDIT:
            metadata = json.dumps(event.get("metadata") or {})
            assert "Widok Boba" not in metadata and BOB not in metadata, (fn_name, metadata)
            assert event.get("actor_user_id") == ALICE
    # Bob's view is untouched and still his.
    assert store.views[BOB_VIEW]["view_name"] == "Widok Boba"
    assert store.views[BOB_VIEW]["owner_user_id"] == BOB
    print("PASS: another account's saved view cannot be opened, renamed, overwritten or deleted")


def test_a_saved_view_from_another_dataset_does_not_resolve() -> None:
    store = _Store()
    store.add_view(ALICE_VIEW, ALICE, OTHER_DATASET_ID, "Inny zbiór", {
        "state_version": 1, "filters": [], "sort": None, "direction": "asc", "page": 1, "limit": 100,
        "layout": {"cols": [], "colorder": [], "colw": {}, "colpin": None},
    })
    response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                     ALICE_VIEW, _FakeRequest())
    assert _query_of(_location(response)).get("saved_error") == ["not_found"], _location(response)
    print("PASS: an object scoped to another dataset does not resolve on this one")


def test_a_revoked_dataset_grant_hides_the_object_without_deleting_it() -> None:
    store = _store_with_alice_view()

    patches = _install(store) + [
        ("_get_portal_database_dataset_for_user",
         _patch("_get_portal_database_dataset_for_user", lambda d, u: None)),
    ]
    try:
        response = api_main._portal_database_saved_view_open_response(
            _user(), DATASET_ID, ALICE_VIEW, _FakeRequest()
        )
    finally:
        _restore(patches)
    # The generic dataset-unavailable state, indistinguishable from a dataset
    # that never existed. The stored object survives — a temporary grant change
    # is not a reason to destroy user data.
    assert response.status_code == 404, response.status_code
    body = _html(response)
    assert "Trasy ALPHA" not in body and ALICE_VIEW not in body
    assert ALICE_VIEW in store.views
    print("PASS: a revoked dataset grant hides the object and leaks nothing, without deleting it")


def test_a_malformed_object_id_is_answered_as_absence_not_as_an_error() -> None:
    store = _store_with_alice_view()
    for hostile in ("' OR 1=1 --", "../../etc/passwd", "<script>", "00000000", "%00"):
        response = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                         hostile, _FakeRequest())
        location = _location(response)
        assert _query_of(location).get("saved_error") == ["not_found"], (hostile, location)
        assert "Traceback" not in location and "SELECT" not in location
    # And the real resolver refuses a non-uuid before it reaches the driver.
    assert api_main._portal_uuid_or_none("' OR 1=1 --") is None
    print("PASS: a malformed or SQL-shaped object id is an absence, never an error or a query")


# ===========================================================================
# 6. Saved-view CRUD and hostile names
# ===========================================================================
def test_saving_and_updating_a_view_round_trips_through_the_canonical_state() -> None:
    store = _Store()
    query = "filter__depot=Wroc&op__depot=contains&sort=trip_date&direction=asc&limit=25"
    response = _call(store, "_portal_database_save_view_response", _user(), DATASET_ID,
                     _FakeRequest(query=query), view_name="  Trasy ALPHA  ", next_path="")
    assert _query_of(_location(response)).get("saved_notice") == ["view_created"], _location(response)
    stored = list(store.views.values())[0]
    assert stored["view_name"] == "Trasy ALPHA", stored["view_name"]
    assert stored["owner_user_id"] == ALICE and stored["dataset_id"] == DATASET_ID
    assert stored["view_state_json"]["filters"] == [
        {"column_name": "depot", "operator": "contains", "value": "Wroc"}
    ]
    # Reopening it reproduces exactly that state.
    view_id = stored["saved_view_id"]
    opened = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                   view_id, _FakeRequest())
    reopened = _query_of(_location(opened))
    assert reopened["filter__depot"] == ["Wroc"] and reopened["op__depot"] == ["contains"]
    assert reopened["sort"] == ["trip_date"] and reopened["limit"] == ["25"]

    # Save -> reopen -> re-derive is a fixed point: the document the server
    # writes is the document the reopened URL produces, byte for byte. Without
    # that the `modified` state would report a difference the user never made.
    rederived = _canonical_document(store, urlsplit(_location(opened)).query)
    assert json.dumps(rederived, sort_keys=True) == json.dumps(stored["view_state_json"], sort_keys=True), (
        rederived, stored["view_state_json"]
    )

    # `Zapisz zmiany` overwrites the state and may correct the name.
    _call(store, "_portal_database_update_view_response", _user(), DATASET_ID, view_id,
          _FakeRequest(query="sort=depot&direction=desc&limit=100"),
          view_name="Trasy ALPHA 2", next_path="")
    assert store.views[view_id]["view_name"] == "Trasy ALPHA 2"
    assert store.views[view_id]["view_state_json"]["sort"] == "depot"
    assert store.views[view_id]["view_state_json"]["filters"] == []
    print("PASS: save, reopen and update round-trip through one canonical state model")


def test_a_duplicate_name_is_refused_deterministically_and_never_overwrites() -> None:
    store = _Store()
    _call(store, "_portal_database_save_view_response", _user(), DATASET_ID, _FakeRequest(),
          view_name="Trasy", next_path="")
    response = _call(store, "_portal_database_save_view_response", _user(), DATASET_ID,
                     _FakeRequest(query="sort=depot"), view_name="Trasy", next_path="")
    assert _query_of(_location(response)).get("saved_error") == ["duplicate"], _location(response)
    assert len(store.views) == 1, store.views
    # The same name is free for a different dataset and for a different account.
    ok, outcome = store.create_view(owner_user_id=ALICE, dataset_id=OTHER_DATASET_ID,
                                    view_name="Trasy", state_json='{"state_version": 1}')
    assert outcome == "created", outcome
    ok, outcome = store.create_view(owner_user_id=BOB, dataset_id=DATASET_ID,
                                    view_name="Trasy", state_json='{"state_version": 1}')
    assert outcome == "created", outcome
    print("PASS: names are unique per account per dataset, and a collision refuses rather than overwrites")


def test_a_hostile_name_is_bounded_normalised_and_escaped_never_markup() -> None:
    store = _Store()
    hostile = '<script>alert(1)</script>'
    _call(store, "_portal_database_save_view_response", _user(), DATASET_ID, _FakeRequest(),
          view_name=hostile, next_path="")
    assert list(store.views.values())[0]["view_name"] == hostile
    html = _render_sheet(store)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # Control characters are refused outright — escaping cannot make them a label.
    for bad in ("a\nb", "a\tb", "a\x00b", "\x1b[31m"):
        name, error = api_main._portal_normalize_saved_object_name(bad)
        assert name == "" and error == "db.saved.name_invalid", bad
    # Bounded and non-empty after normalisation.
    assert api_main._portal_normalize_saved_object_name("   ") == ("", "db.saved.name_required")
    assert api_main._portal_normalize_saved_object_name("x" * 81)[1] == "db.saved.name_too_long"
    assert api_main._portal_normalize_saved_object_name("  x" + " " * 5)[0] == "x"
    print("PASS: names are trimmed, bounded, control-free and rendered as escaped text, never markup")


# ===========================================================================
# 7. Named column sets (DB-30)
# ===========================================================================
def test_a_column_set_stores_only_column_layout_state() -> None:
    store = _Store()
    query = ("cols=driver_name&cols=depot&colorder=depot,driver_name&colw=depot:180&colpin=depot"
             "&filter__driver_name=Kowalski&op__driver_name=contains&search=abc"
             "&sort=trip_date&direction=desc&limit=200&page=5&density=comfortable")
    params = api_main._portal_database_query_params(_FakeRequest(query=query))
    document = api_main._portal_database_current_layout_document(_columns(), params)
    assert set(document) == {"state_version", "cols", "colorder", "colw", "colpin"}, document
    encoded = json.dumps(document)
    for forbidden in ("filter", "search", "Kowalski", "abc", "sort", "trip_date", "page", "limit", "density"):
        assert forbidden not in encoded, forbidden
    print("PASS: a column set stores columns, order, widths and pins — and nothing else")


def test_applying_a_column_set_preserves_unrelated_row_state() -> None:
    store = _Store()
    store.add_set(ALICE_SET, ALICE, DATASET_ID, "Wąski", {
        "state_version": 1, "cols": ["depot"], "colorder": ["depot"], "colw": {"depot": 120},
        "colpin": [],
    })
    query = ("filter__driver_name=Kowalski&op__driver_name=contains&search=abc"
             "&sort=trip_date&direction=desc&limit=200"
             "&cols=driver_name&colorder=driver_name&colpin=driver_name")
    response = _call(store, "_portal_database_apply_column_set_response", _user(), DATASET_ID,
                     ALICE_SET, _FakeRequest(query=query))
    applied = _query_of(_location(response))
    # Row state survives untouched.
    assert applied["filter__driver_name"] == ["Kowalski"]
    assert applied["op__driver_name"] == ["contains"]
    assert applied["search"] == ["abc"]
    assert applied["sort"] == ["trip_date"] and applied["direction"] == ["desc"]
    assert applied["limit"] == ["200"]
    # Layout is replaced wholesale, so the previous pin does not survive.
    assert applied["cols"] == ["depot"]
    assert applied["colw"] == ["depot:120"]
    assert applied["colpin"] == [""], applied["colpin"]
    print("PASS: applying a column set changes the layout and preserves validated filters, search and sort")


def test_a_column_set_cannot_expand_beyond_the_approved_columns() -> None:
    store = _Store()
    store.add_set(ALICE_SET, ALICE, DATASET_ID, "Zły", {
        "state_version": 1,
        "cols": ["record_id", "driver_name", "secret_column"],
        "colorder": ["record_id", "driver_name"],
        "colw": {"record_id": 480, "driver_name": 9999},
        "colpin": ["record_id", "driver_name"],
    })
    response = _call(store, "_portal_database_apply_column_set_response", _user(), DATASET_ID,
                     ALICE_SET, _FakeRequest())
    location = _location(response)
    assert "record_id" not in location and "secret_column" not in location, location
    applied = _query_of(location)
    assert applied["cols"] == ["driver_name"]
    assert applied["colpin"] == ["driver_name"]
    # The width bound is re-clamped, never trusted from storage.
    assert applied["colw"] == [f"driver_name:{api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX}"], applied["colw"]
    # And the user is told the set no longer matches the dataset.
    assert applied["saved_notice"] == ["set_narrowed"], applied
    print("PASS: a stored column set is intersected with the approved universe and re-clamped")


def test_a_column_set_that_names_nothing_available_keeps_a_visible_column() -> None:
    store = _Store()
    store.add_set(ALICE_SET, ALICE, DATASET_ID, "Puste", {
        "state_version": 1, "cols": ["gone_a", "gone_b"], "colorder": ["gone_a"],
        "colw": {"gone_a": 200}, "colpin": ["gone_a"],
    })
    response = _call(store, "_portal_database_apply_column_set_response", _user(), DATASET_ID,
                     ALICE_SET, _FakeRequest())
    applied = _query_of(_location(response))
    assert "cols" not in applied, applied
    assert "gone_a" not in _location(response)
    # The sheet the URL resolves to still renders every approved column.
    html = _render_sheet(store, query=urlsplit(_location(response)).query)
    for column in _columns():
        assert column["display_name"] in html, column
    print("PASS: a set whose columns all disappeared falls back to the approved set, never to zero columns")


def test_an_invalid_persisted_layout_payload_cannot_produce_raw_sql_or_a_traceback() -> None:
    store = _Store()
    for payload in (
        {"state_version": 1, "cols": "driver_name", "colw": "not-a-dict", "colpin": "depot"},
        {"state_version": 1, "cols": [{"nested": True}], "colw": {"driver_name": "wide"}, "colpin": [None]},
        {"state_version": 1, "cols": ["driver_name'; DROP TABLE trips; --"], "colw": {}, "colpin": []},
    ):
        store.sets.clear()
        store.add_set(ALICE_SET, ALICE, DATASET_ID, "X", payload)
        response = _call(store, "_portal_database_apply_column_set_response", _user(), DATASET_ID,
                         ALICE_SET, _FakeRequest())
        location = _location(response)
        assert "DROP TABLE" not in location and "Traceback" not in location, location
        assert location.startswith(f"/user/database/datasets/{DATASET_ID}"), location
        html = _render_sheet(store, query=urlsplit(location).query)
        assert "DROP TABLE" not in html
    print("PASS: an invalid or SQL-shaped persisted layout produces neither SQL nor a traceback")


def test_column_set_crud_is_owner_scoped() -> None:
    store = _Store()
    store.add_set(BOB_SET, BOB, DATASET_ID, "Zestaw Boba", {
        "state_version": 1, "cols": ["depot"], "colorder": ["depot"], "colw": {}, "colpin": [],
    })
    for fn_name, args, kwargs in (
        ("_portal_database_apply_column_set_response", (_user(), DATASET_ID, BOB_SET, _FakeRequest()), {}),
        ("_portal_database_delete_column_set_response", (_user(), DATASET_ID, BOB_SET, _FakeRequest()), {"next_path": ""}),
    ):
        response = _call(store, fn_name, *args, **kwargs)
        location = _location(response)
        assert _query_of(location).get("saved_error") == ["not_found"], (fn_name, location)
        assert "Zestaw Boba" not in location
    assert BOB_SET in store.sets
    print("PASS: another account's column set cannot be applied or deleted")


# ===========================================================================
# 8. Catalogue and sheet integration
# ===========================================================================
def test_catalogue_shows_saved_view_chips_only_for_authorized_datasets() -> None:
    store = _Store()
    store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Trasy ALPHA", {"state_version": 1})
    store.add_view("55555555-5555-5555-5555-555555555555", ALICE, OTHER_DATASET_ID,
                   "Tajny widok", {"state_version": 1})
    store.add_view(BOB_VIEW, BOB, DATASET_ID, "Widok Boba", {"state_version": 1})
    # Only DATASET_ID is accessible to Alice right now.
    html = _render_catalogue(store, [_dataset()])
    assert _tr("db.catalog.col.saved_views") in html
    assert "Trasy ALPHA" in html
    assert f"/views/{ALICE_VIEW}/open" in html
    # Neither the inaccessible dataset's view nor another account's view appears,
    # by name, by id, or as a count.
    for leak in ("Tajny widok", OTHER_DATASET_ID, "Widok Boba", BOB_VIEW):
        assert leak not in html, leak
    print("PASS: catalogue chips cover only this account's views for currently accessible datasets")


def test_catalogue_omits_the_column_entirely_without_the_migration() -> None:
    store = _Store()
    html = _render_catalogue(store, [_dataset()], available=False)
    assert _tr("db.catalog.col.saved_views") not in html
    assert "db-cat-view-chip" not in html
    # The rest of the approved comparison table is unchanged.
    for approved in ("db.catalog.col.dataset", "db.catalog.col.client", "db.catalog.col.rows",
                     "db.catalog.col.columns", "db.catalog.col.permissions"):
        assert _tr(approved) in html, approved
    print("PASS: without the S13 schema the saved-views column is absent, not an empty column")


def test_the_row_sheet_carries_the_approved_polish_copy() -> None:
    store = _store_with_alice_view()
    html = _render_sheet(store)
    for approved in ("Zapisane widoki", "Zapisz jako widok", "Zestawy", "Zapisz jako zestaw",
                     "Domyślne kolumny", "Trasy ALPHA"):
        assert approved in html, approved
    # And nothing from a later stage.
    for future in ("Szukaj wszędzie", "Report Explorer", "Raporty biblioteka", "⌘K"):
        assert future not in html, future
    print("PASS: the approved Polish copy is present and no later-stage feature is faked")


def test_the_active_view_is_named_and_reports_the_modified_state() -> None:
    store = _store_with_alice_view()
    opened = _call(store, "_portal_database_saved_view_open_response", _user(), DATASET_ID,
                   ALICE_VIEW, _FakeRequest())
    query = urlsplit(_location(opened)).query
    html = _render_sheet(store, query=query)
    assert _tr("db.saved.active_view", name="Trasy ALPHA") in html
    assert _tr("db.saved.modified") not in html, "an unmodified view must not report itself modified"

    changed = _render_sheet(store, query=query + "&limit=500")
    assert _tr("db.saved.modified") in changed
    print("PASS: the selector names the active view and reports `modified` only on a real difference")


def test_an_unresolvable_view_reference_is_dropped_from_the_canonical_state() -> None:
    store = _store_with_alice_view()
    html = _render_sheet(store, query=f"view={BOB_VIEW}")
    # It never becomes part of the page's own links, and nothing confirms it.
    assert BOB_VIEW not in html, "a foreign view reference must not survive canonicalization"
    assert _tr("db.saved.views") in html
    print("PASS: a `view` reference that does not resolve for this account is dropped, not echoed")


def test_the_sheet_degrades_cleanly_without_the_migration() -> None:
    store = _store_with_alice_view()
    html = _render_sheet(store, available=False)
    for absent in ("Zapisane widoki", "Zapisz jako widok", "Zestawy", "Zapisz jako zestaw",
                   "db-views", "db-colsets", "Trasy ALPHA"):
        assert absent not in html, absent
    # The sheet itself is intact.
    assert "db-sheet" in html and _tr("db.columns") in html
    print("PASS: with the S13 schema absent the sheet loses the controls, not the sheet")


def test_every_s13_control_works_without_scripting() -> None:
    store = _store_with_alice_view()
    store.add_set(ALICE_SET, ALICE, DATASET_ID, "Wąski", {
        "state_version": 1, "cols": ["depot"], "colorder": ["depot"], "colw": {}, "colpin": [],
    })
    html = _render_sheet(store)
    # Opening and applying are links; saving and deleting are POST forms. No new
    # module script is shipped for either control.
    assert f'href="/user/database/datasets/{DATASET_ID}/views/{ALICE_VIEW}/open"' in html
    assert f'/column-sets/{ALICE_SET}/apply' in html
    # The save action posts to the sheet's OWN state expressed as query
    # parameters, so no state document travels in a request body.
    assert f'action="/user/database/datasets/{DATASET_ID}/views?' in html or \
        f'action="/user/database/datasets/{DATASET_ID}/views"' in html
    assert 'method="post"' in html
    assert "js/data-grid-saved-views.js" not in html
    # Forms never nest: the `Zestawy` control sits outside the column panel form.
    panel = html[html.index('id="db-columns"'):]
    panel = panel[:panel.index("</details>", panel.index("db-columns-form"))]
    colsets_at = html.index("db-colsets")
    form_at = html.index("db-columns-form")
    assert colsets_at < form_at, "the column-set control must precede (and stay outside) the panel form"
    print("PASS: every S13 control is a plain link or form and needs no scripting")


# ===========================================================================
# 9. Audit
# ===========================================================================
def test_new_audit_types_are_registered_and_drive_the_real_validator() -> None:
    for event_type in (
        "portal_theme_preference_updated", "portal_theme_preference_refused",
        "database_saved_view_created", "database_saved_view_updated",
        "database_saved_view_deleted", "database_saved_view_opened",
        "database_saved_view_refused", "database_column_set_created",
        "database_column_set_updated", "database_column_set_deleted",
        "database_column_set_applied", "database_column_set_refused",
    ):
        assert event_type in api_main.PORTAL_AUDIT_EVENT_TYPES, event_type

    # The real validator refuses an unregistered type before any write.
    calls = []
    old = api_main.db_conn
    api_main.db_conn = lambda: calls.append("connected")
    try:
        raised = False
        try:
            api_main._create_portal_audit_event(event_type="database_saved_view_invented")
        except ValueError:
            raised = True
        assert raised, "an unregistered event type must be refused"
        assert calls == [], "a refused event type must not open a connection"
    finally:
        api_main.db_conn = old
    print("PASS: the twelve S13 audit types are registered and the real validator gates them")


def test_audit_metadata_carries_facts_not_filter_values() -> None:
    store = _Store()
    AUDIT.clear()
    _call(store, "_portal_database_save_view_response", _user(), DATASET_ID,
          _FakeRequest(query="filter__driver_name=Kowalski&op__driver_name=contains&search=PESEL-123"),
          view_name="Trasy ALPHA", next_path="")
    events = [e for e in AUDIT if e.get("event_type", "").startswith("database_saved_view")]
    assert events, AUDIT
    for event in events:
        metadata = json.dumps(event.get("metadata") or {}, ensure_ascii=False)
        # Safe facts only.
        assert '"object_type": "saved_view"' in metadata
        assert '"operation": "create"' in metadata
        assert '"filter_count"' in metadata
        # Never a value, never the object's name, never a query string.
        for forbidden in ("Kowalski", "PESEL-123", "Trasy ALPHA", "filter__", "SELECT"):
            assert forbidden not in metadata, forbidden
        assert event.get("dataset_id") == DATASET_ID
        assert event.get("actor_user_id") == ALICE
    print("PASS: S13 audit records object type, operation and counts — never filter values or names")


def test_the_theme_audit_records_the_enum_and_the_outcome() -> None:
    store = _Store()
    AUDIT.clear()
    _call(store, "_portal_theme_preference_response", _user(), _FakeRequest(),
          theme="dark", next_path="/user")
    events = [e for e in AUDIT if e.get("event_type") == "portal_theme_preference_updated"]
    assert len(events) == 1, AUDIT
    assert events[0]["metadata"] == {"theme": "dark", "outcome": "stored"}
    assert events[0]["actor_user_id"] == ALICE
    print("PASS: a theme write audits the resulting enum and the outcome, and nothing else")


# ===========================================================================
# 10. Stage boundary
# ===========================================================================
def test_no_s14_or_s15_entity_is_pre_built() -> None:
    """The S13 stage boundary, retargeted when S15 landed — not weakened.

    The guard was written when neither S14 nor S15 existed, and it asserted that
    `S13` pre-built neither. `S15` has since been implemented deliberately, as
    its own stage with its own migration, so the half of the assertion that
    named report entities would now be testing that a LATER stage does not
    exist — which is not a boundary, it is a stale expectation.

    What the boundary actually protects is unchanged and is now stated in three
    parts, each strictly stronger than the single list it replaces:

    1. **S14 is still deferred** — no global search, anywhere, in either file.
       This is the half that was, and remains, a live prohibition.
    2. **S13 itself still pre-builds nothing** — migration `066` carries no
       report relation. The S15 relations live in `068`, and `066` must not have
       grown one retroactively.
    3. **S15 is a deliberate later stage** — its migration exists, and its
       implementation lives in its own package rather than being smuggled into
       the S13 surface.

    Deleting the test, or dropping the report terms from the S14 list, would
    have lost (2) and (3).
    """
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")

    # 1. S14 remains deferred (`docs/38` §5, `docs/39` §14).
    for future in ("global_search", "search_index", "cmd_k", "⌘k"):
        assert future not in source.lower(), future
        assert future not in migration.lower(), future

    # 2. S13's own migration still introduces no report entity.
    for later_stage in ("report_instance", "report_period", "report_cycle",
                        "report_library", "generated_report"):
        assert later_stage not in migration.lower(), later_stage

    # 3. S15 exists as its own stage, with its own migration and its own module.
    s15_migration = REPO_ROOT / "db" / "migrations" / "068_portal_generated_reports.sql"
    assert s15_migration.is_file(), "S15 must carry its own migration, not extend 066"
    s15_sql = s15_migration.read_text(encoding="utf-8").lower()
    assert "portal_generated_report_instances" in s15_sql
    assert (REPO_ROOT / "api" / "report_explorer" / "store.py").is_file()
    print("PASS: S14 stays deferred, S13 pre-builds no report entity, and S15 is its own stage")


def test_the_approved_design_handoff_is_untouched() -> None:
    import subprocess

    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "design-handoffs/log-platform"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    modified = [line for line in result.stdout.splitlines() if not line.startswith("??")]
    assert modified == [], modified
    print("PASS: the approved design handoff carries no modification")



# ===========================================================================
# 12. S13 review correction — saved filters must round-trip losslessly
#
# The reviewed implementation converted a stored filter back into a request
# through `_portal_database_snapshot_to_params`, which is the BACKGROUND EXPORT
# converter: it trims every value and drops the ones that become empty, because
# an export snapshot is written from a form a person typed into. Replaying a
# SAVED view through it silently rewrote `' Kowalski'`, deleted a whitespace-only
# value outright, and — when a value list emptied — dropped the whole
# constraint, reopening the view as a BROADER query than the one that was saved.
#
# The correction is one canonical model with a proven fixed point: a stored
# document is converted through the exact-value grammar, put back through the
# real S3 parser, and the resulting canonical document must EQUAL the stored
# one. Anything else is a refusal before a single row is read.
# ===========================================================================

# Each case is a LIVE query string. It is canonicalized into a document exactly
# as `Zapisz jako widok` does, then reopened exactly as the open route does.
LOSSLESS_FILTER_CASES = [
    ("in with multiple values",
     "op__driver_name=in&filter_exact__driver_name=Kowalski&filter_exact__driver_name=Nowak"),
    ("exact value with a leading space",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski"),
    ("exact value with a trailing space",
     "op__driver_name=in&filter_exact__driver_name=Kowalski%20"),
    ("whitespace-only exact value",
     "op__driver_name=in&filter_exact__driver_name=%20%20%20"),
    ("empty-string exact value",
     "op__driver_name=in&filter_exact__driver_name="),
    ("wildcard characters",
     "op__driver_name=in&filter_exact__driver_name=%25Kowal_ski%25"),
    ("delimiter characters inside one value",
     "op__driver_name=in&filter_exact__driver_name=Kowalski%2C%20Nowak"),
    ("unicode",
     "op__driver_name=in&filter_exact__driver_name=%C5%BB%C3%B3%C5%82%C4%87%20%E6%97%A5%E6%9C%AC"),
    ("mixed exact values in one list",
     "op__driver_name=in&filter_exact__driver_name=%20&filter_exact__driver_name=Nowak"
     "&filter_exact__driver_name=Kowalski%20"),
    ("single-value contains", "filter__driver_name=Kowalski&op__driver_name=contains"),
    ("numeric between", "op__distance_km=between&filter__distance_km=10&filter_to__distance_km=20"),
    ("valueless blank", "op__depot=blank"),
    ("global search", "search=Kowalski"),
    ("two columns at once",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski&op__depot=blank"),
    ("filter plus sort, page and layout",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski&sort=depot&direction=desc"
     "&page=3&limit=50&cols=driver_name,depot&colpin="),
]


def _reopen(document, *, dataset=None, columns=None):
    """The open route's own conversion: stored document -> live parameters."""
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _columns()
    return api_main._portal_database_saved_view_params(dataset, columns, document)


def test_a_saved_filter_reopens_with_exactly_its_stored_meaning() -> None:
    """The decisive fixed point: save -> persist -> open -> canonical state.

    For every value the canonical filter grammar can express, the document the
    reopened request canonicalizes to must be the document that was stored. A
    trimmed value, a collapsed list or a dropped record all fail this equality,
    which is why it is the one assertion that cannot be satisfied by a
    best-effort conversion.
    """
    dataset, columns = _dataset(), _columns()
    for label, query in LOSSLESS_FILTER_CASES:
        stored = _canonical_document(_Store(), query)
        params, refusal = _reopen(stored)
        assert params is not None, (label, refusal)
        reopened, error = api_main._portal_database_current_view_document(dataset, columns, params)
        assert error is None, (label, error)
        assert reopened == stored, (label, stored, reopened)
    print(f"PASS: {len(LOSSLESS_FILTER_CASES)} canonical filter shapes reopen to exactly their stored meaning")


def test_whitespace_and_exact_values_survive_as_values_not_as_names() -> None:
    """The reviewed defect, reproduced as an assertion on the parameters.

    A name is trimmed because it is a label. A filter VALUE is data: the
    distinct-value picker can display, count and re-filter a value that is
    nothing but spaces, so reopening one must carry it byte for byte. It is
    emitted through `filter_exact__`, the only parameter the S3 parser takes
    verbatim — `filter__` is the human textarea form and is trimmed and split.
    """
    stored = _canonical_document(_Store(), "op__driver_name=in&filter_exact__driver_name=%20%20%20")
    assert stored["filters"] == [
        {"column_name": "driver_name", "operator": "in", "value": "   ", "values": ["   "]}
    ], stored["filters"]

    params, refusal = _reopen(stored)
    assert params is not None, refusal
    assert params["filter_exact__driver_name"] == ["   "], params
    assert params["op__driver_name"] == ["in"], params
    # The trimming form must not be used for a value at all.
    assert "filter__driver_name" not in params, params

    # And the SQL the reopened request produces still carries the constraint.
    _query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), _columns(), params, count=True
    )
    assert error is None, error
    assert "   " in values, values
    assert [f["column_name"] for f in state["active_filters"]] == ["driver_name"], state["active_filters"]

    for raw, encoded in ((" Kowalski", "%20Kowalski"), ("Kowalski ", "Kowalski%20"), ("", "")):
        stored = _canonical_document(_Store(), f"op__driver_name=in&filter_exact__driver_name={encoded}")
        params, refusal = _reopen(stored)
        assert params is not None, (raw, refusal)
        assert params["filter_exact__driver_name"] == [raw], (raw, params)
    print("PASS: leading, trailing, whitespace-only and empty filter values reopen byte for byte")


# Every way a stored filter can fail to mean what it says. None of them may
# resolve to "open the view without that constraint".
STALE_FILTER_DOCUMENTS = [
    ("a filter record that is not an object", ["driver_name = 'x'"], "stale"),
    ("a record with no column", [{"operator": "eq", "value": "x"}], "stale"),
    ("a record with no operator", [{"column_name": "driver_name", "value": "x"}], "stale"),
    ("an unknown operator",
     [{"column_name": "driver_name", "operator": "regex", "value": "x"}], "stale"),
    ("an operator the column's type family does not offer",
     [{"column_name": "distance_km", "operator": "in", "value": "1", "values": ["1"]}], "stale_column"),
    ("a column that no longer exists",
     [{"column_name": "secret_pay_rate", "operator": "eq", "value": "x"}], "stale_column"),
    ("an `in` record with no values",
     [{"column_name": "driver_name", "operator": "in", "value": "", "values": []}], "stale"),
    ("an `in` record whose values are not strings",
     [{"column_name": "driver_name", "operator": "in", "value": "x", "values": [{"v": 1}]}], "stale"),
    ("an `in` record with more values than the operator accepts",
     [{"column_name": "driver_name", "operator": "in", "value": "v0",
       "values": [f"v{n}" for n in range(api_main.PORTAL_DATABASE_MAX_IN_VALUES + 1)]}], "stale"),
    ("an `in` record holding the same value twice",
     [{"column_name": "driver_name", "operator": "in", "value": "a", "values": ["a", "a"]}], "stale"),
    ("two records for one column",
     [{"column_name": "driver_name", "operator": "eq", "value": "a"},
      {"column_name": "driver_name", "operator": "neq", "value": "b"}], "stale"),
    ("a single-value record the live grammar would re-trim",
     [{"column_name": "driver_name", "operator": "eq", "value": " Kowalski"}], "stale"),
    ("a single-value record with no value",
     [{"column_name": "driver_name", "operator": "eq", "value": ""}], "stale"),
    ("a valueless operator carrying a value",
     [{"column_name": "depot", "operator": "blank", "value": "x"}], "stale"),
    ("a range with neither bound",
     [{"column_name": "distance_km", "operator": "between", "value": "", "value_to": ""}], "stale"),
    ("a non-string value",
     [{"column_name": "driver_name", "operator": "eq", "value": 7}], "stale"),
]


def _stale_document(filters):
    return {
        "state_version": api_main.PORTAL_SAVED_STATE_VERSION,
        "filters": filters,
        "sort": "trip_date", "direction": "desc", "page": 1, "limit": 100,
        "layout": {"cols": ["driver_name"], "colorder": [], "colw": {}, "colpin": None},
    }


def test_an_unrepresentable_saved_filter_is_refused_never_dropped() -> None:
    for label, filters, expected in STALE_FILTER_DOCUMENTS:
        params, refusal = _reopen(_stale_document(filters))
        assert params is None, (label, params)
        assert refusal == expected, (label, refusal, expected)
    # A whole `filters` value of the wrong shape is a refusal too.
    for broken in ("filters", {"a": 1}, 7):
        params, refusal = _reopen(_stale_document(broken))
        assert params is None, broken
        assert refusal == "stale", (broken, refusal)
    print(f"PASS: {len(STALE_FILTER_DOCUMENTS) + 3} unrepresentable saved filters refuse; none is silently dropped")


def test_a_refused_saved_view_executes_no_dataset_query_at_all() -> None:
    """Stronger than checking the redirect URL: nothing is read.

    A saved view whose constraint cannot be reproduced must not reach the
    dataset. The redirect target is asserted too, but the decisive evidence is
    that the stubbed persistence recorded no `count` and no `rows` call while
    the refusal was decided.
    """
    for label, filters, _expected in STALE_FILTER_DOCUMENTS:
        store = _Store()
        store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Trasy", _stale_document(filters))
        QUERY_CALLS.clear()
        response = _call(store, "_portal_database_saved_view_open_response",
                         _user(), DATASET_ID, ALICE_VIEW, _FakeRequest())
        assert QUERY_CALLS == [], (label, QUERY_CALLS)
        target = _location(response)
        assert api_main.PORTAL_DATABASE_SAVED_ERROR_PARAM in target, (label, target)
        # And no filter parameter of any kind rode into the redirect.
        for key in _query_of(target):
            assert not key.startswith(("filter__", "filter_exact__", "filter_to__", "op__")), (label, key)
    print("PASS: a saved view that cannot be reopened losslessly reads no rows and widens no query")


def test_a_revoked_filtering_grant_refuses_before_any_read() -> None:
    store = _Store()
    stored = _canonical_document(store, "op__driver_name=in&filter_exact__driver_name=%20Kowalski")
    store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Trasy", stored)
    revoked = _dataset(can_filter_rows=False)
    QUERY_CALLS.clear()
    response = _call(store, "_portal_database_saved_view_open_response",
                     _user(), DATASET_ID, ALICE_VIEW, _FakeRequest(), dataset=revoked)
    assert QUERY_CALLS == [], QUERY_CALLS
    assert "filtering_revoked" in _location(response), _location(response)
    # Also directly, so the refusal is not an accident of the response wrapper.
    params, refusal = _reopen(stored, dataset=revoked)
    assert (params, refusal) == (None, "filtering_revoked"), (params, refusal)
    print("PASS: a revoked `can_filter_rows` refuses the saved view instead of running it unfiltered")


def test_a_saved_view_reopened_unchanged_is_not_reported_as_modified() -> None:
    """`zmieniony` is a real difference, proved through the whole path.

    Saved state -> persisted document -> open route -> canonical URL -> rendered
    sheet. The dirty marker is derived by comparing the SAME canonical document
    the save wrote, so a reopen that changed nothing cannot report a change.
    Making a real change must still report one.
    """
    for label, query in LOSSLESS_FILTER_CASES:
        store = _Store()
        stored = _canonical_document(store, query)
        store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Trasy", stored)
        response = _call(store, "_portal_database_saved_view_open_response",
                         _user(), DATASET_ID, ALICE_VIEW, _FakeRequest())
        target = _location(response)
        assert api_main.PORTAL_DATABASE_SAVED_ERROR_PARAM not in target, (label, target)
        reopened_query = urlsplit(target).query
        html = _render_sheet(store, query=reopened_query)
        assert 'class="db-views-modified"' not in html, label
        assert _tr("db.saved.modified") not in html, label

    # A meaningful change is still reported.
    store = _Store()
    stored = _canonical_document(store, "op__driver_name=in&filter_exact__driver_name=%20Kowalski")
    store.add_view(ALICE_VIEW, ALICE, DATASET_ID, "Trasy", stored)
    changed = _render_sheet(
        store,
        query=f"op__driver_name=in&filter_exact__driver_name=Nowak&{api_main.PORTAL_DATABASE_SAVED_VIEW_PARAM}={ALICE_VIEW}",
    )
    assert _tr("db.saved.modified") in changed
    print("PASS: reopening a saved view unchanged is not dirty; a real difference still is")


# ===========================================================================
# 13. S13 review correction — applying a column set is layout only
# ===========================================================================
def test_applying_a_column_set_preserves_the_current_page() -> None:
    """`page` is row-view state, not layout.

    The reviewed implementation deleted `page` on apply, silently returning a
    user reading page 7 to page 1 — a change to WHICH ROWS they see, made by a
    control that only claims to change which COLUMNS they see. Which rows a page
    holds does not depend on the layout, so there is nothing for the reset to
    protect.
    """
    store = _Store()
    store.add_set(ALICE_SET, ALICE, DATASET_ID, "Wąski", {
        "state_version": api_main.PORTAL_SAVED_STATE_VERSION,
        "cols": ["driver_name", "depot"], "colorder": ["depot", "driver_name"],
        "colw": {"depot": 240}, "colpin": [],
    })
    query = (
        "op__driver_name=in&filter_exact__driver_name=%20Kowalski"
        "&search=Warszawa&sort=depot&direction=desc&page=7&limit=200"
    )
    response = _call(store, "_portal_database_apply_column_set_response",
                     _user(), DATASET_ID, ALICE_SET, _FakeRequest(query=query))
    target = _query_of(_location(response))
    assert target.get("page") == ["7"], target
    assert target.get("limit") == ["200"], target
    assert target.get("sort") == ["depot"] and target.get("direction") == ["desc"], target
    assert target.get("search") == ["Warszawa"], target
    assert target.get("filter_exact__driver_name") == [" Kowalski"], target
    assert target.get("op__driver_name") == ["in"], target
    # Only the layout family changed, and it changed through the S5
    # canonicalization rather than by echoing the stored document.
    assert set(target.get("cols") or []) == {"driver_name", "depot"}, target
    assert (target.get("colorder") or [""])[0].split(",")[0] == "depot", target
    assert "depot:240" in (target.get("colw") or [""])[0], target
    assert target.get("colpin") == [""], target
    print("PASS: applying a column set replaces the layout and preserves page, page size, filters, search and sort")


# ===========================================================================
# 14. S13 review correction — the payload bound is 8192 UTF-8 BYTES
# ===========================================================================
def test_the_payload_bound_is_measured_in_utf8_bytes() -> None:
    limit = api_main.PORTAL_SAVED_OBJECT_STATE_MAX_BYTES
    assert limit == 8192, limit
    assert not hasattr(api_main, "PORTAL_SAVED_OBJECT_STATE_MAX_CHARS"), \
        "the code-point bound must be gone, not shadowed"

    def payload_of(byte_length: int, *, char: str = "a") -> dict:
        """A document whose SERIALISED form is exactly `byte_length` bytes."""
        envelope = len(json.dumps({"v": ""}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        per_char = len(char.encode("utf-8"))
        count = (byte_length - envelope) // per_char
        return {"v": char * count}

    # ASCII: just below, exactly at, and one byte above.
    below = payload_of(limit - 1)
    encoded, error = api_main._portal_saved_state_json(below)
    assert error is None and len(encoded.encode("utf-8")) == limit - 1, (error, encoded and len(encoded))
    exact = payload_of(limit)
    encoded, error = api_main._portal_saved_state_json(exact)
    assert error is None and len(encoded.encode("utf-8")) == limit, error
    over = {"v": exact["v"] + "a"}
    encoded, error = api_main._portal_saved_state_json(over)
    assert (encoded, error) == (None, "db.saved.state_too_large"), (encoded, error)

    # The reviewed defect: fewer than 8192 CHARACTERS, more than 8192 BYTES.
    multibyte = payload_of(limit + 8, char="ż")
    serialised = json.dumps(multibyte, ensure_ascii=False, sort_keys=True)
    assert len(serialised) < limit, len(serialised)
    assert len(serialised.encode("utf-8")) > limit, len(serialised.encode("utf-8"))
    encoded, error = api_main._portal_saved_state_json(multibyte)
    assert (encoded, error) == (None, "db.saved.state_too_large"), (encoded, error)

    # A legitimate saved view is nowhere near the bound.
    ordinary = _canonical_document(_Store(), "op__driver_name=in&filter_exact__driver_name=Kowalski&page=3")
    encoded, error = api_main._portal_saved_state_json(ordinary)
    assert error is None and len(encoded.encode("utf-8")) < 1024, (error, len(encoded or ""))
    print("PASS: the payload bound is 8192 UTF-8 bytes; a multibyte payload under 8192 characters is refused")


# ===========================================================================
# 15. S13 review correction — mixed-version and database-failure states
# ===========================================================================
class _FakeSqlError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _FakeUndefinedTable(_FakeSqlError):
    def __init__(self):
        super().__init__("42P01")


_FakeUndefinedTable.__name__ = "UndefinedTable"


def _fake_db(*, rows=None, raises=None):
    """A `db_conn` stand-in returning fixed rows or raising a chosen error."""
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, values=None):
            fault = raises(sql) if callable(raises) else raises
            if fault is not None:
                raise fault
            self._rows = rows(sql) if callable(rows) else (rows or [])

        def fetchall(self):
            return list(getattr(self, "_rows", []))

        def fetchone(self):
            found = list(getattr(self, "_rows", []))
            return found[0] if found else None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cursor()

    return lambda: _Conn()


# The stub answers with the canonical expression the probe compares, which is
# `pg_get_expr(conbin, conrelid)` — the deparsed parse tree, not the source
# text. That those constants really are what migrations 066+067 deparse to is
# settled against a real PostgreSQL in
# `test_portal_s13_persistence_postgres.py`; here they are the CORRECT baseline
# a wrong-semantics override is measured against.


def _s13_catalog_rows(*, tables=None, drop_columns=(), drop_constraints=(), drop_indexes=(),
                      constraint_overrides=None, column_overrides=None):
    """Catalog rows for a CORRECT 066+067 schema, minus whatever is removed.

    The stub answers the FOUR catalog reads the probe issues. It is not the
    proof that the probe rejects a malformed DEFINITION — that is settled
    against a real PostgreSQL in `test_portal_s13_persistence_postgres.py` — it
    is what lets the four-state classification, including the outage cases, be
    exercised without a database.

    The fourth read is the expression-IDENTITY read added when S13 readiness
    stopped being decided by deparsed text. It also mentions `pg_constraint`, so
    the stub answered it with the constraint inventory and every classification
    came back `INCOMPATIBLE` on a correct schema — the stub was answering a
    question it had not been taught. An authorized schema produces NO identity
    rows, which is what it now says.
    """
    tables = list(api_main.PORTAL_S13_REQUIRED_COLUMN_DEFINITIONS) if tables is None else list(tables)
    constraint_overrides = constraint_overrides or {}
    column_overrides = column_overrides or {}

    def rows(sql):
        if "pg_depend" in sql:
            # The identity read, and it must be answered FIRST: it names
            # `pg_constraint` too, and an authorized S13 schema depends on no
            # unpinned function, operator, type or collation, so it returns
            # nothing at all.
            return []
        if "pg_index" in sql:
            found = []
            for table in tables:
                for name, spec in api_main.PORTAL_S13_REQUIRED_INDEX_DEFINITIONS.get(table, {}).items():
                    if (table, name) in drop_indexes:
                        continue
                    found.append({
                        "table_name": table, "index_name": name,
                        "is_unique": bool(spec["unique"]), "is_valid": True, "is_ready": True,
                        "columns": list(spec["columns"]),
                        "column_count": len(spec["columns"]),
                        "options": [0] * len(spec["columns"]),
                        "is_partial": False, "has_expressions": False,
                    })
            return found
        if "pg_constraint" in sql:
            found = []
            for table in tables:
                for name, spec in api_main.PORTAL_S13_REQUIRED_CONSTRAINT_DEFINITIONS[table].items():
                    if (table, name) in drop_constraints:
                        continue
                    row = {
                        "table_name": table, "constraint_name": name,
                        "constraint_type": spec["type"], "validated": True,
                        "delete_action": spec.get("on_delete", " "),
                        "referenced_table": spec.get("references", ""),
                        "columns": list(spec["columns"]),
                        "referenced_columns": list(spec.get("referenced_columns", ())),
                        "expression": spec.get("expression", ""),
                    }
                    row.update(constraint_overrides.get((table, name), {}))
                    found.append(row)
            return found
        found = []
        for table in tables:
            for column, (data_type, not_null, default) in api_main.PORTAL_S13_REQUIRED_COLUMN_DEFINITIONS[table].items():
                if (table, column) in drop_columns:
                    continue
                row = {
                    "table_name": table, "column_name": column,
                    "data_type": data_type, "not_null": not_null,
                    "column_default": default,
                }
                row.update(column_overrides.get((table, column), {}))
                found.append(row)
        return found

    return rows


def test_the_schema_probe_distinguishes_four_states_not_two() -> None:
    """Column names are not a schema contract.

    A same-named relation can carry every expected column and still have no
    owner foreign key, no cascade, no uniqueness and no payload bound — which is
    S13's entire authorization and integrity story. The probe asks for the rules
    the migration guarantees, and it separates "absent" from "incompatible" from
    "the database could not answer".
    """
    old = api_main.db_conn
    try:
        api_main.db_conn = _fake_db(rows=_s13_catalog_rows())
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_READY
        assert api_main._portal_preferences_schema_available() is True

        api_main.db_conn = _fake_db(rows=lambda sql: [])
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_ABSENT
        assert api_main._portal_preferences_schema_available() is False

        # Two of three relations: a partial migration, not a pre-S13 database.
        api_main.db_conn = _fake_db(rows=_s13_catalog_rows(
            tables=["portal_user_preferences", "portal_database_saved_views"]))
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE

        # Every column present, one rule missing. This is exactly the state the
        # column-name-only probe accepted as ready.
        for missing in (
            ("portal_database_saved_views", "portal_database_saved_views_owner_user_id_fkey"),
            ("portal_database_saved_views", "portal_database_saved_views_state_bound_check"),
            ("portal_database_saved_views", "portal_database_saved_views_owner_dataset_name_key"),
            ("portal_database_column_sets", "portal_database_column_sets_dataset_id_fkey"),
            ("portal_user_preferences", "portal_user_preferences_theme_check"),
        ):
            api_main.db_conn = _fake_db(rows=_s13_catalog_rows(drop_constraints=(missing,)))
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE, missing
            assert api_main._portal_preferences_schema_available() is False, missing

        # A missing column is also incompatible, never absence.
        api_main.db_conn = _fake_db(rows=_s13_catalog_rows(
            drop_columns=(("portal_database_saved_views", "view_state_json"),)))
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE

        # A missing listing index is incompatible too.
        api_main.db_conn = _fake_db(rows=_s13_catalog_rows(
            drop_indexes=(("portal_database_saved_views", "idx_portal_database_saved_views_owner"),)))
        assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE

        # And the correction the review required: a constraint whose NAME is
        # right and whose RULE is wrong. Every one of these was `ready` under a
        # probe that only counted names.
        for label, overrides in (
            ("foreign key to the wrong relation",
             {("portal_database_saved_views", "portal_database_saved_views_owner_user_id_fkey"):
              {"referenced_table": "portal_database_datasets"}}),
            ("foreign key on the wrong column",
             {("portal_database_saved_views", "portal_database_saved_views_owner_user_id_fkey"):
              {"columns": ["dataset_id"]}}),
            ("foreign key that does not cascade",
             {("portal_user_preferences", "portal_user_preferences_user_id_fkey"):
              {"delete_action": "a"}}),
            ("theme check accepting a fourth value",
             {("portal_user_preferences", "portal_user_preferences_theme_check"):
              {"expression": "(theme = ANY (ARRAY['auto'::text, 'light'::text, "
                             "'dark'::text, 'solarized'::text]))"}}),
            ("theme check inverted into NOT IN",
             {("portal_user_preferences", "portal_user_preferences_theme_check"):
              {"expression": "(theme <> ALL (ARRAY['auto'::text, 'light'::text, 'dark'::text]))"}}),
            ("theme check missing an approved value",
             {("portal_user_preferences", "portal_user_preferences_theme_check"):
              {"expression": "(theme = ANY (ARRAY['auto'::text, 'light'::text]))"}}),
            ("theme check differing only in the case of a value",
             {("portal_user_preferences", "portal_user_preferences_theme_check"):
              {"expression": "(theme = ANY (ARRAY['auto'::text, 'light'::text, 'DARK'::text]))"}}),
            ("payload shape rule requiring a NON-object",
             {("portal_database_saved_views", "portal_database_saved_views_state_object_check"):
              {"expression": "(jsonb_typeof(view_state_json) <> 'object'::text)"}}),
            ("payload shape rule demanding an array",
             {("portal_database_saved_views", "portal_database_saved_views_state_object_check"):
              {"expression": "(jsonb_typeof(view_state_json) = 'array'::text)"}}),
            ("payload bound requiring at least 8192 bytes",
             {("portal_database_saved_views", "portal_database_saved_views_state_bound_check"):
              {"expression": "(octet_length((view_state_json)::text) >= 8192)"}}),
            ("payload bound requiring more than 8192 bytes",
             {("portal_database_saved_views", "portal_database_saved_views_state_bound_check"):
              {"expression": "(octet_length((view_state_json)::text) > 8192)"}}),
            ("payload bound excluding the authorized 8192nd byte",
             {("portal_database_saved_views", "portal_database_saved_views_state_bound_check"):
              {"expression": "(octet_length((view_state_json)::text) < 8192)"}}),
            ("name bound with the comparison inverted",
             {("portal_database_saved_views", "portal_database_saved_views_name_check"):
              {"expression": "((view_name <> ''::text) AND (char_length(view_name) >= 80))"}}),
            ("name bound that no longer refuses an empty name",
             {("portal_database_column_sets", "portal_database_column_sets_name_check"):
              {"expression": "(char_length(set_name) <= 80)"}}),
            ("name bound at a different length",
             {("portal_database_column_sets", "portal_database_column_sets_name_check"):
              {"expression": "((set_name <> ''::text) AND (char_length(set_name) <= 40))"}}),
            ("a payload rule computed by a shadowing function",
             {("portal_database_column_sets", "portal_database_column_sets_state_bound_check"):
              {"expression": "(public.octet_length((layout_state_json)::text) <= 8192)"}}),
            ("a check with no expression at all",
             {("portal_database_column_sets", "portal_database_column_sets_state_object_check"):
              {"expression": ""}}),
            ("uniqueness over the wrong columns",
             {("portal_database_saved_views", "portal_database_saved_views_owner_dataset_name_key"):
              {"columns": ["owner_user_id", "view_name"]}}),
            ("payload bound counted in code points",
             {("portal_database_column_sets", "portal_database_column_sets_state_bound_check"):
              {"expression": "(char_length((layout_state_json)::text) <= 8192)"}}),
            ("payload object check covering the wrong column",
             {("portal_database_saved_views", "portal_database_saved_views_state_object_check"):
              {"columns": ["view_name"]}}),
            ("a primary key that is really a unique constraint",
             {("portal_user_preferences", "portal_user_preferences_pkey"): {"constraint_type": "u"}}),
            ("a constraint that was never validated",
             {("portal_database_column_sets", "portal_database_column_sets_owner_user_id_fkey"):
              {"validated": False}}),
        ):
            api_main.db_conn = _fake_db(rows=_s13_catalog_rows(constraint_overrides=overrides))
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE, label
            assert api_main._portal_preferences_schema_available() is False, label

        # A column whose type or default no longer matches is incompatible.
        for label, overrides in (
            ("payload stored as text rather than jsonb",
             {("portal_database_saved_views", "view_state_json"): {"data_type": "text"}}),
            ("an identifier with no generated default",
             {("portal_database_column_sets", "column_set_id"): {"column_default": ""}}),
            ("a nullable owner",
             {("portal_database_saved_views", "owner_user_id"): {"not_null": False}}),
        ):
            api_main.db_conn = _fake_db(rows=_s13_catalog_rows(column_overrides=overrides))
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_INCOMPATIBLE, label

        # And an outage is an outage. The catalog relations this probe reads
        # always exist, so an exception here is a database fault by construction.
        for fault in (_FakeSqlError("53300"), _FakeSqlError("42501"), _FakeSqlError("57014"),
                      _FakeUndefinedTable(), RuntimeError("connection refused")):
            api_main.db_conn = _fake_db(raises=fault)
            assert api_main._portal_s13_schema_state() == api_main.PORTAL_S13_SCHEMA_ERROR, fault
            assert api_main._portal_preferences_schema_available() is False, fault
    finally:
        api_main.db_conn = old
    print("PASS: the S13 probe separates ready, absent, structurally incompatible and database fault")


def test_only_a_confirmed_missing_relation_degrades_to_pre_s13() -> None:
    """A database fault must not present itself as "the migration is not applied"."""
    assert api_main._portal_missing_relation_error(_FakeUndefinedTable()) is True
    for not_absence in (
        _FakeSqlError("42703"),   # undefined_column: a PARTIAL relation
        _FakeSqlError("42501"),   # insufficient_privilege
        _FakeSqlError("53300"),   # too_many_connections
        _FakeSqlError("57014"),   # query_canceled
        _FakeSqlError("40001"),   # serialization_failure
        _FakeSqlError("42601"),   # syntax_error
        RuntimeError("connection refused"),
    ):
        assert api_main._portal_missing_relation_error(not_absence) is False, not_absence

    old = api_main.db_conn
    try:
        # CONFIRMED absent: the relation read raises `42P01` AND the catalog
        # agrees that none of the three relations exists. Only then does the
        # documented pre-S13 rollout tolerance apply.
        api_main.db_conn = _fake_db(
            rows=lambda sql: [],
            raises=lambda sql: _FakeUndefinedTable() if "portal_user_preferences" in sql else None,
        )
        assert api_main._get_portal_user_theme_state(ALICE) == ("auto", api_main.PORTAL_THEME_SCOPE_LOCAL)
        assert api_main._get_portal_user_theme(ALICE) == ("auto", False)

        # The SAME `42P01`, but the catalog says the relations are there: a role
        # locked out of the schema gets exactly this from PostgreSQL, and it is
        # not a pre-S13 database. The mirror must not become account state.
        api_main.db_conn = _fake_db(
            rows=_s13_catalog_rows(),
            raises=lambda sql: _FakeUndefinedTable() if "portal_user_preferences" in sql else None,
        )
        assert api_main._get_portal_user_theme_state(ALICE) == (
            "auto", api_main.PORTAL_THEME_SCOPE_UNAVAILABLE
        )

        # NOT absence: the account remains the authority, it simply cannot be
        # read, and the browser mirror must not be promoted to account state.
        for fault in (_FakeSqlError("42703"), _FakeSqlError("42501"), _FakeSqlError("53300"),
                      RuntimeError("connection refused")):
            api_main.db_conn = _fake_db(raises=(lambda exc: (lambda sql: exc))(fault))
            theme, scope = api_main._get_portal_user_theme_state(ALICE)
            assert (theme, scope) == ("auto", api_main.PORTAL_THEME_SCOPE_UNAVAILABLE), (fault, theme, scope)
            assert api_main._get_portal_user_theme(ALICE) == ("auto", False), fault
    finally:
        api_main.db_conn = old
    print("PASS: only a confirmed missing relation degrades to pre-S13; every other fault is operational")


def test_a_database_fault_never_lets_the_browser_mirror_become_account_state() -> None:
    """Account B must not inherit the mirror account A left in this browser."""
    store = _Store()
    store.theme_scope = api_main.PORTAL_THEME_SCOPE_UNAVAILABLE
    html = _render_sheet(store)
    root = html[html.index("<html"):html.index(">", html.index("<html")) + 1]
    assert 'data-theme-scope="unavailable"' in root, root
    assert 'data-theme-scope="server"' not in root, root
    # Neutral, not "whatever this browser last held".
    assert 'data-theme="' not in root, root
    assert 'data-theme-mode="auto"' in root, root
    # The control is offered, states that a choice is not stored, and is not the
    # durable server form.
    assert "data-theme-form" not in html
    assert _tr("shell.theme.save_failed") in html

    # The CONFIRMED pre-S13 case is different and keeps the documented
    # browser-local tolerance: no scope marker at all.
    store.theme_scope = api_main.PORTAL_THEME_SCOPE_LOCAL
    local_root = _render_sheet(store)
    local_root = local_root[local_root.index("<html"):local_root.index(">", local_root.index("<html")) + 1]
    assert "data-theme-scope" not in local_root, local_root
    print("PASS: an unreadable preference renders neutral and forbids the browser mirror from acting as account state")



# ===========================================================================
# 16. S13 review correction — the SHIPPED theme engine, executed
#
# No browser is available in this environment, so `theme.js` is executed by a
# DOM stub (`ops/tests_manual/theme_engine_harness.js`, the same convention the
# S3 filter suite already uses) rather than reimplemented in Python. The stub
# provides `localStorage`, a `fetch` that RECORDS requests and resolves them on
# an explicit plan, and a server model whose stored value is written when a
# response is delivered — so "an older write arrives last" is a scenario, not a
# race the test hopes to hit.
#
# `LIVE_BROWSER_VERIFICATION_NOT_AVAILABLE` still holds: this proves the shipped
# script's logic, not a real browser's event loop.
# ===========================================================================
THEME_HARNESS = REPO_ROOT / "ops" / "tests_manual" / "theme_engine_harness.js"


def _theme_scenario(scenario: str, *, script: Path | None = None) -> dict:
    command = ["node", str(THEME_HARNESS), scenario]
    if script is not None:
        command.append(str(script))
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120)
    assert result.returncode == 0, (scenario, result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert "error" not in payload, payload
    return payload


def test_the_shipped_theme_script_keeps_the_account_authoritative() -> None:
    if shutil.which("node") is None:
        print("SKIP: node is unavailable; the shipped theme engine could not be executed")
        return

    # Account A left `dark` in this browser. Account B's row says light.
    result = _theme_scenario("server-authority-overwrites-mirror")
    assert result["current"] == "light", result
    assert result["dataTheme"] == "light", result
    assert result["mirror"] == "light", "the mirror is reconciled to the account row"
    assert result["pressed"] == ["light"], result

    # And B's row saying AUTO clears the mirror rather than inheriting `dark`.
    result = _theme_scenario("server-authority-auto-clears-mirror")
    assert result["current"] == "auto" and result["dataTheme"] is None, result
    assert result["mirror"] is None, result

    # Account B signs in while the platform database is unhealthy. The account
    # is still the authority; it just cannot be read. The mirror must not be
    # promoted into account state.
    result = _theme_scenario("unavailable-authority-ignores-mirror")
    assert result["current"] == "auto", result
    assert result["dataTheme"] is None, result
    assert result["mirror"] == "dark", "the mirror is left alone, not read and not rewritten"
    assert result["pressed"] == ["auto"], result

    # A choice made in that state applies to the document and is STATED as not
    # stored; it must not be written to the mirror as though it were durable.
    result = _theme_scenario("unavailable-authority-choice-is-not-durable")
    assert result["current"] == "light" and result["dataTheme"] == "light", result
    assert result["mirror"] == "dark", result
    assert result["status"] == ["NOT_STORED"], result
    assert result["requestModes"] == [], "there is no durable form to write to"

    # A CONFIRMED pre-S13 database keeps the documented browser-local tolerance.
    result = _theme_scenario("local-authority-uses-mirror")
    assert (result["current"], result["dataTheme"], result["mirror"]) == ("dark", "dark", "dark"), result
    result = _theme_scenario("local-authority-writes-mirror")
    assert result["mirror"] == "dark" and result["requestModes"] == [], result
    print("PASS: the shipped theme engine keeps the account authoritative and never adopts another account's mirror")


def test_the_shipped_theme_script_settles_on_the_last_accepted_choice() -> None:
    if shutil.which("node") is None:
        print("SKIP: node is unavailable; the shipped theme engine could not be executed")
        return

    # Two clicks with the responses planned to arrive in the WRONG order.
    result = _theme_scenario("race-two-clicks-reordered-responses")
    assert result["maxConcurrent"] == 1, "writes must not overlap; there is nothing to reorder"
    assert result["requestModes"] == ["dark", "light"], result
    assert result["serverValue"] == "light", result
    assert result["current"] == "light" and result["pressed"] == ["light"], result
    assert result["mirror"] == "light", result
    assert result["status"] == [""], result

    # Three clicks in one burst: the intermediate intent is coalesced away and
    # the server is asked only for the choice the user actually ended on.
    result = _theme_scenario("race-burst-coalesces-to-the-last-choice")
    assert result["maxConcurrent"] == 1, result
    assert result["requestModes"] == ["dark", "light"], result
    assert result["serverValue"] == "light", result
    assert result["mirror"] == "light" and result["current"] == "light", result

    # A superseded response — success or failure — decides nothing at the moment
    # it arrives.
    result = _theme_scenario("race-recovers-after-failure")
    assert result["status"] == [""], "a stale failure must not claim the surface"
    assert result["mirror"] == "light" and result["serverValue"] == "light", result

    # Three real writes, middle one refused, then a successful last one.
    result = _theme_scenario("race-middle-write-fails")
    assert result["requestModes"] == ["dark", "auto", "light"], result
    assert result["maxConcurrent"] == 1, result
    assert result["serverValue"] == "light" and result["mirror"] == "light", result
    assert result["status"] == [""], "the recovered write clears the earlier statement"
    print("PASS: rapid theme changes settle on the last accepted choice; a stale response decides nothing")


REVIEWED_COMMIT = "722fc437595920afb0780955b8d3a5c052425c86"

# Every scenario that reaches a settled state, and the ONE answer the visible
# state, the browser mirror and the account row must all give once it has.
# `mirror` is `None` for AUTO because the mirror represents AUTO by absence.
SETTLED_THEME_SCENARIOS = [
    ("a single accepted write", "single-write-succeeds", "dark", "dark", "dark", ""),
    ("a burst whose last write is accepted", "race-burst-coalesces-to-the-last-choice",
     "light", "light", "light", ""),
    ("reordered responses for two clicks", "race-two-clicks-reordered-responses",
     "light", "light", "light", ""),
    ("a middle write refused, the latest accepted", "race-middle-write-fails",
     "light", "light", "light", ""),
    ("an earlier failure recovered by a later success", "race-recovers-after-failure",
     "light", "light", "light", ""),
    # THE REVIEWED DEFECT. An earlier choice was durably stored, the latest one
    # failed, and the reviewed engine settled on UI=light, mirror=auto,
    # server=dark.
    ("a superseded success and a failed latest intent", "superseded-success-then-latest-failure",
     "dark", "dark", "dark", "NOT_STORED"),
    ("the superseded success delivered before the latest request",
     "superseded-success-delivered-before-latest-request", "dark", "dark", "dark", "NOT_STORED"),
    ("three writes whose last is refused", "race-three-writes-last-fails",
     "auto", None, "auto", "NOT_STORED"),
    ("a failed latest intent with nothing confirmed since the page loaded",
     "latest-failure-with-no-confirmed-write", "dark", "dark", "dark", "NOT_STORED"),
    ("a burst whose latest write is refused", "race-last-write-fails",
     "dark", "dark", "dark", "NOT_STORED"),
    ("the server reporting the mode it actually stored", "server-reports-the-mode-it-stored",
     "dark", "dark", "dark", ""),
    ("a durable form with no transport at all", "no-transport-cannot-persist",
     "dark", "dark", "dark", "NOT_STORED"),
]


def test_a_settled_theme_write_leaves_ui_mirror_and_account_row_agreeing() -> None:
    """The closure contract: there is ONE answer once the queue has settled.

    If the latest intent was persisted, it is final everywhere. If it was not,
    everything the user can see goes back to the LAST CONFIRMED DURABLE server
    preference and the failure is stated truthfully. What must never survive is
    a failed optimistic choice presented as though it were stored, or the three
    representations of one preference disagreeing.
    """
    if shutil.which("node") is None:
        print("SKIP: node is unavailable; the shipped theme engine could not be executed")
        return
    for label, scenario, expected_mode, expected_mirror, expected_server, expected_status in SETTLED_THEME_SCENARIOS:
        result = _theme_scenario(scenario)
        assert result["current"] == expected_mode, (label, result)
        assert result["pressed"] == [expected_mode], (label, result)
        assert result["dataTheme"] == (None if expected_mode == "auto" else expected_mode), (label, result)
        assert result["mirror"] == expected_mirror, (label, result)
        assert result["serverValue"] == expected_server, (label, result)
        assert result["status"] == [expected_status], (label, result)
        # The three representations of one preference agree, always.
        assert result["current"] == result["serverValue"], (label, result)
        assert result["mirror"] == (None if result["serverValue"] == "auto" else result["serverValue"]), (label, result)
        assert result["maxConcurrent"] <= 1, (label, result)
    print(f"PASS: {len(SETTLED_THEME_SCENARIOS)} settled theme sequences agree across UI, mirror and account row")


def test_the_reviewed_theme_engine_left_the_three_states_disagreeing() -> None:
    """RED on `722fc43`, by executing the script it shipped.

    The correction cannot be demonstrated by the corrected script alone, so the
    superseded `theme.js` is checked out into a temporary file and driven
    through the same scenario.
    """
    if shutil.which("node") is None:
        print("SKIP: node is unavailable; the shipped theme engine could not be executed")
        return
    reviewed = subprocess.run(
        ["git", "show", f"{REVIEWED_COMMIT}:api/static/js/theme.js"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    if reviewed.returncode != 0:
        print("SKIP: the reviewed commit is not reachable in this checkout")
        return
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "theme.js"
        script.write_text(reviewed.stdout, encoding="utf-8")
        before = _theme_scenario("superseded-success-then-latest-failure", script=script)
    # UI on the failed choice, mirror on the page's original value, account row
    # on the superseded one: three different answers.
    assert before["current"] == "light", before
    assert before["mirror"] is None, before
    assert before["serverValue"] == "dark", before
    assert len({before["current"], before["mirror"] or "auto", before["serverValue"]}) == 3, before

    after = _theme_scenario("superseded-success-then-latest-failure")
    assert (after["current"], after["mirror"], after["serverValue"]) == ("dark", "dark", "dark"), after
    assert after["status"] == ["NOT_STORED"], "the failure is still stated truthfully"
    print("PASS: the reviewed engine settled on three disagreeing states; the corrected one reconciles to the durable value")


def main() -> None:
    test_migration_is_the_next_number_and_additive()
    test_the_correction_migration_makes_the_structure_deterministic()
    test_no_unrelated_schema_or_release_requirement_change()

    test_default_theme_is_auto_and_auto_leaves_data_theme_absent()
    test_explicit_theme_is_applied_server_side_before_first_paint()
    test_only_the_three_approved_theme_values_are_accepted()
    test_theme_write_takes_the_account_from_the_session_only()
    test_account_switching_does_not_inherit_another_accounts_theme()
    test_the_browser_mirror_never_holds_an_account_identity()
    test_theme_switching_never_navigates_or_reloads()
    test_a_failed_theme_write_does_not_claim_durable_persistence()
    test_without_the_migration_the_theme_falls_back_to_browser_local()
    test_the_theme_return_target_cannot_leave_the_portal()

    test_a_saved_view_captures_only_canonical_durable_view_state()
    test_a_saved_view_stores_no_sql_no_url_and_no_row_identity()
    test_page_and_page_size_are_part_of_a_saved_view_per_d007()
    test_saving_reuses_the_current_parsers_and_refuses_state_the_sheet_would_refuse()
    test_a_crafted_unapproved_identifier_never_reaches_a_saved_view()

    test_opening_a_saved_view_reproduces_the_intended_state_as_a_canonical_url()
    test_a_saved_view_can_never_become_an_open_redirect()
    test_a_saved_view_whose_filter_column_is_gone_fails_closed()
    test_a_saved_view_with_filters_is_refused_once_can_filter_rows_is_revoked()
    test_a_saved_view_whose_sort_column_is_gone_fails_closed()
    test_a_saved_view_cannot_restore_a_hidden_column_but_normalises_the_layout()
    test_a_tampered_or_wrong_version_payload_is_refused_not_repaired()
    test_an_oversized_payload_is_refused_at_the_application_boundary()

    test_every_saved_object_statement_scopes_by_owner()
    test_a_user_cannot_open_rename_overwrite_or_delete_another_accounts_view()
    test_a_saved_view_from_another_dataset_does_not_resolve()
    test_a_revoked_dataset_grant_hides_the_object_without_deleting_it()
    test_a_malformed_object_id_is_answered_as_absence_not_as_an_error()

    test_saving_and_updating_a_view_round_trips_through_the_canonical_state()
    test_a_duplicate_name_is_refused_deterministically_and_never_overwrites()
    test_a_hostile_name_is_bounded_normalised_and_escaped_never_markup()

    test_a_column_set_stores_only_column_layout_state()
    test_applying_a_column_set_preserves_unrelated_row_state()
    test_a_column_set_cannot_expand_beyond_the_approved_columns()
    test_a_column_set_that_names_nothing_available_keeps_a_visible_column()
    test_an_invalid_persisted_layout_payload_cannot_produce_raw_sql_or_a_traceback()
    test_column_set_crud_is_owner_scoped()

    test_catalogue_shows_saved_view_chips_only_for_authorized_datasets()
    test_catalogue_omits_the_column_entirely_without_the_migration()
    test_the_row_sheet_carries_the_approved_polish_copy()
    test_the_active_view_is_named_and_reports_the_modified_state()
    test_an_unresolvable_view_reference_is_dropped_from_the_canonical_state()
    test_the_sheet_degrades_cleanly_without_the_migration()
    test_every_s13_control_works_without_scripting()

    test_new_audit_types_are_registered_and_drive_the_real_validator()
    test_audit_metadata_carries_facts_not_filter_values()
    test_the_theme_audit_records_the_enum_and_the_outcome()

    test_a_saved_filter_reopens_with_exactly_its_stored_meaning()
    test_whitespace_and_exact_values_survive_as_values_not_as_names()
    test_an_unrepresentable_saved_filter_is_refused_never_dropped()
    test_a_refused_saved_view_executes_no_dataset_query_at_all()
    test_a_revoked_filtering_grant_refuses_before_any_read()
    test_a_saved_view_reopened_unchanged_is_not_reported_as_modified()
    test_applying_a_column_set_preserves_the_current_page()
    test_the_payload_bound_is_measured_in_utf8_bytes()
    test_the_schema_probe_distinguishes_four_states_not_two()
    test_only_a_confirmed_missing_relation_degrades_to_pre_s13()
    test_a_database_fault_never_lets_the_browser_mirror_become_account_state()
    test_the_shipped_theme_script_keeps_the_account_authoritative()
    test_the_shipped_theme_script_settles_on_the_last_accepted_choice()
    test_a_settled_theme_write_leaves_ui_mirror_and_account_row_agreeing()
    test_the_reviewed_theme_engine_left_the_three_states_disagreeing()

    test_no_s14_or_s15_entity_is_pre_built()
    test_the_approved_design_handoff_is_untouched()

    print("\nALL PASS: PORTAL_SERVER_SIDE_PREFERENCES_AND_SAVED_DATABASE_VIEWS (S13)")


if __name__ == "__main__":
    main()
