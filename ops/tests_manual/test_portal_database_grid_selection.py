#!/usr/bin/env python3
"""Database Explorer grid selection and clipboard (approved stage S7).

Two coupled contracts are under test.

**The selection model.** A rectangular, page-local, ephemeral cell range: one
contiguous rectangle over the rows the server already rendered, extended by
pointer drag, `Shift`+click or `Shift`+arrows, bounded by the first and last
rendered row and the first and last displayed column. It never crosses a
pagination boundary (`D-012` phase 1), never enters the URL, `localStorage` or
the server, and never survives a filter, sort or page change.

**The clipboard grammar.** One rectangle serialised as spreadsheet-pasteable
text: tabs between columns, CRLF between rows, CSV-style quoting for any value
that would otherwise break the geometry. Values come from the canonical
``data-db-copy`` contract S2 established, never from the rendered text, and the
technical row identity is outside the selection universe by construction.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screen ``DB-003``, ``PRODUCT_BEHAVIOR_CONTRACT.md`` §2.11,
``INTERACTION_SPEC.md`` §2.2/§3/§5/§8, ``TABLE_AND_DATA_GRID_SPEC.md`` §7,
``RESPONSIVE_SPEC.md`` (table), ``ACCESSIBILITY_SPEC.md`` §4/§9,
``COMPONENT_CATALOG.md`` (selection counter), criteria ``DB-44``, ``AC-2`` and
decision ``D-012``.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_grid_selection.py
"""
from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import sys
import types
from datetime import datetime, timezone
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
from api.portal_ui import i18n  # noqa: E402

HARNESS = REPO_ROOT / "ops" / "tests_manual" / "data_grid_selection_harness.js"
MODULE = REPO_ROOT / "api" / "static" / "js" / "data-grid-selection.js"

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ROUTE = f"/user/database/datasets/{DATASET_ID}"

# A value that exists nowhere else in the fixture, the source or the vocabulary,
# so any appearance in a rendered response is unambiguously a leak.
RAW_IDENTITY = "QQZX-RECORDID-NEVER-SHOWN-773311"
TEST_SECRET = "deterministic-test-key-material-for-row-references"


# ===========================================================================
# Server-render fixture
# ===========================================================================
class _FakeUrl:
    def __init__(self, path, query):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, query="", path=ROUTE):
        self.url = _FakeUrl(path, query)
        self.cookies = {}


def _user():
    return {"user_id": USER_ID, "username": "alice", "display_name": "Alice",
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
    """The technical row identifier, in both catalog states S6 must handle."""
    return _col(
        "record_id", "Record", "text",
        is_visible=legacy_visible, is_filterable=legacy_visible, is_sortable=legacy_visible,
        is_row_identifier=True, display_order=1,
    )


def _business_columns():
    return [
        _col("trip_start", "Start", "timestamp with time zone", display_order=10),
        _col("driver_name", "Kierowca", "text", display_order=20),
        _col("distance_km", "Dystans", "numeric", display_order=30),
        _col("is_billable", "Rozliczalny", "boolean", display_order=40),
    ]


def _rows(count=3):
    out = []
    for index in range(count):
        out.append({
            "record_id": f"{RAW_IDENTITY}-{index}",
            "trip_start": datetime(2026, 5, 28, 7, 30, tzinfo=timezone.utc),
            "driver_name": "Kowalski" if index else "Nowak",
            "distance_km": Decimal("1284.40"),
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


_SQL_LOG: list[tuple[str, list]] = []


def _render(*, query="", rows=None, columns=None, identity=True, legacy_visible=False):
    dataset = _dataset()
    columns = columns if columns is not None else _business_columns()
    rows = _rows() if rows is None else rows
    identity_col = _identity_column(legacy_visible=legacy_visible) if identity else None
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    sortable = {str(c.get("column_name")) for c in columns if c.get("is_sortable")}
    requested_sort = api_main._portal_database_first_param(parsed, "sort", "")
    requested_direction = api_main._portal_database_first_param(parsed, "direction", "")
    state = {
        "sort": requested_sort if requested_sort in sortable else "trip_start",
        "direction": "asc" if requested_direction == "asc" else "desc",
        "active_filters": {},
        "filter_entries": [],
    }

    def _list(d, c, p, limit=0, offset=0, display_columns=None):
        _SQL_LOG.append(("select", [str(col.get("column_name")) for col in (display_columns or c)]))
        return (rows, state, None)

    def _fetch(d, c, identifier_column, value):
        record = next((r for r in rows if str(r.get("record_id")) == str(value)), None)
        return (record, None) if record else (None, "not_found")

    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_get_portal_database_row_identity_column", _patch("_get_portal_database_row_identity_column", lambda d: identity_col)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (len(rows), state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", _list)),
        ("_count_portal_database_rows_unfiltered", _patch("_count_portal_database_rows_unfiltered", lambda d, c: (48213, None))),
        ("_portal_database_fetch_row_by_identity", _patch("_portal_database_fetch_row_by_identity", _fetch)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
        ("_artifact_explorer_session_secret", _patch("_artifact_explorer_session_secret", lambda: TEST_SECRET)),
    ]
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)
    return response.body.decode("utf-8")


# ===========================================================================
# Harness driver
# ===========================================================================
def _run(scenario: str) -> dict:
    out = subprocess.run(
        ["node", str(HARNESS), scenario],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
    )
    assert out.returncode == 0, f"{scenario}: {out.stderr.strip()}"
    return json.loads(out.stdout)


def _module_code() -> str:
    """The shipped module with its comments removed.

    The prose legitimately *names* the mechanisms S7 refuses to use — that is the
    point of documenting a boundary — so a substring search over the raw file
    would fail on its own explanation. Only executable code is searched.
    """
    text = MODULE.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", text)


def _parse_tsv(payload: str) -> list[list[str]]:
    """Read the approved clipboard grammar back into a rectangle.

    Deliberately a *reader*, not a mirror of the writer: the point is to prove a
    spreadsheet's own parse of the payload recovers the exact cells, so tabs,
    line breaks, quotes and edge whitespace inside a value cannot shift a
    column, invent a row or lose meaningful padding.
    """
    rows: list[list[str]] = []
    field: list[str] = []
    row: list[str] = []
    index = 0
    quoted = False
    length = len(payload)
    while index < length:
        char = payload[index]
        if quoted:
            if char == '"':
                if index + 1 < length and payload[index + 1] == '"':
                    field.append('"')
                    index += 2
                    continue
                quoted = False
                index += 1
                continue
            field.append(char)
            index += 1
            continue
        if char == '"' and not field:
            quoted = True
            index += 1
            continue
        if char == "\t":
            row.append("".join(field))
            field = []
            index += 1
            continue
        if char == "\r" and index + 1 < length and payload[index + 1] == "\n":
            row.append("".join(field))
            rows.append(row)
            field, row = [], []
            index += 2
            continue
        field.append(char)
        index += 1
    row.append("".join(field))
    rows.append(row)
    return rows


# ===========================================================================
# 1. The rectangle
# ===========================================================================
def test_one_cell_selects_and_states_itself_in_the_footer() -> None:
    result = _run("single-cell")
    assert result["selected"] == ["1:driver_name"]
    assert result["active"] == "1:driver_name"
    assert result["footer"] == "zaznaczono 1 wiersz × 1 kolumna · 1 komórka"
    assert result["footerHidden"] is False
    # The product's selection model is the rectangle, not a text highlight.
    assert result["nativeCleared"] is True
    assert result["fetches"] == 0 and result["navigations"] == []
    print("PASS: one visible data cell selects, and the footer states the selection")


def test_ranges_form_in_every_direction() -> None:
    row = _run("row-range")
    assert row["selected"] == ["1:trip_start", "1:driver_name", "1:distance_km"]
    column = _run("column-range")
    assert column["selected"] == ["0:driver_name", "1:driver_name", "2:driver_name"]

    expected = [
        f"{r}:{c}"
        for r in (1, 2, 3)
        for c in ("trip_start", "driver_name", "distance_km")
    ]
    # Down-right and up-left describe the same rectangle; only the active corner
    # differs, because the anchor is wherever the drag began.
    down_right = _run("rectangle-down-right")
    up_left = _run("rectangle-up-left")
    assert down_right["selected"] == expected
    assert up_left["selected"] == expected
    assert down_right["active"] == "3:distance_km"
    assert up_left["active"] == "1:trip_start"

    down_left = _run("rectangle-down-left")
    assert down_left["selected"] == [
        f"{r}:{c}" for r in (0, 1, 2) for c in ("driver_name", "distance_km", "is_billable")
    ]
    up_right = _run("rectangle-up-right")
    assert up_right["selected"] == [
        f"{r}:{c}" for r in (1, 2, 3)
        for c in ("trip_start", "driver_name", "distance_km", "is_billable")
    ]
    print("PASS: one contiguous rectangle forms in all four drag directions")


def test_dragging_back_toward_the_anchor_shrinks_the_rectangle() -> None:
    result = _run("drag-shrinks-back")
    assert result["widest"] == 16, result["widest"]
    assert result["selected"] == ["0:trip_start", "0:driver_name", "1:trip_start", "1:driver_name"]
    print("PASS: a drag back toward the anchor shrinks the rectangle rather than leaving the widest extent")


def test_shift_click_extends_from_the_existing_anchor() -> None:
    result = _run("shift-click-extends")
    assert result["selected"] == [
        f"{r}:{c}" for r in (1, 2, 3) for c in ("driver_name", "distance_km", "is_billable")
    ]
    print("PASS: Shift+click extends the rectangle from the anchor (INTERACTION_SPEC §5)")


def test_a_drag_that_leaves_the_grid_ends_safely() -> None:
    result = _run("pointer-leaves-grid")
    assert result["atRelease"] == ["0:trip_start", "0:driver_name", "1:trip_start", "1:driver_name"]
    # Release commits: a later pointer move over another cell must not extend it.
    assert result["selected"] == result["atRelease"]
    print("PASS: releasing commits the range and a later pointer move does not extend it")


# ===========================================================================
# 2. Keyboard (AC-2)
# ===========================================================================
def test_keyboard_enters_the_grid_and_extends_with_shift_arrows() -> None:
    entered = _run("keyboard-enters-grid")
    assert entered["selected"] == ["2:trip_start"], entered["selected"]

    assert _run("shift-right")["selected"] == ["1:trip_start", "1:driver_name", "1:distance_km"]
    assert _run("shift-left")["selected"] == ["1:distance_km", "1:is_billable"]
    assert _run("shift-down")["selected"] == ["0:driver_name", "1:driver_name", "2:driver_name"]
    assert _run("shift-up")["selected"] == ["2:driver_name", "3:driver_name"]
    print("PASS: keyboard reaches the grid and Shift+arrows extend the range (AC-2)")


def test_shift_arrows_shrink_back_through_the_fixed_anchor() -> None:
    result = _run("shift-shrinks-then-crosses")
    # Grow to 2 then 3 rows, shrink back to 2, then cross the anchor upward.
    assert result["steps"] == [2, 3, 2, 2], result["steps"]
    assert result["selected"] == ["0:driver_name", "1:driver_name"]
    print("PASS: the anchor stays fixed while the active edge grows, shrinks and crosses it")


def test_extension_stops_at_the_page_and_column_bounds() -> None:
    """`D-012` phase 1: a range never leaves the rendered page."""
    for scenario, expected in (
        ("bounds-top", ["0:driver_name"]),
        ("bounds-bottom", ["3:driver_name"]),
        ("bounds-left", ["1:trip_start"]),
        ("bounds-right", ["1:is_billable"]),
    ):
        result = _run(scenario)
        assert result["selected"] == expected, (scenario, result["selected"])
        # No wrap-around, and above all no page request of any kind.
        assert result["navigations"] == [], scenario
        assert result["fetches"] == 0, scenario
    print("PASS: extension stops at the page and column bounds — no wrap, no page crossing")


def test_plain_arrows_move_the_active_cell_and_home_end_reach_the_row_edges() -> None:
    moved = _run("plain-arrow-moves")
    assert moved["selected"] == ["2:is_billable"], moved["selected"]
    assert moved["active"] == "2:is_billable"

    edges = _run("home-and-end")
    assert edges["end"] == ["1:driver_name", "1:distance_km", "1:is_billable"]
    assert edges["selected"] == ["1:trip_start", "1:driver_name"]
    print("PASS: plain arrows move the active cell; Home/End reach the row edges")


def test_editable_controls_keep_ordinary_text_editing() -> None:
    """A user typing a filter keeps `Shift`+arrow and `Ctrl+C` unchanged."""
    result = _run("editable-keeps-shortcuts")
    assert result["shiftPrevented"] is False
    assert result["copyPrevented"] is False
    assert result["escapePrevented"] is False
    assert result["clipboard"] == [], "a field copy must not be hijacked into a range copy"
    # The grid selection is untouched by what happens inside the field.
    assert result["selected"] == ["0:trip_start", "0:driver_name", "1:trip_start", "1:driver_name"]
    print("PASS: Shift+arrow, Ctrl+C and Esc keep their text-editing meaning inside a field")


# ===========================================================================
# 3. Interactive-element exclusion
# ===========================================================================
def test_controls_inside_the_grid_keep_their_own_meaning() -> None:
    for scenario in ("interactive-children-do-not-select", "detail-cell-is-not-selectable"):
        result = _run(scenario)
        assert result["selected"] == [], scenario
        assert result["footerHidden"] is True, scenario
    print("PASS: column menus, resize handles and the row-detail control never start a selection")


def test_row_detail_and_cell_selection_coexist() -> None:
    """`INTERACTION_SPEC.md` §5: a cell range does not open `DB-006`."""
    yielded = _run("row-detail-yields-to-cell-selection")
    assert yielded["navigations"] == [], "a cell press opened the row drawer"
    assert yielded["selected"] == ["1:driver_name"]

    # The row stays openable by its own control and by Enter.
    control = _run("row-detail-opens-from-its-own-control")
    assert len(control["navigations"]) == 1 and "row=tok-OPAQUE-REF-1" in control["navigations"][0]
    assert control["selected"] == []
    enter = _run("row-detail-opens-on-enter")
    assert len(enter["navigations"]) == 1 and "row=tok-OPAQUE-REF-2" in enter["navigations"][0]

    # Below the cutoff S7 is off, so the pre-S7 whole-row click is intact.
    narrow = _run("row-detail-click-intact-when-narrow")
    assert len(narrow["navigations"]) == 1, narrow["navigations"]
    assert narrow["selected"] == []
    print("PASS: cell selection and row detail coexist without either losing its interaction")


def test_escape_follows_topmost_component_semantics() -> None:
    cleared = _run("escape-clears")
    assert cleared["selected"] == [] and cleared["prevented"] is True
    assert cleared["footerHidden"] is True
    assert cleared["status"] == "", "a stale summary was left in the live region"

    for scenario in ("escape-yields-to-open-menu", "escape-yields-to-row-drawer"):
        result = _run(scenario)
        assert result["prevented"] is False, scenario
        assert len(result["selected"]) == 4, scenario
    print("PASS: Escape clears the selection only when no nearer transient surface owns it")


def test_the_open_drawer_keeps_plain_arrow_traversal() -> None:
    result = _run("drawer-keeps-plain-arrows")
    assert result["plainPrevented"] is False, "S7 stole the drawer's own traversal"
    assert result["plainSelection"] == ["1:driver_name"]
    assert result["shiftedPrevented"] is True
    assert result["selected"] == ["1:driver_name", "2:driver_name"]
    print("PASS: with the drawer open, plain arrows traverse rows and only Shift+arrows extend the range")


# ===========================================================================
# 4. The clipboard rectangle (DB-44)
# ===========================================================================
def test_copy_produces_a_tsv_rectangle_from_canonical_values() -> None:
    single = _run("copy-single")
    assert single["prevented"] is True
    assert single["clipboard"] == ["Kowalski 1"]

    pair = _run("copy-rectangle")
    payload = pair["clipboard"][0]
    assert _parse_tsv(payload) == [
        ["2026-05-28T07:31:45.512+00:00", "Kowalski 1"],
        ["2026-05-28T07:32:45.512+00:00", "Kowalski 2"],
    ]
    assert "\r\n" in payload and payload.count("\t") == 2

    wide = _run("copy-wide-rectangle")
    grid = _parse_tsv(wide["clipboard"][0])
    assert len(grid) == 3 and all(len(row) == 4 for row in grid)
    print("PASS: Ctrl/⌘+C produces a rectangular TSV payload (DB-44)")


def test_copy_uses_the_canonical_value_not_the_rendered_text() -> None:
    """The S2 copy contract, reused rather than reconstructed."""
    grid = _parse_tsv(_run("copy-wide-rectangle")["clipboard"][0])
    # Grouped number → canonical value; minute-precision timestamp → full value;
    # boolean badge → canonical value, not the Polish label or any markup.
    assert grid[0] == ["2026-05-28T07:30:45.512+00:00", "Kowalski 0", "1284.40", "True"]
    assert grid[1][3] == "False"
    flat = "\n".join("\t".join(row) for row in grid)
    for forbidden in ("1 284,4", "TAK", "NIE", "<span", "db-badge-bool", "…"):
        assert forbidden not in flat, forbidden
    print("PASS: the clipboard carries canonical copy values, never the decorative rendered text")


def test_long_and_structured_values_copy_whole() -> None:
    result = _run("copy-long-and-structured")
    row = _parse_tsv(result["clipboard"][0])[0]
    assert row[0] == "0c64dabd-d486-43f9-887d-fa81cd4cbcac", "a mid-truncated identifier was copied"
    assert len(row[1]) == result["longLength"] == 420, "long text was truncated into the clipboard"
    assert json.loads(row[2]) == {"driver": "Kowalski", "legs": [1, 2, 3], "note": "a\tb"}
    print("PASS: long text, identifiers and structured values copy in full, never as the preview")


def test_clipboard_order_follows_the_visible_column_order() -> None:
    result = _run("copy-reordered-columns")
    assert _parse_tsv(result["clipboard"][0]) == [
        ["Kowalski 0", "True", "2026-05-28T07:30:45.512+00:00"],
        ["Kowalski 1", "False", "2026-05-28T07:31:45.512+00:00"],
    ]
    print("PASS: clipboard cell order matches the S5 display order, not the catalogue order")


def test_null_and_empty_string_both_copy_as_empty_without_collapsing_the_rectangle() -> None:
    """The approved copy-layer limitation, stated rather than silently changed.

    `SCREEN_STATE_MATRIX.md` gives *copy yields empty* for both a SQL NULL and an
    empty string, so the clipboard cannot distinguish them. The distinction the
    product does preserve is in the rendering (`brak wartości` vs `pusty tekst`)
    and in the row-detail panel; S7 leaves both untouched.
    """
    grid = _parse_tsv(_run("copy-null-and-empty")["clipboard"][0])
    assert grid == [["a", "", "", "b"], ["c", "d", "", "e"]]
    print("PASS: NULL and empty string both copy as empty and neither collapses the rectangle")


def test_special_characters_cannot_corrupt_the_rectangle() -> None:
    """Tabs, both line-break characters, quotes and edge whitespace."""
    payload = _run("copy-special-characters")["clipboard"][0]
    grid = _parse_tsv(payload)
    assert grid == [
        ["left\tright", "first\nsecond", "one\r\ntwo", "alpha\rbeta"],
        ['say "hi"', "  padded  ", 'a\t"b"', "plain"],
    ], grid
    # Geometry survives: two rows of four, exactly as selected.
    assert len(grid) == 2 and all(len(row) == 4 for row in grid)
    # The quoting is the spreadsheet convention: quoted field, doubled quotes.
    assert payload.startswith('"left\tright"\t')
    assert '"say ""hi"""' in payload
    print("PASS: tabs, CR, LF, quotes and edge whitespace round-trip without shifting the rectangle")


def test_copy_is_not_intercepted_without_a_grid_selection() -> None:
    result = _run("copy-without-selection")
    assert result["prevented"] is False
    assert result["clipboard"] == [] and result["execCopies"] == []
    print("PASS: with no grid selection, Ctrl+C stays an ordinary browser copy")


# ===========================================================================
# 5. Clipboard feedback
# ===========================================================================
def test_copy_feedback_reports_the_count_and_never_the_data() -> None:
    ok = _run("copy-success-feedback")
    assert ok["status"] == "Skopiowano 4 komórki do schowka."
    assert "Kowalski" not in ok["status"] and "2026-05-28" not in ok["status"]
    print("PASS: a successful copy announces the count in the live region, never the data")


def test_clipboard_failures_are_handled_without_losing_the_selection() -> None:
    fell_back = _run("copy-rejected-falls-back")
    assert fell_back["execCopies"] == ["copy"], "the rejected write did not fall back"
    assert fell_back["status"] == "Skopiowano 2 komórki do schowka."

    failed = _run("copy-rejected-and-fallback-fails")
    assert failed["status"] == "Nie udało się skopiować zaznaczenia do schowka."
    assert len(failed["selected"]) == 2, "a failed copy destroyed the selection"
    assert "Kowalski" not in failed["status"]

    absent = _run("copy-without-clipboard-api")
    assert absent["execCopies"] == ["copy"] and absent["status"].startswith("Skopiowano")

    throwing = _run("copy-throwing-clipboard")
    assert throwing["execCopies"] == ["copy"], "a throwing clipboard API broke the copy path"
    assert len(throwing["selected"]) == 2
    print("PASS: rejected, absent and throwing clipboard APIs all degrade safely")


# ===========================================================================
# 6. S5 layout compatibility
# ===========================================================================
def test_column_reorder_never_attaches_the_selection_to_another_value() -> None:
    result = _run("reorder-keeps-value-identity")
    assert result["before"] == ["0:driver_name", "0:distance_km", "1:driver_name", "1:distance_km"]
    assert result["selected"] == result["before"], "the selection followed positions, not columns"
    assert _parse_tsv(result["clipboard"][0]) == [
        ["Kowalski 0", "1284.40"],
        ["Kowalski 1", "1284.41"],
    ]
    print("PASS: an S5 reorder moves whole columns and the selection keeps the same values")


def test_pinning_and_width_changes_preserve_the_selected_values() -> None:
    """Pure layout changes are harmless: they move geometry, not identity."""
    result = _run("pin-and-width-preserve-selection")
    assert result["selected"] == result["before"], "a pin or width change moved the selection"
    assert result["pinnedStillInRange"] is True, (
        "a pinned cell inside the rectangle dropped out of the range"
    )
    assert _parse_tsv(result["clipboard"][0]) == [
        ["2026-05-28T07:30:45.512+00:00", "Kowalski 0", "1284.40"],
        ["2026-05-28T07:31:45.512+00:00", "Kowalski 1", "1284.41"],
    ]
    print("PASS: pin/unpin and width changes leave the selected values and the rectangle intact")


def test_the_rectangle_holds_at_the_largest_page_size() -> None:
    """200 rows × 12 columns — the top of the product's page-size range."""
    result = _run("large-page-rectangle")
    assert result["cells"] == 2400, result["cells"]
    assert result["lines"] == 200, result["lines"]
    assert result["firstLine"].split("\t") == [f"0-{c}" for c in range(12)]
    assert result["lastLine"].split("\t") == [f"199-{c}" for c in range(12)]
    assert result["footer"] == "zaznaczono 200 wierszy × 12 kolumn · 2400 komórek"
    print("PASS: the rectangle, the footer and the payload geometry hold on a 200-row page")


def test_hiding_a_selected_column_clears_rather_than_leaving_a_ghost() -> None:
    result = _run("hidden-column-clears-selection")
    assert len(result["before"]) == 4
    assert result["selected"] == [], "an invisible ghost selection survived"
    assert result["footerHidden"] is True
    assert result["clipboard"] == [] and result["prevented"] is False, (
        "a hidden column's values were still copyable"
    )
    print("PASS: hiding a selected column clears the selection instead of leaving a ghost range")


# ===========================================================================
# 7. Invalidation and persistence
# ===========================================================================
def test_the_grid_publishes_row_positions_and_nothing_else() -> None:
    """The S7→S8 boundary: positions out, never references and never values.

    The export panel resolves a position to its opaque S6 reference itself, so
    the grid keeps knowing nothing about row identity or about export — which is
    what the hidden-identity test below is able to assert unconditionally.
    """
    result = _run("selection-publishes-row-positions")
    published = result["published"]
    assert published, "the grid published no selection change"
    assert all(event["type"] == "db-selection-change" for event in published), published
    assert published[-1]["detail"]["rows"] == [1, 2], published[-1]
    assert published[-1]["detail"]["cells"] == 4, published[-1]
    # Positions only: no reference, no value, no column name.
    assert set(published[-1]["detail"]) == {"rows", "cells"}, published[-1]
    # Clearing publishes an empty set, so a consumer cannot keep a stale row set.
    assert result["last"]["detail"]["rows"] == [], result["last"]
    print("PASS: the grid publishes touched row positions only, and publishes the clear as well")


def test_selection_is_never_serialized_anywhere() -> None:
    module = _module_code()
    for forbidden in (
        "localStorage", "sessionStorage", "pushState", "replaceState",
        "fetch(", "XMLHttpRequest", "location.assign", "location.href =",
    ):
        assert forbidden not in module, f"S7 must not reach for {forbidden}"
    # And no URL parameter of its own.
    assert "selection=" not in module and "select=" not in module

    # A filter, a sort and a page change are real navigations here, so the next
    # document simply has no selection: the footer ships empty and hidden.
    for query in ("", "sort=driver_name&direction=asc", "page=2", "driver_name_op=contains&driver_name=Kowal"):
        html = _render(query=query)
        marker = re.search(r'<span class="db-select-count" data-db-select-count([^>]*)>(.*?)</span>', html)
        assert marker, query
        assert "hidden" in marker.group(1), query
        assert marker.group(2) == "", query
        assert "selection=" not in html, query
    print("PASS: selection lives only in the page — no URL, no storage, no server state")


def test_popstate_clears_the_selection() -> None:
    result = _run("popstate-clears")
    assert len(result["before"]) == 4 and result["selected"] == []
    assert result["footerHidden"] is True
    print("PASS: a history move clears the selection rather than carrying a stale rectangle")


# ===========================================================================
# 8. Responsive (RESPONSIVE_SPEC)
# ===========================================================================
def test_selection_is_disabled_at_or_below_768px() -> None:
    result = _run("narrow-viewport-disables")
    assert result["enabled"] == "off"
    assert result["mediaQuery"] == "(min-width: 769px)", result["mediaQuery"]
    assert result["downPrevented"] is False, "a pointer press was still intercepted"
    assert result["arrowPrevented"] is False, "an arrow key was still intercepted"
    assert result["copyPrevented"] is False, "range copy was still intercepted"
    assert result["selected"] == [] and result["footerHidden"] is True
    assert result["clipboard"] == [] and result["execCopies"] == []
    print("PASS: at ≤768 px no pointer, keyboard or copy interaction is intercepted")


def test_crossing_the_breakpoint_deactivates_and_clears() -> None:
    result = _run("crossing-the-cutoff-clears")
    assert len(result["before"]["selected"]) == 9
    assert result["selected"] == [] and result["footerHidden"] is True, "a stale footer count survived"
    assert result["enabled"] == "off"
    # Returning above the cutoff re-enables the feature from an unselected state.
    assert result["backOn"] == "on" and result["afterReturn"] == []
    print("PASS: crossing the cutoff deactivates the feature and returning starts unselected")


def test_the_footer_hint_is_hidden_below_the_cutoff_by_the_shipped_css() -> None:
    # The selection layer moved to its own sheet when the Eco tables started
    # sharing it (`UI-20260827-02`); the cutoff rule moved with it.
    css = (REPO_ROOT / "api" / "static" / "css" / "grid-selection.css").read_text(encoding="utf-8")
    block = css.split("@media (max-width: 768px)")[-1]
    assert ".db-select-count" in block and ".db-select-hint" in block, (
        "the selection footer still promises a feature that is disabled"
    )
    print("PASS: the shipped CSS hides the selection footer below the approved cutoff")


# ===========================================================================
# 9. The hidden row identity boundary (S6, unchanged)
# ===========================================================================
def test_the_technical_row_identity_is_outside_the_selection_universe() -> None:
    for legacy_visible in (False, True):
        html = _render(legacy_visible=legacy_visible)
        label = f"legacy_visible={legacy_visible}"
        assert RAW_IDENTITY not in html, label
        assert "record_id" not in html, label
        # The selectable universe is exactly `td[data-db-column]`; the identifier
        # is not among the column identities the sheet renders.
        assert "record_id" not in " ".join(re.findall(r'data-db-column="([^"]+)"', html)), label
        # The detail column — which carries the opaque reference — has no column
        # identity, so it can never enter the geometry.
        assert 'class="db-detail-col"' in html, label
        assert not re.search(r'<td class="db-detail-col"[^>]*data-db-column', html), label

    module = _module_code()
    assert "record_id" not in module
    # The module may *name* the row-detail control and drawer, because it has to
    # step aside for them; what it must never do is read the reference value.
    assert 'getAttribute("data-db-row")' not in module
    assert not re.search(r'\[data-db-row\]|data-db-row="', module), (
        "the opaque row reference entered the S7 data model"
    )

    payload = _run("module-holds-no-identity")
    assert RAW_IDENTITY not in payload["clipboard"][0]
    assert "tok-OPAQUE" not in payload["clipboard"][0], "an opaque reference was copied as cell data"
    assert "tok-OPAQUE" not in payload["footer"] and "tok-OPAQUE" not in payload["status"]
    print("PASS: raw record_id and the opaque row reference are neither selectable nor copyable")


# ===========================================================================
# 10. No new surface
# ===========================================================================
def test_s7_adds_no_query_endpoint_or_export_behaviour() -> None:
    for scenario in ("copy-wide-rectangle", "rectangle-down-right", "shift-right"):
        result = _run(scenario)
        assert result["fetches"] == 0, scenario
        assert result["navigations"] == [], scenario

    module = _module_code()
    for forbidden in ("database_export_jobs", "/export", "format_name", "checkbox", "Zaznaczone wiersze"):
        assert forbidden not in module, f"S8 export surface leaked into S7: {forbidden}"

    html = _render()
    # No row-checkbox column, no select-all header control.
    assert 'type="checkbox"' not in html.split('<table class="db-table"')[1].split("</table>")[0]
    print("PASS: S7 issues no request, adds no export path and introduces no row-checkbox feature")


# ===========================================================================
# 11. Rendered page and vocabulary
# ===========================================================================
def test_row_browser_page_actually_loads_the_selection_module() -> None:
    html = _render()
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    selection = [src for src in scripts if "data-grid-selection.js" in src]
    assert len(selection) == 1, scripts
    assert re.search(r"/static/js/data-grid-selection\.js\?v=[0-9a-f]+", selection[0]), selection
    for variant, label in (
        (_render(identity=False), "no configured identity"),
        (_render(query="cols=driver_name,distance_km"), "narrowed columns"),
        (_render(rows=[]), "empty page"),
    ):
        assert "data-grid-selection.js" in variant, label
    print("PASS: the rendered row-browser page loads the versioned S7 module")


def test_selection_module_is_page_scoped_not_global() -> None:
    patches = [
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [])),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        catalogue = api_main._user_database_response(_user(), _FakeRequest()).body.decode("utf-8")
    finally:
        _restore(patches)
    assert "data-grid-selection.js" not in catalogue, "the module leaked onto a non-grid page"
    print("PASS: the selection module stays page-scoped to the row browser")


def test_footer_supplements_rather_than_replaces_and_ships_the_approved_copy() -> None:
    html = _render()
    footer = html.split('<div class="db-footer"')[1].split("</div></div>")[0]
    # Pager, page-size control and the result counter all survive.
    assert "db-pager" in footer and "db-page-size" in footer
    assert "wierszy na stronie" in footer
    assert "db-counter" in html
    # The approved copy hint, verbatim.
    assert html_mod.escape("zaznacz zakres i ⌘C, aby skopiować do arkusza") in html or \
        "zaznacz zakres i ⌘C, aby skopiować do arkusza" in html
    # One polite live region for copy and selection feedback.
    assert 'role="status" aria-live="polite"' in footer and "data-db-select-status" in footer
    print("PASS: the footer gains a selection counter, a copy hint and a live region, losing nothing")


def test_the_module_ships_no_polish_and_reads_it_from_the_catalogue() -> None:
    code = _module_code()
    for word in ("zaznacz", "Skopiowano", "wiersze", "komórek", "kolumny"):
        assert word not in code, f"the module hard-codes the Polish string {word!r}"

    catalogue = json.loads(html_mod.unescape(
        re.search(r'data-db-select-strings="([^"]*)"', _render()).group(1)
    ))
    assert catalogue["summary"] == "zaznaczono {rows} × {columns} · {cells}"
    assert catalogue["copied"] == "Skopiowano {cells} do schowka."
    assert catalogue["row"] == ["wiersz", "wiersze", "wierszy"]
    assert catalogue["column"] == ["kolumna", "kolumny", "kolumn"]
    assert catalogue["cell"] == ["komórka", "komórki", "komórek"]
    known = i18n.available_keys()
    for key in (
        "db.select.hint", "db.select.summary", "db.select.copied", "db.select.copy_failed",
        "db.select.status_aria", "db.select.row.one", "db.select.cell.many",
    ):
        assert key in known, key
    print("PASS: every S7 string travels through the translation catalogue (D-010)")


def test_polish_plural_selection_matches_the_server_rule() -> None:
    """The module holds the rule; the server holds the words. They must agree."""
    for count in (1, 2, 3, 4, 5, 11, 12, 13, 14, 21, 22, 25, 102, 112):
        expected = api_main._portal_database_polish_plural(count, "komórka", "komórki", "komórek")
        # Mirror of the rule shipped in data-grid-selection.js.
        if count == 1:
            actual = "komórka"
        elif 12 <= count % 100 <= 14:
            actual = "komórek"
        elif 2 <= count % 10 <= 4:
            actual = "komórki"
        else:
            actual = "komórek"
        assert actual == expected, count
    module = MODULE.read_text(encoding="utf-8")
    assert "lastTwo >= 12 && lastTwo <= 14" in module, "the teens exception is missing"
    print("PASS: the module's Polish plural rule matches the server's")


def test_no_framework_or_dependency_arrives_with_s7() -> None:
    code = _module_code()
    for forbidden in ("import ", "require(", "React", "Vue", "Svelte", "from '"):
        assert forbidden not in code, forbidden
    raw = MODULE.read_text(encoding="utf-8")
    assert raw.lstrip().startswith("/*") and "(function () {" in raw
    print("PASS: S7 stays a bounded vanilla ES module with no dependency")


def main() -> None:
    test_one_cell_selects_and_states_itself_in_the_footer()
    test_ranges_form_in_every_direction()
    test_dragging_back_toward_the_anchor_shrinks_the_rectangle()
    test_shift_click_extends_from_the_existing_anchor()
    test_a_drag_that_leaves_the_grid_ends_safely()
    test_keyboard_enters_the_grid_and_extends_with_shift_arrows()
    test_shift_arrows_shrink_back_through_the_fixed_anchor()
    test_extension_stops_at_the_page_and_column_bounds()
    test_plain_arrows_move_the_active_cell_and_home_end_reach_the_row_edges()
    test_editable_controls_keep_ordinary_text_editing()
    test_controls_inside_the_grid_keep_their_own_meaning()
    test_row_detail_and_cell_selection_coexist()
    test_escape_follows_topmost_component_semantics()
    test_the_open_drawer_keeps_plain_arrow_traversal()
    test_copy_produces_a_tsv_rectangle_from_canonical_values()
    test_copy_uses_the_canonical_value_not_the_rendered_text()
    test_long_and_structured_values_copy_whole()
    test_clipboard_order_follows_the_visible_column_order()
    test_null_and_empty_string_both_copy_as_empty_without_collapsing_the_rectangle()
    test_special_characters_cannot_corrupt_the_rectangle()
    test_copy_is_not_intercepted_without_a_grid_selection()
    test_copy_feedback_reports_the_count_and_never_the_data()
    test_clipboard_failures_are_handled_without_losing_the_selection()
    test_column_reorder_never_attaches_the_selection_to_another_value()
    test_pinning_and_width_changes_preserve_the_selected_values()
    test_the_rectangle_holds_at_the_largest_page_size()
    test_hiding_a_selected_column_clears_rather_than_leaving_a_ghost()
    test_the_grid_publishes_row_positions_and_nothing_else()
    test_selection_is_never_serialized_anywhere()
    test_popstate_clears_the_selection()
    test_selection_is_disabled_at_or_below_768px()
    test_crossing_the_breakpoint_deactivates_and_clears()
    test_the_footer_hint_is_hidden_below_the_cutoff_by_the_shipped_css()
    test_the_technical_row_identity_is_outside_the_selection_universe()
    test_s7_adds_no_query_endpoint_or_export_behaviour()
    test_row_browser_page_actually_loads_the_selection_module()
    test_selection_module_is_page_scoped_not_global()
    test_footer_supplements_rather_than_replaces_and_ships_the_approved_copy()
    test_the_module_ships_no_polish_and_reads_it_from_the_catalogue()
    test_polish_plural_selection_matches_the_server_rule()
    test_no_framework_or_dependency_arrives_with_s7()
    print("\nALL GRID SELECTION AND CLIPBOARD TESTS PASSED")


if __name__ == "__main__":
    main()
