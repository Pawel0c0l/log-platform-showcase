#!/usr/bin/env python3
"""Database Explorer responsive and accessibility completion (approved stage S10).

Covers `RSP-001`–`RSP-003`, `RS-1`–`RS-11` and `AC-1`–`AC-14`: the approved
three-band responsive contract and its exact boundaries, the collapsed
navigation shell, the `RSP-002` filter drawer with its staged-state guarantee,
the `RSP-003` below-768 advisory and its session-scoped `Otwórz mimo to`
acknowledgement, the 44 px interaction-target model, focus containment and
return, single-layer `Esc` precedence, `aria-sort`, live-region and busy
semantics, and reduced motion.

Approved design reference (read-only, not tracked in this repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— ``RESPONSIVE_SPEC.md``, ``ACCESSIBILITY_SPEC.md``, ``INTERACTION_SPEC.md``,
``COPY_AND_TERMINOLOGY.md`` §9, ``IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`` §6–§7.

No browser automation exists in this environment, so the JavaScript half runs
the shipped modules against a DOM stub (`data_grid_responsive_harness.js`) and
the CSS half is asserted against the shipped stylesheet's own declarations.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_responsive_accessibility.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HARNESS = REPO_ROOT / "ops" / "tests_manual" / "data_grid_responsive_harness.js"
STATIC = REPO_ROOT / "api" / "static"
PORTAL_CSS = (STATIC / "css" / "portal.css").read_text(encoding="utf-8")
# The grid layer is what the dataset page ships, which since `UI-20260827-02` is
# two files: the Database Explorer's own chrome and the selection presentation
# layer it now shares with the Eco tables. They are concatenated in load order,
# so the band contract below is asserted against the layer the browser actually
# resolves rather than against whichever half a rule happens to live in.
GRID_CSS = "\n".join((
    (STATIC / "css" / "data-grid.css").read_text(encoding="utf-8"),
    (STATIC / "css" / "grid-selection.css").read_text(encoding="utf-8"),
))
RESPONSIVE_JS = (STATIC / "js" / "data-grid-responsive.js").read_text(encoding="utf-8")
SHELL_JS = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
FILTERS_JS = (STATIC / "js" / "data-grid-filters.js").read_text(encoding="utf-8")
COLUMNS_JS = (STATIC / "js" / "data-grid-columns.js").read_text(encoding="utf-8")
ROW_DETAIL_JS = (STATIC / "js" / "data-grid-row-detail.js").read_text(encoding="utf-8")
SELECTION_JS = (STATIC / "js" / "data-grid-selection.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Import stubs (same shape as the other Database Explorer suites)
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
from api.portal_ui import assets as portal_assets  # noqa: E402
from api.portal_ui import shell as portal_shell  # noqa: E402
from api.portal_ui.i18n import t as tr  # noqa: E402

USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


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
    # `note` is visible but deliberately NOT sortable: a header that cannot be
    # sorted must not advertise a sort state at all.
    return [
        _col("trip_date", "Trip date", "date", display_order=10),
        _col("driver_name", "Driver", "text", display_order=20),
        _col("distance_km", "Distance", "numeric", display_order=30),
        _col("note", "Note", "text", is_sortable=False, is_filterable=False, display_order=40),
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _render(*, query="", dataset=None, columns=None):
    dataset = dataset if dataset is not None else _dataset()
    columns = columns if columns is not None else _columns()
    rows = [{"trip_date": date(2026, 5, 28), "driver_name": "Alice",
             "distance_km": Decimal("128.40"), "note": "ok"}]
    parsed = api_main._portal_database_query_params(_FakeRequest(query=query))
    allowed = {c["column_name"] for c in columns if c.get("is_sortable")}
    requested = api_main._portal_database_first_param(parsed, "sort", "")
    resolved_sort = requested if requested in allowed else "trip_date"
    resolved_dir = "asc" if api_main._portal_database_first_param(parsed, "direction", "") == "asc" else "desc"
    _c, _v, applied_filters, _e, filter_entries = api_main._build_portal_database_filter_conditions(
        dataset, columns, parsed
    )
    state = {"sort": resolved_sort, "direction": resolved_dir,
             "active_filters": applied_filters, "filter_entries": filter_entries}
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda d, u: dataset)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda d: columns)),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda d, c, p: (1, state, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows",
                                              lambda d, c, p, limit, offset, display_columns=None: (rows, state, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        response = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(query=query))
    finally:
        _restore(patches)
    return response.body.decode("utf-8")


def _run(scenario: str) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), scenario],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    assert proc.returncode == 0, f"{scenario}: {proc.stderr}"
    return json.loads(proc.stdout)


def _top_level(css: str) -> str:
    """The stylesheet with every `@media` block removed.

    What remains is the unconditional cascade — which is where a responsive
    duplicate must be `display: none` if it is to be inert by default.
    """
    out = []
    index = 0
    while index < len(css):
        at = css.find("@media", index)
        if at == -1:
            out.append(css[index:])
            break
        out.append(css[index:at])
        depth = 0
        cursor = css.index("{", at)
        for position in range(cursor, len(css)):
            if css[position] == "{":
                depth += 1
            elif css[position] == "}":
                depth -= 1
                if depth == 0:
                    index = position + 1
                    break
        else:
            raise AssertionError("unterminated media block")
    return "".join(out)


def _media_block(css: str, query: str) -> str:
    """Every top-level `@media` block for one query, concatenated.

    A band may legitimately be expressed in more than one block (the grid frame
    and the `RSP-002` drawer are authored separately), so the assertion target
    is the band's whole declared behaviour rather than whichever block happens
    to come first.
    """
    marker = f"@media {query} {{"
    bodies = []
    cursor = 0
    while True:
        start = css.find(marker, cursor)
        if start == -1:
            break
        depth = 0
        for index in range(start + len(marker) - 1, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(css[start + len(marker):index])
                    cursor = index + 1
                    break
        else:
            raise AssertionError(f"unterminated media block: {query}")
    assert bodies, f"no media block for {query}"
    return "\n".join(bodies)


# ===========================================================================
# 1. The approved band model and its exact boundaries (RSP-001, RS-1, RS-11)
# ===========================================================================
def test_the_shell_uses_exactly_the_approved_bands() -> None:
    shell_queries = set(re.findall(r"@media \(max-width: (\d+)px\)", PORTAL_CSS))
    # 1439 is the pre-existing `bp/desktop` padding step, which the approved
    # breakpoint table also defines. Nothing else may exist: an ad-hoc width
    # would make the final responsive behaviour indeterminate.
    assert shell_queries == {"1439", "1279", "1023", "767"}, sorted(shell_queries)

    grid_queries = set(re.findall(r"@media \(max-width: (\d+)px\)", GRID_CSS))
    # S7's own ≤768 px cutoff is its established contract and is deliberately
    # unchanged by this stage; every other Database Explorer rule sits on an
    # approved band boundary.
    assert grid_queries == {"1279", "1023", "768"}, sorted(grid_queries)
    assert "max-width: 900px" not in GRID_CSS and "max-width: 760px" not in GRID_CSS

    band = _media_block(PORTAL_CSS, "(max-width: 1279px)")
    # RS-1: at 1024 px the primary navigation is a menu button AND the active
    # module name is still visible.
    assert ".lp-nav { display: none; }" in band
    assert ".lp-nav-toggle { display: inline-flex; }" in band
    assert ".lp-nav-current { display: inline-flex; }" in band

    narrower = _media_block(PORTAL_CSS, "(max-width: 1023px)")
    assert ".lp-nav-current { display: none; }" in narrower
    print("PASS: the shell and the grid use exactly the approved responsive bands")


def test_the_active_module_name_is_not_a_second_navigation_control() -> None:
    items = portal_shell.primary_nav_items(active_key="database", is_admin=False, has_eco_ranking_access=False)
    appbar = portal_shell.appbar_html(nav_items=items, user_label="Alice")

    assert '<span class="lp-nav-current" data-nav-current>Dane</span>' in appbar, appbar
    # It is a span: not focusable, not a link, and it never duplicates the
    # `aria-current` semantics the real navigation item carries.
    assert 'href' not in appbar.split('data-nav-current>')[1].split("</span>")[0]
    assert appbar.count('aria-current="page"') == 1, appbar
    # The trigger and the surface it opens are one relationship.
    assert 'aria-controls="lp-nav-drawer"' in appbar, appbar
    assert f'id="{portal_shell.NAV_DRAWER_ID}"' in portal_shell.nav_drawer_html(items, user_label="Alice")
    # `.lp-nav-current` is absent — not merely invisible — outside its band.
    base = PORTAL_CSS.split("@media (max-width: 1279px)")[0]
    assert ".lp-nav-current {\n  display: none;" in base, "the module name must default to absent"
    print("PASS: the collapsed-nav module name is presentation, not a second navigation model")


def test_the_navigation_drawer_reveals_no_unauthorized_entry() -> None:
    plain = portal_shell.primary_nav_items(active_key="database", is_admin=False, has_eco_ranking_access=False)
    drawer = portal_shell.nav_drawer_html(plain, user_label="Alice")
    for forbidden in ("Artefakty", "Administracja", "Analizy"):
        assert forbidden not in drawer, forbidden
    assert "/artifact-explorer" not in drawer and "/admin" not in drawer, drawer
    # The account controls the app bar drops at <=1023 px stay reachable here.
    assert "/logout" in drawer and "Alice" in drawer, drawer
    assert 'aria-current="page"' in drawer, drawer

    admin = portal_shell.primary_nav_items(active_key="database", is_admin=True, has_eco_ranking_access=True)
    admin_drawer = portal_shell.nav_drawer_html(admin, user_label="Root")
    for expected in ("Artefakty", "Administracja", "Analizy"):
        assert expected in admin_drawer, expected
    # One authorization model: the drawer renders the same list the app bar does.
    assert admin_drawer.count("lp-nav-drawer-item") == len(admin) + 1, admin_drawer
    print("PASS: the navigation drawer is the same authorization model as the app bar")


def test_client_name_and_code_survive_every_band() -> None:
    html = _render()
    context = html[html.index('<div class="lp-context'):html.index('<nav class="lp-subnav"')
                   if '<nav class="lp-subnav"' in html else html.index('<main')]
    assert "Acme Logistics" in context and "ACME_01" in context, context
    assert "TYLKO ODCZYT" in context, context

    # RS-10: nothing in any band removes the client identity, and the <768 px
    # band restates it explicitly.
    for query in ("(max-width: 1279px)", "(max-width: 1023px)", "(max-width: 767px)"):
        block = _media_block(PORTAL_CSS, query)
        assert "lp-client-name { display: none" not in block, query
        assert "lp-client-code { display: none" not in block, query
    narrow = _media_block(PORTAL_CSS, "(max-width: 767px)")
    assert ".lp-client-code { max-width: none; }" in narrow, narrow

    # What DOES give way is the physical table name and the column count, and
    # both carry the class the stylesheet keys on.
    assert 'class="lp-mono lp-context-physical"' in context, context
    assert 'class="lp-context-colcount"' in context, context
    tablet = _media_block(PORTAL_CSS, "(max-width: 1279px)")
    assert ".lp-context-physical,\n  .lp-context-colcount { display: none; }" in tablet, tablet
    print("PASS: the client name and code are available at every band; only the meta degrades")


# ===========================================================================
# 2. The table stays a table (RS-3, RS-4)
# ===========================================================================
def test_the_table_is_never_converted_at_any_width() -> None:
    for query in ("(max-width: 1279px)", "(max-width: 1023px)", "(max-width: 768px)"):
        block = _media_block(GRID_CSS, query)
        for forbidden in ("display: block", "display: grid", "display: flex"):
            assert f".db-table {forbidden}" not in block, (query, forbidden)
        assert "db-card" not in block, query
        assert ".db-table tbody tr { display" not in block, query
        assert ".db-table tbody td { display" not in block, query

    band = _media_block(GRID_CSS, "(max-width: 1023px)")
    # The scroll box is what the sticky header sticks to and what makes the
    # horizontal scroll the band expects possible. Removing it silently unsticks
    # the header, which is exactly the pre-approval defect.
    assert ".db-table-scroll { max-height: 70vh; overflow: auto; }" in band, band
    assert ".db-table-scroll { display: none" not in band
    assert "position: sticky" in GRID_CSS.split("@media")[0], "the header must stay sticky"

    html = _render()
    assert "<table" in html and "<thead>" in html and 'scope="col"' in html, html[:400]
    assert 'class="db-sticky-col"' in html or "db-sticky-col" in html, "the identifier column stays pinned"
    # `DB-001` stays a comparison table: the rail stacks above it and the table
    # keeps its own horizontal scroll rather than becoming a card grid.
    assert ".db-catalogue { flex-direction: column; }" in band, band
    assert ".db-cat-table-wrap { overflow-x: auto; }" in band, band
    print("PASS: the table remains a semantic table with a sticky header at every supported width")


# ===========================================================================
# 3. Interaction targets (RS-2)
# ===========================================================================
def test_the_44px_target_model_without_destroying_density() -> None:
    band = _media_block(GRID_CSS, "(max-width: 1279px)")
    families = (
        ".db-tool-button", ".db-page-button", ".db-size", ".db-density-option", ".db-preset",
        ".db-state-action", ".db-cat-open", ".db-export-copy", ".db-export-submit",
        ".db-panel-add-item", ".db-col-action", ".db-col-apply", ".db-col-clear",
        ".db-cols-move", ".db-cols-handle", ".db-distribution-pick",
        ".db-row-traverse", ".db-row-close", ".db-filters-trigger", ".db-columns-trigger",
    )
    for family in families:
        assert family in band, family
    assert "min-height: var(--lp-height-control-touch);" in band

    # The chip `×` keeps its 16 px visual box and gains a 44 px hit area from a
    # transparent pseudo-element — the approved shape, and the reason the chip
    # strip keeps its density.
    assert ".db-chip-remove::after,\n  .db-panel-remove::after {" in band, band
    assert "width: var(--lp-height-control-touch);" in band and "height: var(--lp-height-control-touch);" in band

    # Density is untouched: no rule in any responsive band changes the row
    # height tokens or the cell padding.
    for query in ("(max-width: 1279px)", "(max-width: 1023px)", "(max-width: 768px)"):
        block = _media_block(GRID_CSS, query)
        assert "--db-row-height" not in block, query
        assert "--lp-height-row-compact" not in block, query
        assert ".db-table tbody td { height" not in block, query

    shell_band = _media_block(PORTAL_CSS, "(max-width: 1279px)")
    for family in (".portal-button", ".lp-theme-option", ".lp-logout", ".lp-export-indicator"):
        assert family in shell_band, family
    assert "min-height: var(--lp-height-control-touch);" in PORTAL_CSS
    # The nav trigger and the drawer rows are 44 px in every band by construction.
    assert "width: var(--lp-height-control-touch);\n  height: var(--lp-height-control-touch);" in PORTAL_CSS
    assert ".lp-nav-drawer-item {" in PORTAL_CSS and "min-height: var(--lp-height-control-touch);" in PORTAL_CSS
    print("PASS: the 44 px target model is satisfied by hit area, not by inflating grid density")


def test_pointer_only_resize_gives_way_to_the_keyboard_equivalent() -> None:
    tablet = _media_block(GRID_CSS, "(max-width: 1279px)")
    assert ".db-col-resize { width: var(--lp-height-control-touch); }" in tablet, tablet
    portrait = _media_block(GRID_CSS, "(max-width: 1023px)")
    # `display: none` removes it from the tab order as well as the screen, so a
    # hidden responsive control never stays focusable.
    assert ".db-col-resize { display: none; }" in portrait, portrait
    html = _render()
    # The keyboard equivalent stays available in the column menu at every width.
    assert "Dopasuj szerokość" in html or "data-db-autofit-action" in html, "AC-2 keyboard resize path"
    print("PASS: column resize keeps a 44 px drag target, then yields to its keyboard equivalent")


# ===========================================================================
# 4. RSP-002 filter drawer (RS-5, RS-6)
# ===========================================================================
def test_the_filter_surface_is_one_form_relocated_by_css() -> None:
    html = _render(query="filter__driver_name=Kowal&op__driver_name=contains")

    assert html.count("data-db-panel-form") == 1, "exactly one staged filter form may exist"
    assert html.count("data-db-filter-panel") == 1, "exactly one filter panel may exist"
    # Within the staged form there is exactly one successful control per filter
    # parameter. The `p-` prefix is the panel's own field namespace, and the
    # responsive relocation adds no second copy of it. (The column menus carry
    # their own separate `m-` forms by the established S3 contract; only one
    # form ever submits, and this stage did not change that.)
    panel = html[html.index("data-db-panel-form"):html.index("db-panel-actions")]
    assert panel.count('name="filter__driver_name"') == 1, panel
    assert panel.count('name="op__driver_name"') == 1, panel
    assert html.count('id="p-filter__driver_name"') == 1, "the panel field exists once"
    assert "data-db-filter-scrim" in html and "db-filter-drawer-head" in html, html[:200]
    assert "data-db-filter-close" in html, "the drawer needs its own close control"

    band = _media_block(GRID_CSS, "(max-width: 1023px)")
    # The chip strip gives way to the count badge in this band, and every filter
    # is still represented exactly once by its entry inside the drawer.
    assert ".db-chips { display: none; }" in band, band
    assert "db-count-badge" in html and "db-panel-remove" in html, "the filter stays removable"
    assert "position: fixed;" in band and "var(--lp-width-drawer)" in band, band
    assert ".db-filters[open] .db-filter-scrim {" in band, band
    assert "background: var(--lp-elevation-scrim);" in band, band
    # `Zastosuj` / `Wyczyść` pinned at the drawer bottom, 44 px (RS-5).
    assert ".db-panel-actions {" in band and "position: sticky;" in band and "bottom: 0;" in band, band
    assert ".db-panel-apply,\n  .db-panel-clear {" in band, band
    # The drawer head and the scrim are inert at the docked widths: their
    # unconditional (non-media) declaration is `display: none`, so a docked
    # panel never carries a focusable close button.
    base = _top_level(GRID_CSS)
    assert ".db-filter-scrim { display: none; }" in base, base[-2000:]
    assert ".db-filter-drawer-head { display: none; }" in base, base[-2000:]
    print("PASS: the filter drawer is the docked panel relocated, with one form and one staged state")


def test_dismissing_the_filter_drawer_keeps_staged_edits() -> None:
    scrim = _run("drawer-scrim-keeps-staged-edits")
    assert scrim["filtersOpen"] is False, "the scrim must dismiss the drawer"
    assert scrim["submissions"] == [], "dismissal must never apply anything"
    assert scrim["stagedValue"] == "Nowak" and scrim["exactValue"] == "WAW-01", scrim
    assert scrim["reopenedStaged"] == "Nowak" and scrim["reopenedExact"] == "WAW-01", scrim
    assert scrim["focused"] == "filterSummary", "focus returns to Filtry"

    close = _run("drawer-close-button-keeps-staged-edits")
    assert close["filtersOpen"] is False and close["stagedValue"] == "Nowak"
    assert close["submissions"] == [] and close["focused"] == "filterSummary"

    escape = _run("drawer-escape-keeps-staged-edits")
    assert escape["filtersOpen"] is False and escape["stagedValue"] == "Nowak"
    assert escape["prevented"] is True and escape["submissions"] == []
    assert escape["focused"] == "filterSummary"
    print("PASS: scrim, close and Esc all dismiss the filter drawer and keep the staged edits")


def test_crossing_the_breakpoint_neither_applies_nor_duplicates_staged_state() -> None:
    result = _run("drawer-breakpoint-crossing-preserves-staged")
    assert result["submissions"] == [], "a breakpoint change must never submit"
    assert result["stagedValue"] == "Nowak" and result["exactValue"] == "WAW-01", result
    assert result["afterStaged"] == "Nowak" and result["afterExact"] == "WAW-01", result
    assert result["afterOpen"] is True, "the surface stays open across the band change"
    assert result["formCount"] == 1 and result["stagedFieldCount"] == 1 and result["exactFieldCount"] == 1
    # `role` is only claimed where it is true.
    assert result["panelRole"] == "group" and result["panelModal"] is None
    assert result["afterRole"] == "dialog"
    print("PASS: crossing the drawer breakpoint preserves staged state and creates no duplicate control")


def test_the_drawer_is_modal_and_the_docked_panel_is_not() -> None:
    bands = _run("drawer-role-follows-band")
    assert bands["drawer"] == {"role": "dialog", "modal": "true"}, bands
    assert bands["docked"] == {"role": "group", "modal": None}, bands
    assert bands["back"] == {"role": "dialog", "modal": "true"}, bands
    assert _run("drawer-boundary-1024-is-docked")["panelRole"] == "group", "1024 px is the docked band"

    trap = _run("drawer-traps-focus")
    assert trap["entering"] is True and trap["entered"] == "drawerClose"
    assert trap["wrapping"] is True and trap["wrapped"] == "drawerClose"
    assert trap["backwards"] is True and trap["backwardTarget"] == "clear"

    docked = _run("docked-panel-does-not-trap-focus")
    assert docked["prevented"] is False, "the docked filter panel is part of the page and is not trapped"

    yielded = _run("drawer-trap-yields-to-nav-drawer")
    assert yielded["navDrawerHidden"] is False and yielded["navExpanded"] == "true"
    assert yielded["prevented"] is False, "the shell drawer owns focus containment while it is open"
    print("PASS: the filter drawer traps focus only where it is an overlay, and yields to the nav drawer")


# ===========================================================================
# 5. RSP-003 advisory (RS-7, RS-8, RS-9)
# ===========================================================================
def test_the_advisory_copy_is_the_approved_copy() -> None:
    html = _render()
    assert "Poniżej 768 px" in html, "eyebrow"
    assert "Przeglądarka danych wymaga szerszego ekranu" in html, "title"
    assert ("Tabela z 4 kolumnami nie da się rzetelnie obsłużyć na tej szerokości. "
            "Nie zamieniamy jej na karty, bo porównywanie wierszy jest tu całym sensem pracy.") in html
    assert "Przejdź do Raportów" in html and "Otwórz mimo to" in html, html
    assert "Raporty i Eco Driving działają na tej szerokości w pełni." in html

    section = html[html.index("db-narrow-advisory"):html.index("db-narrow-footnote")]
    # Shipped hidden: the viewport is a client fact and the module reveals it.
    assert "hidden" in html[html.index("<section class=\"db-narrow-advisory\""):][:200]
    assert 'href="/user/reports"' in section, "the primary route out is a real link"
    assert "data-db-narrow-accept" in section, "the escape hatch is a button, not a link"
    assert 'aria-labelledby="db-narrow-title"' in html, "the advisory is named by its visible heading"
    # RS-9 is a statement about the other modules, and the footnote is where the
    # advisory makes it.
    footnote = html[html.index("db-narrow-footnote"):]
    assert "Eco Driving" in footnote[:200], footnote[:200]
    print("PASS: the RSP-003 advisory carries the approved Polish copy and both approved routes")


def test_the_advisory_appears_exactly_below_768px() -> None:
    assert _run("boundary-767-shows-advisory")["advisoryHidden"] is False
    assert _run("boundary-768-shows-sheet")["advisoryHidden"] is True
    assert _run("wide-load-shows-sheet")["advisoryHidden"] is True

    first = _run("narrow-first-load")
    assert first["advisoryHidden"] is False
    # The sheet is hidden with the `hidden` attribute, so it leaves the tab
    # order and the accessibility tree together — never merely off-screen.
    assert first["sheetHidden"] is True and first["secondaryHidden"] is True
    assert first["focused"] == "advisoryTitle", "focus moves to the advisory heading"
    assert first["ack"] is None, "showing the advisory writes nothing"
    print("PASS: the advisory appears at 767 px and not at 768 px, and hides the sheet honestly")


def test_open_anyway_works_and_is_remembered_for_the_session_only() -> None:
    accepted = _run("narrow-accept")
    assert accepted["advisoryHidden"] is True and accepted["sheetHidden"] is False
    assert accepted["secondaryHidden"] is False, "the export surface comes back with the sheet"
    assert accepted["submissions"] == [], "accepting must not navigate or submit"
    assert accepted["ack"] == "1", "the acknowledgement lives in session storage"
    assert accepted["focused"] == "filterSummary", "focus follows into the working layout"

    survives = _run("narrow-accept-survives-band-changes")
    assert survives["hiddenWhileWide"] is True
    assert survives["advisoryHidden"] is True, "a re-narrowed viewport must not re-interrupt"

    follows = _run("narrow-without-accept-follows-viewport")
    assert follows["hiddenOnLoad"] is False and follows["hiddenWhileWide"] is True
    assert follows["advisoryHidden"] is False, "without acceptance the advisory returns on re-narrowing"
    assert follows["ack"] is None

    restored = _run("narrow-bfcache-restore")
    assert restored["advisoryHidden"] is True and restored["sheetHidden"] is False

    # Back must not restore a stuck overlay or a stale expanded state either.
    nav = _run("nav-drawer-resets-on-bfcache-restore")
    assert nav["opened"] == {"hidden": False, "expanded": "true"}, nav
    assert nav["hidden"] is True and nav["scrimHidden"] is True, nav
    assert nav["expanded"] == "false", nav
    assert nav["focused"] == "navToggle", "focus must not be left inside a hidden drawer"
    print("PASS: `Otwórz mimo to` opens the sheet and is remembered for the browser session only")


def test_the_acknowledgement_is_not_persistent_and_fails_safely() -> None:
    assert "sessionStorage" in RESPONSIVE_JS
    assert "localStorage" not in RESPONSIVE_JS, "the acknowledgement must never outlive the session"
    for forbidden in ("pushState", "replaceState", "location.search", "fetch(", "XMLHttpRequest"):
        assert forbidden not in RESPONSIVE_JS, forbidden
    assert RESPONSIVE_JS.count('"logplatform.db.narrow-ack"') == 1, "one namespaced key"
    # Nothing about a dataset, a client or a business value is stored.
    key_line = [line for line in RESPONSIVE_JS.splitlines() if "narrow-ack" in line][0]
    assert "dataset" not in key_line and "client" not in key_line

    throwing = _run("narrow-storage-throws")
    assert throwing["hiddenOnLoad"] is False, "a throwing store still shows the advisory"
    assert throwing["hiddenAfterAccept"] is True and throwing["sheetHiddenAfterAccept"] is False
    assert throwing["freshDocumentAdvisoryHidden"] is False, "a throwing store must not fake persistence"

    assert _run("no-sheet-is-inert")["advisoryHidden"] is True
    print("PASS: the acknowledgement is session-scoped, namespaced, non-persistent and fail-safe")


# ===========================================================================
# 6. Row detail responsiveness (S6 preserved)
# ===========================================================================
def test_the_row_panel_becomes_an_overlay_across_the_whole_portrait_band() -> None:
    # The pre-approval rule left the 768-1023 band docked at 520 px on a
    # viewport that cannot hold it. The approved band is the whole portrait
    # band and below.
    assert "@media (max-width: 1023px) {\n  .db-sheet.has-row-detail" in GRID_CSS
    dialog = _run("row-detail-overlay-is-a-dialog")
    assert dialog["overlay"]["rowRole"] == "dialog" and dialog["overlay"]["rowModal"] == "true"
    assert dialog["overlay"]["rowLabelledBy"] == "db-row-detail-title"
    assert dialog["docked"] == {"rowRole": None, "rowModal": None, "rowLabelledBy": None}, dialog

    assert _run("row-detail-overlay-traps-focus")["prevented"] is True
    assert _run("row-detail-docked-does-not-trap")["prevented"] is False
    # The identity model is untouched: no raw identifier reaches the page.
    html = _render(query="row=deadbeef")
    assert "record_id" not in html, "the technical row identity must never be rendered"
    print("PASS: the row panel is a trapped overlay in the portrait band and a docked panel above it")


# ===========================================================================
# 7. Escape precedence (INTERACTION_SPEC §3)
# ===========================================================================
def test_escape_closes_exactly_one_layer() -> None:
    nav = _run("escape-nav-drawer-wins")
    assert nav["navDrawerHidden"] is True, "the nav drawer closes"
    assert nav["filtersOpen"] is True, "the filter panel underneath must not collapse with it"
    assert nav["prevented"] is True

    menu = _run("escape-column-menu-before-filter-panel")
    assert menu["menuOpen"] is False and menu["filtersOpen"] is True
    assert menu["focused"] == "menuSummary", "focus returns to the header that opened the menu"

    panel = _run("escape-column-panel-before-filter-panel")
    assert panel["columnPanelOpen"] is True and panel["filtersOpen"] is True
    assert panel["prevented"] is False, "the filter panel yields to the column panel above it"

    drawer = _run("escape-closes-filter-drawer-only")
    assert drawer["filtersOpen"] is False and drawer["stagedValue"] == "Kowal"

    idle = _run("escape-with-no-layer-is-not-consumed")
    assert idle["prevented"] is False, "Esc is not swallowed when no product surface owns it"
    print("PASS: Escape dismisses exactly the topmost active surface and is not globally consumed")


def test_every_escape_owner_yields_to_a_layer_that_already_acted() -> None:
    for name, source in (
        ("data-grid-filters.js", FILTERS_JS),
        ("data-grid-columns.js", COLUMNS_JS),
        ("data-grid-row-detail.js", ROW_DETAIL_JS),
        ("data-grid-selection.js", SELECTION_JS),
    ):
        assert "event.defaultPrevented" in source, name
    # The shell drawer is the top of the stack and marks the key handled.
    assert "close();" in SHELL_JS and "event.preventDefault();" in SHELL_JS

    # The guard selectors must name the attribute the server actually renders.
    # `[data-db-menu]` never matched anything and therefore never guarded.
    for name, source in (("row-detail", ROW_DETAIL_JS), ("selection", SELECTION_JS)):
        assert "[data-db-col-menu]" in source, name
        assert re.search(r"\[data-db-menu\]", source) is None, name
        assert "[data-nav-drawer]:not([hidden])" in source, name
    print("PASS: every Escape owner yields to the layers above it, by state and by an already-handled key")


# ===========================================================================
# 8. Table and control semantics (AC-6, AC-8, AC-14)
# ===========================================================================
def test_aria_sort_states_the_truth_and_only_where_sorting_exists() -> None:
    html = _render(query="sort=driver_name&direction=asc")
    headers = re.findall(r"<th [^>]*>", html)
    sortable = [h for h in headers if "aria-sort" in h]

    assert len(sortable) == 3, headers
    ascending = [h for h in headers if 'aria-sort="ascending"' in h]
    assert len(ascending) == 1 and 'data-db-column="driver_name"' in ascending[0], ascending
    assert not [h for h in headers if 'aria-sort="descending"' in h]
    # A column that cannot be sorted advertises nothing rather than "none".
    note_header = [h for h in headers if 'data-db-column="note"' in h]
    assert note_header and "aria-sort" not in note_header[0], note_header
    # Neither does the non-column detail header.
    detail = [h for h in headers if "db-detail-col" in h]
    assert all("aria-sort" not in h for h in detail), detail

    descending = _render(query="sort=trip_date&direction=desc")
    marked = re.findall(r'<th [^>]*aria-sort="descending"[^>]*>', descending)
    assert len(marked) == 1 and 'data-db-column="trip_date"' in marked[0], marked
    # The sort is also stated in words, so it never depends on the caret alone.
    assert "sortowanie" in descending
    print("PASS: aria-sort names exactly one sorted column and never claims a sort that cannot exist")


def test_the_page_keeps_one_meaningful_heading_and_its_landmarks() -> None:
    html = _render()
    assert html.count("<h1") == 1, "exactly one top-level heading"
    assert '<h1 class="lp-visually-hidden">Approved trips</h1>' in html, html[:600]
    # It is visually hidden, so the approved table-first geometry is unchanged.
    assert "lp-page-head" not in html and "lp-page-title" not in html
    assert '<main class="lp-main portal-main" id="lp-main"' in html
    assert 'class="lp-skip-link"' in html
    assert '<header class="lp-appbar">' in html
    assert 'aria-label="Nawigacja główna"' in html
    # The document language is declared on the root element. The element also
    # carries the S13 theme attributes, whose presence depends on whether the
    # account preference could be read, so the assertion is on the LANGUAGE
    # rather than on the element having no other attributes.
    root = re.search(r"<html\b[^>]*>", html)
    assert root is not None, html[:200]
    assert 'lang="pl"' in root.group(0), root.group(0)
    assert html.count("<html") == 1, html.count("<html")
    print("PASS: the row sheet exposes one meaningful h1 and the approved landmark set")


def test_no_positive_tabindex_and_no_hidden_tabbable_duplicate() -> None:
    html = _render()
    positive = re.findall(r'tabindex="([^"]+)"', html)
    for value in positive:
        assert value in {"-1", "0"}, f"positive tabindex: {value}"
    for source in (RESPONSIVE_JS, SHELL_JS, FILTERS_JS):
        assert 'setAttribute("tabindex", "1")' not in source
        assert re.search(r'tabindex["\']\s*,\s*["\'][1-9]', source) is None

    # Responsive duplicates are removed with `display: none` or the `hidden`
    # attribute — never merely moved off-screen where they stay focusable.
    for selector in (".db-filter-scrim { display: none; }", ".db-filter-drawer-head { display: none; }"):
        assert selector in GRID_CSS, selector
    assert ".db-narrow-advisory[hidden] { display: none; }" in GRID_CSS
    # The module hides surfaces with the attribute, not with a CSS offset.
    assert 'setAttribute("hidden", "")' in RESPONSIVE_JS
    assert "left: -9999" not in GRID_CSS and "text-indent: -9999" not in GRID_CSS
    print("PASS: no positive tabindex exists and no responsive duplicate stays focusable")


def test_live_regions_and_busy_state_are_not_duplicated() -> None:
    html = _render()
    live = re.findall(r'aria-live="([a-z]+)"', html)
    assert live, "the sheet must keep its polite announcements"
    assert all(value == "polite" for value in live), live
    # One owner per event: the counter is the result-change region, the column
    # panel and the selection each own their own, and none of them nest.
    assert html.count("data-db-select-status") == 1
    assert html.count("data-db-cols-status") == 1
    assert html.count('class="db-counter" role="status"') == 1
    assert "aria-live" not in html[html.index("db-narrow-advisory"):html.index("db-narrow-footnote")], \
        "the advisory is a screen, not an announcement"
    # AC-14: the skeleton is hidden from assistive technology and the table
    # region reports the busy state.
    states_js = (STATIC / "js" / "data-grid-states.js").read_text(encoding="utf-8")
    assert 'setAttribute("aria-hidden", "true")' in states_js
    assert 'setAttribute("aria-busy", "true")' in states_js
    assert 'removeAttribute("aria-busy")' in states_js
    print("PASS: live regions have exactly one owner each and the busy state stays correct")


# ===========================================================================
# 9. Reduced motion (AC-12)
# ===========================================================================
def test_reduced_motion_suppresses_layers_but_not_export_progress() -> None:
    assert "@media (prefers-reduced-motion: reduce)" in PORTAL_CSS
    shell_block = _media_block(PORTAL_CSS, "(prefers-reduced-motion: reduce)")
    assert "animation-duration: 0.01ms !important;" in shell_block
    assert "transition-duration: 0.01ms !important;" in shell_block

    grid_blocks = GRID_CSS.count("@media (prefers-reduced-motion: reduce)")
    assert grid_blocks == 2, grid_blocks
    layers = GRID_CSS.split("@media (prefers-reduced-motion: reduce)")[1]
    assert ".db-col-panel,\n  .db-filter-panel { animation: none; }" in layers, layers
    assert ".db-scroll-fade { transition: none; }" in layers

    # The one motion that carries information keeps a static, honest fallback
    # instead of disappearing, and the rendered state states the progress in
    # words as well.
    exemption = GRID_CSS.split("@media (prefers-reduced-motion: reduce)")[2]
    assert ".db-export-progress.is-indeterminate .db-export-progress-fill {" in exemption
    assert "animation: none;" in exemption and "width: 100%;" in exemption
    assert 'role="progressbar"' in api_main._database_export_progress_html(
        {"row_count": 5000, "expected_rows": 10000}
    )
    determinate = api_main._database_export_progress_html({"row_count": 5000, "expected_rows": 10000})
    assert 'aria-valuenow="50"' in determinate and "width:50%" in determinate, determinate
    indeterminate = api_main._database_export_progress_html({"row_count": 5000, "expected_rows": None})
    assert "db-export-progress-label" in indeterminate, indeterminate

    # The drawer's own motion is declared, so reduced motion has something to
    # suppress rather than being a claim about nothing.
    assert "animation: db-drawer-in 200ms ease-out;" in GRID_CSS
    assert "@keyframes db-drawer-in" in GRID_CSS
    print("PASS: reduced motion removes every layer animation and keeps the export progress meaningful")


# ===========================================================================
# 10. Module hygiene and page inclusion
# ===========================================================================
def test_the_responsive_module_ships_and_is_page_scoped() -> None:
    assert "js/data-grid-responsive.js" in portal_assets.PAGE_ASSETS
    html = _render()
    assert 'src="/static/js/data-grid-responsive.js?v=' in html, "the sheet must load the module"
    assert "<script>" not in html, "no inline script blob"

    # Not loaded by unrelated portal pages.
    assert "js/data-grid-responsive.js" not in portal_assets.SCRIPTS

    # No external dependency, no browser sniffing, no resize storm.
    for forbidden in ("require(", "import ", "navigator.userAgent", "window.onresize",
                      'addEventListener("resize"', "window.innerWidth", "screen.width"):
        assert forbidden not in RESPONSIVE_JS, forbidden
    # RS-11: bands are decided in CSS pixels by `matchMedia`, so 200 % browser
    # zoom on a 1920 px viewport resolves to the 768-1023 band exactly as an
    # actual 960 px viewport does. No device or user-agent input exists.
    assert "matchMedia" in RESPONSIVE_JS
    assert RESPONSIVE_JS.lstrip().startswith("/*") and '"use strict";' in RESPONSIVE_JS
    print("PASS: the S10 module is a page-scoped vanilla enhancement with no new dependency")


def test_no_new_backend_or_disclosure_surface() -> None:
    html = _render()
    # The advisory adds no request, no parameter and no identifier.
    assert "narrow" not in api_main._portal_database_query_params(_FakeRequest(query="")).keys()
    assert "db-narrow-ack" not in html, "the acknowledgement never reaches the server"
    for forbidden in ("fetch(", "XMLHttpRequest", "navigator.sendBeacon"):
        assert forbidden not in RESPONSIVE_JS, forbidden
    # Filter parameters are unchanged: the drawer submits the same canonical set
    # the docked panel did, from the same single staged form.
    filtered = _render(query="filter__driver_name=Kowal&op__driver_name=contains")
    panel = filtered[filtered.index("data-db-panel-form"):filtered.index("db-panel-actions")]
    assert panel.count('name="filter__driver_name"') == 1
    assert panel.count('name="op__driver_name"') == 1
    assert "filter_exact__" not in panel or panel.count('name="filter_exact__driver_name"') <= 1
    print("PASS: S10 introduces no request, no parameter and no new disclosure surface")


def main() -> None:
    test_the_shell_uses_exactly_the_approved_bands()
    test_the_active_module_name_is_not_a_second_navigation_control()
    test_the_navigation_drawer_reveals_no_unauthorized_entry()
    test_client_name_and_code_survive_every_band()
    test_the_table_is_never_converted_at_any_width()
    test_the_44px_target_model_without_destroying_density()
    test_pointer_only_resize_gives_way_to_the_keyboard_equivalent()
    test_the_filter_surface_is_one_form_relocated_by_css()
    test_dismissing_the_filter_drawer_keeps_staged_edits()
    test_crossing_the_breakpoint_neither_applies_nor_duplicates_staged_state()
    test_the_drawer_is_modal_and_the_docked_panel_is_not()
    test_the_advisory_copy_is_the_approved_copy()
    test_the_advisory_appears_exactly_below_768px()
    test_open_anyway_works_and_is_remembered_for_the_session_only()
    test_the_acknowledgement_is_not_persistent_and_fails_safely()
    test_the_row_panel_becomes_an_overlay_across_the_whole_portrait_band()
    test_escape_closes_exactly_one_layer()
    test_every_escape_owner_yields_to_a_layer_that_already_acted()
    test_aria_sort_states_the_truth_and_only_where_sorting_exists()
    test_the_page_keeps_one_meaningful_heading_and_its_landmarks()
    test_no_positive_tabindex_and_no_hidden_tabbable_duplicate()
    test_live_regions_and_busy_state_are_not_duplicated()
    test_reduced_motion_suppresses_layers_but_not_export_progress()
    test_the_responsive_module_ships_and_is_page_scoped()
    test_no_new_backend_or_disclosure_surface()
    print("\nALL RESPONSIVE AND ACCESSIBILITY TESTS PASSED")


if __name__ == "__main__":
    main()
