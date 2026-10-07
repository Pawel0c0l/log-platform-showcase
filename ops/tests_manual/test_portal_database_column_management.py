#!/usr/bin/env python3
"""Database Explorer column management and URL state (approved stage S5).

Covers the approved `DB-008` column-management model and the URL contract that
makes a configured sheet reproducible: visibility, order, widths, autofit,
pinning, the column-layout reset, and the validation that keeps every one of
them inside the approved-column boundary.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— ``TABLE_AND_DATA_GRID_SPEC.md`` §1.3, §1.7, §5.2, §5.3, ``INTERACTION_SPEC.md``
§4, ``ACCESSIBILITY_SPEC.md`` §4, criteria ``DB-26``–``DB-29`` and ``DB-34``.

Query semantics, authorization and aggregates belong to the phase2/S3/S4 suites;
they are asserted here only where this stage could have weakened them.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_column_management.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from datetime import date, datetime, timezone
from decimal import Decimal
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
ROUTE = f"/user/database/datasets/{DATASET_ID}"


class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query=""):
        self.url = _FakeUrl(ROUTE, query)
        self.cookies = {}


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": None,
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": False, "display_order": 10,
    }
    column.update(extra)
    return column


def _columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
        _col("internal_secret", "Ukryta", "text", is_visible=False, is_filterable=False,
             is_sortable=False, display_order=50),
    ]


def _visible_columns():
    return [c for c in _columns() if c["is_visible"]]


def _rows(count=3):
    out = []
    for index in range(count):
        out.append({
            "trip_start": datetime(2026, 5, 28, 7, 30, tzinfo=timezone.utc),
            "driver_name": "Kowalski" if index else "A",
            "distance_km": Decimal("128.40"),
            "is_billable": True,
        })
    return out


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


_SQL_LOG: list[str] = []


def _render(*, query="", rows=None, columns=None, dataset=None, total=3):
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _visible_columns()
    rows = _rows() if rows is None else rows
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    _conditions, _values, applied, _error, entries = api_main._build_portal_database_filter_conditions(
        dataset, columns, parsed
    )
    allowed = {c["column_name"] for c in columns if c.get("is_sortable")}
    requested = api_main._portal_database_first_param(parsed, "sort", "")
    state = {
        "sort": requested if requested in allowed else "trip_start",
        "direction": "asc" if api_main._portal_database_first_param(parsed, "direction", "") == "asc" else "desc",
        "active_filters": applied,
        "filter_entries": entries,
    }

    def _count(d, c, p):
        _SQL_LOG.append("count")
        return (total, state, None)

    def _list(d, c, p, limit, offset, display_columns=None):
        _SQL_LOG.append("select")
        return (rows, state, None)

    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", _count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", _list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)
    return response.body.decode("utf-8")


def _colgroup(html: str) -> list[tuple[str, int]]:
    return [
        (name, int(width))
        for name, width in re.findall(r'<col data-db-column="([^"]+)" style="width:(\d+)px"', html)
    ]


def _order(html: str) -> list[str]:
    return [name for name, _ in _colgroup(html)]


def _header_order(html: str) -> list[str]:
    head = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    return re.findall(r'<th scope="col" data-db-column="([^"]+)"', head)


def _body_order(html: str) -> list[list[str]]:
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    return [re.findall(r'<td data-db-column="([^"]+)"', row) for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)]


def _pins(html: str) -> list[tuple[str, int]]:
    head = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    return [
        (name, int(left))
        for name, _classes, left in re.findall(
            r'<th scope="col" data-db-column="([^"]+)" class="([^"]*db-sticky-col[^"]*)" style="left:(\d+)px"', head
        )
    ]


def _link(html: str, label: str) -> str:
    match = re.search(r'href="([^"]+)"[^>]*>' + re.escape(label), html)
    assert match, f"missing link: {label}"
    return match.group(1).replace("&amp;", "&")


def _query(url: str) -> dict:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def _warnings(html: str) -> list[str]:
    """Inline panel warnings only — never the script's string catalogue."""
    return [
        re.sub(r"<[^>]*>", "", body)
        for body in re.findall(r'<p class="db-cols-warning"[^>]*>(.*?)</p>', html, re.S)
    ]


def _refusals(html: str) -> list[str]:
    return [
        re.sub(r"<[^>]*>", "", body)
        for body in re.findall(r'<p class="db-col-note db-col-refusal"[^>]*>(.*?)</p>', html, re.S)
    ]


def _generated_state(html: str) -> dict:
    """Query state of an arbitrary generated link, i.e. what the page propagates."""
    match = re.search(r'href="(/user/database/datasets/[^"]*density=compact[^"]*)"', html)
    assert match, "no generated density link on the page"
    return _query(match.group(1).replace("&amp;", "&"))


# ===========================================================================
# 1. Visibility (DB-26, DB-28)
# ===========================================================================
def test_panel_lists_every_approved_column_with_its_controls() -> None:
    html = _render()
    panel = html[html.index('id="db-columns"'):html.index("</details>", html.index('id="db-columns"'))]

    for column in _visible_columns():
        assert f'name="cols" value="{column["column_name"]}"' in panel, column["column_name"]
        assert f'name="colpin" value="{column["column_name"]}"' in panel, column["column_name"]
        assert f'data-db-column="{column["column_name"]}"' in panel, column["column_name"]
    # DB-26: a checkbox, a type badge and a drag handle per row.
    assert panel.count("db-cols-type") == 4, panel
    assert panel.count("data-db-handle") == 4, panel
    assert "Zmień kolejność kolumny Kierowca" in panel, panel

    # The approved-column boundary: a catalog column the admin did not make
    # visible is not offered and does not appear at all.
    assert "internal_secret" not in html, "an unapproved column must never be selectable"
    print("PASS: DB-008 lists every approved column with visibility, type, pin and reorder controls")


def test_hiding_and_showing_columns_preserves_every_other_state() -> None:
    query = "cols=trip_start&cols=driver_name&search=abc&sort=distance_km&direction=asc&limit=50&density=comfortable"
    html = _render(query=query)
    assert _order(html) == ["trip_start", "driver_name"], _order(html)

    # `Ukryj kolumnę` narrows through `cols` and nothing else moves. The first
    # such link on the page belongs to the first displayed column.
    hide = _link(html, "Ukryj kolumnę")
    q = _query(hide)
    assert q["cols"] == ["driver_name"], q
    for key, value in (("search", ["abc"]), ("sort", ["distance_km"]),
                       ("direction", ["asc"]), ("limit", ["50"]), ("density", ["comfortable"])):
        assert q[key] == value, (key, q)
    assert q["page"] == ["1"], q

    # Showing a column back is the same contract in the other direction.
    shown = _render(query="cols=trip_start&cols=driver_name&cols=distance_km")
    assert _order(shown) == ["trip_start", "driver_name", "distance_km"], _order(shown)
    print("PASS: hiding and showing approved columns preserves filters, search, sort, page size and density")


def test_the_last_visible_column_cannot_be_hidden() -> None:
    html = _render(query="cols=driver_name")
    assert _order(html) == ["driver_name"], _order(html)
    # DB-28 through the column menu: the action is absent, not disabled.
    assert "Ukryj kolumnę" not in html, "the only visible column must not offer to hide itself"

    # DB-28 through the panel: an explicit empty selection is refused inline and
    # the approved set is NOT silently restored as "no narrowing requested".
    # A submit that did keep a column carries no refusal.
    kept = _render(query="cols=driver_name&colsel=1")
    assert _warnings(kept) == [], _warnings(kept)
    empty = _render(query="colsel=1")
    assert _warnings(empty) == ["Co najmniej jedna kolumna musi pozostać widoczna."], _warnings(empty)
    blank_only = _render(query="colsel=1&cols=")
    assert _warnings(blank_only) == ["Co najmniej jedna kolumna musi pozostać widoczna."], _warnings(blank_only)
    # The panel opens itself on refusal so the user can correct the selection.
    assert 'data-db-columns open' in empty, empty
    print("PASS: at least one data column always stays visible (DB-28)")


def test_repeated_cols_urls_keep_working_and_survive_clear_all() -> None:
    html = _render(query="cols=driver_name&cols=distance_km&filter__driver_name=Kowal&op__driver_name=contains")
    assert _order(html) == ["driver_name", "distance_km"], _order(html)

    clear = _link(html, "Wyczyść wszystkie")
    q = _query(clear)
    # The S3 correction: every repeated value survives, not just the last.
    assert q["cols"] == ["driver_name", "distance_km"], q
    assert "filter__driver_name" not in q, q
    print("PASS: repeated `cols` links still work and survive Wyczyść wszystkie")


# ===========================================================================
# 2. Order (DB-27)
# ===========================================================================
def test_column_order_is_user_controlled_and_reproducible_from_the_url() -> None:
    default = _order(_render())
    assert default == ["trip_start", "driver_name", "distance_km", "is_billable"], default

    html = _render(query="colpin=&colorder=is_billable,distance_km")
    assert _order(html) == ["is_billable", "distance_km", "trip_start", "driver_name"], _order(html)
    # A round trip through the canonical URL reproduces exactly that order.
    generated = _generated_state(html)
    again = _render(query="colpin=&colorder=" + generated["colorder"][0])
    assert _order(again) == _order(html), (_order(again), _order(html))
    print("PASS: user-defined column order applies and round-trips through the URL (DB-27, DB-34)")


def test_reorder_moves_headers_colgroup_and_every_body_cell_together() -> None:
    html = _render(query="colpin=&colorder=distance_km,is_billable", rows=_rows(3))
    expected = ["distance_km", "is_billable", "trip_start", "driver_name"]
    assert _order(html) == expected, _order(html)
    assert _header_order(html) == expected, _header_order(html)
    for row in _body_order(html):
        assert row == expected, row
    print("PASS: reordering moves the colgroup, the header and every body cell identically")


def test_filters_and_distributions_stay_attached_to_their_own_column_after_reorder() -> None:
    html = _render(
        query="colpin=&colorder=is_billable,driver_name&filter__driver_name=Kowal&op__driver_name=contains"
    )
    head = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    cells = re.findall(r'<th scope="col" data-db-column="([^"]+)".*?</th>', head, re.S)
    assert cells[0] == "is_billable", cells

    for name in ("driver_name", "distance_km", "trip_start"):
        cell = re.search(
            r'<th scope="col" data-db-column="' + name + r'".*?</th>', head, re.S
        ).group(0)
        # S3: the menu, its filter form and its clear link target this column.
        assert f'data-db-col-menu data-db-column="{name}"' in cell, cell
        # S4: the distribution container is keyed on the same identity and its
        # endpoint asks for that column and no other.
        dist = re.search(r'data-db-distribution-url="([^"]+)"', cell)
        if dist:
            assert _query(dist.group(1).replace("&amp;", "&"))["column"] == [name], dist.group(1)
            assert f'data-db-column="{name}"' in cell, cell

    # The moved, filtered column keeps its own filter marker and no other does.
    filtered = re.findall(r'<th scope="col" data-db-column="([^"]+)" class="[^"]*db-col-filtered', head)
    assert filtered == ["driver_name"], filtered
    print("PASS: filter and distribution controls stay attached to their own column after reorder")


def test_malformed_order_state_is_canonicalized_without_widening_anything() -> None:
    html = _render(query=(
        "colpin=&colorder=internal_secret,nope,,distance_km,distance_km,%20trip_start%20"
    ))
    # Unknown and unapproved names are dropped, duplicates collapse to the first
    # occurrence, and the fill rule appends every unnamed visible column.
    assert _order(html) == ["distance_km", "trip_start", "driver_name", "is_billable"], _order(html)
    assert "internal_secret" not in html, "an unapproved identifier must never surface"
    assert "nope" not in html, "a rejected identifier must not be echoed into the page"
    generated = _generated_state(html)
    assert generated["colorder"] == ["distance_km"], generated
    print("PASS: unknown, unapproved and duplicate order identifiers canonicalize safely")


def test_order_survives_a_hidden_column_and_reorders_the_remainder() -> None:
    html = _render(query="colpin=&cols=trip_start&cols=distance_km&colorder=is_billable,distance_km")
    assert _order(html) == ["distance_km", "trip_start"], _order(html)
    print("PASS: an order naming a hidden column still orders the visible remainder")


def test_the_order_representation_is_compact() -> None:
    columns = [_col(f"c{i:02d}", f"K {i}", "text", display_order=i) for i in range(42)]
    moved = ["c41"] + [f"c{i:02d}" for i in range(41)]
    compact = api_main._portal_database_compact_column_order(moved, columns)
    assert compact == ["c41"], compact
    assert [str(c["column_name"]) for c in api_main._portal_database_fill_column_order(compact, columns)] == moved
    print("PASS: moving one column costs one token in the URL, not the whole permutation")


# ===========================================================================
# 3. Widths (DB-29)
# ===========================================================================
def test_default_widths_come_from_the_data_family() -> None:
    widths = dict(_colgroup(_render()))
    assert widths["distance_km"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["decimal"], widths
    assert widths["is_billable"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["boolean"], widths
    assert widths["driver_name"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["text"], widths
    assert widths["trip_start"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["timestamp"], widths
    # Grid spec §1.7: the declared table width is the sum of the column widths.
    declared = int(re.search(r'<table class="db-table" style="width:(\d+)px"', _render()).group(1))
    assert declared == sum(widths.values()), (declared, widths)
    # No default width is serialized: it is reconstructible.
    assert "colw" not in _generated_state(_render()), _generated_state(_render())
    print("PASS: default widths derive from the data family and are never serialized")


def test_manual_width_applies_clamps_and_round_trips() -> None:
    html = _render(query="colw=driver_name:320")
    assert dict(_colgroup(html))["driver_name"] == 320, _colgroup(html)
    assert _generated_state(html)["colw"] == ["driver_name:320"], _generated_state(html)

    minimum = api_main.PORTAL_DATABASE_COLUMN_MIN_WIDTH_PX
    maximum = api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX
    assert dict(_colgroup(_render(query="colw=driver_name:1")))["driver_name"] == minimum
    assert dict(_colgroup(_render(query="colw=driver_name:100000")))["driver_name"] == maximum
    # A crafted value cannot produce unbounded CSS.
    huge = _render(query="colw=driver_name:999999999999999999999")
    assert dict(_colgroup(huge))["driver_name"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["text"], _colgroup(huge)
    print("PASS: manual widths apply, clamp to the approved bounds and round-trip")


def test_invalid_width_state_fails_safely() -> None:
    default_text = api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["text"]
    for query in (
        "colw=driver_name:-40",
        "colw=driver_name:0",
        "colw=driver_name:abc",
        "colw=driver_name:12.5",
        "colw=driver_name",
        "colw=:200",
        "colw=internal_secret:200",
        "colw=" + "x" * 5000,
    ):
        html = _render(query=query)
        widths = dict(_colgroup(html))
        assert widths["driver_name"] == default_text, (query, widths)
        assert "internal_secret" not in html, query
        assert "colw" not in _generated_state(html), (query, _generated_state(html))

    # A duplicate definition resolves to the first valid one, deterministically.
    duplicated = _render(query="colw=driver_name:120,driver_name:400")
    assert dict(_colgroup(duplicated))["driver_name"] == 120, _colgroup(duplicated)
    print("PASS: malformed, negative, absurd, unapproved and duplicated widths fail safely")


def test_width_survives_filtering_sorting_and_paging() -> None:
    html = _render(query="colw=driver_name:320&filter__driver_name=Kowal&op__driver_name=contains")
    for label in ("Sortuj rosnąco", "Następna strona", "Zwarta"):
        match = re.search(r'href="([^"]+)"[^>]*>' + re.escape(label), html)
        if not match:
            continue
        assert _query(match.group(1).replace("&amp;", "&")).get("colw") == ["driver_name:320"], label
    print("PASS: an explicit width rides through sort, page and density navigation")


def test_the_explicit_width_field_is_the_keyboard_equivalent() -> None:
    html = _render()
    # ACCESSIBILITY_SPEC §4: pointer resize has a keyboard equivalent, and it is
    # an explicit width entry in the column menu.
    assert 'name="colw__driver_name"' in html, html
    assert f'min="{api_main.PORTAL_DATABASE_COLUMN_MIN_WIDTH_PX}"' in html, html
    assert f'max="{api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX}"' in html, html

    applied = _render(query="colw__driver_name=288")
    assert dict(_colgroup(applied))["driver_name"] == 288, _colgroup(applied)
    # It merges into `colw` and is consumed, so the operation is idempotent and
    # the intent parameter does not accumulate in generated links.
    state = _generated_state(applied)
    assert state["colw"] == ["driver_name:288"], state
    assert "colw__driver_name" not in state, state

    # The same bounds apply, and an emptied field returns the family default.
    assert dict(_colgroup(_render(query="colw__driver_name=1")))["driver_name"] == api_main.PORTAL_DATABASE_COLUMN_MIN_WIDTH_PX
    cleared = _render(query="colw=driver_name:320&colw__driver_name=")
    assert dict(_colgroup(cleared))["driver_name"] == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["text"], _colgroup(cleared)

    # It cannot reach a column the catalog did not approve.
    sneaky = _render(query="colw__internal_secret=400")
    assert "internal_secret" not in sneaky, sneaky
    print("PASS: the explicit width field is a bounded, idempotent, approved-column-only keyboard path")


# ===========================================================================
# 4. Autofit (DB-29)
# ===========================================================================
def _autofit(column, rows):
    return api_main._portal_database_autofit_width(column, rows)


def test_autofit_fits_the_current_page_and_stays_bounded() -> None:
    column = _col("driver_name", "Kierowca", "text")
    header_only = _autofit(column, [])
    # An empty dataset still fits the header, and never less than the minimum.
    assert header_only >= api_main.PORTAL_DATABASE_COLUMN_MIN_WIDTH_PX, header_only
    normal = _autofit(column, [{"driver_name": "Kowalski-Nowak"}, {"driver_name": "Nowak"}])
    assert normal > header_only, (normal, header_only)

    long_value = _autofit(column, [{"driver_name": "A" * 40}])
    assert long_value > normal, (long_value, normal)
    # DB-29 bound: one extreme value must not produce an absurdly wide column.
    extreme = _autofit(column, [{"driver_name": "A" * 100000}])
    assert extreme == api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX, extreme

    # It measures what the cell RENDERS, not the value behind it: a structured
    # value shows its collapsed preview, so it does not fit the raw JSON.
    structured = _col("payload", "Dane", "jsonb")
    preview = _autofit(structured, [{"payload": {"a": 1, "b": 2, "c": 3, "d": "x" * 400}}])
    assert preview < api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX, preview
    print("PASS: autofit fits the rendered current page and stays inside the approved bounds")


def test_autofit_issues_no_query_and_writes_the_same_state_as_a_resize() -> None:
    _SQL_LOG.clear()
    html = _render(rows=_rows(3))
    baseline = list(_SQL_LOG)
    assert baseline == ["count", "select"], baseline

    fit = _link(html, "Dopasuj szerokość do treści")
    q = _query(fit)
    assert "colw" in q, q
    # The link is a plain layout URL: same parameter, same bounds, no aggregate
    # and no new endpoint.
    name, _, width = q["colw"][0].partition(":")
    assert name in {c["column_name"] for c in _visible_columns()}, q
    assert api_main.PORTAL_DATABASE_COLUMN_MIN_WIDTH_PX <= int(width) <= api_main.PORTAL_DATABASE_COLUMN_MAX_WIDTH_PX
    assert "/distribution" not in fit and "/export" not in fit, fit
    # Rendering the whole page with every autofit link computed cost exactly the
    # two statements the row browser already issued.
    assert list(_SQL_LOG) == baseline, _SQL_LOG
    print("PASS: autofit runs off the rendered page, issues no query and writes ordinary width state")


# ===========================================================================
# 5. Pinning (grid spec §1.3)
# ===========================================================================
def test_the_transitional_default_pins_the_first_displayed_column() -> None:
    html = _render()
    assert _pins(html) == [("trip_start", 0)], _pins(html)
    # It is derivable, so it is not serialized into every generated link.
    assert "colpin" not in _generated_state(html), _generated_state(html)
    # It follows the displayed order, not the catalog position.
    reordered = _render(query="colorder=is_billable")
    assert _pins(reordered) == [("is_billable", 0)], _pins(reordered)
    print("PASS: the transitional default pin follows the first displayed column by identity")


def test_pinning_unpinning_and_cumulative_offsets() -> None:
    pinned = _render(query="colpin=trip_start,driver_name")
    assert _pins(pinned) == [("trip_start", 0), ("driver_name", 168)], _pins(pinned)
    # Pinned columns are hoisted in pin order and never overlap: each offset is
    # the sum of the widths before it.
    widths = dict(_colgroup(pinned))
    assert _order(pinned)[:2] == ["trip_start", "driver_name"], _order(pinned)
    assert _pins(pinned)[1][1] == widths["trip_start"], (widths, _pins(pinned))
    # Only the last pin carries the region edge.
    head = re.search(r"<thead>(.*?)</thead>", pinned, re.S).group(1)
    assert head.count("db-pin-edge") == 1, head

    # Explicit "nothing pinned" is a real state and survives the round trip.
    unpinned = _render(query="colpin=")
    assert _pins(unpinned) == [], _pins(unpinned)
    assert _generated_state(unpinned)["colpin"] == [""], _generated_state(unpinned)

    # Pin order is preserved, not sorted.
    reversed_pins = _render(query="colpin=driver_name,trip_start")
    assert [name for name, _ in _pins(reversed_pins)] == ["driver_name", "trip_start"], _pins(reversed_pins)
    print("PASS: pin, unpin, pin order and cumulative sticky offsets are all identity-driven")


def test_pinning_survives_reorder_and_pins_reorder_with_the_user() -> None:
    html = _render(query="colpin=distance_km&colorder=is_billable,driver_name")
    assert _pins(html) == [("distance_km", 0)], _pins(html)
    assert _order(html) == ["distance_km", "is_billable", "driver_name", "trip_start"], _order(html)
    for row in _body_order(html):
        assert row == _order(html), row
    print("PASS: pin state targets column identity across a reorder")


def test_pinned_width_and_count_are_bounded() -> None:
    # Four wide columns: the pinned region stops before it eats the viewport.
    wide = [_col(f"w{i}", f"W {i}", "text", display_order=i) for i in range(6)]
    html = _render(query="colpin=w0,w1,w2,w3,w4,w5", columns=wide,
                   rows=[{f"w{i}": "x" for i in range(6)}])
    pinned = [name for name, _ in _pins(html)]
    assert len(pinned) <= api_main.PORTAL_DATABASE_MAX_PINNED_COLUMNS, pinned
    widths = dict(_colgroup(html))
    assert sum(widths[name] for name in pinned) <= api_main.PORTAL_DATABASE_MAX_PINNED_WIDTH_PX, pinned
    # The refusal is stated inline, not silent, and no other column's width changed.
    assert any("40%" in text for text in _warnings(html)), _warnings(html)
    assert all(width == api_main.PORTAL_DATABASE_COLUMN_DEFAULT_WIDTHS["text"] for width in widths.values()), widths
    # The column menu states the reason instead of offering a pin the resolver
    # would refuse.
    assert any("40%" in text for text in _refusals(html)), _refusals(html)
    print("PASS: the pinned region is bounded by count and by width, and the refusal is communicated")


def test_a_crafted_pin_cannot_reach_an_unapproved_or_hidden_column() -> None:
    for query in ("colpin=internal_secret", "colpin=nope,internal_secret",
                  "colpin=driver_name&cols=trip_start&cols=distance_km"):
        html = _render(query=query)
        pinned = [name for name, _ in _pins(html)]
        assert "internal_secret" not in pinned and "nope" not in pinned, (query, pinned)
        assert "internal_secret" not in html, query
        assert set(pinned) <= set(_order(html)), (query, pinned)
    # A duplicated pin identifier collapses rather than doubling an offset.
    duplicated = _render(query="colpin=driver_name,driver_name,trip_start")
    assert [name for name, _ in _pins(duplicated)] == ["driver_name", "trip_start"], _pins(duplicated)
    print("PASS: crafted pin state cannot pin an unapproved, unknown or hidden column")


# ===========================================================================
# 6. Reset (grid spec §5.3)
# ===========================================================================
def test_column_layout_reset_restores_only_the_column_domain() -> None:
    query = ("cols=trip_start&cols=driver_name&colorder=driver_name&colw=driver_name:320&colpin="
             "&search=abc&filter__driver_name=Kowal&op__driver_name=contains"
             "&sort=distance_km&direction=asc&limit=50&density=comfortable&page=3")
    html = _render(query=query)
    reset = _link(html, "Domyślne kolumny")
    q = _query(reset)
    for gone in ("cols", "colorder", "colw", "colpin"):
        assert gone not in q, (gone, q)
    for kept, value in (("search", ["abc"]), ("filter__driver_name", ["Kowal"]),
                        ("op__driver_name", ["contains"]), ("sort", ["distance_km"]),
                        ("direction", ["asc"]), ("limit", ["50"]), ("density", ["comfortable"])):
        assert q.get(kept) == value, (kept, q)
    assert q["page"] == ["1"], q
    print("PASS: Domyślne kolumny resets only the column layout and keeps filters, sort, density and page size")


def test_clear_all_filters_does_not_reset_the_column_layout() -> None:
    query = ("colorder=driver_name&colw=driver_name:320&colpin=distance_km"
             "&search=abc&filter__driver_name=Kowal&op__driver_name=contains")
    html = _render(query=query)
    clear = _link(html, "Wyczyść wszystkie")
    q = _query(clear)
    # The canonical order is the resolved one, with the pin hoisted to the front.
    assert q["colorder"] == ["distance_km,driver_name"], q
    assert q["colw"] == ["driver_name:320"], q
    assert q["colpin"] == ["distance_km"], q
    assert "search" not in q and "filter__driver_name" not in q, q
    print("PASS: Wyczyść wszystkie clears filters and search without touching the column layout")


def test_hidden_filtered_and_sorted_columns_keep_their_query_state() -> None:
    html = _render(query="cols=trip_start&filter__driver_name=Kowal&op__driver_name=contains"
                         "&sort=distance_km&direction=asc")
    assert _order(html) == ["trip_start"], _order(html)
    # Visibility and filtering are separate concerns: the chip stays, and it can
    # still be cleared.
    assert "db-chip" in html and "Kierowca" in html, html
    assert "Usuń filtr" in html, html
    # A valid sort on a hidden column is preserved and still stated in words.
    assert "sortowanie" in html and "Dystans" in html, html
    print("PASS: hiding a column neither clears its filter nor its sort")


# ===========================================================================
# 7. Security and query surface
# ===========================================================================
def test_layout_state_never_reaches_the_query_or_the_export() -> None:
    dataset = _dataset()
    columns = _visible_columns()
    query = "colorder=distance_km&colw=driver_name:320&colpin=driver_name&colsel=1&colpanel=1"
    params = api_main._portal_database_query_params(_FakeRequest(query=query))
    generated, values, state, error = api_main._build_portal_database_rows_query(
        dataset, columns, params, limit=10, offset=0
    )
    assert error is None, error
    for token in ("colorder", "colw", "colpin", "colsel", "colpanel", "320"):
        assert token not in generated, (token, generated)
    assert values == [10, 0], values

    # Exports are unchanged by presentation state.
    for url in (
        api_main._portal_database_export_url(DATASET_ID, params, format_name="csv"),
        api_main._portal_database_async_export_url(DATASET_ID, params),
    ):
        q = _query(url)
        for token in ("colorder", "colw", "colpin", "cols"):
            assert token not in q, (token, url)
    print("PASS: layout state reaches neither the SQL nor the export scope")


def test_column_layout_state_stays_in_the_url() -> None:
    """S5 owns the URL as the home of column layout, and still does.

    This test originally also asserted that the three S13 relations did not
    exist and that `db/` carried no diff. Approved stage S13 added them
    deliberately, so asserting their absence would now assert that an approved
    stage was not built. What S5 actually owns is unchanged and is what is
    checked here: layout lives in the URL contract, and named column sets are a
    reference to that contract rather than a second persistence model that
    bypasses it.
    """
    source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert "/columns" not in source or "def _portal_database_column_layout_url" in source
    # A column set is applied by REDIRECTING to a canonical sheet URL built from
    # the same layout parameters, so it cannot describe a layout the URL cannot.
    assert "def _portal_database_apply_column_set_response" in source
    assert "_portal_database_saved_layout_params" in source
    assert "PORTAL_DATABASE_LAYOUT_PARAM_KEYS" in source
    print("PASS: column layout state stays in the URL contract")


def test_no_later_stage_feature_is_present() -> None:
    html = _render()
    for future in ("Zapisz jako zestaw", "Zestawy", "Zapisz jako widok",
                   "is_row_identifier", "record_id"):
        assert future not in html, f"{future} belongs to a later stage"
    print("PASS: saved views, named column sets and row identity stay out of S5")


# ===========================================================================
# 8. Progressive enhancement and accessibility
# ===========================================================================
def test_every_column_management_action_works_without_scripting() -> None:
    html = _render()
    panel = html[html.index('id="db-columns"'):html.index("</details>", html.index('id="db-columns"'))]
    # Visibility, order and pin state all commit through one plain GET form.
    assert '<form class="db-columns-form" method="get"' in panel, panel
    assert 'data-db-move="up"' in panel and 'data-db-move="down"' in panel, panel
    assert panel.count("<a class=\"db-cols-move\"") >= 2, panel
    # Controls that would do nothing without the script ship hidden.
    for jsonly in ("db-cols-search", "db-cols-tabs"):
        assert re.search(jsonly + r'[^>]*hidden', panel), (jsonly, panel)
    assert re.search(r'data-db-handle[^>]*hidden', panel), panel
    # The row sheet still ships no inline script.
    assert "<script>" not in html, html
    print("PASS: visibility, order and pin state are all reachable with scripting unavailable")


def test_reorder_and_resize_expose_accessible_names_and_state() -> None:
    html = _render()
    assert "Zmień kolejność kolumny Dystans" in html, html
    assert "Przenieś kolumnę Dystans w górę" in html or "Przenieś kolumnę Dystans w dół" in html, html
    assert re.search(r'aria-label="Szerokość kolumny Dystans: \d+ pikseli', html), html
    assert 'data-db-cols-status' in html and 'aria-live="polite"' in html, html
    # A hidden column is not communicated by colour alone: its checkbox is
    # unchecked and its row carries an explicit state.
    partial = _render(query="cols=trip_start")
    assert 'data-db-state="hidden"' in partial, partial
    print("PASS: reorder, resize and hidden state carry accessible names and non-colour signals")


def test_move_links_are_bounded_and_stay_inside_the_approved_set() -> None:
    html = _render(query="colpin=")
    panel = html[html.index('id="db-columns"'):html.index("</details>", html.index('id="db-columns"'))]
    moves = re.findall(r'<a class="db-cols-move" href="([^"]+)"', panel)
    assert moves, panel
    for href in moves:
        q = _query(href.replace("&amp;", "&"))
        assert q["colpanel"] == ["1"], q
        for name in q.get("colorder", [""])[0].split(","):
            if name:
                assert name in {c["column_name"] for c in _visible_columns()}, name
    # The first row cannot move up and the last cannot move down.
    rows = re.findall(r'<li class="db-cols-row"[^>]*data-db-column="([^"]+)".*?</li>', panel, re.S)
    first = re.search(r'<li class="db-cols-row"[^>]*data-db-column="' + rows[0] + r'".*?</li>', panel, re.S).group(0)
    assert 'data-db-move="up"' not in first, first
    print("PASS: panel move links stay inside the approved set and respect the list bounds")


# ===========================================================================
# 9. URL length
# ===========================================================================
def test_worst_case_layout_url_stays_reasonable() -> None:
    columns = [_col(f"column_number_{i:02d}", f"Kolumna {i}", "text", display_order=i) for i in range(42)]
    names = [c["column_name"] for c in columns]
    reversed_names = list(reversed(names))
    widths = ",".join(f"{name}:{200 + i}" for i, name in enumerate(names))
    query = (
        "&".join(f"cols={name}" for name in names)
        + "&colorder=" + ",".join(reversed_names)
        + "&colw=" + widths
        + "&colpin=" + ",".join(names[:4])
    )
    html = _render(query=query, columns=columns, rows=[{name: "x" for name in names}])
    pinned = [name for name, _ in _pins(html)]
    # Only as many pins as the bounded pinned region admits are honoured.
    assert pinned == names[:len(pinned)] and pinned, pinned
    assert _order(html)[:len(pinned)] == pinned, _order(html)[:len(pinned)]
    generated = _generated_state(html)
    rebuilt = ROUTE + "?" + "&".join(
        f"{key}={value}" for key, values in generated.items() for value in values
    )
    print(f"       worst-case S5 URL length for 42 approved columns: {len(rebuilt)} characters")
    assert len(rebuilt) < 8000, len(rebuilt)
    print("PASS: a pathological 42-column layout URL stays well inside practical limits")


# ===========================================================================
# 10. Shipped script behaviour
# ===========================================================================
def _run_harness(scenario: str, args: dict | None = None) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_columns_harness.js"), scenario]
    if args is not None:
        command.append(json.dumps(args))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_script_applies_layout_in_place_without_requerying() -> None:
    result = _run_harness("panel-apply-order")
    assert result["navigated"] is False, result
    assert result["submitted"] is False, result
    assert result["fetches"] == 0, result
    assert result["reloads"] == 0, result
    # Staged until Zastosuj (INT §4): the panel list moved, the grid did not.
    assert result["staged"] == ["distance_km", "trip_start", "driver_name"], result
    assert result["beforeCommit"] == ["trip_start", "driver_name", "distance_km"], result
    assert result["headerOrder"] == ["distance_km", "trip_start", "driver_name"], result
    assert result["bodyOrder"] == [["distance_km", "trip_start", "driver_name"]], result
    assert result["history"] == ["push"], result
    assert "colorder=distance_km" in result["url"], result
    # The rest of the view state rides along untouched.
    assert "filter__driver_name=Kowal" in result["url"] and "sort=trip_start" in result["url"], result
    print("PASS: a committed reorder applies in place, pushes one history entry and issues no request")


def test_script_refuses_to_hide_every_column() -> None:
    result = _run_harness("panel-apply-empty")
    assert result["submitted"] is False, result
    assert result["warning"] == "Co najmniej jedna kolumna musi pozostać widoczna.", result
    assert result["history"] == [], result
    print("PASS: the script refuses an all-hidden selection inline (DB-28)")


def test_script_navigates_when_visibility_changes() -> None:
    result = _run_harness("panel-apply-visibility")
    # Revealing a column needs the server: the row SELECT changes.
    assert result["submitted"] is True, result
    assert result["history"] == [], result
    print("PASS: a visibility change still round-trips through the server")


def test_script_resize_commits_once_and_syncs_the_url() -> None:
    result = _run_harness("resize-drag")
    assert result["widths"]["driver_name"] == 260, result
    # Intermediate movement must not spam history.
    assert result["midHistory"] == 0, result
    assert result["history"] == ["push"], result
    assert "colw=driver_name%3A260" in result["url"] or "colw=driver_name:260" in result["url"], result
    assert result["fetches"] == 0 and result["reloads"] == 0, result

    bounded = _run_harness("resize-drag", {"delta": -5000})
    assert bounded["widths"]["driver_name"] == 64, bounded
    over = _run_harness("resize-drag", {"delta": 5000})
    assert over["widths"]["driver_name"] == 480, over
    print("PASS: pointer resize commits one history entry, syncs the URL and honours min/max")


def test_script_autofit_matches_the_server_and_queries_nothing() -> None:
    result = _run_harness("autofit")
    assert result["widths"]["driver_name"] == result["serverAutofit"], result
    assert result["fetches"] == 0, result
    assert result["history"] == ["push"], result
    assert "colw=driver_name" in result["url"].replace("%3A", ":"), result
    print("PASS: in-place autofit reproduces the server's value and issues no request")


def test_script_pin_toggle_syncs_and_enforces_the_viewport_rule() -> None:
    result = _run_harness("pin")
    assert result["pinned"] == ["trip_start", "driver_name"], result
    assert result["offsets"] == [0, 168], result
    assert result["history"] == ["push"], result
    assert "colpin=trip_start%2Cdriver_name" in result["url"] or "colpin=trip_start,driver_name" in result["url"], result

    refused = _run_harness("pin", {"viewport": 200})
    assert refused["pinned"] == ["trip_start"], refused
    assert refused["history"] == [], refused
    assert refused["refusal"], refused
    print("PASS: pinning syncs the URL, accumulates offsets and refuses past 40 % of the viewport")


def test_script_keyboard_resize_is_bounded_and_commits_once() -> None:
    result = _run_harness("resize-keyboard")
    # Three arrow presses move the width three steps and write one entry.
    assert result["duringBurst"]["widths"]["driver_name"] == 248, result
    assert result["duringBurst"]["history"] == [], result
    assert result["widths"]["driver_name"] == 248, result
    assert result["history"] == ["push"], result
    assert "colw=driver_name%3A248" in result["url"] or "colw=driver_name:248" in result["url"], result
    print("PASS: keyboard resize honours the same bounds and URL state as pointer resize")


def test_script_back_and_forward_restore_a_coherent_layout() -> None:
    result = _run_harness("history")
    assert result["afterBack"]["order"] == ["trip_start", "driver_name", "distance_km"], result
    assert result["afterBack"]["widths"]["driver_name"] == 200, result
    assert result["afterBack"]["pinned"] == ["trip_start"], result
    assert result["afterForward"]["order"] == ["trip_start", "distance_km", "driver_name"], result
    assert result["afterForward"]["widths"]["driver_name"] == 300, result
    assert result["afterForward"]["pinned"] == ["trip_start", "distance_km"], result
    assert result["fetches"] == 0 and result["reloads"] == 0, result
    print("PASS: Back and Forward restore order, widths and pins with no requery")


def test_script_reloads_when_history_crosses_a_visibility_change() -> None:
    result = _run_harness("history-visibility")
    assert result["reloadsAfterMatchingEntry"] == 0, result
    assert result["reloads"] == 1, result
    print("PASS: a history entry with a different column selection reloads instead of lying")


def test_script_ignores_unapproved_identifiers_in_history_state() -> None:
    result = _run_harness("history-hostile")
    assert result["order"] == ["distance_km", "trip_start", "driver_name"], result
    # The crafted identifiers may sit in the address bar the attacker wrote, but
    # they reach neither the rendered columns, nor the widths, nor the pins.
    rendered = {k: v for k, v in result.items() if k != "url"}
    assert "internal_secret" not in json.dumps(rendered), rendered
    assert "nope" not in json.dumps(rendered), rendered
    assert result["widths"]["driver_name"] == 200, result
    assert result["pinned"] == [], result
    print("PASS: crafted history state cannot introduce an unapproved column client-side")


def main() -> None:
    test_panel_lists_every_approved_column_with_its_controls()
    test_hiding_and_showing_columns_preserves_every_other_state()
    test_the_last_visible_column_cannot_be_hidden()
    test_repeated_cols_urls_keep_working_and_survive_clear_all()

    test_column_order_is_user_controlled_and_reproducible_from_the_url()
    test_reorder_moves_headers_colgroup_and_every_body_cell_together()
    test_filters_and_distributions_stay_attached_to_their_own_column_after_reorder()
    test_malformed_order_state_is_canonicalized_without_widening_anything()
    test_order_survives_a_hidden_column_and_reorders_the_remainder()
    test_the_order_representation_is_compact()

    test_default_widths_come_from_the_data_family()
    test_manual_width_applies_clamps_and_round_trips()
    test_invalid_width_state_fails_safely()
    test_width_survives_filtering_sorting_and_paging()
    test_the_explicit_width_field_is_the_keyboard_equivalent()

    test_autofit_fits_the_current_page_and_stays_bounded()
    test_autofit_issues_no_query_and_writes_the_same_state_as_a_resize()

    test_the_transitional_default_pins_the_first_displayed_column()
    test_pinning_unpinning_and_cumulative_offsets()
    test_pinning_survives_reorder_and_pins_reorder_with_the_user()
    test_pinned_width_and_count_are_bounded()
    test_a_crafted_pin_cannot_reach_an_unapproved_or_hidden_column()

    test_column_layout_reset_restores_only_the_column_domain()
    test_clear_all_filters_does_not_reset_the_column_layout()
    test_hidden_filtered_and_sorted_columns_keep_their_query_state()

    test_layout_state_never_reaches_the_query_or_the_export()
    test_column_layout_state_stays_in_the_url()
    test_no_later_stage_feature_is_present()

    test_every_column_management_action_works_without_scripting()
    test_reorder_and_resize_expose_accessible_names_and_state()
    test_move_links_are_bounded_and_stay_inside_the_approved_set()

    test_worst_case_layout_url_stays_reasonable()

    test_script_applies_layout_in_place_without_requerying()
    test_script_refuses_to_hide_every_column()
    test_script_navigates_when_visibility_changes()
    test_script_resize_commits_once_and_syncs_the_url()
    test_script_autofit_matches_the_server_and_queries_nothing()
    test_script_pin_toggle_syncs_and_enforces_the_viewport_rule()
    test_script_keyboard_resize_is_bounded_and_commits_once()
    test_script_back_and_forward_restore_a_coherent_layout()
    test_script_reloads_when_history_crosses_a_visibility_change()
    test_script_ignores_unapproved_identifiers_in_history_state()

    print("\nALL COLUMN MANAGEMENT AND URL STATE TESTS PASSED")


if __name__ == "__main__":
    main()
