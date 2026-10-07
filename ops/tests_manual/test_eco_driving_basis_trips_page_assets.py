#!/usr/bin/env python3
"""`UI-20260827-01` — the ranking-basis trips page must ship the selection module.

The trips page has two variants that render the SAME table for the same user:
the period-key variant, and the ranking-basis variant reached when the request
carries a `month`. The basis variant used to emit the full selection host —
`eco-select-sheet` with its `data-grid-*` vocabulary, every cell marked with
`data-eco-column` and `data-eco-copy` — while shipping only the stylesheet, so
`data-grid-selection.js` never arrived and the browser's native text selection
took over the table. Nothing failed; the markup was simply inert.

These checks assert the property that was missing rather than the line that was
wrong: **whichever variant answers, the assets it declares must be able to
animate the markup it emitted.** The controller runs against a stub service, so
no database and no HTTP client are involved and both variants are driven through
the real `EcoDrivingPages.ranking_entry_trips` entry point.

Run:

    cd /opt/log-platform-worktrees/ui-format
    PYTHONPATH="$PWD" /opt/log-platform/.venv/bin/python \\
        ops/tests_manual/test_eco_driving_basis_trips_page_assets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_provider as prov  # noqa: E402

from api.eco_driving_explorer import html as H  # noqa: E402
from api.eco_driving_explorer import trip_view_models as T  # noqa: E402
from api.eco_driving_explorer.pages import EcoDrivingPages, PageResult  # noqa: E402
from api.eco_driving_explorer.service import ApiResult  # noqa: E402
from api.portal_ui import assets as portal_assets  # noqa: E402

SELECTION_MODULE = "js/data-grid-selection.js"
CC, FAMILY, MONTH = "ALPHA00001", "driver", "2026-08"
PERIOD_KEY = "W:2026-08-03"
USER = {"user_id": "u-1", "username": "tester"}

PASSES: list[str] = []


def ok(message: str) -> None:
    PASSES.append(message)
    print(f"PASS: {message}")


# --- stub service ------------------------------------------------------------


ENTRY = {
    "client_code": CC,
    "ranking_family": FAMILY,
    "period_key": PERIOD_KEY,
    "assigned_id": "12345",
    "period_label": "sierpień 2026",
    "ranking_group": "INCLUDED",
    "current_chart": {"current_driver_name": "Jan Kowalski"},
    "selection": {"month": MONTH, "label": "cały miesiąc", "canonical_weeks_param": None},
}

TRIPS_BODY = {
    "data": [prov.make_trip_row()],
    "meta": {"page": 1, "limit": 50, "total_count": 1,
             "sort": "trip_start_ts", "direction": "ASC", "filters_active": False},
}


class StubService:
    """Answers both variants with the same one-row table.

    The variants must differ only in how the period is chosen; anything the two
    pages disagree about beyond that is the defect this file exists to catch, so
    the fixture deliberately gives them identical data.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, name: str, body: dict) -> ApiResult:
        self.calls.append(name)
        return ApiResult(200, body)

    def list_contributing_trips(self, **_kwargs) -> ApiResult:
        return self._record("list_contributing_trips", TRIPS_BODY)

    def get_ranking_entry(self, **_kwargs) -> ApiResult:
        return self._record("get_ranking_entry", {"data": ENTRY})

    def list_basis_contributing_trips(self, **_kwargs) -> ApiResult:
        return self._record("list_basis_contributing_trips", TRIPS_BODY)

    def get_basis_ranking_entry(self, **_kwargs) -> ApiResult:
        return self._record("get_basis_ranking_entry", {"data": ENTRY})


def basis_page() -> tuple[PageResult, StubService]:
    service = StubService()
    result = EcoDrivingPages(service).ranking_entry_trips(
        user=USER, client_code=CC, ranking_family=FAMILY, month=MONTH,
        assigned_id="12345", unit="rate", ranking_group="INCLUDED",
        ranking_page=1, ranking_limit=50,
    )
    return result, service


def period_key_page() -> tuple[PageResult, StubService]:
    service = StubService()
    result = EcoDrivingPages(service).ranking_entry_trips(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=PERIOD_KEY,
        assigned_id="12345", unit="rate", ranking_group="INCLUDED",
        ranking_page=1, ranking_limit=50,
    )
    return result, service


# --- 1. the reported defect --------------------------------------------------


def test_basis_variant_is_the_one_under_test():
    """A `month` really does route to the basis service, not the period one."""

    _result, service = basis_page()
    assert "list_basis_contributing_trips" in service.calls, service.calls
    assert "list_contributing_trips" not in service.calls, service.calls
    _result, service = period_key_page()
    assert "list_contributing_trips" in service.calls, service.calls
    assert "list_basis_contributing_trips" not in service.calls, service.calls
    ok("`month` selects the ranking-basis variant and its absence selects the period variant")


def test_basis_trips_page_ships_the_selection_module():
    result, _service = basis_page()
    assert result.status_code == 200
    assert result.page_assets == H.ECO_DRIVING_TRIPS_PAGE_ASSETS, result.page_assets
    assert SELECTION_MODULE in result.page_assets, result.page_assets
    ok("the ranking-basis trips page declares the trips asset set, selection module included")


def test_basis_trips_page_emits_a_script_tag_for_the_module():
    """Acceptance criterion 1, at the layer that actually writes the <head>."""

    result, _service = basis_page()
    tags = portal_assets.page_asset_tags(*result.page_assets)
    assert "<script" in tags and SELECTION_MODULE in tags, tags
    assert "css/eco-driving.css" in tags, tags
    ok("the declared basis asset set renders a <script> tag for data-grid-selection.js")


def test_basis_markup_and_assets_agree():
    """The host and the module that animates it must arrive together.

    Asserted as a biconditional over the rendered body rather than as two
    independent facts: a page that emits the selection host without the module
    is the reported defect, and a page that ships the module for a table with no
    marked cells would breach the page-scoped-assets invariant from the other
    side.
    """

    for name, page in (("basis", basis_page), ("period-key", period_key_page)):
        result, _service = page()
        host = 'class="eco-select-sheet" data-db-sheet' in result.body_html
        marked = "data-eco-column=" in result.body_html
        shipped = SELECTION_MODULE in result.page_assets
        assert host and marked, name
        assert host == shipped, f"{name}: host={host} module={shipped}"
        for attr in ("data-grid-table=", "data-grid-column-attr=", "data-grid-copy-attr="):
            assert attr in result.body_html, (name, attr)
    ok("both trips variants emit the selection host AND ship the module that animates it")


def test_basis_page_keeps_its_own_context_line():
    """The fix must not have flattened the two variants into one.

    The basis page prepends the period context line; losing it while chasing the
    asset set would be a silent regression of the surface being fixed.
    """

    basis, _ = basis_page()
    period, _ = period_key_page()
    assert "eco-basis-context" in basis.body_html
    assert "eco-basis-context" not in period.body_html
    ok("the ranking-basis page keeps its basis context line and the period page keeps none")


# --- 2. the shape that made the omission possible ----------------------------


def test_the_renderer_owns_the_asset_decision():
    """`render()` returns markup and assets as one value.

    The defect was a single omitted keyword argument at one of two structurally
    identical return sites. A caller can no longer obtain the HTML without also
    being handed the assets it requires, so a future third caller cannot repeat
    the omission by simply not thinking about it.
    """

    rendered = T.render(
        ENTRY, TRIPS_BODY["data"], TRIPS_BODY["meta"],
        ranking_context={"ranking_group": "INCLUDED", "unit": "rate"},
        filter_values={},
        # Required since `UI-20260827-05`: a caller states the scope that
        # reproduces its rows rather than letting the renderer guess at one.
        export_scope=T.ExportScope.for_period_key({
            "client_code": CC, "ranking_family": FAMILY,
            "period_key": PERIOD_KEY, "assigned_id": "12345",
        }),
    )
    assert isinstance(rendered, T.RenderedTrips)
    assert rendered.page_assets == H.ECO_DRIVING_TRIPS_PAGE_ASSETS
    assert "eco-select-sheet" in rendered.html
    ok("the trips renderer returns its markup and its required assets as one value")


def test_page_result_default_still_excludes_the_module():
    """The default remains CSS-only, so the coupling above is load-bearing.

    If the default ever gained the module this suite would keep passing while
    every Eco page silently started downloading the grid layer.
    """

    assert PageResult(200, "t", "<p></p>").page_assets == H.ECO_DRIVING_PAGE_ASSETS
    assert SELECTION_MODULE not in H.ECO_DRIVING_PAGE_ASSETS
    ok("the PageResult default is still the CSS-only Eco asset set")


# --- 3. what must not have moved ---------------------------------------------


def test_no_other_eco_surface_acquires_the_module():
    """Assets stay page-scoped: only the trips tuple carries the grid layer."""

    bearing = {
        getattr(H, name) for name in dir(H)
        if name.endswith("_PAGE_ASSETS") and SELECTION_MODULE in getattr(H, name)
    }
    # Compared by VALUE, not by name: the selectable tuple is exported under two
    # names (the trips page's historical one and the shared one both tables now
    # use) and those are one asset set, not two.
    assert bearing == {H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS}, bearing
    assert SELECTION_MODULE not in H.ECO_DRIVING_PAGE_ASSETS
    ok("exactly one Eco asset set carries the selection module")


def test_database_explorer_vocabulary_is_untouched():
    """The Eco host overrides the module's vocabulary; it does not redefine it.

    The Database Explorer's own grid sets no `data-grid-*` overrides and rides
    the module defaults, so those defaults must keep naming the `data-db-*`
    attributes its cells actually carry.
    """

    source = (ROOT / "api" / "static" / "js" / "data-grid-selection.js").read_text(encoding="utf-8")
    for default in ('columnAttr: "data-db-column"', 'copyAttr: "data-db-copy"',
                    '"[data-db-sheet]"'):
        assert default in source, default
    attrs = H.selection_sheet_attrs("{}")
    assert 'data-grid-column-attr="data-eco-column"' in attrs
    assert 'data-grid-copy-attr="data-eco-copy"' in attrs
    assert "data-db-sheet" in attrs
    ok("the module keeps its data-db-* defaults and the Eco host only overrides them")


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"\nOK - ranking-basis trips page asset checks passed ({len(PASSES)} checks)")


if __name__ == "__main__":
    main()
