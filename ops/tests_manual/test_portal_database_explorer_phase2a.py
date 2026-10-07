#!/usr/bin/env python3
"""Phase 2A tests: Client Database Explorer UX improvements.

Covers the safe MVP additions while preserving query semantics, permissions, and
exports: global search (parameterized, visible text columns only), active filter
chips + precise removal, reset-that-preserves-sort/page-size, date presets, the
decoupled export limit, and the redesigned browser page chrome. Hidden columns
must never leak into queries, chips, search, or the rendered page.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_explorer_phase2a.py
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


class _FakeUrl:
    def __init__(self, path=f"/user/database/datasets/{DATASET_ID}", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path=f"/user/database/datasets/{DATASET_ID}", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False):
    return {
        "user_id": USER_ID,
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "permissions": [],
    }


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "slug": "approved-trips",
        "description": "Approved portal dataset",
        "schema_name": "public",
        "table_name": "trips",
        "default_date_column": "trip_date",
        "is_active": True,
        "visible_columns": 2,
        "assigned_users": 1,
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _columns():
    return [
        {"dataset_id": DATASET_ID, "column_name": "trip_date", "display_name": "Trip date", "data_type": "date",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": True, "display_order": 10},
        {"dataset_id": DATASET_ID, "column_name": "driver_name", "display_name": "Driver", "data_type": "text",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "display_order": 20},
        {"dataset_id": DATASET_ID, "column_name": "internal_secret", "display_name": "Hidden", "data_type": "text",
         "is_visible": False, "is_filterable": False, "is_sortable": False, "is_default_date_column": False, "display_order": 30},
    ]


def _visible_columns():
    return [c for c in _columns() if c["is_visible"]]


def _params(query: str) -> dict:
    return api_main._portal_database_query_params(_FakeRequest(query=query))


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


# --------------------------------------------------------------------------
def _test_global_search_query_is_parameterized_and_safe() -> None:
    params = _params("search=Ali")
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), _visible_columns(), params, limit=50, offset=0
    )
    assert error is None, error
    # search spans visible filterable text columns only (driver_name), never date or hidden columns
    assert 'CAST("driver_name" AS TEXT) ILIKE %s' in query, query
    assert "trip_date" in query  # still selected/sorted, but not part of the search OR group
    assert 'CAST("trip_date" AS TEXT) ILIKE' not in query, query
    assert "internal_secret" not in query, query
    assert values == ["%Ali%", 50, 0], values
    print("PASS: global search builds a parameterized OR over visible text columns only")


def _test_search_respects_filter_permission() -> None:
    _, _, _, error = api_main._build_portal_database_rows_query(
        _dataset(can_filter_rows=False), _visible_columns(), _params("search=Ali"), limit=50, offset=0
    )
    assert error and "Filtering is not enabled" in error, error
    print("PASS: search is blocked when the user cannot filter rows")


def _test_no_search_keeps_legacy_query_unchanged() -> None:
    # Regression guard: without search/cols the builder output is byte-for-byte legacy.
    params = _params("sort=trip_date&direction=desc&filter__driver_name=Ali&op__driver_name=contains&date_from=2026-05-01&date_to=2026-05-31")
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), _visible_columns(), params, limit=25, offset=50
    )
    assert error is None, error
    assert 'SELECT "trip_date", "driver_name" FROM "public"."trips"' in query, query
    assert values == ["%Ali%", "2026-05-01", "2026-05-31", 25, 50], values
    print("PASS: queries without search/cols stay identical to the legacy builder")


def _test_row_identifier_secondary_sort_tiebreaker() -> None:
    columns = [{**column, "is_row_identifier": False} for column in _visible_columns()]
    columns.append({
        "dataset_id": DATASET_ID,
        "column_name": "trip_id",
        "display_name": "Trip ID",
        "data_type": "uuid",
        "is_visible": True,
        "is_filterable": False,
        "is_sortable": True,
        "is_default_date_column": False,
        "is_row_identifier": True,
        "display_order": 5,
    })
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), columns, _params("sort=trip_date&direction=desc"), limit=50, offset=0
    )
    assert error is None, error
    assert 'ORDER BY "trip_date" DESC, "trip_id" ASC' in query, query
    assert state.get("secondary_sort") == "trip_id", state
    assert values[-2:] == [50, 0], values

    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), columns, _params("sort=trip_id&direction=desc"), limit=50, offset=0
    )
    assert error is None, error
    assert 'ORDER BY "trip_id" DESC, "trip_id" ASC' not in query, query
    assert state.get("secondary_sort") is None, state

    no_identifier_columns = [{**column, "is_row_identifier": False} for column in columns]
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), no_identifier_columns, _params("sort=trip_date&direction=desc"), limit=50, offset=0
    )
    assert error is None, error
    assert 'ORDER BY "trip_date" DESC, "trip_id" ASC' not in query, query
    assert state.get("secondary_sort") is None, state
    print("PASS: row identifier is used as a secondary deterministic sort key only when configured and distinct")


def _test_active_filter_chips_and_precise_removal() -> None:
    params = _params("search=foo&date_from=2026-05-01&filter__driver_name=Ali&op__driver_name=contains&sort=trip_date&limit=50")
    # S3 builds chips from the query builder's validated record rather than by
    # re-reading the query string, so a rejected or no-op parameter can never
    # become a chip.
    _c, _v, _active, error, entries = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), params
    )
    assert error is None, error
    chips = api_main._portal_database_active_filter_chips(_dataset(), params, entries)
    labels = [c["label"] for c in chips]
    # The global search is its own toolbar field, not a chip (`PBC` 2.6).
    assert "Search" not in labels, labels
    assert "Driver" in labels and "Trip date" in labels, labels
    by_label = {c["label"]: c for c in chips}
    # removing the Driver filter drops both its value and operator, keeps the rest
    driver_remove = by_label["Driver"]["remove_url"]
    assert "filter__driver_name" not in driver_remove, driver_remove
    assert "op__driver_name" not in driver_remove, driver_remove
    assert "date_from=2026-05-01" in driver_remove and "search=foo" in driver_remove, driver_remove
    # removing the legacy date range drops only it
    date_remove = by_label["Trip date"]["remove_url"]
    date_query = parse_qs(urlparse(date_remove).query)
    assert "date_from" not in date_query and "date_to" not in date_query, date_remove
    assert date_query.get("filter__driver_name") == ["Ali"], date_remove
    assert date_query.get("search") == ["foo"], date_remove
    print("PASS: active filter chips remove exactly one filter and preserve the rest")


def _test_reset_preserves_sort_and_page_size() -> None:
    params = _params("sort=trip_date&direction=desc&limit=50&density=compact&filter__driver_name=Ali&op__driver_name=contains&date_from=2026-05-01&search=foo&page=4")
    reset = api_main._portal_database_reset_url(DATASET_ID, params)
    q = parse_qs(urlparse(reset).query)
    assert q.get("sort") == ["trip_date"] and q.get("direction") == ["desc"], reset
    assert q.get("limit") == ["50"] and q.get("density") == ["compact"], reset
    for dropped in ("filter__driver_name", "op__driver_name", "date_from", "search", "page"):
        assert dropped not in q, (dropped, reset)
    print("PASS: Reset filters keeps sort/direction/page size/density and clears filters/date/search")


def _test_date_presets_map_to_date_range() -> None:
    links = api_main._portal_database_date_preset_links(_dataset(), _visible_columns(), _params(""))
    for label in ("Today", "Yesterday", "Last 7 days", "Last 30 days", "This month", "Previous month"):
        assert label in links, label
    assert "date_from=" in links and "date_to=" in links, links
    # a pure date column must not get an end-of-day time component
    assert "T23" not in links, links
    # a timestamp default-date column gets an inclusive end-of-day bound
    ts_columns = [dict(c) for c in _visible_columns()]
    ts_columns[0]["column_name"] = "trip_date"
    ts_columns[0]["data_type"] = "timestamp without time zone"
    ts_links = api_main._portal_database_date_preset_links(_dataset(), ts_columns, _params(""))
    assert "T23" in ts_links, ts_links
    print("PASS: date presets map to date_from/date_to with inclusive end-of-day for timestamps")


def _test_date_filter_modes_and_validation() -> None:
    dataset = _dataset()
    columns = _visible_columns()

    older = _params("dateop__trip_date=older&date__trip_date=2026-05-10")
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, columns, older, limit=10, offset=0)
    assert error is None, error
    assert '"trip_date" < %s' in query and values[:1] == ["2026-05-10"], (query, values)

    newer = _params("dateop__trip_date=newer&date__trip_date=2026-05-10")
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, columns, newer, limit=10, offset=0)
    assert error is None, error
    assert '"trip_date" > %s' in query and values[:1] == ["2026-05-10"], (query, values)

    closed = _params("dateop__trip_date=range&date_from__trip_date=2026-05-10&date_to__trip_date=2026-05-12")
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, columns, closed, limit=10, offset=0)
    assert error is None, error
    assert '"trip_date" >= %s' in query and '"trip_date" <= %s' in query, query
    assert values[:2] == ["2026-05-10", "2026-05-12"], values

    open_ended = _params("dateop__trip_date=range&date_from__trip_date=2026-05-10")
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, columns, open_ended, limit=10, offset=0)
    assert error is None, error
    assert '"trip_date" >= %s' in query and '"trip_date" <= %s' not in query, query
    assert values[:1] == ["2026-05-10"], values

    invalid = _params("dateop__trip_date=older&date__trip_date=not-a-date")
    _, _, _, error = api_main._build_portal_database_rows_query(dataset, columns, invalid, limit=10, offset=0)
    assert error and "must be an ISO date or datetime" in error, error

    reversed_range = _params("dateop__trip_date=range&date_from__trip_date=2026-05-12&date_to__trip_date=2026-05-10")
    _, _, _, error = api_main._build_portal_database_rows_query(dataset, columns, reversed_range, limit=10, offset=0)
    assert error and "from must be earlier than or equal to" in error, error

    operator_only = _params("dateop__trip_date=range")
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, columns, operator_only, limit=10, offset=0)
    assert error is None and values == [10, 0], (query, values, error)

    # S3 moved the typed date controls into the column's own header menu.
    date_column = next(c for c in columns if c["column_name"] == "trip_date")
    html = api_main._portal_database_column_menu_html(
        dataset, date_column, _params(""), sort_column=None, direction="asc", entry=None
    )
    assert 'name="dateop__trip_date"' in html and 'name="date_from__trip_date"' in html and 'type="date"' in html, html
    ts_column = dict(date_column, data_type="timestamp with time zone")
    ts_html = api_main._portal_database_column_menu_html(
        dataset, ts_column, _params(""), sort_column=None, direction="asc", entry=None
    )
    assert 'type="datetime-local"' in ts_html, ts_html
    print("PASS: date filters support older/newer/range, inclusive/open bounds, validation, and typed controls")


def _test_export_url_decoupled_from_page_size() -> None:
    params = _params("limit=50&page=3&density=compact&filter__driver_name=Ali&op__driver_name=contains")
    url = api_main._portal_database_export_url(DATASET_ID, params, format_name="csv")
    q = parse_qs(urlparse(url).query)
    assert q.get("format") == ["csv"], url
    assert q.get("limit") == [str(api_main.PORTAL_DATABASE_DEFAULT_EXPORT_LIMIT)], url
    assert "page" not in q and "density" not in q, url
    # filters are preserved so the export matches the on-screen filtered view
    assert q.get("filter__driver_name") == ["Ali"], url
    print("PASS: export URL keeps filters but ignores on-screen page size/density")


def _render_browser(dataset, *, query="", async_schema_available=True, total=3, captured_calls=None):
    captured_calls = [] if captured_calls is None else captured_calls

    # Chips, the count badge and the column-menu state all read the query
    # builder's validated record, so resolve it through the real builder.
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    _c, _v, applied, _e, entries = api_main._build_portal_database_filter_conditions(
        dataset, _visible_columns(), parsed
    )
    state = {
        "sort": "trip_date",
        "direction": "desc",
        "active_filters": applied,
        "filter_entries": entries,
    }

    def fake_count(d, c, p):
        captured_calls.append({"kind": "count", "params": p})
        return total, state, None

    def fake_list(d, c, p, limit, offset, display_columns=None):
        captured_calls.append({"kind": "list", "limit": limit, "offset": offset, "display_columns": display_columns})
        return ([{"trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"}], state, None)

    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _visible_columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", fake_count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", fake_list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (total, None))),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: async_schema_available)),
        ("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browse must not queue exports")))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query)))
    finally:
        _restore(patches)


def _test_browser_page_chrome_and_no_hidden_columns() -> None:
    html = _render_browser(_dataset(can_export_rows=True), query="filter__driver_name=Ali&op__driver_name=contains&limit=50")
    # unified shell + scoped enhancements
    assert "db-browser" in html and "portal-shell" in html, html
    # S3 replaced the standalone filter form with the column-centric surfaces:
    # a per-column header menu and the toolbar's staged filter panel.
    assert "db-col-menu" in html and 'class="db-col-filter"' not in html.split("db-col-menu", 1)[0], html
    assert 'class="db-panel-form"' in html, "the staged filter panel must render"
    # The row sheet is table-first: the approved data grid replaced the generic
    # bordered `portal-table portal-table-dense` panel, and the toolbar/footer
    # replaced the old rows-panel chrome.
    assert 'class="db-table"' in html and "db-toolbar" in html and "db-footer" in html, html
    # active filters are chips in the toolbar strip
    assert "db-chips" in html and "db-chip" in html, html
    assert "Wyczyść wszystkie" in html, html
    # search box, presets, density control, page-size selector
    assert 'name="search"' in html, html
    assert "Quick ranges" in html and "wierszy na stronie" in html, html
    # Density is the approved two-option segmented control, not a single toggle.
    assert 'data-density-option="compact"' in html and 'data-density-option="comfortable"' in html, html
    assert "Zwarta" in html and "Wygodna" in html, html
    # Export clarity for an export-enabled dataset. Approved stage S8 replaced
    # the legacy `Export report` form and its English notes with the `DB-009`
    # panel: one form, three scopes, two column scopes, two formats and a path
    # notice stated before the user commits. What this assertion has always been
    # about — exactly one export control, no stale duplicate submit labels, and
    # the thresholds stated — is unchanged.
    assert html.count('class="db-export-form"') == 1, html
    assert 'name="format_name"' in html and 'value="xlsx"' in html and 'value="csv"' in html, html
    assert "Bieżący widok" in html and "Cały zbiór danych" in html and "Zaznaczone wiersze" in html, html
    assert "Jak na ekranie" in html and "Wszystkie zatwierdzone" in html, html
    for stale in ("Download CSV now", "Download XLSX now", "Prepare background export",
                  "Prepare CSV background export", "Prepare XLSX background export",
                  ">Export report<", "Exports always include"):
        assert stale not in html, (stale, html)
    # The direct cap and the retention window are still stated before commit.
    assert "20 000" in html.replace(" ", " ").replace(" ", " "), html
    assert "retencja 3 dni" in html or "Pobranie natychmiastowe" in html, html
    # hidden column never leaks into the page
    assert "internal_secret" not in html and ">Hidden<" not in html and ">hidden<" not in html, html
    print("PASS: browser renders unified chrome, chips, search, presets, export clarity; hides hidden columns")


def _test_browser_hides_background_export_links_before_migration() -> None:
    html = _render_browser(_dataset(can_export_rows=True), query="", async_schema_available=False)
    # One export control, now the approved `DB-009` panel rather than the legacy
    # `Export report` form; the background-export links stay absent and the
    # panel says why a scope above the direct cap has nowhere to run.
    assert html.count('class="db-export-form"') == 1, html
    assert 'name="format_name"' in html and 'value="xlsx"' in html and 'value="csv"' in html, html
    for stale in ("Download CSV now", "Download XLSX now", "Prepare CSV background export",
                  "Prepare XLSX background export", "My Background Exports", "My background exports",
                  ">Export report<", "Moje eksporty danych"):
        assert stale not in html, (stale, html)
    assert api_main.ASYNC_DATABASE_EXPORT_SCHEMA_UNAVAILABLE_MESSAGE in html, html
    print("PASS: browser keeps one export control and hides background-export links before migration 043")


def _test_valid_browse_route_pre043_uses_configured_client_database() -> None:
    class Cursor:
        def __init__(self):
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=()):
            self.query = str(query)
            if self.query == "SET statement_timeout = %s":
                raise RuntimeError("parameterized SET statement_timeout is invalid")

        def fetchone(self):
            return {"total": 1}

        def fetchall(self):
            return [{"trip_date": "2026-05-28", "driver_name": "Alice"}]

    class Conn:
        def __init__(self):
            self.cursors = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            cursor = Cursor()
            self.cursors.append(cursor)
            return cursor

    connects = []

    def fake_connect(**kwargs):
        connects.append(kwargs)
        return Conn()

    dataset = _dataset(can_export_rows=True, client_database_name="alpha_main")
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _visible_columns())),
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
        ("set_pg_session_timezone", _patch("set_pg_session_timezone", lambda conn: conn)),
    ]
    old_connect = getattr(api_main.psycopg, "connect", None)
    api_main.psycopg.connect = fake_connect
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=""))
        html = _html(response)
        assert response.status_code == 200, (response.status_code, html)
        assert "Alice" in html and "Approved trips" in html, html
        assert "Client database is not available or not configured" not in html, html
        assert "Dataset cannot be loaded" not in html, html
        assert api_main.ASYNC_DATABASE_EXPORT_SCHEMA_UNAVAILABLE_MESSAGE in html, html
        assert len(connects) == 2, connects
        assert {connect["dbname"] for connect in connects} == {"alpha_main"}, connects
    finally:
        if old_connect is None:
            delattr(api_main.psycopg, "connect")
        else:
            api_main.psycopg.connect = old_connect
        _restore(patches)
    print("PASS: valid pre-043 browse route reaches the configured client database without client-database error")


def _test_large_browse_route_never_enters_export_validation() -> None:
    captured_calls = []
    html = _render_browser(
        _dataset(can_export_rows=True),
        query="limit=50&page=1&sort=trip_date&direction=desc",
        async_schema_available=True,
        total=530875,
        captured_calls=captured_calls,
    )
    assert "Approved trips" in html and "Alice" in html, html
    # Approved result counter: grouped with a non-breaking space, and unfiltered,
    # so it states the dataset size without a second "n z m" clause.
    assert "530\u00a0875" in html, html
    assert "wierszy" in html, html
    assert "Strona 1 z 10618" in html, html
    assert "Export cannot be created" not in html and "Dataset cannot be loaded" not in html, html
    assert "Export matches" not in html and "exceeds" not in html, html
    kinds = [call["kind"] for call in captured_calls]
    assert kinds == ["count", "list"], captured_calls
    assert captured_calls[-1]["limit"] == 50 and captured_calls[-1]["offset"] == 0, captured_calls
    print("PASS: large Dataset Explorer browse renders paginated rows without export validation or queueing")


def _test_browser_export_disabled_message_preserved() -> None:
    html = _render_browser(_dataset(can_export_rows=False), query="")
    # Approved stage S9 replaced the English sentence with the keyed Polish copy
    # in the approved permission vocabulary. The capability semantics — no export
    # form, no submit, server-side refusal — are unchanged.
    assert "Tylko podgl\u0105d" in html, html
    assert "Export is not enabled" not in html, html
    assert "db-export-form" not in html, html
    print("PASS: export-disabled message preserved for read-only access")


def main() -> None:
    _test_global_search_query_is_parameterized_and_safe()
    _test_search_respects_filter_permission()
    _test_no_search_keeps_legacy_query_unchanged()
    _test_row_identifier_secondary_sort_tiebreaker()
    _test_active_filter_chips_and_precise_removal()
    _test_reset_preserves_sort_and_page_size()
    _test_date_presets_map_to_date_range()
    _test_date_filter_modes_and_validation()
    _test_export_url_decoupled_from_page_size()
    _test_browser_page_chrome_and_no_hidden_columns()
    _test_browser_hides_background_export_links_before_migration()
    _test_valid_browse_route_pre043_uses_configured_client_database()
    _test_large_browse_route_never_enters_export_validation()
    _test_browser_export_disabled_message_preserved()
    print("\nALL PASS: Phase 2A Client Database Explorer UX (semantics/permissions/exports preserved)")


if __name__ == "__main__":
    main()
