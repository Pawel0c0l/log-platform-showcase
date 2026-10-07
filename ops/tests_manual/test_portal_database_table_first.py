#!/usr/bin/env python3
"""Database Explorer table-first foundation (approved stage S2).

Covers the row sheet restructure and the value-rendering contract: the
table-first composition, the sticky header and first column, the toolbar and
footer, row density and its precedence rules, the filtered-vs-total counter,
type-aware cell rendering, and the mandatory `NULL` vs empty-string distinction.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screens ``DB-003``/``DB-004``, ``TABLE_AND_DATA_GRID_SPEC.md``.

Security and query semantics are asserted here only where this stage could have
weakened them; the phase2a/2b/2c suites remain their owners.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_table_first.py
"""
from __future__ import annotations

import re
import sys
import types
from datetime import date, datetime, timezone
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

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NBSP = " "


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query=""):
        self.url = _FakeUrl(f"/user/database/datasets/{DATASET_ID}", query)
        self.cookies = {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_date",
        "is_active": True, "visible_columns": 3, "assigned_users": 1,
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
        _col("trip_date", "Trip date", "date", display_order=10),
        _col("driver_name", "Driver", "text", display_order=20),
        _col("distance_km", "Distance", "numeric", display_order=30),
        _col("internal_secret", "Hidden", "text", is_visible=False, is_filterable=False,
             is_sortable=False, display_order=40),
    ]


def _visible_columns():
    return [c for c in _columns() if c["is_visible"]]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _render(dataset=None, *, query="", rows=None, total=3, unfiltered=None, columns=None):
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _visible_columns()
    if rows is None:
        rows = [{"trip_date": date(2026, 5, 28), "driver_name": "Alice",
                 "distance_km": Decimal("128.40"), "internal_secret": "hidden"}]
    # Mirror what the real query builder resolves, so sort and applied-filter
    # assertions test the rendering rather than constants baked into the stub.
    # `active_filters` in particular is the authoritative record of the
    # conditions the builder actually emitted, and the page now reads it, so a
    # stub that always returned [] would make those paths untestable.
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    allowed = {c["column_name"] for c in columns if c.get("is_sortable")}
    requested = api_main._portal_database_first_param(parsed, "sort", "")
    resolved_sort = requested if requested in allowed else "trip_date"
    resolved_dir = "asc" if api_main._portal_database_first_param(parsed, "direction", "") == "asc" else "desc"
    _conditions, _values, applied_filters, _error, filter_entries = api_main._build_portal_database_filter_conditions(
        dataset, columns, parsed
    )
    state = {
        "sort": resolved_sort,
        "direction": resolved_dir,
        "active_filters": applied_filters,
        "filter_entries": filter_entries,
    }
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (total, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows",
                                              lambda d, c, p, limit, offset, display_columns=None: (rows, state, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    if unfiltered is not None:
        patches.append(("_count_portal_database_rows_unfiltered",
                        _patch("_count_portal_database_rows_unfiltered", lambda d, c: (unfiltered, None))))
    try:
        return _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query)))
    finally:
        _restore(patches)


def _cell(value, data_type="text"):
    return api_main._portal_database_cell_html(value, {"data_type": data_type})


# ---------------------------------------------------------------------------
# 1. Table-first composition (DB-003 / DB-004)
# ---------------------------------------------------------------------------
def test_table_is_the_first_working_surface() -> None:
    html = _render()
    body = html.split("<body>", 1)[1]

    toolbar = body.index('class="db-toolbar"')
    table = body.index('class="db-table"')
    footer = body.index('class="db-footer"')
    secondary = body.index('class="db-secondary"')

    # Approved vertical composition: toolbar, table, footer — and the legacy
    # panels that used to precede the table now follow it.
    assert toolbar < table < footer < secondary, (toolbar, table, footer, secondary)

    # The pre-redesign stack put four bordered panels above the table. None of
    # them may sit between the context bar and the data any more.
    head = body[:table]
    assert "db-meta" not in head, "the dataset summary card must not precede the table"
    # S3 moved filtering into the toolbar's collapsed `Filtry` panel and the
    # column menus, so the toolbar legitimately carries filter markup now. What
    # must never come back is an EXPANDED form standing between the user and the
    # data: the panel ships closed (`DB-20`).
    assert "db-filter-group" not in head, "the pre-redesign filter form must not return"
    assert '<details class="db-filters" id="db-filters" data-db-filters>' in head, head[:2000]
    assert "db-filters\" id=\"db-filters\" data-db-filters open" not in head, "the filter panel must ship collapsed"
    assert "Background exports" not in head, "the export explainer must not precede the table"
    assert "lp-page-head" not in body, "a table-first page renders no in-page title block"

    # The sheet owns the remaining viewport height, so the table body scrolls
    # rather than the document.
    assert "lp-work-flush" in body, body
    assert "data-db-sheet" in body, body
    print("PASS: the table is the first and dominant element of the working area")


def test_client_and_dataset_context_stay_visible() -> None:
    html = _render()
    context = re.search(r'<div class="lp-context".*?</div>\s*</div>', html, re.S)
    assert context, "context bar must render"
    context_html = context.group(0)
    assert "Acme Logistics" in context_html, context_html
    assert "ACME_01" in context_html, context_html
    assert "Approved trips" in context_html, context_html
    # Read-only is a security fact, and it is stated as a word, not a colour.
    assert "TYLKO ODCZYT" in context_html, context_html
    assert "public.trips" in context_html, context_html
    assert "3 zatwierdzone kolumny" in context_html, context_html
    print("PASS: client, dataset, physical table and read-only state ride in the context bar")


def test_toolbar_and_footer_expose_only_current_capabilities() -> None:
    html = _render(query="filter__driver_name=Ali&op__driver_name=contains")
    toolbar = html[html.index('class="db-toolbar"'):html.index('class="db-table-viewport"')]

    assert "Filtry" in toolbar and '<span class="db-count-badge">1</span>' in toolbar, toolbar
    assert "Kolumny" in toolbar and "3/3" in toolbar, toolbar
    assert "Zwarta" in toolbar and "Wygodna" in toolbar, toolbar
    assert "Szukaj w" in toolbar, toolbar
    assert "sortowanie" in toolbar, toolbar

    # Future-stage controls must not appear merely to populate the toolbar.
    # Named column sets and saved views need server-side persistence and are
    # still deferred; the S5 column actions arrived and live inside the column
    # menus, so they are excluded from this toolbar-scoped check exactly as the
    # S4 distributions are.
    for future in ("Zestawy", "Zapisz jako widok", "Zapisz jako zestaw"):
        assert future not in html, f"{future} belongs to a later stage"
    for future in ("Rozkład wartości", "Wartości w kolumnie",
                   "Przypnij kolumnę", "Dopasuj szerokość do treści"):
        assert future not in toolbar, f"{future} belongs in the column menu, not the toolbar"

    footer = html[html.index('class="db-footer"'):]
    assert "wierszy na stronie" in footer, footer
    assert "Strona 1 z 1" in footer, footer
    for size in (25, 50, 100, 200, 500):
        assert f">{size}</a>" in footer, size
    print("PASS: toolbar and footer expose current capabilities only")


def test_sticky_header_and_first_column() -> None:
    html = _render()
    # Header cells are real <th scope="col"> and the first display column is the
    # pinned one.
    assert html.count('<th scope="col"') == 3, html
    # S5 replaced the positional rule with the pin model: the pinned column is
    # still the first displayed one by default, but it is now identified by
    # name and carries its cumulative sticky offset.
    assert '<th scope="col" data-db-column="trip_date" class="db-sticky-col db-pin-edge" style="left:0px"' in html, html
    assert 'data-db-column="trip_date" class="db-sticky-col db-pin-edge" style="left:0px"' in html, html
    # Exactly one sticky column per row while the default pin stands.
    body_row = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    assert body_row.count("db-sticky-col") == 1, body_row

    css = (REPO_ROOT / "api" / "static" / "css" / "data-grid.css").read_text(encoding="utf-8")
    assert "position: sticky" in css and "top: 0" in css, "header must stick to the table viewport"
    assert ".db-scroll-fade" in css, "the right-edge fade is a required affordance"
    assert "data-overflowing" in css, css
    print("PASS: sticky header, one pinned column, and the horizontal-overflow affordance")


def test_zero_rows_keeps_the_table_header() -> None:
    filtered = _render(query="filter__driver_name=Zzz&op__driver_name=contains", rows=[], total=0, unfiltered=48213)
    # Keeping the header is what separates "nothing matched" from "it broke".
    assert '<th scope="col"' in filtered, filtered
    assert "Brak wierszy dla aktywnych filtrów" in filtered, filtered
    assert "<tbody></tbody>" in filtered, filtered

    empty_dataset = _render(rows=[], total=0)
    assert '<th scope="col"' in empty_dataset, empty_dataset
    assert "Ten zbiór nie ma wierszy" in empty_dataset, empty_dataset
    # A genuinely empty dataset must not be described as filtered away.
    assert "Brak wierszy dla aktywnych filtrów" not in empty_dataset, empty_dataset
    print("PASS: both empty states keep the header and read differently")


# ---------------------------------------------------------------------------
# 2. Density
# ---------------------------------------------------------------------------
def test_density_default_options_and_precedence() -> None:
    # The approved default is Zwarta, and it is the server's choice when the URL
    # says nothing.
    density, locked = api_main._portal_database_density({})
    assert (density, locked) == ("compact", False), (density, locked)

    # An explicit URL value wins and is reported as locked, so a bookmarked or
    # shared link keeps showing what its author saw.
    for value in ("compact", "comfortable"):
        assert api_main._portal_database_density({"density": [value]}) == (value, True), value

    # A nonsense value falls back to the default rather than rendering nothing.
    assert api_main._portal_database_density({"density": ["huge"]}) == ("compact", False)

    default_html = _render()
    assert 'data-density="compact"' in default_html, default_html
    assert "data-density-locked" not in default_html, "an unpinned page must let the browser preference win"

    pinned = _render(query="density=comfortable")
    assert 'data-density="comfortable"' in pinned, pinned
    assert "data-density-locked" in pinned, pinned

    # Both options are always offered, each as a real link so the control works
    # without scripting.
    assert 'data-density-option="compact"' in default_html and 'data-density-option="comfortable"' in default_html
    assert 'aria-pressed="true"' in default_html and 'aria-pressed="false"' in default_html

    js = (REPO_ROOT / "api" / "static" / "js" / "data-grid.js").read_text(encoding="utf-8")
    assert "logplatform.database.density" in js, "density persists browser-locally"
    assert "data-density-locked" in js, "the script must honour an explicit URL density"
    assert "event.preventDefault()" in js, "switching density must not navigate or requery"
    for forbidden in ("location.reload", "location.assign", "fetch("):
        assert forbidden not in js, f"density must not {forbidden}"
    print("PASS: density defaults to Zwarta with URL > browser > default precedence")


def test_density_link_preserves_every_other_view_parameter() -> None:
    query = "filter__driver_name=Ali&op__driver_name=contains&search=foo&sort=driver_name&direction=asc&limit=200&page=3&cols=trip_date,driver_name"
    html = _render(query=query, unfiltered=9)
    link = re.search(r'href="([^"]+)" data-density-option="comfortable"', html)
    assert link, html
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(link.group(1).replace("&amp;", "&")).query)
    assert params["filter__driver_name"] == ["Ali"], params
    assert params["op__driver_name"] == ["contains"], params
    assert params["search"] == ["foo"], params
    assert params["sort"] == ["driver_name"], params
    assert params["direction"] == ["asc"], params
    assert params["limit"] == ["200"], params
    assert params["page"] == ["3"], params
    assert params["cols"] == ["trip_date", "driver_name"], params
    assert params["density"] == ["comfortable"], params
    print("PASS: the density fallback link preserves filters, search, sort, page, size and columns")


# ---------------------------------------------------------------------------
# 3. Filtered vs total counts
# ---------------------------------------------------------------------------
def test_counter_distinguishes_filtered_from_total() -> None:
    calls = []

    def counted(dataset, columns):
        calls.append((dataset.get("dataset_id"), tuple(c["column_name"] for c in columns)))
        return 48213, None

    old = _patch("_count_portal_database_rows_unfiltered", counted)
    try:
        filtered = _render(query="filter__driver_name=Ali&op__driver_name=contains", total=1274)
        filtered_calls = len(calls)
        calls.clear()
        searched = _render(query="search=foo", total=12)
        search_calls = len(calls)
        calls.clear()
        plain = _render(total=48213)
        plain_calls = len(calls)
    finally:
        api_main._count_portal_database_rows_unfiltered = old

    def counter_text(html: str) -> str:
        match = re.search(r'<span class="db-counter"[^>]*>(.*?)</span>\s*<span', html, re.S)
        assert match, html
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()

    assert counter_text(filtered) == f"1{NBSP}274 z 48{NBSP}213 wierszy", counter_text(filtered)
    assert counter_text(searched) == f"12 z 48{NBSP}213 wierszy", counter_text(searched)
    # Unfiltered, the two numbers are the same, so the counter states one value
    # and the second query would be pure waste.
    assert counter_text(plain) == f"48{NBSP}213 wierszy", counter_text(plain)
    assert filtered_calls == 1 and search_calls == 1, (filtered_calls, search_calls)
    assert plain_calls == 0, "an unfiltered view must not run a second count"

    # The extra count is scoped to the dataset already authorized for this
    # request, and to the same approved column set.
    assert calls == [], calls
    print("PASS: the counter states filtered vs total and costs one extra query only when filtered")


def test_unfiltered_count_reuses_the_authorized_query_path() -> None:
    seen = []

    def fake_count(dataset, columns, params):
        seen.append({"dataset": dataset.get("dataset_id"), "params": dict(params),
                     "columns": [c["column_name"] for c in columns]})
        return 48213, {}, None

    old = _patch("_count_portal_database_rows", fake_count)
    try:
        total, error = api_main._count_portal_database_rows_unfiltered(_dataset(), _visible_columns())
    finally:
        api_main._count_portal_database_rows = old

    assert (total, error) == (48213, None), (total, error)
    assert len(seen) == 1, seen
    # No filters, the same dataset, and the same approved columns: the second
    # count cannot reach another dataset or widen the column set.
    assert seen[0]["params"] == {}, seen
    assert seen[0]["dataset"] == DATASET_ID, seen
    assert seen[0]["columns"] == ["trip_date", "driver_name", "distance_km"], seen
    assert "internal_secret" not in seen[0]["columns"], seen

    # A failing total must not take down a page whose rows loaded.
    def boom(dataset, columns, params):
        raise RuntimeError("client database unreachable")

    old = _patch("_count_portal_database_rows", boom)
    try:
        total, error = api_main._count_portal_database_rows_unfiltered(_dataset(), _visible_columns())
    finally:
        api_main._count_portal_database_rows = old
    assert error is not None and total == 0, (total, error)
    print("PASS: the total count reuses the authorized read-only path and fails soft")


# ---------------------------------------------------------------------------
# 4. Typed cell rendering
# ---------------------------------------------------------------------------
def test_null_and_empty_string_are_visibly_distinct() -> None:
    """The mandatory defect correction.

    The pre-redesign renderer sent both through one str() path and printed the
    same em dash, so a missing value and a present-but-empty one were
    indistinguishable. That makes data verification unreliable.
    """
    null_cell = _cell(None)
    blank_cell = _cell("")

    assert null_cell != blank_cell, (null_cell, blank_cell)
    assert "brak wartości" in null_cell and "db-v-null" in null_cell, null_cell
    assert "pusty tekst" in blank_cell and "db-v-blank" in blank_cell, blank_cell
    assert "pusty tekst" not in null_cell, null_cell
    assert "brak wartości" not in blank_cell, blank_cell

    # Whitespace-only text is a third, distinct case: it is a real value and is
    # rendered as one, with its exact content preserved for copy.
    space_cell = _cell("   ")
    assert space_cell not in (null_cell, blank_cell), space_cell
    assert 'data-db-copy="   "' in space_cell, space_cell

    # And they stay distinct in a rendered page, not only in the helper.
    html = _render(rows=[{"trip_date": None, "driver_name": "", "distance_km": Decimal("0")}])
    assert "brak wartości" in html and "pusty tekst" in html, html
    print("PASS: NULL, empty string and whitespace render as three distinct markers")


def _cell_parts(value, data_type):
    """(visible text, data-db-copy, title) for one rendered cell.

    THE POINT OF SPLITTING THESE. This test used to assert substrings against
    the whole cell HTML, so `"2026-05-29"` for the `date` family was satisfied
    by the `title` and `data-db-copy` attributes while the visible text read
    `29.05.2026`. It would have passed with the visible rendering completely
    broken. Visible text and attributes are different promises to different
    consumers -- a reader, a clipboard, a tooltip -- and each is now asserted
    on its own.
    """

    import html as _htmllib

    cell = _cell(value, data_type)
    visible = _htmllib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
    copy = re.search(r'data-db-copy="([^"]*)"', cell)
    title = re.search(r'title="([^"]*)"', cell)
    return (visible,
            _htmllib.unescape(copy.group(1)) if copy else None,
            _htmllib.unescape(title.group(1)) if title else None,
            cell)


def test_cell_rendering_follows_the_value_contract() -> None:
    """Visible text, clipboard and tooltip, asserted separately per family.

    Since `UI-20260827-06` the clipboard carries what the cell DISPLAYS, so the
    two grids agree about what a copy produces. `title` keeps the raw source
    value, which is the only remaining place the timezone offset is visible.
    The two lossy families are the exception and still copy their source.
    """

    UUID = "f8e66c7e-80fb-43cd-8536-32ed6af16a8d"
    TS = datetime(2026, 5, 29, 10, 3, 7, tzinfo=timezone.utc)

    # (value, data_type, visible, data-db-copy, title, classes that must appear)
    checks = [
        # An integer DISPLAYS its grouping and COPIES without it: the separator
        # is U+00A0, which no spreadsheet parses (`UI-20260827-06a`).
        (48213, "integer", f"48{NBSP}213", "48213", "48213",
         ["db-v-integer", "db-num"]),
        (0, "integer", "0", "0", "0", ["db-v-zero"]),
        (-1234567, "bigint", f"-1{NBSP}234{NBSP}567", "-1234567",
         "-1234567", ["db-v-integer"]),
        (Decimal("28.40"), "numeric", "28,40", "28,40", "28.40", ["db-v-decimal"]),
        # A grouped decimal displays its grouping and copies without it, exactly
        # as an integer does; the decimal comma stays (`UI-20260827-07`).
        (Decimal("1234567.891"), "numeric", f"1{NBSP}234{NBSP}567,891",
         "1234567,891", "1234567.891", ["db-v-decimal"]),
        (True, "boolean", "TAK", "TAK", "True", ["db-badge-bool", "is-true"]),
        (False, "boolean", "NIE", "NIE", "False", ["db-badge-bool", "is-false"]),
        # The rendered time carries no offset; the tooltip still does.
        (TS, "timestamp with time zone", "29.05.2026 10:03:07",
         "29.05.2026 10:03:07", "2026-05-29T10:03:07+00:00", ["db-v-timestamp"]),
        (date(2026, 5, 29), "date", "29.05.2026", "29.05.2026", "2026-05-29",
         ["db-v-date"]),
        # LOSSY: the display is elided, so the copy stays the source.
        (UUID, "uuid", "7f03c4e1\u20260021", UUID, UUID, ["db-v-uuid"]),
        # LOSSY: the display states the shape, not the value.
        ({"a": 1, "b": 2, "c": 3, "d": 4}, "jsonb", "{ 4 pola }",
         '{"a": 1, "b": 2, "c": 3, "d": 4}', '{"a": 1, "b": 2, "c": 3, "d": 4}',
         ["db-v-json"]),
        ([1, 2, 3], "json", "[ 3 elementy ]", "[1, 2, 3]", "[1, 2, 3]", ["db-v-json"]),
        (list(range(12)), "json", "[ 12 elementów ]", None, None, ["db-v-json"]),
        ([1], "json", "[ 1 element ]", "[1]", "[1]", ["db-v-json"]),
        ("Kowalski", "text", "Kowalski", "Kowalski", "Kowalski", ["db-v-text"]),
    ]

    for value, data_type, visible, copy, title, classes in checks:
        got_visible, got_copy, got_title, cell = _cell_parts(value, data_type)
        assert got_visible == visible, (data_type, "VISIBLE", got_visible, visible)
        if copy is not None:
            assert got_copy == copy, (data_type, "COPY", got_copy, copy)
        if title is not None:
            assert got_title == title, (data_type, "TITLE", got_title, title)
        for cls in classes:
            assert cls in cell, (data_type, cls, cell)

    # THE CONTRACT, stated once rather than inferred from the table above:
    # the clipboard gets the displayed value.
    for value, data_type in ((Decimal("28.40"), "numeric"),
                             (True, "boolean"), (date(2026, 5, 29), "date"),
                             (TS, "timestamp"), ("Kowalski", "text")):
        got_visible, got_copy, _title, _cell_html = _cell_parts(value, data_type)
        assert got_copy == got_visible, (data_type, got_copy, got_visible)

    # ...in the form a Polish spreadsheet parses, which for a NUMBER means
    # without the grouping separator it displays. This fails if the separator
    # returns to the clipboard, for either numeric family.
    for value in (48213, 0, -1234567, 9000, 1234567):
        got_visible, got_copy, got_title, _cell_html = _cell_parts(value, "integer")
        assert NBSP not in got_copy, (value, "U+00A0 reached the clipboard", got_copy)
        assert " " not in got_copy, (value, "a space reached the clipboard", got_copy)
        assert got_copy == str(value), (value, got_copy)
        assert got_copy == got_title, (value, got_copy, got_title)
        assert int(got_copy) == value, (value, got_copy)

    for value, expected in ((Decimal("1234567.891"), "1234567,891"),
                            (Decimal("-9876543.21"), "-9876543,21"),
                            (Decimal("51.967419"), "51,967419"),
                            (Decimal("28.40"), "28,40")):
        got_visible, got_copy, _title, _cell_html = _cell_parts(value, "numeric")
        assert NBSP not in got_copy, (value, "U+00A0 reached the clipboard", got_copy)
        assert " " not in got_copy, (value, "a space reached the clipboard", got_copy)
        # The decimal comma survives: it is the mark, not the decoration.
        assert got_copy == expected, (value, got_copy, expected)
        assert Decimal(got_copy.replace(",", ".")) == value, (value, got_copy)

    # NOTHING ELSE may carry a grouping separator into a clipboard either.
    for value, data_type in ((TS, "timestamp"), (date(2026, 5, 29), "date"),
                             (True, "boolean"), ("Kowalski", "text"),
                             (UUID, "uuid"), ({"a": 1}, "jsonb")):
        _visible, got_copy, _title, _cell_html = _cell_parts(value, data_type)
        assert NBSP not in got_copy, (data_type, got_copy)

    # ...and the exception to it, equally explicit: a lossy display must never
    # put a truncated value on the clipboard.
    for value, data_type in ((UUID, "uuid"), ({"a": 1}, "jsonb")):
        got_visible, got_copy, got_title, _cell_html = _cell_parts(value, data_type)
        assert got_copy != got_visible, (data_type, "a lossy cell copied its display")
        assert got_copy == got_title, (data_type, got_copy, got_title)
        assert "\u2026" not in got_copy and "pola" not in got_copy, got_copy

    # A NULL and an empty string carry no copy attribute at all: there is
    # nothing to put on a clipboard, and "" would be indistinguishable.
    for value in (None, ""):
        _visible, got_copy, _title, _cell_html = _cell_parts(value, "text")
        assert got_copy is None, (value, got_copy)

    print("PASS: cells render per data family, and copy what they display")


def test_long_values_do_not_destroy_table_geometry() -> None:
    long_text = "x" * 4000
    cell = _cell(long_text, "text")
    # Truncation is a CSS concern so the full value stays retrievable; what the
    # renderer must guarantee is the hook and the intact value.
    assert "db-v" in cell and f'data-db-copy="{long_text}"' in cell, cell

    css = (REPO_ROOT / "api" / "static" / "css" / "data-grid.css").read_text(encoding="utf-8")
    assert "text-overflow: ellipsis" in css and "white-space: nowrap" in css, css
    # A row must never grow to fit content: fixed height is what makes vertical
    # scanning work.
    assert "height: var(--db-row-height)" in css, css
    assert "--lp-height-row-compact" in css and "--lp-height-row-comfortable" in css, css
    print("PASS: long values truncate without changing row height and stay retrievable")


def test_render_family_is_independent_of_filter_semantics() -> None:
    """Presentation families must not leak into query behaviour.

    ``_portal_database_type_family`` decides which operators a column offers and
    which columns the global search reaches. The renderer needs finer families
    (boolean, uuid, json, integer vs decimal) but must not change that function,
    or this stage would silently alter filtering.
    """
    for data_type, expected in [
        ("boolean", "boolean"), ("uuid", "uuid"), ("jsonb", "json"),
        ("integer", "integer"), ("bigint", "integer"), ("numeric(10,2)", "decimal"),
        ("double precision", "decimal"), ("timestamp with time zone", "timestamp"),
        ("date", "date"), ("text", "text"), ("character varying", "text"), ("", "text"),
    ]:
        assert api_main._portal_database_render_family({"data_type": data_type}) == expected, data_type

    # The query-semantics classifier stays independent. uuid and json still read
    # as text there, which preserves their operator sets and keeps them in the
    # global text search. Boolean is the one deliberate S3 reclassification: the
    # approved contract gives it a four-way control and no substring semantics,
    # so it became its own query family with its own operators.
    assert api_main._portal_database_type_family("uuid") == "text"
    assert api_main._portal_database_type_family("jsonb") == "text"
    assert api_main._portal_database_type_family("integer") == "numeric"
    assert api_main._portal_database_type_family("boolean") == "boolean"
    assert api_main._portal_database_allowed_operators({"data_type": "boolean"}) == ["is_true", "is_false", "blank"]
    print("PASS: render families are presentation-only and leave filter semantics unchanged")


# ---------------------------------------------------------------------------
# 5. Preserved behaviour and security
# ---------------------------------------------------------------------------
def test_hidden_columns_never_reach_the_page() -> None:
    # `cols` may only narrow the approved set, so an unapproved name supplied
    # there must not appear anywhere — not as a header, a cell, or a link.
    for query in ("", "cols=internal_secret", "cols=internal_secret,bogus"):
        html = _render(query=query)
        assert "internal_secret" not in html, (query, "hidden column name leaked")
        assert ">Hidden<" not in html, (query, "hidden column label leaked")

    # S3 normalizes a rejected `sort` out of generated links: the page now emits
    # the sort state the server actually validated. The invariant that mattered
    # before still holds too — the column never becomes a header, a cell, or a
    # named column.
    sorted_html = _render(query="sort=internal_secret")
    assert "internal_secret" not in sorted_html, "a rejected sort identifier must not propagate"
    assert ">Hidden<" not in sorted_html, sorted_html
    assert 'name="cols" value="internal_secret"' not in sorted_html, sorted_html
    headers = re.findall(r'<th scope="col"[^>]*>(.*?)</th>', sorted_html, re.S)
    assert len(headers) == 3, headers
    assert not any("Hidden" in h or "internal_secret" in h for h in headers), headers
    print("PASS: an unapproved column cannot be introduced through the URL")


def test_view_state_survives_the_restructure() -> None:
    query = "filter__driver_name=Ali&op__driver_name=contains&search=foo&sort=driver_name&direction=asc&limit=200&page=2"
    html = _render(query=query, total=1274, unfiltered=48213)

    # Sort state is carried semantically as well as visually.
    assert 'aria-sort="ascending"' in html, html
    assert "sortowanie Driver ↑" in html, html
    # The active filter is still represented exactly once as a chip, and the
    # search value is still in its own field.
    assert "db-chip" in html, html
    assert 'value="foo"' in html, html
    # Page size and page survive into the footer.
    assert "Strona 2 z 7" in html, html
    assert 'aria-current="true"' in html, html
    print("PASS: filters, search, sort, page and page size survive the restructure")


def test_permission_flags_still_gate_controls() -> None:
    no_filter = _render(_dataset(can_filter_rows=False))
    # `DB-62`: for a dataset with filtering disabled the controls are ABSENT,
    # not present-and-disabled and not replaced by an explanatory panel.
    assert "db-filter-panel" not in no_filter, no_filter
    assert "db-col-filter" not in no_filter, no_filter
    # With filtering off there is no search field and no Filtry button to offer.
    toolbar = no_filter[no_filter.index('class="db-toolbar"'):no_filter.index('class="db-table-viewport"')]
    assert "db-toolbar-search" not in toolbar, toolbar
    assert ">Filtry" not in toolbar, toolbar

    no_export = _render(_dataset(can_export_rows=False))
    # Approved stage S9 replaced the last English sentence here with the keyed
    # Polish copy; the capability semantics are unchanged.
    assert "Tylko podgl\u0105d" in no_export, no_export
    assert "Export is not enabled" not in no_export, no_export
    assert "db-export-form" not in no_export, no_export

    with_export = _render(_dataset(can_export_rows=True))
    assert "db-export-form" in with_export, with_export
    print("PASS: can_filter_rows and can_export_rows still gate their controls")


def test_page_uses_the_shared_asset_foundation() -> None:
    html = _render()
    # The module asks for its assets through the shared mechanism rather than
    # inlining another <style>/<script> blob.
    assert "/static/css/data-grid.css?v=" in html, html
    assert "/static/js/data-grid.js?v=" in html, html
    assert "/static/css/tokens.css?v=" in html, html
    assert not re.search(r"<style>", html), "the module must not inline a stylesheet"
    assert not hasattr(api_main, "PORTAL_DATABASE_BROWSER_CSS"), "the legacy inline CSS blob must be retired"
    assert not hasattr(api_main, "PORTAL_DATABASE_BROWSER_SCRIPTS"), "the legacy inline script blob must be retired"

    css = (REPO_ROOT / "api" / "static" / "css" / "data-grid.css").read_text(encoding="utf-8")
    # The sheet is built on approved tokens, not the superseded dark-only values.
    for stale in ("#141d2a", "#c3cad6", "#0f141d", "#080b10", "#ff7300"):
        assert stale not in css, f"superseded literal {stale} in data-grid.css"
    print("PASS: the row sheet uses the shared static asset foundation")


def test_accessibility_of_the_data_sheet() -> None:
    html = _render()
    assert "<thead>" in html and "<tbody>" in html, html
    assert html.count('<th scope="col"') == 3, html
    # Sort state is exposed to assistive technology on every column, sorted or
    # not, and stated in words in the toolbar.
    assert html.count("aria-sort=") == 3, html
    assert 'aria-sort="descending"' in html, html
    assert 'aria-sort="none"' in html, html
    assert "sortowanie" in html, html
    # Named groups for the toolbar, the density control and the footer.
    assert 'aria-label="Narzędzia tabeli"' in html, html
    assert 'aria-label="Gęstość wierszy"' in html, html
    assert 'aria-label="Stronicowanie"' in html, html
    # Density options are keyboard-operable links carrying pressed state.
    assert 'aria-pressed="true"' in html and 'aria-pressed="false"' in html, html
    # Pagination arrows are icon-only and therefore need accessible names.
    paged = _render(query="page=2", total=500)
    assert 'aria-label="Poprzednia strona"' in paged, paged
    assert 'aria-label="Następna strona"' in paged, paged
    # The result counter is a live region: it is the primary feedback that a
    # query changed.
    assert 'class="db-counter" role="status"' in html, html
    # Colour is never the only carrier of the boolean state.
    assert "TAK" in _cell(True, "boolean"), "boolean badges carry a word"
    print("PASS: data-sheet accessibility foundation")


def test_wide_dataset_and_narrowed_columns_render() -> None:
    wide_columns = [_col(f"c{i}", f"Column {i}", "text", display_order=i) for i in range(40)]
    wide_row = {f"c{i}": f"value {i}" for i in range(40)}
    wide = _render(columns=wide_columns, rows=[wide_row])
    assert wide.count('<th scope="col"') == 40, wide.count('<th scope="col"')
    assert wide.count("db-sticky-col") >= 2, "header and body cell both pinned"
    assert ">Kolumny <span class=\"lp-mono\">40/40</span>" in wide, wide

    narrowed = _render(query="cols=driver_name")
    assert ">Kolumny <span class=\"lp-mono\">1/3</span>" in narrowed, narrowed
    assert narrowed.count('<th scope="col"') == 1, narrowed
    print("PASS: wide datasets and narrowed column selections both render")


def test_sheet_geometry_matches_the_acceptance_criteria() -> None:
    """DB-1 and DB-5, computed from the tokens rather than eyeballed.

    The bands are all token-driven, so the geometry is deterministic and a
    regression in any one token surfaces here instead of in a screenshot.
    """
    tokens = (REPO_ROOT / "api" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")

    def token(name: str) -> int:
        match = re.search(rf"{name}:\s*(\d+)px", tokens)
        assert match, name
        return int(match.group(1))

    appbar, context = token("--lp-height-appbar"), token("--lp-height-context-bar")
    toolbar, footer = token("--lp-height-toolbar"), token("--lp-height-footer")
    header = token("--lp-height-row-header")
    compact, comfortable = token("--lp-height-row-compact"), token("--lp-height-row-comfortable")

    assert (appbar, context, toolbar, footer, header) == (56, 64, 48, 44, 36)
    assert (compact, comfortable) == (32, 40)

    # DB-1: the table header starts 168 px from the top and nothing scrolls
    # above it.
    assert appbar + context + toolbar == 168

    # DB-5: at least 26 data rows at Zwarta on a 1080 px viewport.
    body_height = 1080 - (appbar + context + toolbar) - footer - header
    assert body_height // compact >= 26, body_height // compact

    grid = (REPO_ROOT / "api" / "static" / "css" / "data-grid.css").read_text(encoding="utf-8")
    # The frame is built from those tokens, not from hard-coded pixels.
    assert "min-height: var(--lp-height-toolbar)" in grid, grid
    assert "min-height: var(--lp-height-footer)" in grid, grid
    assert "height: var(--lp-height-row-header)" in grid, grid
    assert "100dvh - var(--lp-height-appbar) - var(--lp-height-context-bar)" in grid, grid
    # S5 replaced `min-width: 100%` with explicit per-column widths: the table
    # is laid out fixed and its declared width is the sum of the <colgroup>
    # widths (grid spec §1.7), so it still has no fixed cap of its own.
    table_rule = grid.split(".db-table {", 1)[1].split("}", 1)[0]
    assert "table-layout: fixed" in table_rule and "max-width" not in table_rule, table_rule
    print("PASS: sheet geometry satisfies DB-1 (168 px) and DB-5 (26 rows at Zwarta)")


# ---------------------------------------------------------------------------
# 6. Codex review corrections
# ---------------------------------------------------------------------------
def _run_density_harness(scenario: str, args: dict | None = None) -> dict:
    """Execute the shipped data-grid.js under a DOM/history stub."""
    import json
    import subprocess

    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_density_harness.js"), scenario]
    if args is not None:
        command.append(json.dumps(args))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_unknown_total_is_not_faked_as_the_filtered_count() -> None:
    """Finding A: a failed total count must degrade honestly.

    Substituting the filtered count produced `1 274 z 1 274 wierszy`, which
    asserts that the filters matched every row in the dataset. That is a false
    statement about the data, and it is the worst kind of failure for a tool
    whose job is verification: it looks like a successful answer.
    """
    def counter_text(html: str) -> str:
        match = re.search(r'<span class="db-counter"[^>]*>(.*?)</span>\s*<span', html, re.S)
        assert match, html
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()

    query = "filter__driver_name=Ali&op__driver_name=contains"

    # A. The secondary count succeeds: both real numbers are shown.
    ok = _render(query=query, total=1274, unfiltered=48213)
    assert counter_text(ok) == f"1{NBSP}274 z 48{NBSP}213 wierszy", counter_text(ok)

    # B. The secondary count fails.
    old = _patch("_count_portal_database_rows_unfiltered", lambda d, c: (0, "unavailable"))
    try:
        degraded = _render(query=query, total=1274)
    finally:
        api_main._count_portal_database_rows_unfiltered = old

    text = counter_text(degraded)
    # The page still renders and the match count survives.
    assert '<table class="db-table"' in degraded, degraded
    assert "db-toolbar" in degraded and "db-footer" in degraded, degraded
    assert f"1{NBSP}274" in text, text
    # The regression this test exists to prevent.
    assert text != f"1{NBSP}274 z 1{NBSP}274 wierszy", "fabricated total equal to the filtered count"
    assert f"z 1{NBSP}274 wierszy" not in text, text
    # The denominator is explicitly unknown, and says why.
    assert text == f"1{NBSP}274 z ? wierszy", text
    assert "db-counter-unknown" in degraded, degraded
    assert "Nie udało się ustalić liczby wszystkich wierszy" in degraded, degraded
    # A failed informational count must not become a page error.
    assert "portal-error-state" not in degraded, degraded
    print("PASS: an unavailable dataset total renders as unknown, never as the filtered count")


def test_no_op_search_does_not_trigger_the_total_count() -> None:
    """Finding B: the extra count follows the validated query, not raw input.

    A dataset with no approved searchable text columns cannot apply a search
    condition, so `search=foo` changes nothing. Treating it as filtering both
    charges a needless query and mislabels the unfiltered result as filtered.
    """
    calls = []
    old = _patch("_count_portal_database_rows_unfiltered",
                 lambda d, c: (calls.append(1), (48213, None))[1])
    try:
        # No text column is filterable, so the builder emits no search condition.
        no_text = [
            _col("trip_date", "Trip date", "date", is_filterable=True),
            _col("distance_km", "Distance", "numeric", is_filterable=True, display_order=30),
        ]
        parsed = api_main._portal_database_query_params(_FakeRequest(query="search=foo"))
        assert api_main._portal_database_search_columns(no_text) == [], "precondition: nothing to search"
        _c, _v, applied, _e, _entries = api_main._build_portal_database_filter_conditions(_dataset(), no_text, parsed)
        assert applied == [], f"a no-op search must apply no condition, got {applied}"

        html = _render(query="search=foo", columns=no_text, total=3)
        noop_calls = len(calls)
        calls.clear()

        # The same raw input against a dataset that CAN search it is real filtering.
        searchable = _visible_columns()
        assert api_main._portal_database_search_columns(searchable), "precondition: something to search"
        real = _render(query="search=foo", columns=searchable, total=3)
        real_calls = len(calls)
    finally:
        api_main._count_portal_database_rows_unfiltered = old

    assert noop_calls == 0, "a no-op search must not trigger the dataset total query"
    assert real_calls == 1, "a genuinely applied search must obtain the dataset total"

    # And the no-op view presents as unfiltered: one count, no "n z m".
    counter = re.search(r'<span class="db-counter"[^>]*>(.*?)</span>', html, re.S).group(1)
    assert " z " not in re.sub(r"<[^>]+>", "", counter), counter
    real_counter = re.search(r'<span class="db-counter"[^>]*>(.*?)</span>', real, re.S).group(1)
    assert " z " in re.sub(r"<[^>]+>", "", real_counter), real_counter

    # The user's text is still theirs to clear even though it applied nothing.
    assert 'value="foo"' in html, html
    assert "Wyczyść wszystkie" in html, "typed input must stay clearable"
    print("PASS: only a validated, applied search triggers the dataset total query")


def test_zero_rows_after_a_no_op_search_reads_as_an_empty_dataset() -> None:
    """The empty state follows the same applied-state rule.

    With no condition applied, an empty result means the dataset is empty — not
    that a filter emptied it — so it must not offer to remove a filter that did
    nothing.
    """
    no_text = [
        _col("trip_date", "Trip date", "date", is_filterable=True),
        _col("distance_km", "Distance", "numeric", is_filterable=True, display_order=30),
    ]
    html = _render(query="search=foo", columns=no_text, rows=[], total=0)
    assert "Ten zbiór nie ma wierszy" in html, html
    assert "Brak wierszy dla aktywnych filtrów" not in html, html
    assert '<th scope="col"' in html, "the header stays rendered"
    print("PASS: an empty result under a no-op search reads as an empty dataset")


def test_density_click_syncs_the_url_without_navigating() -> None:
    """Finding C: the address bar must not contradict the table.

    Because a valid URL density legitimately outranks the stored preference, a
    switch that updated only the DOM was silently undone by a reload.
    """
    result = _run_density_harness("url-sync")
    before, after = result["before"], result["after"]

    assert before["dom"] == "comfortable", before
    assert after["dom"] == "compact", after
    assert after["stored"] == "compact", after
    assert after["pressed"] == ["true", "false"], after

    # The URL now describes what is on screen, and everything else survived.
    assert "density=compact" in after["url"], after
    assert "density=comfortable" not in after["url"], after
    assert "filter__driver_name=Ali" in after["url"], after
    assert "page=3" in after["url"], after
    assert after["url"].startswith("/user/database/datasets/D1?"), after

    # Still no navigation and no requery.
    assert after["prevented"] is True, after
    assert after["navigated"] is False, after
    assert len(after["pushed"]) == 1, after
    print("PASS: a density switch rewrites the URL in place, preserving route and params")


def test_density_follows_browser_history() -> None:
    result = _run_density_harness("history")
    start, click = result["start"], result["afterClick"]
    back, forward = result["afterBack"], result["afterForward"]

    assert start["dom"] == "comfortable", start
    assert click["dom"] == "compact" and "density=compact" in click["url"], click

    # Back returns to the comfortable URL, and the DOM follows it even though
    # the stored preference is now compact: the URL is authoritative.
    assert "density=comfortable" in back["url"], back
    assert back["dom"] == "comfortable", "rendered density must follow the history URL"
    assert back["stored"] == "compact", "the explicit preference is kept for URLs without a density"
    assert back["pressed"] == ["false", "true"], back

    assert "density=compact" in forward["url"], forward
    assert forward["dom"] == "compact", forward

    for snapshot in (click, back, forward):
        assert snapshot["navigated"] is False, snapshot
    print("PASS: Back/Forward keep the rendered density synchronized with the history URL")


def test_density_precedence_is_unchanged() -> None:
    """The accepted precedence survives the history work: URL > browser > default."""
    url_wins = _run_density_harness(
        "precedence",
        {"rendered": "comfortable", "locked": True, "search": "?density=comfortable", "stored": "compact"},
    )["state"]
    assert url_wins["dom"] == "comfortable", url_wins

    stored_wins = _run_density_harness(
        "precedence", {"rendered": "compact", "locked": False, "search": "", "stored": "comfortable"}
    )["state"]
    assert stored_wins["dom"] == "comfortable", stored_wins

    default_wins = _run_density_harness(
        "precedence", {"rendered": "compact", "locked": False, "search": ""}
    )["state"]
    assert default_wins["dom"] == "compact", default_wins

    # A corrupted stored value falls back rather than rendering nothing.
    garbage = _run_density_harness(
        "precedence", {"rendered": "compact", "locked": False, "search": "", "stored": "neon"}
    )["state"]
    assert garbage["dom"] == "compact", garbage

    # And the server still refuses an invalid URL density.
    assert api_main._portal_database_density({"density": ["neon"]}) == ("compact", False)
    assert api_main._portal_database_density({"density": ["comfortable"]}) == ("comfortable", True)

    # The server states its own default so the script never has to infer it.
    assert 'data-density-default="compact"' in _render(), "the approved default must be explicit"
    print("PASS: URL > browser-local > default precedence is preserved")


def main() -> None:
    test_table_is_the_first_working_surface()
    test_client_and_dataset_context_stay_visible()
    test_toolbar_and_footer_expose_only_current_capabilities()
    test_sticky_header_and_first_column()
    test_zero_rows_keeps_the_table_header()
    test_density_default_options_and_precedence()
    test_density_link_preserves_every_other_view_parameter()
    test_counter_distinguishes_filtered_from_total()
    test_unfiltered_count_reuses_the_authorized_query_path()
    test_null_and_empty_string_are_visibly_distinct()
    test_cell_rendering_follows_the_value_contract()
    test_long_values_do_not_destroy_table_geometry()
    test_render_family_is_independent_of_filter_semantics()
    test_hidden_columns_never_reach_the_page()
    test_view_state_survives_the_restructure()
    test_permission_flags_still_gate_controls()
    test_page_uses_the_shared_asset_foundation()
    test_accessibility_of_the_data_sheet()
    test_wide_dataset_and_narrowed_columns_render()
    test_sheet_geometry_matches_the_acceptance_criteria()
    test_unknown_total_is_not_faked_as_the_filtered_count()
    test_no_op_search_does_not_trigger_the_total_count()
    test_zero_rows_after_a_no_op_search_reads_as_an_empty_dataset()
    test_density_click_syncs_the_url_without_navigating()
    test_density_follows_browser_history()
    test_density_precedence_is_unchanged()
    print("\nALL PASS: Database Explorer table-first foundation")


if __name__ == "__main__":
    main()
