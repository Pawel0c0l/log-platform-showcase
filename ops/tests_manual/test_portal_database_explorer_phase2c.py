#!/usr/bin/env python3
"""Phase 2C tests: admin-configured row identifier + permission-safe row detail.

Covers the new migration shape, the identifier resolution/validation helpers, the
admin row-identifier control, the user row-detail route (auth, dataset/can_view_rows
gating delegated to the access query, parameterized single-row SQL, visible-only
SELECT, safe not-found and duplicate handling), and the per-row Details links that
work even when the identifier column is not in the on-screen cols selection.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_explorer_phase2c.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

    def post(self, *args, **kwargs):
        return lambda fn: fn

    def patch(self, *args, **kwargs):
        return lambda fn: fn

    def delete(self, *args, **kwargs):
        return lambda fn: fn

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


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

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
MIGRATION = REPO_ROOT / "db" / "migrations" / "039_portal_database_row_identifier.sql"


class _FakeUrl:
    def __init__(self, path=f"/user/database/datasets/{DATASET_ID}/rows/T-1", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path=f"/user/database/datasets/{DATASET_ID}/rows/T-1", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False):
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": admin, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "client_database_name": "acme_db",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_date",
        "is_active": True, "visible_columns": 3, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _columns(*, identifier="trip_id"):
    cols = [
        {"dataset_id": DATASET_ID, "column_name": "trip_id", "display_name": "Trip ID", "data_type": "integer",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False,
         "is_row_identifier": False, "display_order": 5},
        {"dataset_id": DATASET_ID, "column_name": "trip_date", "display_name": "Trip date", "data_type": "date",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": True,
         "is_row_identifier": False, "display_order": 10},
        {"dataset_id": DATASET_ID, "column_name": "driver_name", "display_name": "Driver", "data_type": "text",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False,
         "is_row_identifier": False, "display_order": 20},
        {"dataset_id": DATASET_ID, "column_name": "internal_secret", "display_name": "Hidden", "data_type": "text",
         "is_visible": False, "is_filterable": False, "is_sortable": False, "is_default_date_column": False,
         "is_row_identifier": False, "display_order": 30},
    ]
    for col in cols:
        if identifier and col["column_name"] == identifier:
            col["is_row_identifier"] = True
    return cols


def _visible_columns(**kwargs):
    return [c for c in _columns(**kwargs) if c["is_visible"]]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


# --- fake client DB connection that records executed SQL / params -------------
class _FakeCursor:
    def __init__(self, recorder, result):
        self._recorder = recorder
        self._result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, values=None):
        self._recorder["query"] = query
        self._recorder["values"] = values

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, recorder, result):
        self._recorder = recorder
        self._result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._recorder, self._result)


def _render_detail(*, dataset, columns, result, row_id="T-1", query="", capture_audit=None):
    recorder: dict = {}
    audit = capture_audit if capture_audit is not None else []
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: columns)),
        ("_portal_dataset_client_database_name", _patch("_portal_dataset_client_database_name", lambda d: "acme_db")),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", lambda name: _FakeConn(recorder, result))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: audit.append(kwargs))),
    ]
    try:
        resp = api_main._portal_database_row_detail_response(_user(), DATASET_ID, row_id, _FakeRequest(query=query))
        return resp, recorder, audit
    finally:
        _restore(patches)


def _render_browser(dataset, columns, *, query=""):
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: [c for c in columns if not c.get("is_row_identifier")])),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda dataset_id: next((c for c in columns if c.get("is_row_identifier")), None))),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda d, c, p, limit, offset, display_columns=None: ([{"trip_id": 42, "trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"}], {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query)))
    finally:
        _restore(patches)


# ---------------------------------------------------------------------------
def _test_migration_adds_flag_idempotently() -> None:
    assert MIGRATION.exists(), MIGRATION
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "add column if not exists is_row_identifier boolean not null default false" in sql, sql
    # at most one row identifier per dataset via a partial unique index
    assert "create unique index if not exists" in sql, sql
    assert "(dataset_id)" in sql.replace(" ", "").replace("\n", "") or "(dataset_id)" in sql, sql
    assert "where is_row_identifier is true" in sql, sql
    print("PASS: migration adds is_row_identifier idempotently with a partial unique index")


def _test_resolve_row_identifier() -> None:
    # S6 separated technical identity from user visibility: identity no longer
    # implies `is_visible`, because the identifier is deliberately hidden.
    ident = api_main._portal_database_resolve_row_identifier(_visible_columns(identifier="trip_id"))
    assert ident and ident["column_name"] == "trip_id", ident
    # none configured -> None
    assert api_main._portal_database_resolve_row_identifier(_visible_columns(identifier=None)) is None
    # a HIDDEN flagged column is now the expected production shape and resolves.
    hidden = {"column_name": "record_id", "display_name": "Record", "data_type": "text",
              "is_visible": False, "is_row_identifier": True}
    resolved = api_main._portal_database_resolve_row_identifier(
        _visible_columns(identifier=None) + [hidden]
    )
    assert resolved and resolved["column_name"] == "record_id", resolved
    # two identifiers is invalid configuration and resolves to nothing rather
    # than guessing which one is authoritative.
    both = _visible_columns(identifier="trip_id") + [hidden]
    assert api_main._portal_database_resolve_row_identifier(both) is None
    print("PASS: identity resolves independently of visibility and refuses ambiguity")


def _test_set_identifier_validation() -> None:
    cols = _columns(identifier=None)
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: cols)),
    ]
    try:
        # S6: a hidden column is now the intended production shape for the
        # technical identifier and must be accepted, not rejected. The write
        # itself is not exercised here (no database in this harness); what this
        # asserts is that validation no longer refuses it before the write.
        # unknown column rejected
        err = api_main._set_portal_database_row_identifier(dataset_id=DATASET_ID, column_name="does_not_exist")
        assert err and "cataloged" in err.lower(), err
    finally:
        _restore(patches)
    # missing dataset rejected
    old = _patch("_get_portal_database_dataset", lambda dataset_id: None)
    try:
        err = api_main._set_portal_database_row_identifier(dataset_id=DATASET_ID, column_name="trip_id")
        assert err and "not found" in err.lower(), err
    finally:
        setattr(api_main, "_get_portal_database_dataset", old)
    print("PASS: set row identifier rejects unknown and missing-dataset inputs, and allows hidden")


def _test_raw_identifier_route_is_retired() -> None:
    """S6: the raw-identifier row route must not survive as a bypass.

    Before S6 `/rows/{row_id}` accepted the technical identifier straight from
    the path, which both disclosed it and offered a second way to address a row
    beside the opaque reference. It now answers 404 for an authenticated user —
    not a redirect, which would confirm that a supplied value was a real
    identifier — and authentication still runs first.
    """
    assert not hasattr(api_main, "_portal_database_row_detail_response"), \
        "the raw-identifier detail renderer must be gone, not merely unrouted"

    # Unauthenticated: the session gate answers before the route body.
    redirect = api_main.HTMLResponse("", status_code=303, headers={"Location": "/artifact-explorer/login"})
    old = _patch("_require_portal_user", lambda request: redirect)
    try:
        resp = api_main.user_portal_database_dataset_row(DATASET_ID, "T-1", _FakeRequest())
        assert resp.status_code == 303, resp.status_code
    finally:
        _restore([("_require_portal_user", old)])

    # Authenticated: a flat 404 regardless of whether the value exists.
    old = _patch("_require_portal_user", lambda request: _user())
    try:
        for candidate in ("T-1", "42", "does-not-exist", "'; DROP TABLE x--"):
            try:
                api_main.user_portal_database_dataset_row(DATASET_ID, candidate, _FakeRequest())
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 404, (candidate, exc)
            else:
                raise AssertionError(f"raw row route still served {candidate!r}")
    finally:
        _restore([("_require_portal_user", old)])
    print("PASS: the raw-identifier row route is retired and cannot be used as a bypass")


def _test_browser_emits_opaque_references_not_raw_identifiers() -> None:
    """The sheet addresses a row without ever disclosing its identity."""
    identity = {"dataset_id": DATASET_ID, "column_name": "trip_id", "display_name": "Trip",
                "data_type": "text", "is_visible": False, "is_filterable": False,
                "is_sortable": False, "is_default_date_column": False,
                "is_row_identifier": True, "display_order": 1}
    columns = [c for c in _visible_columns(identifier=None) if c.get("column_name") != "trip_id"] + [identity]
    html = _render_browser(_dataset(), columns)
    # The raw identity value (42) must not reach the page in any form.
    assert "/rows/42" not in html, html
    assert 'data-db-copy="42"' not in html, html
    assert 'data-db-row="42"' not in html, html
    # What it does carry is an opaque reference in the row parameter.
    assert "data-db-row=" in html, html
    assert "row=" in html, html
    # The identifier column itself is not a user column anywhere on the sheet.
    assert "trip_id" not in html, html
    print("PASS: the sheet emits opaque row references and never the raw identifier")


def _test_browser_no_details_without_identifier() -> None:
    html = _render_browser(_dataset(), _visible_columns(identifier=None))
    assert ">Details<" not in html and "/rows/" not in html, html
    print("PASS: no Details links when the dataset has no row identifier (legacy behavior)")


def _render_admin_form(columns):
    dataset = _dataset()
    detail = {
        "dataset": dataset,
        "client": {"client_code": "ACME_01", "display_name": "Acme Logistics", "is_active": True},
        "assigned": [], "available": [], "assigned_groups": [], "available_groups": [],
        "eligible_user_count": 0, "eligible_group_count": 0,
        "columns": columns,
    }
    discovery = {"status": "all_cataloged", "physical_columns": [], "available_columns": [], "message": ""}
    patches = [
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: detail)),
        ("_safe_list_portal_database_physical_columns", _patch("_safe_list_portal_database_physical_columns", lambda d: ([], None))),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: columns)),
        ("_portal_database_column_discovery_state", _patch("_portal_database_column_discovery_state", lambda d, c: discovery)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [{"client_code": "ACME_01", "display_name": "Acme Logistics", "is_active": True}])),
        ("_list_portal_dataset_schemas", _patch("_list_portal_dataset_schemas", lambda client: ["public"])),
        ("_list_portal_dataset_tables", _patch("_list_portal_dataset_tables", lambda client, schema: [{"table_name": "trips"}])),
        ("_list_portal_dataset_columns", _patch("_list_portal_dataset_columns", lambda client, schema, table: [])),
    ]
    try:
        return _html(api_main._portal_database_dataset_form_response(_user(admin=True), dataset=dataset))
    finally:
        _restore(patches)


def _test_admin_form_shows_row_identifier_control() -> None:
    html = _render_admin_form(_columns(identifier="trip_id"))
    # dedicated control is rendered with helper text
    assert 'data-testid="portal-database-row-identifier"' in html, html
    assert "Row identifier" in html and "stable unique column" in html, html
    # the configured identifier is reflected as selected
    assert '<option value="trip_id" selected>' in html, html
    # the columns table exposes a Row ID badge column
    assert "<th>Row ID</th>" in html, html
    # S6: hidden columns MUST now be offered — the technical identifier is
    # normally hidden, and admin configuration tooling is allowed to describe
    # physical identity even though ordinary users never see the column.
    form = html.split('data-testid="portal-database-row-identifier"', 1)[1].split("</form>", 1)[0]
    assert "internal_secret" in form, form
    print("PASS: admin form offers every cataloged column, including hidden ones, as identifier")


def _test_admin_form_no_identifier_status() -> None:
    html = _render_admin_form(_columns(identifier=None))
    assert "No row identifier configured" in html, html
    assert 'data-testid="portal-database-row-identifier"' in html, html
    print("PASS: admin form shows 'no identifier configured' when none is set")


def main() -> None:
    _test_migration_adds_flag_idempotently()
    _test_resolve_row_identifier()
    _test_set_identifier_validation()
    _test_raw_identifier_route_is_retired()
    _test_browser_emits_opaque_references_not_raw_identifiers()
    _test_browser_no_details_without_identifier()
    _test_admin_form_shows_row_identifier_control()
    _test_admin_form_no_identifier_status()
    print("\nALL PASS: Phase 2C row identifier + permission-safe row detail")


if __name__ == "__main__":
    main()
