#!/usr/bin/env python3
"""Manual unit-style checks for the server-rendered Eco Driving Explorer pages
(Stage 3).

No database and no HTTP client are required. The framework-independent
``EcoDrivingPages`` controller is exercised directly against the Stage 2 service
wired to the reusable fake backend / synthetic client-database reader. A minimal
route-registration check exercises the thin FastAPI page adapter.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_eco_driving_explorer_pages.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Reuse Stage 1 + Stage 2 fakes and builders.
import test_eco_driving_explorer_provider as prov  # noqa: E402
import test_eco_driving_explorer_api as api_t  # noqa: E402

from api.eco_driving_explorer import html as H  # noqa: E402
from api.eco_driving_explorer import eco_view as V  # noqa: E402
from api.portal_ui.i18n import t as _t  # noqa: E402
from api.eco_driving_explorer.access import merge_eco_access  # noqa: E402
from api.eco_driving_explorer.errors import (  # noqa: E402
    EnvironmentClientMismatchError,
    ProviderNotFoundError,
)
from api.eco_driving_explorer.pages import EcoDrivingPages, PageResult  # noqa: E402
from api.eco_driving_explorer.registry import available_provider_identities  # noqa: E402
from api.eco_driving_explorer.service import EcoDrivingApiService  # noqa: E402
from api.eco_driving_explorer import page_routes as pr  # noqa: E402

CLIENT_CODE = api_t.CLIENT_CODE
FAMILY = api_t.FAMILY
USER = api_t.USER
ADMIN = api_t.ADMIN
WEEKLY_TOKEN = api_t.WEEKLY_TOKEN
DB_NAME = api_t.DB_NAME
TRUSTED_CLIENT_ID = api_t.TRUSTED_CLIENT_ID

XSS_PAYLOADS = ("<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "' OR 1=1 --")


def _grant(**flags):
    return {(USER["user_id"], CLIENT_CODE): api_t._access(**{"ranking": True, **flags})}


def _pages(access_map=None, responses=None, resolve_error=None):
    backend = api_t.FakeBackend(
        access_map=access_map or {}, responses=responses or {}, resolve_error=resolve_error
    )
    service = EcoDrivingApiService(backend)
    return EcoDrivingPages(service), backend


def _assert_no_raw_html(body: str) -> None:
    # Never emit an actual injected element. Escaped payloads may legitimately
    # contain inert text like "onerror=", but must never appear as a raw tag or
    # an unescaped attribute breakout (a literal " would come from esc()).
    assert "<script" not in body, "unescaped <script> in page"
    assert "<img" not in body, "unescaped <img> in page"
    for banned in ("Traceback", "Exception", "psycopg", "SELECT ", " FROM "):
        assert banned not in body, f"raw internal detail leaked: {banned}"


def _assert_no_sensitive(body: str) -> None:
    low = body.lower()
    for banned in ("latitude", "longitude", "polyline", "address", "email", "@"):
        assert banned not in low, f"sensitive field leaked: {banned}"
    assert DB_NAME not in body, "client database name leaked"
    assert TRUSTED_CLIENT_ID not in body, "platform client_id leaked"


# --- landing -----------------------------------------------------------------

def test_landing_no_access_normal_page():
    pages, backend = _pages({}, {})
    res = pages.landing(user=USER, request=None)
    assert res.status_code == 200
    assert _t("eco.state.no_access_title") in res.body_html
    # non-disclosure: does not name any client/provider
    assert CLIENT_CODE not in res.body_html
    assert backend.reader_opened == 0
    print("PASS: landing with no access is a normal 200 page and discloses no client")


def test_landing_unauthenticated_defensive_401():
    pages, _ = _pages({}, {})
    res = pages.landing(user=None, request=None)
    assert res.status_code == 401
    print("PASS: landing controller is defensively 401 when user is missing")


def test_landing_weekly_provider_periods_w5_partial_exclusive():
    pages, backend = _pages(_grant(), {"weekly_periods": api_t._weekly_period_rows(), "monthly_periods": []})
    res = pages.landing(user=USER, request=None)
    assert res.status_code == 200
    b = res.body_html
    disp = available_provider_identities()[0].display_name
    # The provider identity moved to the shared context bar; a one-provider
    # account gets no selector chip, because an inert one-option control is
    # worse than none.
    #
    # S12 carry-forward (E): the context **client** field is the client identity
    # on every Eco surface. The provider label describes the module and the
    # ranking family, so it must not be rendered as the client's name here —
    # doing so made the ranking and the detail page disagree about who was on
    # screen.
    assert res.context_client_name == CLIENT_CODE
    assert res.context_client_name != disp
    assert res.context_client_code == CLIENT_CODE
    assert _t("eco.period.type_weekly") in b and _t("eco.period.type_monthly") in b
    assert "eco-chip" in b and 'aria-current="true"' in b
    # persisted labels incl. W5, partial marker text, exclusive-end wording
    assert "2026-07-W1" in b and "2026-07-W5" in b
    assert _t("eco.period.partial") in b
    assert _t("eco.period.end_exclusive") in b
    assert _t("eco.basis.cumulative") in b
    # Lineage is a context-bar qualifier now, stated once and never per row.
    assert H.LINEAGE_LABEL in res.context_meta_html
    assert H.LINEAGE_LABEL not in b
    # open-ranking link carries the period token and default group
    assert "ranking_group=INCLUDED" in b and "period_key=" in b
    _assert_no_raw_html(b)
    _assert_no_sensitive(b)
    types = [e["event_type"] for e in backend.audit_events]
    assert types.count("eco_driving_providers_viewed") == 1
    assert types.count("eco_driving_periods_viewed") == 1
    print("PASS: landing shows provider, weekly W1..W5, partial+exclusive end, lineage badge; audits reused")


def test_landing_monthly_empty_state():
    pages, _ = _pages(_grant(), {"weekly_periods": api_t._weekly_period_rows(), "monthly_periods": []})
    res = pages.landing(user=USER, request=None, period_type="monthly")
    assert res.status_code == 200
    assert _t("eco.state.no_periods_title") in res.body_html
    _assert_no_raw_html(res.body_html)
    print("PASS: monthly with zero periods renders a normal empty state (no error, no synthesis)")


def test_landing_malicious_period_label_escaped():
    rows = api_t._weekly_period_rows()
    rows[0]["period_label"] = XSS_PAYLOADS[0]
    pages, _ = _pages(_grant(), {"weekly_periods": rows, "monthly_periods": []})
    res = pages.landing(user=USER, request=None)
    b = res.body_html
    assert XSS_PAYLOADS[0] not in b
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in b
    _assert_no_raw_html(b)
    print("PASS: malicious persisted period label is HTML-escaped")


def test_provider_selector_escapes_display_name():
    pages, _ = _pages()
    fake_provider = {"client_code": CLIENT_CODE, "ranking_family": FAMILY, "display_name": XSS_PAYLOADS[1]}
    other = {"client_code": "OTHER0001", "ranking_family": FAMILY, "display_name": "Other"}
    html_out = pages._provider_chips([fake_provider, other], fake_provider, V.DEFAULT_UNIT)
    assert XSS_PAYLOADS[1] not in html_out  # raw payload absent
    assert "<img" not in html_out  # no raw tag
    assert "&lt;img" in html_out  # present only as escaped text
    print("PASS: provider display name is HTML-escaped in the selector")


# --- rankings ----------------------------------------------------------------

def _ranking_responses(entries, total=None):
    return {
        "entries_list": entries,
        "entries_count": [{"total_count": total if total is not None else len(entries)}],
    }


def test_rankings_default_group_included():
    pages, backend = _pages(_grant(), _ranking_responses([prov.make_entry_row()]))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 200
    b = res.body_html
    # Chips, not tabs: one ranking filtered by group, all three reachable.
    assert _t("eco.group.included") in b and _t("eco.group.excluded") in b
    assert _t("eco.group.unknown") in b
    assert "eco-chip" in b and 'aria-current="true"' in b
    # The old segmented tab strip is gone for good ("eco-table" legitimately
    # contains the substring, so match the tab markup itself).
    assert 'class="eco-tabs"' not in b and "eco-tab active" not in b
    assert "12345" in b and ">100<" in b  # assigned id + persisted score
    # The per-row lineage badge is gone; the qualifier lives in the context bar.
    assert H.LINEAGE_LABEL not in b
    assert H.LINEAGE_LABEL in res.context_meta_html
    # group persisted in URLs and page reset to 1
    assert "ranking_group=INCLUDED" in b and "page=1" in b
    _assert_no_raw_html(b)
    _assert_no_sensitive(b)
    # Two logical reads, two reused service-level events and no page-level
    # event of its own: the ranking rows, and the fleet distribution, which is
    # its own aggregate disclosure surface and is audited as one.
    types = [e["event_type"] for e in backend.audit_events]
    assert types.count("eco_driving_ranking_viewed") == 2
    surfaces = [
        e["metadata"].get("surface")
        for e in backend.audit_events
        if e["event_type"] == "eco_driving_ranking_viewed"
    ]
    assert sorted(str(x) for x in surfaces) == ["None", "score_distribution"]
    assert not any(t.startswith("eco_driving_portal") for t in types)  # no duplicate page event
    print("PASS: rankings default group is INCLUDED; reused service audits; no duplicate page event")


def test_rankings_excluded_group_is_scored_not_merged():
    entry = prov.make_entry_row(ranking_group="EXCLUDED", ranking_included=False)
    pages, _ = _pages(_grant(), _ranking_responses([entry]))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
                         period_key=WEEKLY_TOKEN, ranking_group="EXCLUDED")
    assert res.status_code == 200
    b = res.body_html
    # An EXCLUDED driver stays a real, scored driver: the row keeps its
    # persisted rating band and still links to its own detail page.
    assert "12345" in b
    assert "bezpieczny" in b
    # The group token is a filter value, never visible text and never a rating.
    assert ">EXCLUDED<" not in b
    assert "assigned_id=12345" in b
    print("PASS: EXCLUDED chip renders scored excluded rows that keep their rating band")


def test_rankings_unknown_driver_fallback():
    entry = prov.make_entry_row(
        ranking_group="UNKNOWN_DRIVER", ranking_included=False, ranking_position=None,
        ranking_total_participants=None, current_driver_name=None, current_chart_present=False,
    )
    pages, _ = _pages(_grant(), _ranking_responses([entry]))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
                         period_key=WEEKLY_TOKEN, ranking_group="UNKNOWN_DRIVER")
    assert res.status_code == 200
    b = res.body_html
    assert "12345" in b  # missing chart metadata does not remove the row
    assert "&mdash;" in b or "—" in b  # explicit fallback rather than None/null
    assert ">None<" not in b and ">null<" not in b
    print("PASS: UNKNOWN_DRIVER row keeps identifier, shows a clear fallback, no None/null")


def test_rankings_current_chart_does_not_move_row():
    # persisted EXCLUDED but current-chart says included: row stays under persisted group.
    entry = prov.make_entry_row(ranking_group="EXCLUDED", ranking_included=False,
                                current_chart_ranking_included=True, current_driver_name="Chart Name")
    pages, _ = _pages(_grant(), _ranking_responses([entry]))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
                         period_key=WEEKLY_TOKEN, ranking_group="EXCLUDED")
    assert res.status_code == 200
    b = res.body_html
    assert "12345" in b
    # driver metadata flagged as current, not historical
    assert "bieżącej karty kierowcy" in b
    print("PASS: persisted ranking group used; current-chart flag does not move a row")


def test_rankings_opaque_ids_007_vs_7():
    entries = [prov.make_entry_row(assigned_id="007", ranking_position=1),
               prov.make_entry_row(assigned_id="7", ranking_position=2)]
    pages, _ = _pages(_grant(), _ranking_responses(entries))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    b = res.body_html
    assert ">007<" in b and ">7<" in b
    print("PASS: assigned IDs are opaque text; 007 and 7 render distinctly")


def test_rankings_empty_group_normal_state():
    pages, _ = _pages(_grant(), _ranking_responses([], total=0))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY,
                         period_key=WEEKLY_TOKEN, ranking_group="INCLUDED")
    assert res.status_code == 200
    assert _t("eco.state.no_entries") in res.body_html
    print("PASS: empty ranking group renders a normal empty state")


def test_rankings_validation_errors_are_safe():
    # invalid group / sort / oversize limit / malformed token -> 422 safe pages
    cases = [
        {"ranking_group": "BOGUS"},
        {"sort": "secret_column"},
        {"limit": "501"},
        {"period_key": "not-a-real-token"},
    ]
    for extra in cases:
        pages, _ = _pages(_grant(), _ranking_responses([prov.make_entry_row()]))
        kwargs = dict(client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
        kwargs.update(extra)
        res = pages.rankings(user=USER, request=None, **kwargs)
        assert res.status_code == 422, (extra, res.status_code)
        _assert_no_raw_html(res.body_html)
    print("PASS: invalid group/sort/limit/token all map to safe HTTP 422 pages")


def test_rankings_forbidden_403_no_disclosure():
    pages, backend = _pages({}, _ranking_responses([prov.make_entry_row()]))  # no grant
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 403
    assert backend.reader_opened == 0
    _assert_no_sensitive(res.body_html)
    print("PASS: unauthorized ranking access is 403 and discloses nothing")


def test_rankings_provider_not_found_404():
    pages, _ = _pages(_grant(), resolve_error=ProviderNotFoundError("x"))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 404
    print("PASS: provider-not-found renders a safe 404 page")


def test_rankings_environment_mismatch_503_generic():
    pages, _ = _pages(_grant(), resolve_error=EnvironmentClientMismatchError("db mismatch alpha_main"))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 503
    _assert_no_sensitive(res.body_html)
    print("PASS: environment/client mismatch renders a generic 503 (no db/env details)")


def test_rankings_malicious_query_param_not_injected():
    pages, _ = _pages({}, {})  # no grant -> 403 error page still echoes back link safely
    res = pages.rankings(user=USER, request=None, client_code=XSS_PAYLOADS[0], ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 403
    assert XSS_PAYLOADS[0] not in res.body_html
    _assert_no_raw_html(res.body_html)
    print("PASS: malicious client_code query param is not injected into markup")


def test_rankings_data_exposure_boundary():
    pages, _ = _pages(_grant(trip=True, routes=True), _ranking_responses([prov.make_entry_row()]))
    res = pages.rankings(user=USER, request=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_key=WEEKLY_TOKEN)
    assert res.status_code == 200
    b = res.body_html
    _assert_no_sensitive(b)
    for banned in ("trip_distance_meters", "event_counts", "reconciliation", "provider_trip_id"):
        assert banned not in b, banned
    print("PASS: ranking table exposes no trip/route/reconciliation data even with trip/route grants")


# --- navigation visibility ---------------------------------------------------

def _has_nav(access_map, user=USER):
    backend = api_t.FakeBackend(access_map=access_map)
    return EcoDrivingApiService(backend).has_eco_ranking_access(user)


def test_navigation_visibility_matrix():
    # direct grant
    assert _has_nav(_grant()) is True
    # group-only grant
    group_access = merge_eco_access(CLIENT_CODE, direct=None, group={"can_view_eco_ranking": True}, client_is_active=True)
    assert _has_nav({(USER["user_id"], CLIENT_CODE): group_access}) is True
    # direct false + group true
    both = merge_eco_access(CLIENT_CODE, direct={"can_view_eco_ranking": False},
                            group={"can_view_eco_ranking": True}, client_is_active=True)
    assert _has_nav({(USER["user_id"], CLIENT_CODE): both}) is True
    # no eco grant (e.g. Database Explorer only) -> hidden
    assert _has_nav({(USER["user_id"], CLIENT_CODE): api_t._access(ranking=False)}) is False
    assert _has_nav({}) is False
    # admin without eco grant -> hidden
    assert _has_nav({}, user=ADMIN) is False
    # unauthenticated -> hidden
    assert _has_nav({}, user=None) is False
    print("PASS: nav link follows direct/group union; admin/database-only/none do not reveal it")


# --- route registration ------------------------------------------------------

def _make_request(query: str = "", path: str = "/user/eco-driving"):
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": query.encode("ascii")}
    return Request(scope)


class _Redirect:  # stand-in for an unauthenticated portal redirect Response
    pass


def test_page_routes_register_once_and_render():
    app = api_t._FakeApp()
    pages, backend = _pages(_grant(), {"weekly_periods": api_t._weekly_period_rows(), "monthly_periods": []})
    captured = {}

    def render(result: PageResult, user: dict):
        captured["result"] = result
        return ("RENDERED", result.status_code)

    pr.register_eco_driving_page_routes(app, pages=pages, require_user=lambda r: USER, render=render)
    assert set(app.routes.keys()) == {
        pr.LANDING_PAGE_PATH, pr.RANKINGS_PAGE_PATH, pr.RANKING_ENTRY_PAGE_PATH,
        pr.RANKING_ENTRY_TRIPS_PAGE_PATH, pr.RANKING_ENTRY_TRIPS_EXPORT_PATH,
        pr.RANKINGS_EXPORT_PATH, pr.PERIODS_EXPORT_PATH, pr.RANKING_ENTRY_EXPORT_PATH,
    }
    assert len(app.routes) == 8  # each registered exactly once

    landing_handler = app.routes[pr.LANDING_PAGE_PATH]
    out = asyncio.new_event_loop().run_until_complete(landing_handler(_make_request()))
    assert out == ("RENDERED", 200)
    assert isinstance(captured["result"], PageResult)
    print("PASS: page adapter registers landing + rankings exactly once and renders via callback")


def test_page_routes_pass_through_unauthenticated_redirect():
    app = api_t._FakeApp()
    pages, _ = _pages(_grant(), {})
    redirect = _Redirect()
    rendered = {"called": False}

    def render(result, user):
        rendered["called"] = True
        return "SHOULD_NOT_RENDER"

    pr.register_eco_driving_page_routes(app, pages=pages, require_user=lambda r: redirect, render=render)
    handler = app.routes[pr.RANKINGS_PAGE_PATH]
    out = asyncio.new_event_loop().run_until_complete(handler(_make_request(path="/user/eco-driving/rankings")))
    assert out is redirect
    assert rendered["called"] is False
    print("PASS: non-dict require_user result (redirect) is returned as-is; render is skipped")


def test_no_import_cycle_with_api_main():
    # Importing the Eco page modules must not pull in the api.main monolith.
    assert "api.main" not in sys.modules
    print("PASS: Eco Driving page modules do not import api.main (no cycle)")


def main() -> None:
    test_landing_no_access_normal_page()
    test_landing_unauthenticated_defensive_401()
    test_landing_weekly_provider_periods_w5_partial_exclusive()
    test_landing_monthly_empty_state()
    test_landing_malicious_period_label_escaped()
    test_provider_selector_escapes_display_name()
    test_rankings_default_group_included()
    test_rankings_excluded_group_is_scored_not_merged()
    test_rankings_unknown_driver_fallback()
    test_rankings_current_chart_does_not_move_row()
    test_rankings_opaque_ids_007_vs_7()
    test_rankings_empty_group_normal_state()
    test_rankings_validation_errors_are_safe()
    test_rankings_forbidden_403_no_disclosure()
    test_rankings_provider_not_found_404()
    test_rankings_environment_mismatch_503_generic()
    test_rankings_malicious_query_param_not_injected()
    test_rankings_data_exposure_boundary()
    test_navigation_visibility_matrix()
    test_page_routes_register_once_and_render()
    test_page_routes_pass_through_unauthenticated_redirect()
    test_no_import_cycle_with_api_main()
    print("OK - Eco Driving Explorer server-rendered page tests passed")


if __name__ == "__main__":
    main()
