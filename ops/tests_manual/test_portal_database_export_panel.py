#!/usr/bin/env python3
"""Database Explorer export panel and background export states (stage S8).

Three contracts are under test.

**The export contract.** Three row scopes (`Bieżący widok`, `Cały zbiór danych`,
`Zaznaczone wiersze`) and two column scopes (`Jak na ekranie`,
`Wszystkie zatwierdzone`), each resolved server-side from state the server
already validated. The browser names a scope; it never supplies a row set, a
column list or a count the server is bound by. The existing 20 000-row direct
cap, the 1 000 000-row hard ceiling and the 3-day retention are unchanged.

**The selected-row security model.** An S7 rectangle is page-local, so a
selection is bounded by the page size and therefore always inside the direct
cap. Its rows travel as the opaque AES-GCM references S6 already renders and are
re-resolved server-side; a malformed, tampered or foreign-dataset reference
takes the whole request down, and a dataset with no configured identity cannot
use the scope at all — there is no positional fallback.

**The background lifecycle.** `Anuluj` and `Zleć ponownie` over the existing
worker contract. Cancellation is race-safe because every worker transition is
already conditional on the status it expects, so a job moved to the terminal
`cancelled` status can neither be claimed nor published by a stale worker, and
its partially uploaded attempt object is marked for the cleanup sweep that
already exists.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screens ``DB-009`` and ``DB-007``, ``PRODUCT_BEHAVIOR_CONTRACT.md`` §2.13,
``SCREEN_STATE_MATRIX.md`` ``DB-007``/``DB-009``, ``COMPONENT_CATALOG.md``,
``COPY_AND_TERMINOLOGY.md`` §5, criteria ``DB-47``–``DB-54``.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_export_panel.py
"""
from __future__ import annotations

import html as html_mod
import json
import re
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
from api.portal_ui import assets as portal_assets  # noqa: E402
from api.portal_ui import i18n  # noqa: E402
from api.row_reference import build_row_reference  # noqa: E402

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OTHER_USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_DATASET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
ROUTE = f"/user/database/datasets/{DATASET_ID}"

# Appears nowhere else in the fixture, the source or the vocabulary, so any
# occurrence in a response, a payload or a job snapshot is unmistakably a leak.
RAW_IDENTITY = "QQZX-RECORDID-NEVER-SHOWN-773311"
TEST_SECRET = "deterministic-test-key-material-for-row-references"

DIRECT_CAP = api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP
CEILING = api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP
RETENTION_DAYS = api_main.PORTAL_DATABASE_ASYNC_EXPORT_RETENTION_DAYS
MAX_SELECTED = api_main.PORTAL_DATABASE_EXPORT_MAX_SELECTED_ROWS


# ===========================================================================
# Fixtures
# ===========================================================================
class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query="", path=ROUTE):
        self.url = _FakeUrl(path, query)
        self.cookies = {}


def _user(user_id: str = USER_ID):
    return {"user_id": user_id, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "client_trips", "default_date_column": None,
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": False, "is_row_identifier": False, "display_order": 10,
    }
    column.update(extra)
    return column


def _identity_column(*, legacy_visible: bool = False):
    return _col(
        "record_id", "Record", "text",
        is_visible=legacy_visible, is_filterable=legacy_visible, is_sortable=legacy_visible,
        is_row_identifier=True, display_order=1,
    )


def _columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
    ]


def _rows(count=3):
    return [
        {
            "record_id": f"{RAW_IDENTITY}-{index}",
            "trip_start": datetime(2026, 5, 28, 7, 30, tzinfo=timezone.utc),
            "driver_name": "Kowalski" if index else "Nowak",
            "distance_km": Decimal("1284.40"),
            "is_billable": True,
        }
        for index in range(count)
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _reference(identity: str, *, dataset_id: str = DATASET_ID, client_code: str = "ACME_01",
               identifier_column: str = "record_id") -> str:
    """Mint an opaque reference exactly as the sheet does."""
    return build_row_reference(
        secret=TEST_SECRET,
        dataset_id=dataset_id,
        client_code=client_code,
        identifier_column=identifier_column,
        identity_value=identity,
    )


class _Recorder:
    """Captures what the export path asked the client database for.

    The identity filter is honoured rather than ignored, because the selected-row
    cardinality rule is precisely about which rows come back for which requested
    identities — a fake that returned everything would make the rule untestable.
    `physical` overrides the table contents so a missing or duplicated identity
    can be modelled.
    """

    def __init__(self, rows, *, physical=None, identity_column="record_id"):
        self.rows = rows
        self.physical = rows if physical is None else physical
        self.identity_column = identity_column
        self.queries: list[tuple[str, list]] = []


class _FakeClientCursor:
    def __init__(self, recorder):
        self.recorder = recorder
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        self.recorder.queries.append((normalized, list(params or [])))
        if "= ANY(%s)" in normalized and params:
            wanted = [str(value) for value in (params[0] or [])]
            limit = int(params[1]) if len(params) > 1 else None
            matched = [
                row for row in self.recorder.physical
                if str(row.get(self.recorder.identity_column)) in wanted
            ]
            self._rows = matched[:limit] if limit else matched
            return
        self._rows = list(self.recorder.rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeClientConn:
    def __init__(self, recorder):
        self.recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, name=None):
        return _FakeClientCursor(self.recorder)

    def close(self):
        return None


def _export_patches(
    *,
    dataset=None,
    columns=None,
    rows=None,
    total=2,
    identity=True,
    legacy_visible=False,
    audit=None,
    recorder=None,
    captured=None,
    queued=None,
):
    dataset = _dataset() if dataset is None else dataset
    columns = _columns() if columns is None else columns
    rows = _rows() if rows is None else rows
    audit = [] if audit is None else audit
    captured = [] if captured is None else captured
    queued = [] if queued is None else queued
    identity_col = _identity_column(legacy_visible=legacy_visible) if identity else None
    state = {"sort": "trip_start", "direction": "desc", "active_filters": [], "filter_entries": []}

    def fake_count(dataset_arg, columns_arg, params_arg):
        captured.append({"kind": "count", "params": {k: list(v) for k, v in params_arg.items()}})
        return total, dict(state), None

    def fake_list(dataset_arg, columns_arg, params_arg, limit, offset, display_columns=None):
        captured.append({
            "kind": "list",
            "params": {k: list(v) for k, v in params_arg.items()},
            "limit": limit,
            "offset": offset,
            "display_columns": [str(c.get("column_name")) for c in (display_columns or [])],
        })
        return rows, dict(state), None

    return [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset if d == DATASET_ID and u == USER_ID else None)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: identity_col)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", fake_count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", fake_list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: audit.append(kwargs))),
        ("_artifact_explorer_session_secret", _patch("_artifact_explorer_session_secret", lambda: TEST_SECRET)),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: True)),
        ("_database_export_system_folder_schema_available", _patch("_database_export_system_folder_schema_available", lambda: False)),
        ("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda user, ds, snapshot, format_name: queued.append((snapshot, format_name)) or "job-1")),
        ("_count_active_database_export_jobs_for_user", _patch("_count_active_database_export_jobs_for_user", lambda uid: 0)),
        ("_portal_dataset_client_database_name", _patch("_portal_dataset_client_database_name", lambda ds: "client_acme_01")),
        ("_connect_portal_client_database", _patch(
            "_connect_portal_client_database",
            lambda name, statement_timeout=None, autocommit=None: _FakeClientConn(recorder or _Recorder(rows)),
        )),
    ]


def _enqueue(query: str = "", *, format_name: str = "csv", row_scope=None, column_scope=None,
             row_references=None, user_id: str = USER_ID, **patch_kwargs):
    """Drive the canonical submit path with the panel's posted fields."""
    audit = patch_kwargs.setdefault("audit", [])
    captured = patch_kwargs.setdefault("captured", [])
    queued = patch_kwargs.setdefault("queued", [])
    recorder = patch_kwargs.get("recorder")
    patches = _export_patches(**patch_kwargs)
    try:
        response = api_main._portal_database_enqueue_export_response(
            _user(user_id),
            DATASET_ID,
            _FakeRequest(query=query),
            format_name=format_name,
            row_scope=row_scope,
            column_scope=column_scope,
            row_references=row_references,
        )
    finally:
        _restore(patches)
    return response, {"audit": audit, "captured": captured, "queued": queued, "recorder": recorder}


def _render(*, query="", identity=True, legacy_visible=False, dataset=None, columns=None, rows=None, total=None, dataset_total_available=True):
    """Render the row browser, whose export panel is the surface under test."""
    dataset = _dataset() if dataset is None else dataset
    columns = _columns() if columns is None else columns
    rows = _rows() if rows is None else rows
    identity_col = _identity_column(legacy_visible=legacy_visible) if identity else None
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    sortable = {str(c.get("column_name")) for c in columns if c.get("is_sortable")}
    requested_sort = api_main._portal_database_first_param(parsed, "sort", "")
    # `query_filtered` is derived from the state the query builder reports, so a
    # fixture that claims no active filters would render the unfiltered counter
    # and never exercise the two-count path.
    active = [
        {"column_name": name, "operator": "contains", "value": "Kowal"}
        for name in {key.split("__", 1)[1] for key in parsed if key.startswith("filter__")}
    ]
    state = {
        "sort": requested_sort if requested_sort in sortable else "trip_start",
        "direction": "desc",
        "active_filters": active,
        "filter_entries": active,
    }
    row_total = len(rows) if total is None else total
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: identity_col)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (row_total, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda d, c, p, limit=0, offset=0, display_columns=None: (rows, state, None))),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", (lambda d, c: (48213, None)) if dataset_total_available else (lambda d, c: (0, "unavailable")))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
        ("_artifact_explorer_session_secret", _patch("_artifact_explorer_session_secret", lambda: TEST_SECRET)),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: True)),
        ("_count_active_database_export_jobs_for_user", _patch("_count_active_database_export_jobs_for_user", lambda uid: 0)),
    ]
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)
    return response.body.decode("utf-8")


def _panel(html: str) -> str:
    return html[html.index('data-db-export-panel'):html.index("</details>", html.index('data-db-export-panel'))]


# ===========================================================================
# 1. Three row scopes (DB-47)
# ===========================================================================
def test_panel_offers_exactly_the_three_approved_scopes_with_counts() -> None:
    html = _render(query="op__driver_name=contains&filter__driver_name=Kowal")
    panel = _panel(html)
    for label in ("Bieżący widok", "Cały zbiór danych", "Zaznaczone wiersze"):
        assert label in panel, label
    for scope in ("view", "dataset", "selection"):
        assert f'name="row_scope" value="{scope}"' in panel, scope
    # Each option carries its own count, and they are different numbers.
    assert 'data-db-export-count="view"' in panel
    assert 'data-db-export-count="dataset"' in panel
    assert 'data-db-export-count="selection"' in panel
    assert "48 213" in panel.replace(" ", " ").replace(" ", " "), "the dataset total is not stated"
    print("PASS: the panel offers the three approved scopes, each with its own count (DB-47)")


def test_current_view_exports_the_result_set_not_the_page() -> None:
    query = "op__driver_name=contains&filter__driver_name=Kowal&search=abc&sort=driver_name&direction=asc&page=3&limit=25"
    _response, seen = _enqueue(query, row_scope="view", total=10)
    listed = [c for c in seen["captured"] if c["kind"] == "list"]
    assert listed, seen["captured"]
    params = listed[-1]["params"]
    # Filters, search and sort ride along.
    assert params.get("filter__driver_name") == ["Kowal"], params
    assert params.get("op__driver_name") == ["contains"], params
    assert params.get("search") == ["abc"], params
    assert params.get("sort") == ["driver_name"], params
    # Pagination does not: an export is of the result, not of the page.
    assert "page" not in params and "limit" not in params, params
    assert listed[-1]["offset"] == 0, listed[-1]
    assert listed[-1]["limit"] == DIRECT_CAP, listed[-1]
    print("PASS: `Bieżący widok` exports the whole validated result set, unconstrained by the page")


def test_whole_dataset_ignores_filters_and_search_but_keeps_authorization() -> None:
    query = "op__driver_name=contains&filter__driver_name=Kowal&search=abc&sort=driver_name&direction=asc"
    _response, seen = _enqueue(query, row_scope="dataset", total=10)
    listed = [c for c in seen["captured"] if c["kind"] == "list"]
    params = listed[-1]["params"]
    assert "filter__driver_name" not in params and "op__driver_name" not in params, params
    assert "search" not in params, params
    # Ordering is a separate dimension from narrowing, so the sort survives.
    assert params.get("sort") == ["driver_name"] and params.get("direction") == ["asc"], params

    # Authorization is untouched by the scope: no dataset grant, no export.
    denied, _ = _enqueue(query, row_scope="dataset", dataset=_dataset(can_export_rows=False))
    assert denied.status_code == 403, denied.status_code
    print("PASS: `Cały zbiór danych` drops filters and search and keeps every authorization check")


# ===========================================================================
# 2. Selected rows (DB-47, S6/S7 boundary)
# ===========================================================================
def test_selected_rows_resolve_through_opaque_references_and_deduplicate() -> None:
    identities = [f"{RAW_IDENTITY}-{i}" for i in range(3)]
    recorder = _Recorder(_rows(3))
    # The same row twice — a rectangle covering two of its cells is still one
    # exported row.
    references = [_reference(identities[0]), _reference(identities[0]), _reference(identities[1])]
    response, seen = _enqueue(
        "sort=driver_name&direction=asc",
        row_scope="selection",
        row_references=references,
        recorder=recorder,
        format_name="csv",
    )
    assert response.status_code == 200, response.status_code
    assert seen["queued"] == [], "a selected-row export must never create a background job"
    query, params = recorder.queries[-1]
    assert "= ANY(%s)" in query, query
    # The identity list is one bound array parameter; nothing is interpolated.
    assert params[0] == [identities[0], identities[1]], params
    # One above the bound, so an identity matching more rows than it should is
    # observable instead of being truncated into looking correct.
    assert params[1] == MAX_SELECTED + 1, params
    assert RAW_IDENTITY not in query, query
    assert "ORDER BY" in query and '"driver_name" ASC' in query, query
    print("PASS: selected rows resolve through opaque references, deduplicate, and bind identities as parameters")


def test_malformed_tampered_and_foreign_references_are_refused_identically() -> None:
    good = _reference(f"{RAW_IDENTITY}-0")
    foreign = build_row_reference(
        secret=TEST_SECRET, dataset_id=OTHER_DATASET_ID, client_code="ACME_01",
        identifier_column="record_id", identity_value=f"{RAW_IDENTITY}-0",
    )
    tampered = good[:-4] + ("aaaa" if good[-4:] != "aaaa" else "bbbb")
    for label, refs in (
        ("malformed", ["not-a-reference"]),
        ("tampered", [tampered]),
        ("foreign dataset", [foreign]),
        ("raw identity", [f"{RAW_IDENTITY}-0"]),
        ("mixed good and bad", [good, "not-a-reference"]),
    ):
        response, seen = _enqueue("", row_scope="selection", row_references=refs)
        html = response.body.decode("utf-8")
        assert response.status_code == 400, (label, response.status_code)
        assert api_main._tr("db.export.selection.invalid") in html, label
        assert RAW_IDENTITY not in html, label
        assert seen["queued"] == [], label
        reasons = [e.get("metadata_json", {}).get("reason") for e in seen["audit"]]
        assert "selection_invalid" in reasons, (label, reasons)
    print("PASS: malformed, tampered, foreign and raw-identity references are all refused identically")


def _selection_case(*, physical, requested_indexes, format_name="csv"):
    """Drive a selected-row export against a chosen set of physical rows."""
    recorder = _Recorder(_rows(3), physical=physical)
    references = [_reference(f"{RAW_IDENTITY}-{i}") for i in requested_indexes]
    return _enqueue(
        "", row_scope="selection", row_references=references,
        recorder=recorder, format_name=format_name,
    ) + (recorder,)


def test_selected_rows_export_only_when_every_identity_matches_exactly_one_row() -> None:
    """All or nothing — the same rule the S6 single-row lookup already applies."""
    exact = _rows(3)
    response, seen, recorder = _selection_case(physical=exact, requested_indexes=[0, 1, 2])
    assert response.status_code == 200, response.status_code
    assert seen["queued"] == []
    body = response.body if isinstance(response.body, bytes) else b"".join(response.body)
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    # Three data rows plus the header, and no identity anywhere in the file.
    assert len([line for line in text.splitlines() if line.strip()]) == 4, text
    assert RAW_IDENTITY not in text, "the technical identity reached the exported file"
    assert "record_id" not in text, text
    print("PASS: a selection whose identities each match exactly one row exports")


def test_a_missing_selected_identity_refuses_the_whole_export() -> None:
    # The middle row was deleted between selecting it and submitting.
    physical = [row for row in _rows(3) if row["record_id"] != f"{RAW_IDENTITY}-1"]
    response, seen, _recorder = _selection_case(physical=physical, requested_indexes=[0, 1, 2])
    assert response.status_code == 400, response.status_code
    html = response.body.decode("utf-8")
    assert api_main._tr("db.export.selection.invalid") in html, html
    # Nothing is disclosed about which identity failed, or that one was deleted.
    assert RAW_IDENTITY not in html and "record_id" not in html, html
    assert seen["queued"] == []
    print("PASS: a missing selected identity refuses the whole export rather than exporting a subset")


def test_a_duplicated_selected_identity_refuses_the_whole_export() -> None:
    # A non-unique identifier: one requested identity matches two physical rows.
    physical = _rows(3) + [dict(_rows(1)[0], record_id=f"{RAW_IDENTITY}-2", driver_name="Duplicate")]
    response, seen, _recorder = _selection_case(physical=physical, requested_indexes=[0, 1, 2])
    assert response.status_code == 400, response.status_code
    assert api_main._tr("db.export.selection.invalid") in response.body.decode("utf-8")
    assert seen["queued"] == []
    print("PASS: a duplicated selected identity refuses the whole export rather than picking one")


def test_a_missing_and_duplicated_identity_with_the_same_row_total_is_still_caught() -> None:
    """The case a row-count check cannot see.

    Three identities are requested. One is missing and another matches twice, so
    the query returns exactly three rows — the number a naive
    `len(rows) == len(requested)` check would accept — while the selection the
    user made is not what would be exported.
    """
    physical = [
        _rows(3)[0],
        _rows(3)[2],
        dict(_rows(3)[2], driver_name="Duplicate"),  # a second row for identity 2
    ]
    assert len(physical) == 3
    response, seen, recorder = _selection_case(physical=physical, requested_indexes=[0, 1, 2])
    assert response.status_code == 400, "a row-count check accepted a wrong identity multiplicity"
    assert api_main._tr("db.export.selection.invalid") in response.body.decode("utf-8")
    assert seen["queued"] == []
    # The query really did return three rows, so only identity comparison can
    # have caught this.
    query, params = recorder.queries[-1]
    assert "= ANY(%s)" in query and len(params[0]) == 3, params
    print("PASS: a missing+duplicate combination with the correct row total is still refused")


def test_selected_row_validation_applies_to_both_formats() -> None:
    physical = [row for row in _rows(3) if row["record_id"] != f"{RAW_IDENTITY}-1"]
    for fmt, media_type in (
        ("csv", "text/csv; charset=utf-8"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        refused, _seen, _recorder = _selection_case(physical=physical, requested_indexes=[0, 1, 2], format_name=fmt)
        assert refused.status_code == 400, (fmt, refused.status_code)
        assert api_main._tr("db.export.selection.invalid") in refused.body.decode("utf-8"), fmt

        ok, _seen_ok, _rec_ok = _selection_case(physical=_rows(3), requested_indexes=[0, 1, 2], format_name=fmt)
        assert ok.status_code == 200, (fmt, ok.status_code)
        assert ok.media_type == media_type, (fmt, ok.media_type)
        body = ok.body if isinstance(ok.body, bytes) else b"".join(ok.body)
        assert RAW_IDENTITY.encode() not in body, fmt
    print("PASS: cardinality validation runs before rendering, so CSV and XLSX behave identically")


def test_the_identity_stays_internal_and_parameterized_during_validation() -> None:
    _response, _seen, recorder = _selection_case(physical=_rows(3), requested_indexes=[0, 1])
    query, params = recorder.queries[-1]
    # The identity is selected so multiplicity can be counted...
    assert '"record_id"' in query, query
    # ...but the values are still bound, never interpolated.
    assert RAW_IDENTITY not in query, query
    assert params[0] == [f"{RAW_IDENTITY}-0", f"{RAW_IDENTITY}-1"], params

    # And the rows the fetch hands on carry no identity key at all.
    direct_recorder = _Recorder(_rows(3), physical=_rows(3))
    patches = _export_patches(recorder=direct_recorder)
    try:
        rows, _state, error = api_main._portal_database_fetch_rows_by_identities(
            _dataset(), _columns(), "record_id",
            [f"{RAW_IDENTITY}-0", f"{RAW_IDENTITY}-1"], {},
        )
    finally:
        _restore(patches)
    assert error is None, error
    assert len(rows) == 2, rows
    for row in rows:
        assert "record_id" not in row, row
        assert set(row) <= {"trip_start", "driver_name", "distance_km", "is_billable"}, row
    print("PASS: the identity is bound, used only for validation, and stripped from the returned rows")


def test_a_dataset_without_row_identity_cannot_use_the_selected_scope() -> None:
    response, seen = _enqueue(
        "", row_scope="selection", row_references=[_reference(f"{RAW_IDENTITY}-0")], identity=False
    )
    html = response.body.decode("utf-8")
    assert response.status_code == 400, response.status_code
    assert api_main._tr("db.export.selection.unavailable") in html, html
    reasons = [e.get("metadata_json", {}).get("reason") for e in seen["audit"]]
    assert "selection_identity_unavailable" in reasons, reasons
    # No positional fallback of any kind reached the client database.
    assert not [c for c in seen["captured"] if c["kind"] == "list"], seen["captured"]

    # And the panel itself states the reason rather than offering a dead option.
    panel = _panel(_render(identity=False))
    assert "data-db-export-selection disabled" in panel, panel
    assert api_main._tr("db.export.selection.unavailable") in panel, panel
    print("PASS: without a configured row identity the selected scope is unavailable, with no positional fallback")


def test_the_selected_scope_is_bounded_by_the_page_size() -> None:
    # The S7 rectangle cannot cross a pagination boundary, so a selection can
    # never exceed one page — which is what keeps it structurally below the
    # direct cap and therefore always a direct download.
    assert MAX_SELECTED == api_main.PORTAL_DATABASE_MAX_PAGE_SIZE
    assert MAX_SELECTED <= DIRECT_CAP, (MAX_SELECTED, DIRECT_CAP)
    too_many = [_reference(f"{RAW_IDENTITY}-{i}") for i in range(3)] * (MAX_SELECTED // 3 + 1)
    response, seen = _enqueue("", row_scope="selection", row_references=too_many)
    assert response.status_code == 400, response.status_code
    reasons = [e.get("metadata_json", {}).get("reason") for e in seen["audit"]]
    assert "selection_too_many" in reasons, reasons

    empty, seen_empty = _enqueue("", row_scope="selection", row_references=[])
    assert empty.status_code == 400, empty.status_code
    assert api_main._tr("db.export.selection.empty") in empty.body.decode("utf-8")
    assert seen_empty["queued"] == []
    print("PASS: the selected scope is bounded by the page size and is therefore always a direct export")


# ===========================================================================
# 3. Column scopes
# ===========================================================================
def test_column_scopes_are_the_two_approved_choices_with_counts() -> None:
    panel = _panel(_render(query="cols=driver_name,distance_km"))
    assert "Jak na ekranie" in panel and "Wszystkie zatwierdzone" in panel, panel
    assert 'name="column_scope" value="screen"' in panel
    assert 'name="column_scope" value="approved"' in panel
    # `Jak na ekranie` counts the displayed set, `Wszystkie zatwierdzone` the
    # approved catalogue, and the two differ once columns are hidden.
    counts = re.findall(r'name="column_scope" value="(\w+)"[^>]*>.*?<span class="db-export-count">([^<]+)</span>', panel)
    assert dict(counts) == {"screen": "2", "approved": "4"}, counts
    print("PASS: the two approved column scopes are offered, each with its own count")


def test_screen_scope_uses_the_displayed_columns_in_display_order() -> None:
    query = "cols=trip_start,driver_name,distance_km&colorder=distance_km,driver_name,trip_start&colpin="
    _response, seen = _enqueue(query, row_scope="view", column_scope="screen", total=5)
    listed = [c for c in seen["captured"] if c["kind"] == "list"][-1]
    assert listed["display_columns"] == ["distance_km", "driver_name", "trip_start"], listed
    assert "is_billable" not in listed["display_columns"], listed
    print("PASS: `Jak na ekranie` exports the displayed columns in their S5 display order")


def test_approved_scope_is_the_whole_approved_catalogue() -> None:
    query = "cols=driver_name&colorder=driver_name"
    _response, seen = _enqueue(query, row_scope="view", column_scope="approved", total=5)
    listed = [c for c in seen["captured"] if c["kind"] == "list"][-1]
    assert listed["display_columns"] == ["trip_start", "driver_name", "distance_km", "is_billable"], listed
    print("PASS: `Wszystkie zatwierdzone` exports the whole approved user-visible catalogue")


def test_forged_column_state_cannot_widen_an_export() -> None:
    for label, query in (
        ("unapproved name", "cols=trip_start,secret_column&colorder=secret_column,trip_start"),
        ("row identifier", "cols=record_id&colorder=record_id"),
        ("identifier in order only", "colorder=record_id,trip_start"),
        ("empty selection", "cols="),
    ):
        _response, seen = _enqueue(query, row_scope="view", column_scope="screen", total=5, legacy_visible=True)
        listed = [c for c in seen["captured"] if c["kind"] == "list"][-1]
        names = listed["display_columns"]
        assert "record_id" not in names, (label, names)
        assert "secret_column" not in names, (label, names)
        assert set(names) <= {"trip_start", "driver_name", "distance_km", "is_billable"}, (label, names)
        assert names, (label, "an export must still have columns")
    print("PASS: forged `cols`/`colorder` can narrow or rearrange approved columns, never widen them")


def test_the_hidden_row_identity_never_reaches_an_export() -> None:
    for legacy_visible in (False, True):
        for column_scope in ("screen", "approved"):
            _response, seen = _enqueue(
                "", row_scope="view", column_scope=column_scope, total=5, legacy_visible=legacy_visible
            )
            listed = [c for c in seen["captured"] if c["kind"] == "list"][-1]
            assert "record_id" not in listed["display_columns"], (legacy_visible, column_scope)
        # And in the background path the stored snapshot carries no identity.
        _response, seen = _enqueue(
            "", row_scope="view", column_scope="approved", total=DIRECT_CAP + 1, legacy_visible=legacy_visible
        )
        snapshot = seen["queued"][0][0]
        assert "record_id" not in json.dumps(snapshot), snapshot
        assert RAW_IDENTITY not in json.dumps(snapshot), snapshot
    print("PASS: the technical row identity is absent from every export column set and every job snapshot")


# ===========================================================================
# 4. Path routing and thresholds (DB-48)
# ===========================================================================
def test_execution_path_boundaries_are_unchanged() -> None:
    assert DIRECT_CAP == 20_000, DIRECT_CAP
    assert CEILING == 1_000_000, CEILING
    assert RETENTION_DAYS == 3, RETENTION_DAYS
    for total, expected in (
        (0, "direct"), (1, "direct"),
        (DIRECT_CAP, "direct"), (DIRECT_CAP + 1, "background"),
        (CEILING, "background"), (CEILING + 1, "refused"),
        (None, "unknown"),
    ):
        assert api_main._portal_database_export_path_for(total) == expected, (total, expected)
    print("PASS: the direct cap, the hard ceiling and the retention window are unchanged")


def test_the_submit_handler_routes_authoritatively_at_every_boundary() -> None:
    # Direct at and below the cap: a streamed file, no job.
    for total in (0, 1, DIRECT_CAP):
        response, seen = _enqueue("", row_scope="view", total=total)
        assert response.status_code == 200, (total, response.status_code)
        assert seen["queued"] == [], (total, seen["queued"])

    # Background above the cap and up to the ceiling: a job, no file.
    for total in (DIRECT_CAP + 1, CEILING):
        response, seen = _enqueue("", row_scope="view", total=total)
        assert response.status_code == 303, (total, response.status_code)
        assert len(seen["queued"]) == 1, (total, seen["queued"])
        assert "export_job_queued=" in response.headers.get("Location", ""), response.headers

    # Above the ceiling: no file, no job, and a message that names the limit.
    response, seen = _enqueue("", row_scope="view", total=CEILING + 1)
    assert response.status_code == 400, response.status_code
    assert seen["queued"] == [], seen["queued"]
    body = response.body.decode("utf-8")
    assert f"{CEILING:,}" in body, body
    reasons = [e.get("metadata_json", {}).get("reason") for e in seen["audit"]]
    assert "export_global_limit_exceeded" in reasons, reasons
    print("PASS: 0 / 1 / cap / cap+1 / ceiling / ceiling+1 all route authoritatively on the server")


def test_the_panel_states_the_path_before_the_user_commits() -> None:
    direct = _panel(_render(total=100))
    assert api_main._tr("db.export.path.direct") in direct, direct
    assert "Pobierz XLSX" in direct, direct

    background = _panel(_render(total=DIRECT_CAP + 1))
    assert api_main._tr("db.export.path.background", days=RETENTION_DAYS) in background, background
    assert "Przygotuj w tle" in background, background
    assert str(RETENTION_DAYS) in background, "the retention window is not repeated"

    over = _panel(_render(total=CEILING + 1))
    assert "1 000 000" in over.replace(" ", " ").replace(" ", " "), over
    # The threshold is stated on every render, so the rule is visible before the
    # user is anywhere near it.
    assert "20 000" in direct.replace(" ", " ").replace(" ", " "), direct
    print("PASS: the panel states the path, the retention and the ceiling before commit (DB-48)")


def test_an_unavailable_total_stays_unavailable() -> None:
    """The S2 defect must not reappear: an unknown total is not the filtered count."""
    html = _render(query="op__driver_name=contains&filter__driver_name=Kowal", dataset_total_available=False)
    panel = _panel(html)
    assert 'class="db-export-count is-unknown"' in panel, panel
    assert api_main._tr("db.export.count_unavailable") in panel, panel
    print("PASS: an unavailable dataset total renders an explicit unknown, never the filtered count")


# ===========================================================================
# 5. Authorization (DB-54)
# ===========================================================================
def test_without_the_grant_the_export_action_is_absent_and_forged_posts_fail() -> None:
    html = _render(dataset=_dataset(can_export_rows=False))
    assert "db-export-form" not in html, html
    assert 'name="row_scope"' not in html, html
    assert "Zaznaczone wiersze" not in html, html
    assert "data-db-export-panel" not in html, html

    for scope in ("view", "dataset", "selection"):
        response, seen = _enqueue(
            "", row_scope=scope, row_references=[_reference(f"{RAW_IDENTITY}-0")],
            dataset=_dataset(can_export_rows=False),
        )
        assert response.status_code == 403, (scope, response.status_code)
        assert seen["queued"] == [], scope
        assert not [c for c in seen["captured"] if c["kind"] == "list"], scope
    print("PASS: without `can_export_rows` the action is absent and every forged scope is refused server-side")


def test_an_unavailable_dataset_grant_denies_every_scope() -> None:
    for scope in ("view", "dataset", "selection"):
        response, seen = _enqueue("", row_scope=scope, user_id=OTHER_USER_ID)
        # The dataset resolver returns None for a user without the grant, so the
        # response is the generic unavailable state — never a partial export.
        assert response.status_code in (403, 404), (scope, response.status_code)
        assert seen["queued"] == [], scope
        assert not [c for c in seen["captured"] if c["kind"] == "list"], scope
    print("PASS: a user without the dataset grant reaches no export scope")


# ===========================================================================
# 6. Background job snapshot
# ===========================================================================
def test_the_job_snapshot_stores_canonical_validated_state_only() -> None:
    query = ("op__driver_name=contains&filter__driver_name=Kowal&search=abc"
             "&sort=driver_name&direction=asc&cols=driver_name,distance_km"
             "&colorder=distance_km,driver_name&colpin=&page=7&limit=200")
    _response, seen = _enqueue(query, row_scope="view", column_scope="screen", total=DIRECT_CAP + 1)
    snapshot = seen["queued"][0][0]
    assert snapshot["columns"] == ["distance_km", "driver_name"], snapshot
    assert snapshot["sort"] == "driver_name" and snapshot["direction"] == "asc", snapshot
    assert snapshot["search"] == "abc", snapshot
    assert [f["column_name"] for f in snapshot["filters"]] == ["driver_name"], snapshot
    assert snapshot["row_scope"] == "view" and snapshot["column_scope"] == "screen", snapshot
    assert snapshot["expected_rows"] == DIRECT_CAP + 1, snapshot
    # Canonical state only: no raw query string, no pagination, no identity.
    blob = json.dumps(snapshot)
    assert "record_id" not in blob, blob
    assert "page" not in snapshot and "limit" not in snapshot, snapshot
    print("PASS: the queued snapshot stores canonical validated state — scope, columns, filters, sort — and nothing else")


def test_an_unapproved_filter_column_refuses_the_export_rather_than_widening_it() -> None:
    """`filter__record_id` must not be silently dropped into an unfiltered export."""
    for query in ("filter__record_id=x&op__record_id=eq", "filter__secret=1&op__secret=eq"):
        params = api_main._portal_database_query_params(_FakeRequest(query=query))
        # The direct path runs the shared query builder, which refuses the whole
        # request rather than dropping the condition and returning more rows.
        _q, _v, _state, error = api_main._build_portal_database_rows_query(
            _dataset(), _columns(), params, count=True
        )
        assert error, (query, "the query builder accepted an unapproved filter column")

        # The background path refuses at the snapshot, so no job is created.
        response, seen = _enqueue(query, row_scope="view", total=DIRECT_CAP + 1, legacy_visible=True)
        assert response.status_code == 400, (query, response.status_code)
        assert seen["queued"] == [], query
    print("PASS: an unapproved filter column refuses the export instead of silently widening it")


def test_the_dataset_scope_snapshot_carries_no_filters() -> None:
    query = "op__driver_name=contains&filter__driver_name=Kowal&search=abc"
    _response, seen = _enqueue(query, row_scope="dataset", total=DIRECT_CAP + 1)
    snapshot = seen["queued"][0][0]
    assert snapshot["filters"] == [], snapshot
    assert "search" not in snapshot, snapshot
    assert snapshot["row_scope"] == "dataset", snapshot
    print("PASS: a `Cały zbiór danych` job snapshot carries no filters and no search")


# ===========================================================================
# 7. Background states (DB-007, DB-50..DB-52)
# ===========================================================================
def _job(state: str, **overrides):
    now = api_main.utcnow()
    base = {
        "job_id": "11111111-1111-1111-1111-111111111111",
        "dataset_name": "Approved trips", "client_code": "ACME_01",
        "requested_format": "csv", "row_count": 10, "expected_rows": 40,
        "queued_at": now.isoformat(), "artifact_id": None,
        "expires_at": None, "artifact_expires_at": None, "artifact_expired_at": None,
        "safe_error_code": None, "safe_error_message": None,
    }
    if state == "running":
        base["status"] = "running"
    elif state == "queued":
        base["status"] = "queued"
    elif state == "ready":
        future = (now + timedelta(days=2)).isoformat()
        base.update({"status": "completed", "artifact_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                     "expires_at": future, "artifact_expires_at": future})
    elif state == "expired":
        past = (now - timedelta(days=1)).isoformat()
        base.update({"status": "expired", "artifact_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                     "expires_at": past, "artifact_expires_at": past})
    elif state == "ready_past_expiry":
        past = (now - timedelta(days=1)).isoformat()
        base.update({"status": "completed", "artifact_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                     "expires_at": past, "artifact_expires_at": past})
    elif state == "failed":
        base.update({"status": "failed", "safe_error_code": "QUERY_FAILED",
                     "safe_error_message": api_main.DATABASE_EXPORT_GENERIC_FAILURE_MESSAGE})
    elif state == "cancelled":
        base["status"] = "cancelled"
    base.update(overrides)
    return base


def test_the_four_states_each_present_a_different_action_set() -> None:
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    expectations = {
        "running": {"badge": "W toku", "has": ["/cancel"], "hasnt": ["/download", "/requeue"]},
        "ready": {"badge": "Gotowy", "has": ["/download"], "hasnt": ["/cancel", "/requeue"]},
        "expired": {"badge": "Pliki wygasły", "has": ["/requeue"], "hasnt": ["/download", "/cancel"]},
        "failed": {"badge": "Błąd", "has": ["/requeue", "data-db-export-copy"], "hasnt": ["/download", "/cancel"]},
    }
    for state, expect in expectations.items():
        html = api_main._database_export_state_list(folder, [_job(state)])
        assert expect["badge"] in html, (state, html)
        for token in expect["has"]:
            assert token in html, (state, token, html)
        for token in expect["hasnt"]:
            assert token not in html, (state, token, html)
        # `DB-50`: no state renders a greyed-out control.
        assert "disabled" not in html, (state, html)
    print("PASS: each of the four approved states presents its own action set, none disabled (DB-50)")


def test_queued_and_running_are_one_presented_state() -> None:
    for status in ("queued", "running"):
        assert api_main._database_export_presentation_state(_job(status)) == "running", status
    print("PASS: `queued` and `running` both present as `W toku` — the difference is worker scheduling")


def test_an_expired_export_offers_no_download_at_all() -> None:
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    for state in ("expired", "ready_past_expiry"):
        html = api_main._database_export_state_list(folder, [_job(state)])
        assert "Pliki wygasły" in html, (state, html)
        assert "/download" not in html, (state, html)
        assert "Pobierz" not in html, (state, html)
        assert "Zleć ponownie" in html, (state, html)
        assert api_main._tr("db.exports.expired_note", days=RETENTION_DAYS) in html, state
        # A completed job past retention is presented as expired: the file is
        # what `Gotowy` promises and it is gone.
        assert api_main._database_export_presentation_state(_job(state)) == "expired", state
    print("PASS: an expired export offers `Zleć ponownie` and no download action at all (DB-51)")


def test_a_failed_export_exposes_a_sanitized_message_and_a_copyable_reference() -> None:
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    job = _job("failed", safe_error_message="Detailed internal failure text")
    html = api_main._database_export_state_list(folder, [job])
    assert "Błąd" in html, html
    assert "Kopiuj ref" in html and f'data-db-export-copy="{job["job_id"]}"' in html, html
    # Only the job reference is copyable. Nothing else internal is in the page.
    for forbidden in ("storage_key", "claim_token", "lease_expires", "attempt_object_key",
                      "database_explorer/exports", "MINIO", "SELECT ", "Traceback", "psycopg",
                      "Detailed internal failure text"):
        assert forbidden not in html, forbidden
    print("PASS: a failed export exposes a sanitized message and a copyable reference only (DB-52)")


def test_a_cancelled_export_is_not_listed_and_exposes_no_download() -> None:
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    html = api_main._database_export_state_list(folder, [_job("cancelled", artifact_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")])
    # No fifth badge is invented, and no stale download survives.
    assert "/download" not in html and "Pobierz" not in html, html
    for badge in ("W toku", "Gotowy", "Pliki wygasły", "Błąd"):
        assert badge not in html, badge
    assert api_main._tr("db.exports.empty.title") in html, html
    print("PASS: a cancelled export leaves the list without inventing a state and exposes no download")


def test_the_progress_bar_is_determinate_only_when_a_denominator_exists() -> None:
    determinate = api_main._database_export_progress_html(_job("running", row_count=10, expected_rows=40))
    assert 'aria-valuenow="25"' in determinate, determinate
    assert "role=\"progressbar\"" in determinate
    indeterminate = api_main._database_export_progress_html(_job("running", row_count=10, expected_rows=None))
    assert "is-indeterminate" in indeterminate, indeterminate
    assert "aria-valuenow" not in indeterminate, indeterminate
    print("PASS: progress is determinate where the job recorded an expectation and honest where it did not")


def test_the_empty_state_explains_the_threshold() -> None:
    html = api_main._database_export_state_list(None, [])
    assert api_main._tr("db.exports.empty.title") in html, html
    assert "20 000" in html.replace(" ", " ").replace(" ", " "), html
    print("PASS: the empty background-export state explains the 20 000-row threshold")


# ===========================================================================
# 8. Cancellation and requeue — race safety
# ===========================================================================
class _JobStore:
    """A minimal database that enforces the same conditional predicates as the SQL.

    Only the statements the cancellation races touch are modelled, and each one
    applies the real WHERE clause rather than a simplification — a fake that
    ignored the fences would prove nothing about the races these tests exist for.
    """

    def __init__(self, job: dict, attempts: list[dict] | None = None):
        self.job = job
        self.attempts = attempts if attempts is not None else []
        self.log: list[str] = []

    # -- conditional transitions ------------------------------------------
    def claim(self) -> bool:
        self.log.append("claim")
        if self.job.get("status") != "queued":
            return False
        self.job.update({"status": "running", "claim_token": "claim-1",
                         "attempt_object_key": "attempts/key-1", "lease_valid": True})
        self.attempts.append({"job_id": self.job["job_id"], "claim_token": "claim-1",
                              "object_key": "attempts/key-1", "state": "active"})
        return True

    def refresh_lease(self, token: str) -> bool:
        self.log.append("refresh")
        return self.job.get("status") == "running" and self.job.get("claim_token") == token

    def mark_failed(self, token: str) -> bool:
        self.log.append("mark_failed")
        if self.job.get("status") == "running" and self.job.get("claim_token") == token:
            self.job.update({"status": "failed", "claim_token": None})
            return True
        return False

    def publish(self, token: str, key: str) -> bool:
        self.log.append("publish")
        if (
            self.job.get("status") == "running"
            and self.job.get("claim_token") == token
            and self.job.get("lease_valid", True)
            and self.job.get("attempt_object_key") == key
        ):
            self.job.update({"status": "completed", "claim_token": None,
                             "attempt_object_key": None, "artifact_id": "artifact-1"})
            for attempt in self.attempts:
                if attempt["claim_token"] == token:
                    attempt["state"] = "published"
            return True
        return False

    def recover_stale(self, token: str) -> bool:
        self.log.append("recover")
        return (
            self.job.get("status") == "running"
            and self.job.get("claim_token") == token
            and not self.job.get("lease_valid", True)
        )

    # -- the API's cancel, with the real predicate -------------------------
    def cancel(self, user_id: str) -> str:
        self.log.append("cancel")
        if self.job.get("requested_by_user_id") != user_id:
            return "not_found"
        previous = self.job.get("status")
        if previous not in api_main.DATABASE_EXPORT_CANCELLABLE_STATUSES:
            return "not_cancellable"
        token = self.job.get("claim_token")
        self.job.update({"status": "cancelled", "claim_token": None, "attempt_object_key": None})
        if previous == "running" and token:
            for attempt in self.attempts:
                if attempt["claim_token"] == token and attempt["state"] == "active":
                    attempt["state"] = "cleanup_pending"
        return "cancelled"


def _store(status: str = "queued") -> _JobStore:
    return _JobStore({
        "job_id": "11111111-1111-1111-1111-111111111111",
        "requested_by_user_id": USER_ID,
        "status": status,
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "lease_valid": True,
    })


def test_cancelling_a_queued_job_prevents_any_later_claim() -> None:
    store = _store("queued")
    assert store.cancel(USER_ID) == "cancelled"
    assert store.job["status"] == "cancelled"
    assert store.claim() is False, "a cancelled job was still claimable"
    assert store.job["status"] == "cancelled", store.job
    print("PASS: cancelling a queued export prevents any later worker claim")


def test_cancelling_a_running_job_defeats_a_stale_publication() -> None:
    store = _store("queued")
    assert store.claim() is True
    token = store.job["claim_token"]
    key = store.job["attempt_object_key"]

    assert store.cancel(USER_ID) == "cancelled"
    # Everything the worker could still try, and each one loses its fence.
    assert store.refresh_lease(token) is False, "a cancelled job refreshed its lease"
    assert store.publish(token, key) is False, "a stale worker published READY after cancellation"
    assert store.mark_failed(token) is False, "a stale worker overwrote the terminal state"
    assert store.job["status"] == "cancelled", store.job
    assert store.job.get("artifact_id") is None, "a cancelled job gained a downloadable artifact"
    # The partially uploaded object is queued for the cleanup sweep that already
    # exists, so it cannot survive as an unauthorized orphan.
    assert [a["state"] for a in store.attempts] == ["cleanup_pending"], store.attempts
    print("PASS: cancelling a running export defeats lease refresh, publication and late failure")


def test_a_worker_that_publishes_first_wins_and_cancellation_is_refused() -> None:
    store = _store("queued")
    store.claim()
    token, key = store.job["claim_token"], store.job["attempt_object_key"]
    assert store.publish(token, key) is True
    # The reverse race: only one terminal outcome exists, and the user is told.
    assert store.cancel(USER_ID) == "not_cancellable"
    assert store.job["status"] == "completed", store.job
    assert [a["state"] for a in store.attempts] == ["published"], store.attempts
    print("PASS: a worker that publishes first wins, and the cancel attempt is refused rather than faked")


def test_a_stale_claim_token_can_change_nothing() -> None:
    store = _store("queued")
    store.claim()
    real = store.job["claim_token"]
    stale = "claim-stale"
    assert store.refresh_lease(stale) is False
    assert store.publish(stale, "attempts/key-1") is False
    assert store.mark_failed(stale) is False
    assert store.job["status"] == "running", store.job
    assert store.job["claim_token"] == real, store.job
    print("PASS: a stale claim token cannot refresh, publish or fail the current attempt")


def test_stale_recovery_never_resurrects_a_cancelled_job() -> None:
    store = _store("queued")
    store.claim()
    token = store.job["claim_token"]
    store.job["lease_valid"] = False
    assert store.recover_stale(token) is True, "a stale running job should be recoverable"
    store.job["status"] = "running"
    store.job["claim_token"] = token
    assert store.cancel(USER_ID) == "cancelled"
    store.job["lease_valid"] = False
    assert store.recover_stale(token) is False, "stale recovery requeued a cancelled job"
    print("PASS: stale-lease recovery is conditional on `running` and never resurrects a cancelled job")


def test_only_the_owner_can_cancel() -> None:
    store = _store("queued")
    assert store.cancel(OTHER_USER_ID) == "not_found"
    assert store.job["status"] == "queued", store.job
    print("PASS: cancellation is owner-scoped and another user's attempt is indistinguishable from a miss")


def test_the_cancel_sql_is_conditional_on_ownership_and_an_active_status() -> None:
    """The shipped statement, not a paraphrase of it."""
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    body = source.split("def _cancel_database_export_job_for_user", 1)[1].split("\ndef ", 1)[0]
    assert "FOR UPDATE" in body, "the cancel path does not lock the row it decides on"
    assert "SET status = 'cancelled'" in body, body[:400]
    assert "AND requested_by_user_id = %s" in body, "cancellation is not owner-scoped in SQL"
    assert "AND status IN ('queued', 'running')" in body, "cancellation is not conditional on an active status"
    assert "state = 'cleanup_pending'" in body, "a cancelled attempt object is not queued for cleanup"
    print("PASS: the shipped cancel statement locks, is owner-scoped, and is conditional on an active status")


def test_requeue_exists_only_in_the_approved_states() -> None:
    assert set(api_main.DATABASE_EXPORT_REQUEUEABLE_STATUSES) == {"failed", "expired"}
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    for state, expected in (("running", False), ("ready", False), ("expired", True), ("failed", True)):
        html = api_main._database_export_state_list(folder, [_job(state)])
        assert ("/requeue" in html) is expected, (state, html)
    print("PASS: `Zleć ponownie` appears only on a failed or expired export")


def test_requeue_revalidates_ownership_authorization_and_the_ceiling() -> None:
    snapshot = {"snapshot_version": 1, "format": "csv", "columns": ["trip_start", "driver_name"],
                "sort": "trip_start", "direction": "desc", "filters": [],
                "row_scope": "view", "column_scope": "screen", "expected_rows": DIRECT_CAP + 1}
    job = _job("failed", dataset_id=DATASET_ID, status="failed", requested_format="csv")

    def _run(*, dataset, total, job_status="failed", owned=True):
        queued: list = []
        patches = [
            ("_get_database_export_job_for_user", _patch("_get_database_export_job_for_user", lambda j, u: dict(job, status=job_status, dataset_id=DATASET_ID) if owned else None)),
            ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
            ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: _columns())),
            ("_load_database_export_job_snapshot", _patch("_load_database_export_job_snapshot", lambda j, u: dict(snapshot))),
            ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (total, {"sort": "trip_start", "direction": "desc", "active_filters": [], "filter_entries": []}, None))),
            ("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda user, ds, snap, format_name: queued.append(snap) or "job-2")),
            ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
        ]
        try:
            return api_main._requeue_database_export_job_for_user(_user(), "job-1", None), queued
        finally:
            _restore(patches)

    # Happy path.
    (new_id, outcome), queued = _run(dataset=_dataset(), total=DIRECT_CAP + 1)
    assert outcome == "requeued" and new_id == "job-2", (outcome, new_id)
    assert len(queued) == 1 and queued[0]["columns"] == ["trip_start", "driver_name"], queued
    assert queued[0]["expected_rows"] == DIRECT_CAP + 1, queued

    # Export permission lost since the original export.
    (_, outcome), queued = _run(dataset=_dataset(can_export_rows=False), total=10)
    assert outcome == "not_authorized" and queued == [], (outcome, queued)

    # Dataset grant lost entirely.
    (_, outcome), queued = _run(dataset=None, total=10)
    assert outcome == "not_authorized" and queued == [], (outcome, queued)

    # Not the owner.
    (_, outcome), queued = _run(dataset=_dataset(), total=10, owned=False)
    assert outcome == "not_found" and queued == [], (outcome, queued)

    # Wrong state.
    for status in ("queued", "running", "completed", "cancelled"):
        (_, outcome), queued = _run(dataset=_dataset(), total=10, job_status=status)
        assert outcome == "not_requeueable" and queued == [], (status, outcome)

    # A stored snapshot cannot carry a scope past today's ceiling.
    (_, outcome), queued = _run(dataset=_dataset(), total=CEILING + 1)
    assert outcome == "over_ceiling" and queued == [], (outcome, queued)
    print("PASS: requeue revalidates ownership, dataset access, `can_export_rows`, state and the ceiling")


def test_requeue_creates_a_new_attempt_rather_than_reviving_history() -> None:
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    body = source.split("def _requeue_database_export_job_for_user", 1)[1].split("\ndef ", 1)[0]
    assert "_enqueue_database_export_job" in body, body[:400]
    # The historical row is not mutated: no UPDATE of the source job anywhere.
    assert "UPDATE database_export_jobs" not in body, "requeue rewrote the historical job row"
    print("PASS: requeue inserts a fresh job and leaves the failed/expired record as history")


# ===========================================================================
# 8b. Lifecycle audit registration — through the REAL validator
# ===========================================================================
class _AuditCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        if normalized.startswith("INSERT INTO portal_audit_events"):
            self.sink.append({"event_type": params[0], "actor_user_id": params[1],
                              "metadata_json": params[8]})

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _AuditConn:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _AuditCursor(self.sink)

    def commit(self):
        return None


def _drive_lifecycle_route(route, *, job_id, overrides):
    """Run a real S8 lifecycle route with the audit path UNSTUBBED.

    `_portal_audit_event_safe` is deliberately left in place, so the event goes
    through `_log_portal_audit_event` into `_create_portal_audit_event` and its
    event-type validation. An unregistered type raises there, is swallowed by the
    safe wrapper, and never reaches the INSERT — which is exactly the defect this
    proves is gone.
    """
    inserted: list[dict] = []
    # Applied here, not at call-site construction: `_patch` mutates the module on
    # evaluation, so building them eagerly would leave only the last one standing.
    patches = [
        ("_require_portal_user", _patch("_require_portal_user", lambda request: _user())),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: True)),
        ("db_conn", _patch("db_conn", lambda: _AuditConn(inserted))),
        ("_artifact_explorer_redirect", _patch("_artifact_explorer_redirect", lambda location: _HTMLResponse("", status_code=303, headers={"Location": location}))),
    ] + [(name, _patch(name, value)) for name, value in overrides]
    try:
        response = route(job_id, _FakeRequest())
    finally:
        _restore(patches)
    return response, inserted


def test_cancel_and_requeue_events_reach_the_real_audit_validator() -> None:
    job_id = "11111111-1111-1111-1111-111111111111"

    cases = [
        (
            "cancel success", api_main.user_portal_database_export_cancel,
            [("_cancel_database_export_job_for_user", lambda j, u: "cancelled")],
            "database_export_job_cancelled",
        ),
        (
            "cancel refused", api_main.user_portal_database_export_cancel,
            [("_cancel_database_export_job_for_user", lambda j, u: "not_cancellable")],
            "database_export_cancel_refused",
        ),
        (
            "requeue refused", api_main.user_portal_database_export_requeue,
            [("_requeue_database_export_job_for_user", lambda user, j, request: (None, "not_requeueable"))],
            "database_export_requeue_refused",
        ),
    ]
    for label, route, overrides, expected in cases:
        response, inserted = _drive_lifecycle_route(route, job_id=job_id, overrides=overrides)
        assert response.status_code == 303, (label, response.status_code)
        events = [row["event_type"] for row in inserted]
        assert events == [expected], (label, "the event never reached the audit insert", events)

    # `database_export_job_requeued` is emitted by the requeue helper itself, so
    # the helper is driven for real rather than stubbed away.
    inserted: list[dict] = []
    snapshot = {"snapshot_version": 1, "format": "csv", "columns": ["trip_start"],
                "sort": "trip_start", "direction": "desc", "filters": [],
                "row_scope": "view", "column_scope": "screen"}
    patches = [
        ("db_conn", _patch("db_conn", lambda: _AuditConn(inserted))),
        ("_get_database_export_job_for_user", _patch("_get_database_export_job_for_user", lambda j, u: {"job_id": j, "status": "failed", "dataset_id": DATASET_ID, "requested_format": "csv"})),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: _dataset())),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: _columns())),
        ("_load_database_export_job_snapshot", _patch("_load_database_export_job_snapshot", lambda j, u: dict(snapshot))),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (DIRECT_CAP + 1, {"sort": "trip_start", "direction": "desc", "active_filters": [], "filter_entries": []}, None))),
        ("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda user, ds, snap, format_name: "job-2")),
    ]
    try:
        new_id, outcome = api_main._requeue_database_export_job_for_user(_user(), job_id, None)
    finally:
        _restore(patches)
    assert outcome == "requeued" and new_id == "job-2", (outcome, new_id)
    assert [row["event_type"] for row in inserted] == ["database_export_job_requeued"], inserted
    print("PASS: all four S8 lifecycle events pass the real audit validator and reach the insert")


def test_the_real_validator_accepts_each_lifecycle_event_and_still_refuses_unknown_ones() -> None:
    inserted: list[dict] = []
    patches = [("db_conn", _patch("db_conn", lambda: _AuditConn(inserted)))]
    try:
        for event_type in (
            "database_export_job_cancelled", "database_export_cancel_refused",
            "database_export_job_requeued", "database_export_requeue_refused",
        ):
            api_main._create_portal_audit_event(
                event_type=event_type, actor_user_id=USER_ID, dataset_id=DATASET_ID,
                metadata_json={"job_id": "11111111-1111-1111-1111-111111111111", "outcome": "cancelled"},
            )
        # The allowlist is still an allowlist.
        rejected = False
        try:
            api_main._create_portal_audit_event(event_type="database_export_totally_invented", actor_user_id=USER_ID)
        except ValueError:
            rejected = True
    finally:
        _restore(patches)
    assert [row["event_type"] for row in inserted] == [
        "database_export_job_cancelled", "database_export_cancel_refused",
        "database_export_job_requeued", "database_export_requeue_refused",
    ], inserted
    assert rejected, "the validator stopped refusing unregistered event types"
    print("PASS: the canonical validator accepts the four S8 events and still refuses unknown ones")


def test_lifecycle_audit_metadata_stays_minimized() -> None:
    """Registration must not become an excuse to record more."""
    job_id = "11111111-1111-1111-1111-111111111111"
    _response, inserted = _drive_lifecycle_route(
        api_main.user_portal_database_export_cancel,
        job_id=job_id,
        overrides=[("_cancel_database_export_job_for_user", lambda j, u: "cancelled")],
    )
    payload = json.dumps(inserted)
    assert job_id in payload, "the safe job reference is missing"
    for forbidden in (RAW_IDENTITY, "record_id", "row_ref", "claim_token", "lease", "storage_key", "tok-"):
        assert forbidden not in payload, forbidden
    print("PASS: lifecycle audit records the safe job reference and no identity, token, lease or storage detail")


# ===========================================================================
# 9. App-bar indicator (DB-49)
# ===========================================================================
def test_the_app_bar_indicator_is_owner_scoped_and_absent_at_zero() -> None:
    from api.portal_ui import shell as portal_shell

    assert portal_shell.export_indicator_html(0) == "", "a zero indicator must be absent, not empty"
    active = portal_shell.export_indicator_html(2)
    assert "2 eksport w toku" in active, active
    assert 'href="/user/database/exports"' in active, active
    # The count is stated in words, so the state never depends on the dot.
    assert "lp-export-count" in active and "aria-label" in active, active

    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    body = source.split("def _count_active_database_export_jobs_for_user", 1)[1].split("\ndef ", 1)[0]
    assert "WHERE requested_by_user_id = %s" in body, "the indicator count is not owner-scoped"
    assert "status IN ('queued', 'running')" in body, body[:400]
    # Portal database only: no client business database, and no polling loop.
    assert "_connect_portal_client_database" not in body, body[:400]
    assert "setInterval" not in (REPO_ROOT / "api" / "static" / "js" / "data-grid-export.js").read_text(encoding="utf-8")
    print("PASS: the app-bar indicator is owner-scoped, portal-DB only, word-labelled and absent at zero")


def test_the_indicator_reaches_pages_beyond_the_dataset_it_was_queued_from() -> None:
    calls: list[str] = []
    patches = [
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: True)),
        ("_count_active_database_export_jobs_for_user", _patch("_count_active_database_export_jobs_for_user", lambda uid: calls.append(uid) or 1)),
    ]
    try:
        html = api_main._portal_layout(
            "Title", "<p>body</p>", user=_user(), portal_label="User Portal", active_key="reports",
        ).body.decode("utf-8")
    finally:
        _restore(patches)
    assert calls == [USER_ID], calls
    assert "1 eksport w toku" in html, html
    print("PASS: the indicator renders on every portal page, so the user can navigate away (DB-49)")


# ===========================================================================
# 10. Assets, vocabulary and boundaries
# ===========================================================================
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "data_grid_export_harness.js"


def _run_js(scenario: str) -> dict:
    import subprocess

    out = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
    )
    assert out.returncode == 0, f"{scenario}: {out.stderr.strip()}"
    return json.loads(out.stdout)


def test_the_browser_maps_an_s7_selection_to_opaque_references_only() -> None:
    mapped = _run_js("selection-maps-rows-to-opaque-references")
    assert [p["value"] for p in mapped["posted"]] == ["tok-AAAAref0000", "tok-CCCCref2222"], mapped
    assert all(p["name"] == "row_ref" for p in mapped["posted"]), mapped
    assert mapped["selectionCount"] == "2" and mapped["selectionDisabled"] is False

    # Two cells in one row are one exported row.
    deduped = _run_js("repeated-rows-deduplicate")
    assert [p["value"] for p in deduped["posted"]] == ["tok-BBBBref1111", "tok-DDDDref3333"], deduped

    # Nothing raw ever enters the page or the payload.
    dumped = _run_js("module-holds-no-raw-identity")
    assert dumped["rawIdentity"] not in dumped["markup"], "a raw identity reached the browser"
    assert "record_id" not in dumped["markup"], dumped["markup"][:200]
    assert dumped["fetches"] == 0, "the export module issued a request"
    print("PASS: the browser maps an S7 selection to opaque references, deduplicates rows and holds no identity")


def test_the_selected_scope_is_unavailable_until_a_selection_exists() -> None:
    empty = _run_js("no-selection-disables-the-scope")
    assert empty["selectionDisabled"] is True, empty
    assert empty["posted"] == [], empty

    cleared = _run_js("clearing-the-selection-clears-the-scope")
    assert cleared["before"] == 2, cleared
    assert cleared["posted"] == [], "a stale reference survived a cleared selection"
    assert cleared["selectionDisabled"] is True and cleared["scopeAfter"] is True, cleared

    # A row with no configured identity has no reference and is skipped, never
    # substituted by a position.
    identityless = _run_js("rows-without-a-reference-are-skipped")
    assert identityless["posted"] == [] and identityless["selectionDisabled"] is True, identityless
    print("PASS: the selected scope is unavailable without a selection and never keeps a stale reference")


def test_the_browser_path_notice_follows_the_scope_and_format() -> None:
    switched = _run_js("path-notice-follows-the-scope")
    assert switched["direct"]["submit"] == "Pobierz XLSX", switched
    assert "Pobranie natychmiastowe" in switched["direct"]["notice"], switched
    assert switched["background"]["submit"] == "Przygotuj w tle", switched
    assert "retencja 3 dni" in switched["background"]["notice"], switched

    refused = _run_js("over-the-ceiling-refuses")
    assert refused["submitDisabled"] is True, "an impossible scope stayed submittable"
    assert "is-refused" in refused["noticeClass"], refused

    fmt = _run_js("format-changes-the-submit-label")
    assert fmt["submit"] == "Pobierz CSV", fmt
    print("PASS: the path notice and the submit label follow the chosen scope and format")


def test_copying_a_reference_reports_through_a_live_region() -> None:
    ok = _run_js("copy-reference-succeeds")
    assert ok["prevented"] is True and ok["clipboard"] == ["11111111-1111-1111-1111-111111111111"], ok
    assert ok["status"] == api_main._tr("db.exports.reference_copied"), ok

    failed = _run_js("copy-reference-falls-back")
    assert failed["execCopies"] == ["copy"], "the rejected write did not fall back"
    assert failed["status"] == api_main._tr("db.exports.reference_copy_failed"), failed
    print("PASS: copying a job reference reports success and failure through a live region")


def test_the_row_browser_actually_loads_the_export_module() -> None:
    html = _render()
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    export = [src for src in scripts if "data-grid-export.js" in src]
    assert len(export) == 1, scripts
    assert re.search(r"/static/js/data-grid-export\.js\?v=[0-9a-f]+", export[0]), export
    assert "js/data-grid-export.js" in portal_assets.PAGE_ASSETS
    print("PASS: the rendered row browser loads the versioned S8 module")


def test_the_export_module_is_page_scoped() -> None:
    patches = [
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [])),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        catalogue = api_main._user_database_response(_user(), _FakeRequest()).body.decode("utf-8")
    finally:
        _restore(patches)
    assert "data-grid-export.js" not in catalogue, "the module leaked onto a non-export page"
    print("PASS: the export module stays scoped to the pages that need it")


def test_every_s8_string_travels_through_the_translation_catalogue() -> None:
    known = i18n.available_keys()
    for key in (
        "db.export.scope.view", "db.export.scope.dataset", "db.export.scope.selection",
        "db.export.columns.screen", "db.export.columns.approved",
        "db.export.path.direct", "db.export.path.background", "db.export.path.over_ceiling",
        "db.exports.state.running", "db.exports.state.ready", "db.exports.state.expired",
        "db.exports.state.failed", "db.exports.action.cancel", "db.exports.action.requeue",
        "db.exports.action.copy_reference", "shell.export.indicator",
    ):
        assert key in known, key
    # The shipped module holds the rule, never the words.
    code = (REPO_ROOT / "api" / "static" / "js" / "data-grid-export.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    for word in ("Pobierz", "Przygotuj", "zaznacz", "Skopiowano", "eksport"):
        assert word not in code, f"the module hard-codes the Polish string {word!r}"
    print("PASS: every S8 string is a translation key and the module ships no Polish (D-010)")


def test_no_legacy_export_form_or_stale_column_promise_remains() -> None:
    html = _render()
    # One canonical export interaction. The pre-S8 form, its format select and
    # its "always all approved columns" promise are gone.
    assert html.count('class="db-export-form"') == 1, html.count('class="db-export-form"')
    assert html.count("data-db-export-panel") == 1, html
    assert 'id="db-export-format"' not in html, html
    assert "Export report" not in html, html
    assert "Exports always include" not in html, html
    assert "and rows-per-page only affect the on-screen table" not in html, html
    print("PASS: the legacy export form and its stale column promise are gone, leaving one canonical panel")


def test_the_migration_is_additive_and_only_widens_the_status_vocabulary() -> None:
    path = REPO_ROOT / "db" / "migrations" / "065_database_export_job_cancellation.sql"
    sql = path.read_text(encoding="utf-8")
    assert "'cancelled'" in sql, sql
    for status in ("queued", "running", "completed", "failed", "expired"):
        assert f"'{status}'" in sql, status
    # Additive only: no data change, no column drop, no table rewrite.
    for forbidden in ("DELETE", "TRUNCATE", "DROP TABLE", "DROP COLUMN", "UPDATE database_export_jobs SET"):
        assert forbidden not in sql.upper().replace("DROP CONSTRAINT", ""), forbidden
    # And the numbering does not collide with a concurrent migration.
    numbers = sorted(int(p.name[:3]) for p in (REPO_ROOT / "db" / "migrations").glob("*.sql") if p.name[:3].isdigit())
    assert numbers.count(65) == 1, numbers
    # Re-executable: the old constraint is resolved from the catalog rather than
    # by assuming a generated name, and re-adding is guarded.
    assert "pg_constraint" in sql and "DROP CONSTRAINT %I" in sql, sql
    assert "WHEN duplicate_object THEN NULL" in sql, sql
    assert "WHEN undefined_table THEN NULL" in sql, sql
    print("PASS: migration 065 is additive, re-executable, preserves every status and does not collide")


def test_no_report_explorer_or_later_stage_work_arrives_with_s8() -> None:
    html = _render()
    for future in ("Zapisz jako widok", "Zapisz jako zestaw", "Zestawy"):
        assert future not in html, f"{future} belongs to a later stage"
    # The export panel does not become a row-checkbox feature.
    assert 'type="checkbox"' not in html.split("<tbody>", 1)[1].split("</tbody>", 1)[0], "no row checkboxes"
    # Database exports remain technical artifacts; no report-instance model.
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert "report_instances" not in source, "a report-instance model arrived with S8"
    print("PASS: no saved views, no row checkboxes and no Report Explorer model change arrive with S8")


def main() -> None:
    test_panel_offers_exactly_the_three_approved_scopes_with_counts()
    test_current_view_exports_the_result_set_not_the_page()
    test_whole_dataset_ignores_filters_and_search_but_keeps_authorization()
    test_selected_rows_resolve_through_opaque_references_and_deduplicate()
    test_malformed_tampered_and_foreign_references_are_refused_identically()
    test_selected_rows_export_only_when_every_identity_matches_exactly_one_row()
    test_a_missing_selected_identity_refuses_the_whole_export()
    test_a_duplicated_selected_identity_refuses_the_whole_export()
    test_a_missing_and_duplicated_identity_with_the_same_row_total_is_still_caught()
    test_selected_row_validation_applies_to_both_formats()
    test_the_identity_stays_internal_and_parameterized_during_validation()
    test_a_dataset_without_row_identity_cannot_use_the_selected_scope()
    test_the_selected_scope_is_bounded_by_the_page_size()
    test_column_scopes_are_the_two_approved_choices_with_counts()
    test_screen_scope_uses_the_displayed_columns_in_display_order()
    test_approved_scope_is_the_whole_approved_catalogue()
    test_forged_column_state_cannot_widen_an_export()
    test_the_hidden_row_identity_never_reaches_an_export()
    test_execution_path_boundaries_are_unchanged()
    test_the_submit_handler_routes_authoritatively_at_every_boundary()
    test_the_panel_states_the_path_before_the_user_commits()
    test_an_unavailable_total_stays_unavailable()
    test_without_the_grant_the_export_action_is_absent_and_forged_posts_fail()
    test_an_unavailable_dataset_grant_denies_every_scope()
    test_the_job_snapshot_stores_canonical_validated_state_only()
    test_an_unapproved_filter_column_refuses_the_export_rather_than_widening_it()
    test_the_dataset_scope_snapshot_carries_no_filters()
    test_the_four_states_each_present_a_different_action_set()
    test_queued_and_running_are_one_presented_state()
    test_an_expired_export_offers_no_download_at_all()
    test_a_failed_export_exposes_a_sanitized_message_and_a_copyable_reference()
    test_a_cancelled_export_is_not_listed_and_exposes_no_download()
    test_the_progress_bar_is_determinate_only_when_a_denominator_exists()
    test_the_empty_state_explains_the_threshold()
    test_cancelling_a_queued_job_prevents_any_later_claim()
    test_cancelling_a_running_job_defeats_a_stale_publication()
    test_a_worker_that_publishes_first_wins_and_cancellation_is_refused()
    test_a_stale_claim_token_can_change_nothing()
    test_stale_recovery_never_resurrects_a_cancelled_job()
    test_only_the_owner_can_cancel()
    test_the_cancel_sql_is_conditional_on_ownership_and_an_active_status()
    test_requeue_exists_only_in_the_approved_states()
    test_requeue_revalidates_ownership_authorization_and_the_ceiling()
    test_requeue_creates_a_new_attempt_rather_than_reviving_history()
    test_cancel_and_requeue_events_reach_the_real_audit_validator()
    test_the_real_validator_accepts_each_lifecycle_event_and_still_refuses_unknown_ones()
    test_lifecycle_audit_metadata_stays_minimized()
    test_the_app_bar_indicator_is_owner_scoped_and_absent_at_zero()
    test_the_indicator_reaches_pages_beyond_the_dataset_it_was_queued_from()
    test_the_browser_maps_an_s7_selection_to_opaque_references_only()
    test_the_selected_scope_is_unavailable_until_a_selection_exists()
    test_the_browser_path_notice_follows_the_scope_and_format()
    test_copying_a_reference_reports_through_a_live_region()
    test_the_row_browser_actually_loads_the_export_module()
    test_the_export_module_is_page_scoped()
    test_every_s8_string_travels_through_the_translation_catalogue()
    test_no_legacy_export_form_or_stale_column_promise_remains()
    test_the_migration_is_additive_and_only_widens_the_status_vocabulary()
    test_no_report_explorer_or_later_stage_work_arrives_with_s8()
    print("\nALL EXPORT PANEL AND BACKGROUND STATE TESTS PASSED")


if __name__ == "__main__":
    main()
