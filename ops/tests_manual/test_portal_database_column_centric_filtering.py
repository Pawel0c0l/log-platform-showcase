#!/usr/bin/env python3
"""Database Explorer column-centric filtering and sorting (approved stage S3).

Covers the replacement of the standalone filter form by per-column header menus
and a staged filter panel: the approved typed operator vocabulary, `puste`
semantics per data family, inclusive `od–do` ranges, bounded and parameterized
`in (...)`, immediate-versus-staged application, chips built from validated
state, the effective filter count, canonical sort URLs, and the security
invariants the expanded query surface must not weaken.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screen ``DB-005``, ``TABLE_AND_DATA_GRID_SPEC.md`` §2–§3, ``INTERACTION_SPEC.md``
§3–§4, ``IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`` ``DB-9``–``DB-23``, ``DB-62``.

Value distributions (`DB-10`, the text distinct-value picker) belong to stage S4
and are deliberately absent here; this suite asserts their absence rather than
their behaviour.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_column_centric_filtering.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Import stubs — the module is imported for its pure functions and its HTML
# rendering; no FastAPI, no database, no object store.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query=""):
        self.url = _FakeUrl(f"/user/database/datasets/{DATASET_ID}", query)
        self.cookies = {}


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
            "is_active": True, "is_admin": False, "permissions": []}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID, "client_code": "ACME_01", "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips", "slug": "approved-trips", "description": "Approved portal dataset",
        "schema_name": "public", "table_name": "trips", "default_date_column": "trip_start",
        "is_active": True, "visible_columns": 4, "assigned_users": 1,
        "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _col(name, label, data_type, **extra):
    column = {
        "dataset_id": DATASET_ID, "column_name": name, "display_name": label, "data_type": data_type,
        "is_visible": True, "is_filterable": True, "is_sortable": True,
        "is_default_date_column": name == "trip_start", "display_order": 10,
    }
    column.update(extra)
    return column


def _columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
        _col("note", "Notatka", "text", is_sortable=False, display_order=50),
        _col("internal_secret", "Ukryte", "text", is_visible=False, is_filterable=False,
             is_sortable=False, display_order=60),
    ]


def _visible_columns():
    return [c for c in _columns() if c["is_visible"]]


def _params(query: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        parsed.setdefault(key, []).append(value)
    return parsed


def _build(query: str, *, dataset=None, columns=None):
    """(conditions, values, active_filters, error, filter_entries) for a URL."""
    return api_main._build_portal_database_filter_conditions(
        dataset if dataset is not None else _dataset(),
        columns if columns is not None else _visible_columns(),
        _params(query),
    )


def _rows_query(query: str, *, dataset=None, columns=None):
    return api_main._build_portal_database_rows_query(
        dataset if dataset is not None else _dataset(),
        columns if columns is not None else _visible_columns(),
        _params(query),
        limit=10,
        offset=0,
    )


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _render(dataset=None, *, query="", rows=None, total=3, columns=None):
    """Render the row sheet with the real query builder resolving filter state."""
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _visible_columns()
    if rows is None:
        rows = [{"trip_start": "2026-05-28 09:00", "driver_name": "Alice",
                 "distance_km": 128, "is_billable": True, "note": "x"}]
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    sortable = {c["column_name"] for c in columns if c.get("is_sortable")}
    requested = api_main._portal_database_first_param(parsed, "sort", "")
    resolved_sort = requested if requested in sortable else "trip_start"
    resolved_dir = "asc" if api_main._portal_database_first_param(parsed, "direction", "") == "asc" else "desc"
    _c, _v, applied, _e, entries = api_main._build_portal_database_filter_conditions(dataset, columns, parsed)
    state = {
        "sort": resolved_sort,
        "direction": resolved_dir,
        "active_filters": applied,
        "filter_entries": entries,
    }
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (total, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows",
                                              lambda d, c, p, limit, offset, display_columns=None: (rows, state, None))),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        return api_main._portal_database_row_browser_response(
            _user(), DATASET_ID, _FakeRequest(query=query)
        ).body.decode("utf-8")
    finally:
        _restore(patches)


def _menu(column_name: str, *, query="", dataset=None, sort=None, direction="asc"):
    dataset = dataset if dataset is not None else _dataset()
    columns = _visible_columns()
    column = next(c for c in columns if c["column_name"] == column_name)
    params = _params(query)
    _c, _v, _a, error, entries = api_main._build_portal_database_filter_conditions(dataset, columns, params)
    assert error is None, error
    entry = next((e for e in entries if e["column_name"] == column_name), None)
    return api_main._portal_database_column_menu_html(
        dataset, column, params, sort_column=sort, direction=direction, entry=entry
    )


def _header_cell(html: str, label: str) -> str:
    for cell in re.findall(r"<th scope=\"col\".*?</th>", html, re.S):
        if f">{label}</span>" in cell:
            return cell
    raise AssertionError(f"no header cell for {label}")


# ===========================================================================
# 1. Column header interaction (DB-005)
# ===========================================================================
def test_every_eligible_header_carries_its_own_menu() -> None:
    html = _render()

    # `note` is filterable but not sortable; `trip_start` is both. Both get a
    # menu, and each menu carries only the sections its column supports.
    for name in ("trip_start", "driver_name", "distance_km", "is_billable", "note"):
        assert f'id="dbcol-{name}"' in html, name

    note_menu = _menu("note")
    assert "Sortowanie" not in note_menu, "a non-sortable column must not offer sort actions"
    assert "db-col-filter" in note_menu, note_menu

    # A column that is neither sortable nor filterable gets no control at all —
    # absent, not disabled (INTERACTION_SPEC.md §1).
    inert = dict(_col("code", "Kod", "text"), is_sortable=False, is_filterable=False)
    assert api_main._portal_database_column_menu_html(
        _dataset(), inert, {}, sort_column=None, direction="asc", entry=None
    ) == ""
    print("PASS: each eligible column header carries a menu limited to what that column supports")


def test_header_states_sort_and_filter_without_relying_on_colour() -> None:
    html = _render(query="sort=driver_name&direction=asc&op__driver_name=contains&filter__driver_name=Kowal")
    cell = _header_cell(html, "Kierowca")

    # Sort: aria-sort for assistive technology, a caret for sighted users, and
    # the toolbar sentence as the third, lexical carrier (DB-22).
    assert 'aria-sort="ascending"' in cell, cell
    assert 'class="db-sort-caret"' in cell and "↑" in cell, cell
    assert "sortowanie Kierowca ↑" in html, html

    # Filter: a tint class AND the `≡` marker AND the state in the accessible
    # name (DB-21, ACCESSIBILITY_SPEC.md §3).
    assert "db-col-filtered" in cell, cell
    assert "≡" in cell, cell
    assert 'aria-label="Kierowca, filtr aktywny: Kierowca zawiera Kowal, menu kolumny"' in cell, cell

    # An unfiltered, unsorted column says only what it is.
    plain = _header_cell(html, "Dystans")
    assert 'aria-sort="none"' in plain and 'aria-label="Dystans, menu kolumny"' in plain, plain
    assert "db-col-filtered" not in plain, plain
    print("PASS: sort and filter state are exposed semantically, lexically and visually")


def test_sorting_is_immediate_and_resets_paging() -> None:
    html = _render(query="page=4&limit=50&density=comfortable&filter__driver_name=Kowal&op__driver_name=contains")
    menu = _header_cell(html, "Dystans")
    # Scoped to the sort section: S5 added a column-actions section that uses the
    # same action-row class for pin, hide and autofit.
    sort_section = menu.split("Sortowanie", 1)[1].split("</div>", 1)[0]
    hrefs = re.findall(r'class="db-col-action[^"]*" href="([^"]+)"', sort_section)
    assert len(hrefs) == 2, sort_section

    # Sort rows are links: activating one is a navigation, so it applies at once
    # with no intervening Apply (INTERACTION_SPEC.md §4).
    asc = next(h for h in hrefs if "direction=asc" in h)
    q = parse_qs(urlparse(asc.replace("&amp;", "&")).query)
    assert q["sort"] == ["distance_km"], asc
    assert q["page"] == ["1"], "changing sort must return to page 1"
    assert q["limit"] == ["50"] and q["density"] == ["comfortable"], asc
    assert q["filter__driver_name"] == ["Kowal"], "sorting must preserve active filters"
    print("PASS: sorting applies immediately, resets paging and preserves the rest of the view")


def test_text_columns_use_the_approved_alphabetical_sort_wording() -> None:
    assert "Sortuj A → Z" in _menu("driver_name"), "text columns sort A→Z"
    assert "Sortuj rosnąco" in _menu("distance_km"), "other families sort rosnąco/malejąco"
    # NULL ordering is defined and the menu states it (TGS §2).
    assert "Wartości puste zawsze na końcu." in _menu("distance_km")
    print("PASS: sort wording follows the data family and null ordering is stated")


def test_only_one_column_can_be_sorted_at_a_time() -> None:
    # DB-23: the URL model carries a single `sort`, and a second value cannot
    # accumulate — the sort link replaces it rather than appending.
    html = _render(query="sort=driver_name&direction=asc")
    for href in re.findall(r'href="(/user/database/datasets/[^"]+)"', html):
        query = parse_qs(urlparse(href.replace("&amp;", "&")).query)
        assert len(query.get("sort", [])) <= 1, href
    print("PASS: only one sort column can exist in any generated URL")


# ===========================================================================
# 2. Canonical sort state in generated URLs
# ===========================================================================
def test_rejected_sort_identifier_never_reaches_the_query() -> None:
    # Unchanged authorization semantics: a non-sortable or unknown column is
    # refused by the validator and cannot enter ORDER BY.
    for query in ("sort=internal_secret", "sort=note", "sort=nonexistent"):
        _q, _v, _s, error = _rows_query(query)
        assert error == "Sort column is not available for this dataset.", (query, error)

    ok_query, _v, state, error = _rows_query("sort=driver_name&direction=desc")
    assert error is None and state["sort"] == "driver_name", state
    assert '"driver_name" DESC' in ok_query, ok_query
    print("PASS: an unauthorized sort column is refused and never reaches ORDER BY")


def test_generated_urls_carry_the_validated_sort_state() -> None:
    # A rejected identifier used to survive in every generated link because the
    # page rebuilt its URLs from the raw query string. It no longer does.
    rejected = _render(query="sort=internal_secret&direction=desc")
    assert "internal_secret" not in rejected, "a rejected sort identifier must not propagate"

    # The effective sort is stated explicitly even when the URL did not name it,
    # so a copied link reproduces the view (DB-34). Sort actions legitimately
    # name their own target; everything else carries the current state forward.
    sortable = {"trip_start", "driver_name", "distance_km", "is_billable"}
    defaulted = _render()
    sort_action_hrefs = {
        href for href in re.findall(r'class="db-col-action[^"]*" href="([^"]+)"', defaulted)
    }
    checked = 0
    for href in re.findall(r'href="(/user/database/datasets/[^"]*\?[^"]+)"', defaulted):
        query = parse_qs(urlparse(href.replace("&amp;", "&")).query)
        assert query.get("sort", [""])[0] in sortable, href
        assert query.get("direction", [""])[0] in {"asc", "desc"}, href
        if href in sort_action_hrefs:
            continue
        # A pagination, density or page-size link must reproduce the sort the
        # server actually validated.
        assert query["sort"] == ["trip_start"] and query["direction"] == ["desc"], href
        checked += 1
    assert checked >= 5, checked
    print("PASS: generated URLs reflect the validated sort state, not raw input")


# ===========================================================================
# 3. Typed operator vocabulary (TGS §3.2–§3.5)
# ===========================================================================
def test_operator_sets_match_the_approved_vocabulary() -> None:
    approved = {
        "text": ["contains", "eq", "neq", "in", "blank"],
        "numeric": ["eq", "neq", "gt", "gte", "lt", "lte", "between", "blank"],
        "boolean": ["is_true", "is_false", "blank"],
    }
    assert api_main._portal_database_allowed_operators({"data_type": "text"}) == approved["text"]
    assert api_main._portal_database_allowed_operators({"data_type": "numeric"}) == approved["numeric"]
    assert api_main._portal_database_allowed_operators({"data_type": "boolean"}) == approved["boolean"]
    assert api_main.PORTAL_DATABASE_DATE_FILTER_OPERATORS == frozenset({"older", "newer", "range", "blank"})

    # The approved Polish labels, verbatim from COPY_AND_TERMINOLOGY.md §3.1.
    labels = {op: api_main._portal_database_operator_label(op) for op in
              ("contains", "eq", "neq", "gt", "gte", "lt", "lte", "between", "blank",
               "older", "newer", "range", "is_true", "is_false")}
    assert labels == {
        "contains": "zawiera", "eq": "=", "neq": "≠", "gt": ">", "gte": "≥",
        "lt": "<", "lte": "≤", "between": "od–do", "blank": "puste",
        "older": "przed", "newer": "po", "range": "między",
        "is_true": "tak", "is_false": "nie",
    }, labels

    # No operator outside the approved set exists.
    assert api_main.PORTAL_DATABASE_FILTER_OPERATORS == frozenset({
        "contains", "eq", "neq", "gt", "gte", "lt", "lte", "between", "blank", "in", "is_true", "is_false",
    })
    print("PASS: operator sets and labels match the approved vocabulary exactly")


def test_numeric_column_menu_exposes_all_eight_operators_in_place() -> None:
    # DB-9: without leaving the dataset table page.
    menu = _menu("distance_km")
    # `>` and `<` arrive HTML-escaped, which is itself part of the contract:
    # an operator label is text, never markup.
    for label in ("&gt;", "≥", "&lt;", "≤", "≠", "=", "od–do", "puste"):
        assert f">{label}</option>" in menu, (label, menu)
    assert 'name="op__distance_km"' in menu, menu
    assert 'name="filter_from__distance_km"' in menu and 'name="filter_to__distance_km"' in menu, menu
    print("PASS: DB-9 — the numeric column menu exposes all eight operators inline")


def test_text_column_menu_exposes_the_approved_text_operators() -> None:
    # DB-11's operator half. The distinct-value picker is stage S4.
    menu = _menu("driver_name")
    for label in ("zawiera", "=", "≠", "puste"):
        assert f">{label}</option>" in menu, (label, menu)
    assert 'name="filter_in__driver_name"' in menu, "multi-value entry must exist"
    print("PASS: the text column menu exposes zawiera / = / ≠ / puste")


def test_boolean_column_offers_only_the_four_way_choice() -> None:
    menu = _menu("is_billable")
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', menu)
    assert options == [("", "wszystko"), ("is_true", "tak"), ("is_false", "nie"), ("blank", "puste")], options
    # A boolean has no value field: the operator IS the value.
    assert 'name="filter__is_billable"' not in menu, menu
    assert "zawiera" not in menu, "a boolean must not offer substring matching"
    print("PASS: a boolean column offers exactly wszystko / tak / nie / puste")


def test_date_column_menu_offers_przed_po_miedzy_puste() -> None:
    menu = _menu("trip_start")
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', menu)
    assert [value for value, _ in options] == ["older", "newer", "range", "blank"], options
    assert [text for _, text in options] == ["przed", "po", "między", "puste"], options
    assert 'type="datetime-local"' in menu, "a timestamp column takes a date-time value"
    print("PASS: the date column menu offers przed / po / między / puste")


# ===========================================================================
# 4. `puste` semantics by data family (HIGH ATTENTION)
# ===========================================================================
def test_blank_on_text_covers_null_and_the_empty_string_only() -> None:
    conditions, values, _a, error, entries = _build("op__driver_name=blank")
    assert error is None, error
    assert conditions == ['("driver_name" IS NULL OR CAST("driver_name" AS TEXT) = \'\')'], conditions
    assert values == [], values
    assert entries[0]["operator"] == "blank", entries

    # Whitespace is not blank. S2 deliberately renders NULL, '' and '   ' as
    # three different things, and filtering must not collapse that: nothing in
    # the emitted SQL trims.
    assert "trim" not in conditions[0].lower(), conditions
    assert "btrim" not in conditions[0].lower(), conditions

    # The menu states what `puste` covers for this family.
    assert "Dopasowuje brak wartości i pusty tekst." in _menu("driver_name")
    print("PASS: text `puste` is NULL or empty string, and never trims whitespace")


def test_blank_on_non_text_families_is_sql_null_alone() -> None:
    for query, column in (
        ("op__distance_km=blank", "distance_km"),
        ("op__is_billable=blank", "is_billable"),
        ("dateop__trip_start=blank", "trip_start"),
    ):
        conditions, values, _a, error, _e = _build(query)
        assert error is None, (query, error)
        assert conditions == [f'"{column}" IS NULL'], (query, conditions)
        assert values == [], (query, values)
        assert "= ''" not in conditions[0], "an empty string is not a value these families can hold"
    assert "Dopasowuje brak wartości." in _menu("distance_km")
    print("PASS: numeric, boolean and date `puste` mean SQL NULL alone")


def test_blank_never_swallows_zero_or_false() -> None:
    # `0` and `false` are data, not absence. The blank predicate contains no
    # comparison that could match them, and the ordinary operators still do.
    blank_numeric, _v, _a, error, _e = _build("op__distance_km=blank")
    assert error is None and blank_numeric == ['"distance_km" IS NULL']

    zero, values, _a, error, _e = _build("op__distance_km=eq&filter__distance_km=0")
    assert error is None and zero == ['"distance_km" = %s'] and values == ["0"], (zero, values)

    false_filter, values, _a, error, _e = _build("op__is_billable=is_false")
    assert error is None and false_filter == ['"is_billable" IS FALSE'], false_filter
    # IS FALSE, not `= false`: a NULL boolean is neither true nor false and must
    # not be returned by either.
    true_filter, _v, _a, error, _e = _build("op__is_billable=is_true")
    assert error is None and true_filter == ['"is_billable" IS TRUE'], true_filter
    print("PASS: zero and false are values; NULL is not false")


# ===========================================================================
# 5. `between` / `od–do`
# ===========================================================================
def test_between_is_inclusive_parameterized_and_open_ended() -> None:
    conditions, values, _a, error, entries = _build(
        "op__distance_km=between&filter_from__distance_km=10&filter_to__distance_km=20.5"
    )
    assert error is None, error
    # Inclusive at both ends (TGS §3.3).
    assert conditions == ['"distance_km" >= %s', '"distance_km" <= %s'], conditions
    assert values == ["10", "20.5"], values
    # One filter, therefore one chip, even though it is two conditions.
    assert len(entries) == 1 and entries[0]["operator"] == "between", entries
    assert (entries[0]["value"], entries[0]["value_to"]) == ("10", "20.5"), entries

    # DB-12: either bound may be blank for an open-ended inclusive range.
    lower_only, values, _a, error, _e = _build("op__distance_km=between&filter_from__distance_km=10")
    assert error is None and lower_only == ['"distance_km" >= %s'] and values == ["10"], (lower_only, values)
    upper_only, values, _a, error, _e = _build("op__distance_km=between&filter_to__distance_km=20")
    assert error is None and upper_only == ['"distance_km" <= %s'] and values == ["20"], (upper_only, values)

    # Both bounds blank is not a filter at all.
    empty, values, active, error, entries = _build("op__distance_km=between")
    assert error is None and empty == [] and active == [] and entries == [], (empty, active, entries)
    print("PASS: od–do is inclusive, parameterized and accepts an open bound on either side")


def test_between_rejects_invalid_and_reversed_bounds() -> None:
    _c, _v, _a, error, _e = _build("op__distance_km=between&filter_from__distance_km=30&filter_to__distance_km=20")
    assert error == "Dystans from must be less than or equal to Dystans to.", error

    for bad in ("filter_from__distance_km=abc", "filter_to__distance_km=abc"):
        _c, _v, _a, error, _e = _build(f"op__distance_km=between&{bad}")
        assert error and "must be a number" in error, (bad, error)

    # The date family reuses the same range semantics with ISO validation.
    _c, _v, _a, error, _e = _build("dateop__trip_start=range&date_from__trip_start=2026-05-12&date_to__trip_start=2026-05-10")
    assert error and "from must be earlier than or equal to" in error, error
    _c, _v, _a, error, _e = _build("dateop__trip_start=older&date__trip_start=not-a-date")
    assert error and "must be an ISO date or datetime" in error, error

    conditions, values, _a, error, entries = _build(
        "dateop__trip_start=range&date_from__trip_start=2026-05-01&date_to__trip_start=2026-05-31"
    )
    assert error is None, error
    assert conditions == ['"trip_start" >= %s', '"trip_start" <= %s'], conditions
    assert values == ["2026-05-01", "2026-05-31"], values
    assert len(entries) == 1 and entries[0]["operator"] == "range", entries
    print("PASS: invalid and reversed range bounds are refused, never turned into SQL")


def test_numeric_comparisons_validate_their_value() -> None:
    for operator in ("eq", "neq", "gt", "gte", "lt", "lte"):
        _c, _v, _a, error, _e = _build(f"op__distance_km={operator}&filter__distance_km=NaN")
        assert error == "Dystans must be a number.", (operator, error)
        conditions, values, _a, error, _e = _build(f"op__distance_km={operator}&filter__distance_km=-12.5")
        assert error is None, (operator, error)
        assert values == ["-12.5"], (operator, values)
        # The value reaches the database exactly as written; only the operator
        # decides the SQL fragment.
        assert conditions[0].startswith('"distance_km" '), conditions
        assert conditions[0].endswith("%s"), conditions
    print("PASS: numeric comparisons validate the value and pass it through unchanged")


# ===========================================================================
# 6. `in (...)` multi-value
# ===========================================================================
def test_in_is_parameterized_deduplicated_and_bounded() -> None:
    # Repeated parameters (what a multi-select submits) and newline-separated
    # entries (what the typed control submits) reach the same result.
    for query in (
        "op__driver_name=in&filter__driver_name=Ala&filter__driver_name=Ola",
        "op__driver_name=in&filter_in__driver_name=Ala%0AOla",
    ):
        conditions, values, _a, error, entries = _build(query)
        assert error is None, (query, error)
        assert conditions == ['CAST("driver_name" AS TEXT) IN (%s, %s)'], (query, conditions)
        assert values == ["Ala", "Ola"], (query, values)
        assert entries[0]["values"] == ["Ala", "Ola"], entries

    # Empty entries and duplicates change nothing and are dropped silently.
    _c, values, _a, error, _e = _build("op__driver_name=in&filter_in__driver_name=Ala%0A%0A%20%0AAla%0AOla")
    assert error is None and values == ["Ala", "Ola"], values

    # One placeholder per value, always — never a concatenated fragment.
    _c, values, _a, error, _e = _build(
        "op__driver_name=in&filter_in__driver_name=" + "%0A".join(f"v{i}" for i in range(50))
    )
    assert error is None, error
    assert len(values) == 50, len(values)
    assert _c[0].count("%s") == 50, _c

    # Beyond the bound the request is REJECTED, not silently shortened: a
    # truncated value list would answer a question the user did not ask.
    over = "op__driver_name=in&filter_in__driver_name=" + "%0A".join(f"v{i}" for i in range(51))
    _c, _v, _a, error, _e = _build(over)
    assert error == "Kierowca accepts at most 50 values; 51 were provided.", error
    assert api_main.PORTAL_DATABASE_MAX_IN_VALUES == 50
    print("PASS: in(...) is fully parameterized, deduplicated and bounded at 50 with an honest refusal")


def test_in_is_offered_only_where_the_contract_approves_it() -> None:
    for data_type in ("numeric", "boolean", "timestamp with time zone", "date"):
        assert "in" not in api_main._portal_database_allowed_operators({"data_type": data_type}), data_type
    for query in ("op__distance_km=in&filter__distance_km=1", "op__is_billable=in&filter__is_billable=x"):
        _c, _v, _a, error, _e = _build(query)
        assert error and "is not allowed for" in error, (query, error)
    print("PASS: in(...) exists only for the family the approved contract gives it to")


def test_multi_value_input_stays_data_and_never_becomes_syntax() -> None:
    payloads = ["', 'x') OR 1=1 --", '"; DROP TABLE trips; --', "%s", "\\", "a')"]
    conditions, values, _a, error, _e = _build(
        "op__driver_name=in&filter_in__driver_name=" + "%0A".join(
            payload.replace("%", "%25").replace("&", "%26").replace("+", "%2B").replace(" ", "%20")
            for payload in payloads
        )
    )
    assert error is None, error
    # Exactly one placeholder per value and no payload text anywhere in the SQL.
    assert conditions == ['CAST("driver_name" AS TEXT) IN (%s, %s, %s, %s, %s)'], conditions
    assert values == payloads, values
    for payload in payloads:
        if payload == "%s":
            # A value that happens to look like a placeholder is still a value:
            # it appears in `values`, and the condition holds exactly as many
            # placeholders as there are values, so it added no syntax.
            continue
        assert payload not in conditions[0], payload
    print("PASS: injection-shaped multi-values remain bound parameters, never syntax")


# ===========================================================================
# 7. Immediate versus staged application (D-005)
# ===========================================================================
def test_column_menu_applies_immediately_and_the_panel_stages() -> None:
    html = _render(query="op__driver_name=contains&filter__driver_name=Kowal")

    # DB-13: confirming inside the column menu is a submit of that menu's own
    # form — there is no second Apply anywhere else in the flow.
    menu = _header_cell(html, "Kierowca")
    assert '<form class="db-col-section db-col-filter" method="get" data-db-col-form>' in menu, menu
    assert '<button class="db-col-apply" type="submit">Zastosuj</button>' in menu, menu
    assert "↵ zastosuj · esc" in menu, menu

    # DB-14: the panel is one staged GET form. Nothing in it can reach the
    # server without its own Zastosuj, because a GET form submits only on
    # submit — there is no auto-apply hook of any kind on its fields.
    panel = html[html.index('class="db-filter-panel"'):html.index("</details>", html.index('class="db-filter-panel"'))]
    assert '<form class="db-panel-form" method="get" data-db-panel-form>' in panel, panel
    assert panel.count('type="submit"') == 1, "the panel commits through exactly one Zastosuj"
    assert "onchange" not in panel and "onsubmit" not in panel, panel

    # The two surfaces submit the same parameter contract — one filter model.
    assert 'name="op__driver_name"' in panel and 'name="op__driver_name"' in menu, panel
    print("PASS: D-005 — the column menu applies immediately, the panel stages until Zastosuj")


def test_removing_one_filter_and_clearing_all_apply_immediately() -> None:
    html = _render(query=(
        "op__driver_name=contains&filter__driver_name=Kowal"
        "&op__distance_km=gt&filter__distance_km=100"
        "&search=abc&limit=50&density=comfortable&page=3"
    ))

    # A chip's `×` is a link: activating it is the application (INT §4).
    chips = re.findall(r'<a class="db-chip-remove" href="([^"]+)"', html)
    assert len(chips) == 2, chips
    driver_remove = next(h for h in chips if "distance_km" in h)
    q = parse_qs(urlparse(driver_remove.replace("&amp;", "&")).query)
    assert "filter__driver_name" not in q and "op__driver_name" not in q, driver_remove
    # DB-17: only that filter goes; everything else stands.
    assert q["filter__distance_km"] == ["100"], driver_remove
    assert q["search"] == ["abc"], driver_remove
    assert q["limit"] == ["50"] and q["density"] == ["comfortable"], driver_remove
    assert q["sort"] == ["trip_start"] and q["direction"] == ["desc"], driver_remove
    assert q["page"] == ["1"], "removing a filter returns to page 1"

    # DB-18: one action clears every column filter and the global search, and
    # leaves the unrelated view settings alone.
    clear_all = re.search(r'<a class="portal-button secondary db-panel-clear" href="([^"]+)"', html).group(1)
    q = parse_qs(urlparse(clear_all.replace("&amp;", "&")).query)
    for dropped in ("filter__driver_name", "op__driver_name", "filter__distance_km", "op__distance_km", "search", "page"):
        assert dropped not in q, (dropped, clear_all)
    assert q["sort"] == ["trip_start"] and q["limit"] == ["50"] and q["density"] == ["comfortable"], clear_all
    print("PASS: chip removal and Wyczyść wszystkie apply immediately and preserve unrelated state")


# ===========================================================================
# 8. Chips and the filter count come from validated state
# ===========================================================================
def test_every_active_filter_is_one_chip_with_column_operator_and_value() -> None:
    html = _render(query=(
        "op__driver_name=contains&filter__driver_name=Kowal"
        "&op__distance_km=between&filter_from__distance_km=10&filter_to__distance_km=20"
        "&op__is_billable=is_true"
        "&dateop__trip_start=blank"
        "&op__note=in&filter_in__note=a%0Ab%0Ac"
    ))
    # The chips ride inside the toolbar band: an extra band would push the table
    # header past the approved 168 px the moment a filter was applied (`DB-1`).
    toolbar = html[html.index('class="db-toolbar"'):html.index('class="db-table-viewport"')]
    assert 'class="db-chips"' in toolbar, toolbar
    assert 'class="db-chips"' not in html[html.index('class="db-table-viewport"'):], "no second chip strip"
    strip = html[html.index('class="db-chips"'):html.index("</div>", html.index('class="db-chips"'))]
    chips = re.findall(r'<span class="db-chip">(.*?)</a></span>', strip, re.S)
    assert len(chips) == 5, chips

    def chip_text(fragment: str) -> str:
        # Drop the remove control's glyph; what remains is the chip's own words.
        text = re.sub(r"<[^>]+>", " ", fragment).replace("×", " ")
        return re.sub(r"\s+", " ", text).strip()

    texts = [chip_text(chip) for chip in chips]
    assert "Kierowca zawiera Kowal" in texts, texts
    # A two-sided range is ONE chip carrying both bounds (DB-16).
    assert "Dystans od–do 10 – 20" in texts, texts
    assert "Rozliczalny tak" in texts, texts
    assert "Start puste" in texts, texts
    # The approved chip form for a value list is its count, not the list.
    assert "Notatka in (3)" in texts, texts

    # Each `×` names what it removes, for assistive technology.
    assert 'aria-label="Usuń filtr: Kierowca zawiera Kowal"' in strip, strip
    print("PASS: DB-16 — every active filter is exactly one chip stating column, operator and value")


def test_the_filter_count_uses_validated_state_not_raw_query_presence() -> None:
    def badge(html: str) -> str:
        match = re.search(r'Filtry<span class="db-count-badge">(\d+)</span>', html)
        return match.group(1) if match else "0"

    # Two real filters.
    assert badge(_render(query="filter__driver_name=Kowal&op__driver_name=contains&op__distance_km=blank")) == "2"

    # Filter-shaped parameters the builder rejected as no-ops must not inflate
    # it: an empty value, and an operator with nothing to compare.
    assert badge(_render(query="filter__driver_name=&op__driver_name=contains")) == "0"
    assert badge(_render(query="op__distance_km=between")) == "0"

    # The global search is its own toolbar field, not a chip, so it does not
    # count (PBC §2.6) — but it is still cleared by Wyczyść wszystkie.
    searched = _render(query="search=abc")
    assert badge(searched) == "0", searched[:0]
    assert "db-chip" not in searched, "the search term must not be represented as a chip"
    _c, _v, _a, error, entries = _build("search=abc")
    assert error is None and [e["column_name"] for e in entries] == ["__search__"], entries
    print("PASS: DB-19 — the badge counts effective validated column filters only")


def test_clear_all_preserves_every_selected_column() -> None:
    """Codex S3 finding: the reset URL read `cols` as a scalar.

    With more than one column selected it kept only the last, so clearing
    filters silently hid the rest — `DB-18` requires unrelated view settings to
    survive.
    """
    params = _params(
        "cols=trip_start&cols=driver_name&cols=distance_km"
        "&sort=driver_name&direction=asc&limit=50&density=comfortable"
        "&op__driver_name=contains&filter__driver_name=K&search=abc&page=4"
    )
    reset = api_main._portal_database_reset_url(DATASET_ID, params)
    query = parse_qs(urlparse(reset).query)
    assert query["cols"] == ["trip_start", "driver_name", "distance_km"], reset
    assert query["sort"] == ["driver_name"] and query["direction"] == ["asc"], reset
    assert query["limit"] == ["50"] and query["density"] == ["comfortable"], reset
    for dropped in ("filter__driver_name", "op__driver_name", "search", "page"):
        assert dropped not in query, (dropped, reset)

    # And through the rendered page, where `cols` is the normalized list.
    html = _render(query="cols=trip_start&cols=driver_name&cols=distance_km&op__driver_name=contains&filter__driver_name=K")
    clear_all = re.search(r'db-panel-clear" href="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert parse_qs(urlparse(clear_all).query)["cols"] == ["trip_start", "driver_name", "distance_km"], clear_all
    print("PASS: Wyczyść wszystkie preserves every selected column")


def test_the_panel_only_offers_columns_whose_menu_is_on_the_page() -> None:
    # `Dodaj filtr` opens a column's own header menu. Offering a column the user
    # has hidden from the table would be an anchor to a menu that is not
    # rendered — a control that does nothing.
    html = _render(query="cols=driver_name,distance_km")
    panel = html[html.index('class="db-panel-form"'):]
    offered = set(re.findall(r'data-db-add-column="([^"]+)"', panel))
    assert offered == {"driver_name", "distance_km"}, offered
    assert "trip_start" not in offered and "note" not in offered, "a hidden column has no menu to open"

    shown = _render()
    all_offered = set(re.findall(r'data-db-add-column="([^"]+)"', shown[shown.index('class="db-panel-form"'):]))
    assert all_offered == {"trip_start", "driver_name", "distance_km", "is_billable", "note"}, all_offered
    print("PASS: the panel offers only columns whose header menu is actually rendered")


def test_chips_are_built_from_the_builder_record_not_the_query_string() -> None:
    # A search against a dataset with no approved searchable text column applies
    # no condition; a chip for it would assert a filter that does not exist.
    numeric_only = [_col("distance_km", "Dystans", "numeric")]
    _c, _v, active, error, entries = _build("search=abc&filter__distance_km=", columns=numeric_only)
    assert error is None and active == [] and entries == [], (active, entries)
    chips = api_main._portal_database_active_filter_chips(
        _dataset(), _params("search=abc&filter__distance_km="), entries
    )
    assert chips == [], chips
    print("PASS: a rejected or no-op parameter never becomes a chip")


# ===========================================================================
# 9. The legacy form is retired; progressive enhancement survives
# ===========================================================================
def test_the_always_expanded_filter_form_is_gone() -> None:
    html = _render()

    # DB-20: opening the row sheet shows no filter input field until the user
    # opens a menu or the panel. Every filter control ships inside a CLOSED
    # <details>, and nothing renders one open.
    assert "db-filter-group" not in html, "the pre-redesign filter form must not return"
    assert "db-filter-grid" not in html, html
    assert "<details open" not in html, html
    assert "data-db-filters>" in html and "data-db-filters open" not in html, html
    for menu in re.findall(r"<details class=\"db-col-menu\"[^>]*>", html):
        assert " open" not in menu, menu

    # The demoted section below the sheet no longer carries filtering at all.
    secondary = html[html.index('class="db-secondary"'):]
    assert "db-panel-form" not in secondary and "db-col-filter" not in secondary, secondary
    print("PASS: DB-20 — no permanently expanded filter form remains anywhere on the sheet")


def test_filtering_works_without_javascript() -> None:
    html = _render(query="op__driver_name=contains&filter__driver_name=Kowal&limit=50&density=comfortable")

    # Every filter surface is a real GET form or a real link. Nothing depends on
    # a script to submit, and no control is a scripted-only element.
    menu = _header_cell(html, "Dystans")
    assert 'method="get"' in menu, menu
    assert "<button" in menu and 'type="submit"' in menu, menu
    assert 'href="' in _header_cell(html, "Kierowca"), "sort actions are links"

    # The menu form carries the rest of the view state forward as hidden inputs,
    # so submitting it without a script preserves the view.
    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', menu))
    assert hidden.get("limit") == "50" and hidden.get("density") == "comfortable", hidden
    assert hidden.get("sort") == "trip_start" and hidden.get("direction") == "desc", hidden
    assert hidden.get("filter__driver_name") == "Kowal", "another column's filter must survive"
    # It must NOT re-emit its own column's parameters, or the hidden copy would
    # fight the visible control.
    assert "filter__distance_km" not in hidden and "op__distance_km" not in hidden, hidden
    # Applying a filter returns to page 1, so `page` is never carried over.
    assert "page" not in hidden, hidden

    # The panel likewise carries non-filter state only, and owns every filter
    # parameter it renders.
    panel = html[html.index('class="db-panel-form"'):]
    panel_hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', panel[:4000]))
    assert panel_hidden.get("limit") == "50" and panel_hidden.get("sort") == "trip_start", panel_hidden
    for owned in ("filter__driver_name", "op__driver_name", "page"):
        assert owned not in panel_hidden, (owned, panel_hidden)
    print("PASS: filtering and sorting remain fully usable with scripting disabled")


def test_the_script_is_a_progressive_enhancement_only() -> None:
    source = (REPO_ROOT / "api" / "static" / "js" / "data-grid-filters.js").read_text(encoding="utf-8")
    # The script must not become the security or correctness boundary.
    for forbidden in ("SELECT ", "WHERE ", "can_filter_rows", "ILIKE"):
        assert forbidden not in source, forbidden
    # It is requested through the shared page-asset mechanism, not inlined.
    html = _render()
    assert "/static/js/data-grid-filters.js?v=" in html, html
    assert "<script>" not in html, "no inline script may return to this page"
    print("PASS: the filter script carries no query semantics and ships through the asset mechanism")


# ===========================================================================
# 10. Permission and security invariants
# ===========================================================================
def test_filtering_disabled_removes_the_ui_and_fails_closed() -> None:
    denied = _dataset(can_filter_rows=False)
    html = _render(denied)

    # DB-62: absent, not disabled.
    assert "db-filter-panel" not in html and "db-col-filter" not in html, html
    assert ">Filtry" not in html and "db-toolbar-search" not in html, html
    assert "db-chips" not in html, html
    assert "disabled" not in html.lower().split("db-toolbar")[1].split("db-table")[0], html
    # Sorting is a separate capability and still works.
    assert "Sortuj rosnąco" in html, html

    # A forged URL gains nothing — every operator, old and new, is refused.
    forged = [
        "filter__driver_name=Kowal&op__driver_name=contains",
        "op__driver_name=blank",
        "op__driver_name=in&filter_in__driver_name=a%0Ab",
        "op__distance_km=between&filter_from__distance_km=1&filter_to__distance_km=2",
        "op__distance_km=gt&filter__distance_km=5",
        "op__is_billable=is_true",
        "op__is_billable=is_false",
        "dateop__trip_start=blank",
        "dateop__trip_start=range&date_from__trip_start=2026-01-01",
        "date_from=2026-01-01",
        "search=abc",
    ]
    for query in forged:
        conditions, values, active, error, entries = _build(query, dataset=denied)
        assert error == "Filtering is not enabled for your access to this dataset.", (query, error)
        assert conditions == [] and values == [] and active == [] and entries == [], query
        _q, _v, _s, query_error = _rows_query(query, dataset=denied)
        assert query_error == "Filtering is not enabled for your access to this dataset.", (query, query_error)
    print("PASS: can_filter_rows=false removes the UI and refuses every operator from a forged URL")


def test_unapproved_columns_cannot_be_filtered_through_any_operator() -> None:
    for template in (
        "filter__{c}=x&op__{c}=eq",
        "op__{c}=blank",
        "op__{c}=in&filter_in__{c}=a",
        "op__{c}=between&filter_from__{c}=1&filter_to__{c}=2",
        "filter_to__{c}=2",
        "filter_from__{c}=2",
    ):
        for column in ("internal_secret", "note_x", "trips; DROP TABLE trips"):
            query = template.format(c=column.replace(" ", "%20").replace(";", "%3B"))
            _c, _v, _a, error, _e = _build(query)
            assert error == "Filter column is not available for this dataset.", (query, error)

    # A hidden column stays unreachable as a date filter too.
    _c, _v, _a, error, _e = _build("dateop__internal_secret=blank")
    assert error and "not available for this dataset" in error, error

    # A non-filterable but visible column is equally out of reach.
    columns = [dict(c, is_filterable=False) if c["column_name"] == "driver_name" else c for c in _visible_columns()]
    _c, _v, _a, error, _e = _build("op__driver_name=blank", columns=columns)
    assert error == "Filter column is not available for this dataset.", error
    print("PASS: no operator can reach a column outside the approved filterable set")


def test_no_operator_token_can_become_sql_syntax() -> None:
    hostile = [
        "eq; DROP TABLE trips", "1=1", "IS NOT NULL", "in)--", "between)) OR (1=1",
        "OR", "UNION", "", "is_true; --",
    ]
    for token in hostile:
        encoded = token.replace(" ", "%20").replace(";", "%3B").replace("=", "%3D").replace("&", "%26")
        _c, _v, _a, error, _e = _build(f"op__driver_name={encoded}&filter__driver_name=x")
        if token == "":
            # An absent operator falls back to the family default, never to raw text.
            continue
        assert error and "is not allowed for" in error, (token, error)
        assert token not in str(_c), token

    # Case is normalized before the allowlist check, which is existing behaviour
    # and still resolves to a named token rather than to user text.
    conditions, _v, _a, error, _e = _build("op__driver_name=BLANK")
    assert error is None and conditions == ['("driver_name" IS NULL OR CAST("driver_name" AS TEXT) = \'\')'], conditions

    # The default is a named token, not user text.
    conditions, values, _a, error, _e = _build("filter__driver_name=x")
    assert error is None and conditions == ['CAST("driver_name" AS TEXT) ILIKE %s'], conditions
    assert values == ["%x%"], values
    print("PASS: an operator token is an allowlist key, never SQL text")


def test_values_are_always_bound_parameters() -> None:
    payloads = ["' OR '1'='1", "%; --", "\\'", "x') UNION SELECT 1 --"]
    for payload in payloads:
        encoded = payload.replace("%", "%25").replace("&", "%26").replace("+", "%2B").replace(" ", "%20").replace("'", "%27")
        for query, expected in (
            (f"op__driver_name=eq&filter__driver_name={encoded}", payload),
            (f"op__driver_name=contains&filter__driver_name={encoded}", f"%{payload}%"),
        ):
            conditions, values, _a, error, _e = _build(query)
            assert error is None, (query, error)
            assert values == [expected], (query, values)
            assert payload not in " ".join(conditions), (query, conditions)
    print("PASS: every filter value reaches the database as a bound parameter")


def test_search_scope_did_not_widen() -> None:
    # DB-25: the placeholder states how many text columns are searched, and the
    # number matches the dataset's approved text columns. Boolean columns are
    # not text and must not be inside the search predicate.
    columns = _visible_columns()
    search_columns = api_main._portal_database_search_columns(columns)
    assert [c["column_name"] for c in search_columns] == ["driver_name", "note"], search_columns
    assert "Szukaj w 2 kolumnach tekstowych" in _render(), "the stated count must match"

    conditions, values, _a, error, _e = _build("search=abc")
    assert error is None, error
    assert conditions == ['(CAST("driver_name" AS TEXT) ILIKE %s OR CAST("note" AS TEXT) ILIKE %s)'], conditions
    assert values == ["%abc%", "%abc%"], values
    assert "is_billable" not in conditions[0], "a boolean column must not be searched"
    assert "distance_km" not in conditions[0] and "trip_start" not in conditions[0], conditions
    print("PASS: the global search still spans approved text columns only")


def test_the_query_shape_and_its_guards_are_unchanged() -> None:
    query, values, state, error = _rows_query(
        "op__driver_name=in&filter_in__driver_name=Ala%0AOla&op__distance_km=between"
        "&filter_from__distance_km=10&filter_to__distance_km=20&sort=driver_name&direction=asc"
    )
    assert error is None, error
    # Identifiers come from the catalog and are quoted; every value is bound.
    assert query.startswith('SELECT "trip_start", "driver_name", "distance_km", "is_billable", "note" FROM "public"."trips"'), query
    assert "internal_secret" not in query, query
    assert "Ala" not in query and "Ola" not in query and "10" not in query.split("LIMIT")[0], query
    assert query.count("%s") == len(values), (query, values)
    assert values[-2:] == [10, 0], values
    assert 'ORDER BY "driver_name" ASC' in query, query
    print("PASS: the generated statement keeps catalog identifiers, quoting and full parameterization")


def test_audit_records_filtered_columns_without_values() -> None:
    params = _params(
        "op__driver_name=in&filter_in__driver_name=Kowalski&op__distance_km=between"
        "&filter_from__distance_km=10&op__is_billable=is_true&dateop__trip_start=blank"
    )
    keys = api_main._portal_audit_filter_keys(params)
    assert keys == ["distance_km", "driver_name", "is_billable", "trip_start"], keys
    assert "Kowalski" not in str(keys), keys
    print("PASS: the audit record still names every filtered column and carries no values")


# ===========================================================================
# 11. Background-export round trip for the new operators
# ===========================================================================
def test_new_operators_survive_the_background_export_snapshot() -> None:
    for query, expected_conditions in (
        ("op__driver_name=in&filter_in__driver_name=Ala%0AOla",
         ['CAST("driver_name" AS TEXT) IN (%s, %s)']),
        ("op__distance_km=between&filter_from__distance_km=10&filter_to__distance_km=20",
         ['"distance_km" >= %s', '"distance_km" <= %s']),
        ("op__driver_name=blank",
         ['("driver_name" IS NULL OR CAST("driver_name" AS TEXT) = \'\')']),
        ("op__is_billable=is_true", ['"is_billable" IS TRUE']),
        ("dateop__trip_start=range&date_from__trip_start=2026-05-01&date_to__trip_start=2026-05-31",
         ['"trip_start" >= %s', '"trip_start" <= %s']),
    ):
        conditions, values, active, error, _e = _build(query)
        assert error is None, (query, error)

        snapshot_filters = []
        for entry in active:
            record = {"column_name": entry["column_name"], "operator": entry["operator"], "value": entry.get("value", "")}
            if entry.get("values"):
                record["values"] = entry["values"]
            if entry.get("value_to"):
                record["value_to"] = entry["value_to"]
            snapshot_filters.append(record)
        rebuilt, rebuilt_refusal = api_main._portal_database_export_snapshot_params(
            _dataset(), _visible_columns(), {"filters": snapshot_filters}
        )
        assert rebuilt_refusal is None and rebuilt is not None, (query, rebuilt_refusal)
        rebuilt_conditions, rebuilt_values, _a, rebuilt_error, _e = api_main._build_portal_database_filter_conditions(
            _dataset(), _visible_columns(), rebuilt
        )
        assert rebuilt_error is None, (query, rebuilt_error)
        assert rebuilt_conditions == expected_conditions == conditions, (query, rebuilt_conditions, conditions)
        assert rebuilt_values == values, (query, rebuilt_values, values)

    # A snapshot written before the `between` record existed stored a two-sided
    # range as two records keyed by the same column. Rebuilding it must not lose
    # a bound and silently widen the exported result.
    legacy, legacy_refusal = api_main._portal_database_export_snapshot_params(
        _dataset(), _visible_columns(), {"filters": [
            {"column_name": "distance_km", "operator": "gte", "value": "10"},
            {"column_name": "distance_km", "operator": "lte", "value": "20"},
        ]}
    )
    assert legacy_refusal is None and legacy is not None, legacy_refusal
    conditions, values, _a, error, _e = api_main._build_portal_database_filter_conditions(
        _dataset(), _visible_columns(), legacy
    )
    assert error is None, error
    assert conditions == ['"distance_km" >= %s', '"distance_km" <= %s'], conditions
    assert values == ["10", "20"], values
    print("PASS: every new operator round-trips through the background-export snapshot")


# ===========================================================================
# 12. Stage boundaries — S4 and beyond stay out
# ===========================================================================
def test_no_later_stage_feature_is_present() -> None:
    html = _render()
    # S4 added the distribution containers to these menus and S5 the column
    # actions; named column sets and saved views still need server persistence
    # and must not appear.
    for future in ("Zapisz jako zestaw", "Zestawy", "Zapisz jako widok"):
        assert future not in html, f"{future} belongs to a later stage"
    # The row query itself never aggregates: distributions are a separate,
    # separately authorized request.
    for query in ("", "op__driver_name=blank", "op__distance_km=between&filter_from__distance_km=1"):
        generated, _v, _s, error = _rows_query(query)
        assert error is None, (query, error)
        assert "GROUP BY" not in generated and "width_bucket" not in generated, generated
    print("PASS: the row query never aggregates and no later-stage column action is present")


# ===========================================================================
# 13. Menu and panel behaviour in the shipped script
# ===========================================================================
def _run_filter_harness(scenario: str) -> dict:
    command = ["node", str(REPO_ROOT / "ops" / "tests_manual" / "data_grid_filters_harness.js"), scenario]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_menu_opens_focuses_and_discards_on_dismissal() -> None:
    result = _run_filter_harness("menu-lifecycle")
    # Opening moves focus into the menu (INTERACTION_SPEC.md §2.3).
    assert result["afterOpen"]["open"] is True, result
    assert result["afterOpen"]["focused"] == "first-control", result

    # A staged edit is not applied by anything the script does.
    assert result["afterEdit"]["submitted"] is False, result
    assert result["afterEdit"]["operator"] == "blank", result

    # DB-15: Esc closes, restores the previously applied filter, and returns
    # focus to the header that opened the menu.
    assert result["afterEscape"]["open"] is False, result
    assert result["afterEscape"]["operator"] == "contains", "Esc must discard the pending edit"
    assert result["afterEscape"]["value"] == "Kowal", result
    assert result["afterEscape"]["focused"] == "summary", result
    assert result["afterEscape"]["submitted"] is False, result
    print("PASS: a column menu opens with focus, and Esc closes it discarding pending edits")


def test_outside_click_and_table_scroll_discard_too() -> None:
    result = _run_filter_harness("menu-dismissal")
    for key in ("afterOutsideClick", "afterScroll"):
        assert result[key]["open"] is False, (key, result)
        assert result[key]["operator"] == "contains", (key, result)
        assert result[key]["submitted"] is False, (key, result)
    print("PASS: an outside click and a table scroll both close a menu and discard its edits")


def test_only_one_menu_is_open_at_a_time() -> None:
    result = _run_filter_harness("menu-exclusive")
    assert result["openCount"] == 1, result
    assert result["openColumn"] == "distance_km", result
    print("PASS: opening a column menu closes any other open menu")


def test_panel_keeps_staged_edits_when_collapsed() -> None:
    result = _run_filter_harness("panel-staging")
    # The panel stages: Esc collapses it and does NOT submit.
    assert result["afterEscape"]["panelOpen"] is False, result
    assert result["afterEscape"]["submitted"] is False, result
    # Unlike the menu, reopening shows the staged edit again (INT §3).
    assert result["afterReopen"]["operator"] == "blank", result
    assert result["afterEscape"]["focused"] == "summary", result
    print("PASS: the filter panel collapses without applying and keeps its staged edits")


def test_escape_closes_the_topmost_layer_only() -> None:
    result = _run_filter_harness("layer-order")
    # With both open, the first Esc takes the menu and leaves the panel.
    assert result["afterFirstEscape"] == {"menuOpen": False, "panelOpen": True}, result
    assert result["afterSecondEscape"] == {"menuOpen": False, "panelOpen": False}, result
    print("PASS: Esc dismisses the topmost transient layer only")


def test_operator_choice_switches_the_value_controls() -> None:
    result = _run_filter_harness("operator-switch")
    assert result["contains"] == {"single": False, "range": True, "multi": True}, result
    assert result["between"] == {"single": True, "range": False, "multi": True}, result
    assert result["in"] == {"single": True, "range": True, "multi": False}, result
    assert result["blank"] == {"single": True, "range": True, "multi": True}, result
    print("PASS: the operator selection shows exactly the value controls that operator needs")


def test_apply_cannot_be_submitted_twice() -> None:
    result = _run_filter_harness("submit-guard")
    assert result["submissions"] == 1, result
    assert result["widthPinned"] is True, "a loading control must not change width"
    print("PASS: an Apply cannot be double-submitted into conflicting duplicate requests")


# ===========================================================================
def main() -> None:
    test_every_eligible_header_carries_its_own_menu()
    test_header_states_sort_and_filter_without_relying_on_colour()
    test_sorting_is_immediate_and_resets_paging()
    test_text_columns_use_the_approved_alphabetical_sort_wording()
    test_only_one_column_can_be_sorted_at_a_time()

    test_rejected_sort_identifier_never_reaches_the_query()
    test_generated_urls_carry_the_validated_sort_state()

    test_operator_sets_match_the_approved_vocabulary()
    test_numeric_column_menu_exposes_all_eight_operators_in_place()
    test_text_column_menu_exposes_the_approved_text_operators()
    test_boolean_column_offers_only_the_four_way_choice()
    test_date_column_menu_offers_przed_po_miedzy_puste()

    test_blank_on_text_covers_null_and_the_empty_string_only()
    test_blank_on_non_text_families_is_sql_null_alone()
    test_blank_never_swallows_zero_or_false()

    test_between_is_inclusive_parameterized_and_open_ended()
    test_between_rejects_invalid_and_reversed_bounds()
    test_numeric_comparisons_validate_their_value()

    test_in_is_parameterized_deduplicated_and_bounded()
    test_in_is_offered_only_where_the_contract_approves_it()
    test_multi_value_input_stays_data_and_never_becomes_syntax()

    test_column_menu_applies_immediately_and_the_panel_stages()
    test_removing_one_filter_and_clearing_all_apply_immediately()

    test_every_active_filter_is_one_chip_with_column_operator_and_value()
    test_the_filter_count_uses_validated_state_not_raw_query_presence()
    test_chips_are_built_from_the_builder_record_not_the_query_string()
    test_clear_all_preserves_every_selected_column()
    test_the_panel_only_offers_columns_whose_menu_is_on_the_page()

    test_the_always_expanded_filter_form_is_gone()
    test_filtering_works_without_javascript()
    test_the_script_is_a_progressive_enhancement_only()

    test_filtering_disabled_removes_the_ui_and_fails_closed()
    test_unapproved_columns_cannot_be_filtered_through_any_operator()
    test_no_operator_token_can_become_sql_syntax()
    test_values_are_always_bound_parameters()
    test_search_scope_did_not_widen()
    test_the_query_shape_and_its_guards_are_unchanged()
    test_audit_records_filtered_columns_without_values()

    test_new_operators_survive_the_background_export_snapshot()
    test_no_later_stage_feature_is_present()

    test_menu_opens_focuses_and_discards_on_dismissal()
    test_outside_click_and_table_scroll_discard_too()
    test_only_one_menu_is_open_at_a_time()
    test_panel_keeps_staged_edits_when_collapsed()
    test_escape_closes_the_topmost_layer_only()
    test_operator_choice_switches_the_value_controls()
    test_apply_cannot_be_submitted_twice()

    print("\nALL COLUMN-CENTRIC FILTERING TESTS PASSED")


if __name__ == "__main__":
    main()
