#!/usr/bin/env python3
"""Phase 2B tests: Database Explorer URL-based column visibility (column picker).

Covers the display/allowed column separation: only permitted columns can be
selected, the SELECT narrows to the chosen subset while filters/sort/search still
operate on the full permitted set, exports always include all approved columns,
reset-columns preserves the rest of the view, and hidden columns never leak.
Row detail remains deferred because the catalog has no stable row identifier.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_explorer_phase2b.py
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
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": admin, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_date",
        "is_active": True, "visible_columns": 2, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
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


def _names(columns):
    return [str(c.get("column_name")) for c in columns]


def _select_clause(query: str) -> str:
    return query.split(" FROM ", 1)[0]


# --------------------------------------------------------------------------
def _test_parse_cols_only_permits_visible_columns() -> None:
    vis = _visible_columns()
    # absent -> default to all permitted, in catalog order
    display, fell = api_main._portal_database_parse_cols(_params(""), vis)
    assert _names(display) == ["trip_date", "driver_name"] and fell is False, display

    # valid subset
    display, fell = api_main._portal_database_parse_cols(_params("cols=driver_name"), vis)
    assert _names(display) == ["driver_name"] and fell is False, display

    # hidden + unknown names are silently ignored, valid ones kept
    display, fell = api_main._portal_database_parse_cols(_params("cols=driver_name,internal_secret,bogus"), vis)
    assert _names(display) == ["driver_name"] and fell is False, display

    # all invalid -> safe fallback to all permitted columns, flagged
    display, fell = api_main._portal_database_parse_cols(_params("cols=internal_secret,bogus"), vis)
    assert _names(display) == ["trip_date", "driver_name"] and fell is True, display

    # catalog order preserved regardless of cols order
    display, _ = api_main._portal_database_parse_cols(_params("cols=driver_name,trip_date"), vis)
    assert _names(display) == ["trip_date", "driver_name"], display
    print("PASS: cols parsing only permits visible columns and falls back safely")


def _test_select_narrows_but_filters_sort_use_all_columns() -> None:
    vis = _visible_columns()
    display = [c for c in vis if c["column_name"] == "driver_name"]
    params = _params("sort=trip_date&direction=desc&filter__driver_name=Ali&op__driver_name=contains")
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), vis, params, limit=10, offset=0, display_columns=display
    )
    assert error is None, error
    # SELECT narrows to the chosen display column only
    assert _select_clause(query) == 'SELECT "driver_name"', query
    assert '"trip_date"' not in _select_clause(query), query
    # sort by a permitted-but-not-displayed column still works
    assert 'ORDER BY "trip_date" DESC' in query, query
    # filter on a permitted column still applies (parameterized)
    assert 'CAST("driver_name" AS TEXT) ILIKE %s' in query and values[0] == "%Ali%", (query, values)
    # hidden column never appears anywhere
    assert "internal_secret" not in query, query
    print("PASS: display narrows SELECT while filters/sort still use the full permitted set")


def _test_legacy_query_unchanged_without_cols() -> None:
    params = _params("sort=trip_date&direction=desc&filter__driver_name=Ali&op__driver_name=contains&date_from=2026-05-01&date_to=2026-05-31")
    query, values, state, error = api_main._build_portal_database_rows_query(
        _dataset(), _visible_columns(), params, limit=25, offset=50
    )
    assert error is None, error
    assert 'SELECT "trip_date", "driver_name" FROM "public"."trips"' in query, query
    assert values == ["%Ali%", "2026-05-01", "2026-05-31", 25, 50], values
    print("PASS: query without display_columns stays identical to legacy behavior")


def _test_query_builder_quotes_allowlisted_non_simple_columns() -> None:
    columns = [
        {"dataset_id": DATASET_ID, "column_name": "registration", "display_name": "Registration", "data_type": "text",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "display_order": 10},
        {"dataset_id": DATASET_ID, "column_name": "Driver Name", "display_name": "Driver", "data_type": "text",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "display_order": 20},
        {"dataset_id": DATASET_ID, "column_name": "bad_column; DROP TABLE users; --", "display_name": "Literal scary name", "data_type": "text",
         "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "display_order": 30},
    ]
    params = _params("sort=Driver+Name&filter__Driver+Name=Ali&op__Driver+Name=contains")
    query, values, _state, error = api_main._build_portal_database_rows_query(
        _dataset(), columns, params, limit=10, offset=0
    )
    assert error is None, error
    assert 'SELECT "registration", "Driver Name", "bad_column; DROP TABLE users; --" FROM "public"."trips"' in query, query
    assert 'CAST("Driver Name" AS TEXT) ILIKE %s' in query, query
    assert 'ORDER BY "Driver Name" ASC' in query, query
    assert values == ["%Ali%", 10, 0], values

    injected_params = _params("filter__not_available%3B+DROP+TABLE+users%3B+--=x")
    _query, _values, _state, injected_error = api_main._build_portal_database_rows_query(
        _dataset(), columns, injected_params, limit=10, offset=0
    )
    assert injected_error == "Filter column is not available for this dataset.", injected_error

    literal_params = _params("filter__bad_column%3B+DROP+TABLE+users%3B+--=x")
    literal_query, literal_values, _state, literal_error = api_main._build_portal_database_rows_query(
        _dataset(), columns, literal_params, limit=10, offset=0
    )
    assert literal_error is None, literal_error
    assert 'CAST("bad_column; DROP TABLE users; --" AS TEXT) ILIKE %s' in literal_query, literal_query
    assert literal_values == ["%x%", 10, 0], literal_values
    print("PASS: query builder quotes allowlisted non-simple columns and rejects non-allowlisted request strings")


def _test_reset_columns_preserves_other_state() -> None:
    params = _params("cols=driver_name&filter__driver_name=Ali&op__driver_name=contains&search=foo&sort=trip_date&direction=desc&limit=50&density=compact&date_from=2026-05-01&page=3")
    url = api_main._portal_database_reset_columns_url(DATASET_ID, params)
    q = parse_qs(urlparse(url).query)
    assert "cols" not in q, url
    for keep in ("filter__driver_name", "op__driver_name", "search", "sort", "direction", "limit", "density", "date_from"):
        assert keep in q, (keep, url)
    assert q.get("page", ["1"]) == ["1"], url  # reset to first page
    print("PASS: Reset columns drops cols, resets page, and preserves filters/search/sort/limit/density")


def _test_export_url_ignores_cols() -> None:
    url = api_main._portal_database_export_url(DATASET_ID, _params("cols=driver_name&filter__driver_name=Ali"), format_name="csv")
    q = parse_qs(urlparse(url).query)
    assert "cols" not in q, url
    assert q.get("filter__driver_name") == ["Ali"], url
    assert q.get("limit") == [str(api_main.PORTAL_DATABASE_DEFAULT_EXPORT_LIMIT)], url
    print("PASS: export URL ignores cols so exports always include all approved columns")


def _render_browser(dataset, *, query=""):
    # The page renders chips and column-menu state from the query builder's own
    # validated record, so the stub resolves it through the real builder rather
    # than asserting an empty one.
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
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _visible_columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (3, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda d, c, p, limit, offset, display_columns=None: ([{"trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"}], state, None))),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (3, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query)))
    finally:
        _restore(patches)


def _test_browser_renders_column_picker_and_selected_subset() -> None:
    html = _render_browser(_dataset(can_export_rows=True), query="cols=driver_name")
    # S5 replaced the demoted picker with the approved `DB-008` panel behind the
    # toolbar's `Kolumny n/m` button; the same `cols` contract drives it.
    assert "db-columns" in html and "data-db-columns" in html, html
    assert 'name="cols" value="driver_name"' in html and 'name="cols" value="trip_date"' in html, html
    assert ">Kolumny <span class=\"lp-mono\">1/2</span>" in html, html
    assert "Domyślne kolumny" in html, html
    # the selected display column header is present; the de-selected one is not a table header
    assert ">Driver<" in html, html
    assert "<th scope=\"col\"" in html and "aria-sort=" in html, "sortable headers expose their state"
    # hidden column never leaks
    assert "internal_secret" not in html, html
    print("PASS: browser renders column picker, selected-subset table, indicators, and reset columns")


def _test_browser_chip_shows_for_filtered_nondisplayed_column() -> None:
    # display only trip_date, but filter on driver_name (permitted, not displayed)
    html = _render_browser(_dataset(), query="cols=trip_date&filter__driver_name=Ali&op__driver_name=contains")
    # S3 moved the chips out of a bordered "Active filters" panel and into the
    # toolbar strip; the invariant under test is unchanged.
    assert "db-chips" in html and "db-chip" in html, html
    assert "Driver" in html, "chip must use the friendly display name even when the column is not shown"
    assert "internal_secret" not in html, html
    print("PASS: active filter chips render for permitted filtered columns that are not displayed")


def _test_invalid_cols_fall_back_with_notice() -> None:
    html = _render_browser(_dataset(), query="cols=internal_secret,bogus")
    assert ">Kolumny <span class=\"lp-mono\">2/2</span>" in html, html
    assert "were not recognised" in html, html
    assert "internal_secret" not in html, html
    print("PASS: all-invalid cols fall back to all approved columns with a non-sensitive notice")


def _test_row_detail_absent_without_identifier() -> None:
    # Phase 2C added the row-detail helper, but it is only wired up when an admin has
    # configured a row identifier. With no identifier on these columns the browser
    # must not render any per-row Details link (legacy 2B behavior preserved).
    # S6 replaced the Phase 2C raw-identifier row route with the opaque `?row=`
    # reference, so the old helper is gone by design; what must still hold is
    # that a dataset with no configured identity offers no row-detail affordance
    # at all.
    assert not hasattr(api_main, "_portal_database_row_detail_response"), \
        "the raw-identifier row detail route was retired in S6"
    html = _render_browser(_dataset())
    assert ">Details<" not in html, html
    assert "/rows/" not in html, html
    assert "data-db-row=" not in html, html
    print("PASS: no row-detail affordance when no row identifier is configured")


def main() -> None:
    _test_parse_cols_only_permits_visible_columns()
    _test_select_narrows_but_filters_sort_use_all_columns()
    _test_legacy_query_unchanged_without_cols()
    _test_query_builder_quotes_allowlisted_non_simple_columns()
    _test_reset_columns_preserves_other_state()
    _test_export_url_ignores_cols()
    _test_browser_renders_column_picker_and_selected_subset()
    _test_browser_chip_shows_for_filtered_nondisplayed_column()
    _test_invalid_cols_fall_back_with_notice()
    _test_row_detail_absent_without_identifier()
    print("\nALL PASS: Phase 2B Database Explorer column visibility (semantics/permissions/exports preserved)")


if __name__ == "__main__":
    main()
