#!/usr/bin/env python3
"""Focused checks for the Eco Driving driver drill-down page (`ECO-003`).

Updated for the S12 presentation: the reconciliation panel and the
score-definition / "how the score was calculated" panel were removed by owner
decision (`D-003` / `EC-30`), so the assertions that described them are replaced
by explicit **negative** regression assertions — the panels must not reappear
under another heading.

The backend behaviour those panels used to exercise is not dropped: the
reconciliation service keeps its MATCH / MISMATCH / UNAVAILABLE contract and its
sanitized-error audit path, and both are asserted here against the service
directly.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_eco_driving_explorer_ranking_entry_page.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_api as api_t
import test_eco_driving_explorer_provider as prov

from api.eco_driving_explorer import detail_view_models as D
from api.eco_driving_explorer import page_routes as routes
from api.eco_driving_explorer.pages import EcoDrivingPages
from api.eco_driving_explorer.service import EcoDrivingApiService
from api.portal_ui.i18n import t
from jobs.ecodriving.eco_scoring import REQUIRED_METRICS

CC, FAMILY, TOKEN, USER = api_t.CLIENT_CODE, api_t.FAMILY, api_t.WEEKLY_TOKEN, api_t.USER

# Panel copy that must never come back, in any heading (`EC-30`, `EC-18`).
REMOVED_PANEL_MARKERS = (
    "Reconciliation",
    "Rekoncyliacja",
    "reconciliation_status",
    "Persisted versus current reconstructed aggregates",
    "How the score was calculated",
    "Jak policzono wynik",
    "Full scoring definition and rating thresholds",
    "Minimum qualifying distance",
    "Threshold rules",
)


def pages(*, trip=False, routes_flag=False, responses=None):
    access = {(USER["user_id"], CC): api_t._access(ranking=True, trip=trip, routes=routes_flag)}
    backend = api_t.FakeBackend(access_map=access, responses=responses or api_t._match_responses())
    return EcoDrivingPages(EcoDrivingApiService(backend)), backend


def service(*, trip=False, responses=None):
    access = {(USER["user_id"], CC): api_t._access(ranking=True, trip=trip)}
    backend = api_t.FakeBackend(access_map=access, responses=responses or api_t._match_responses())
    return EcoDrivingApiService(backend), backend


def call(controller, **extra):
    args = dict(user=USER, client_code=CC, ranking_family=FAMILY,
                period_key=TOKEN, assigned_id="12345")
    args.update(extra)
    return controller.ranking_entry(**args)


def assert_safe(body):
    """No injected markup, no route/location data, no infrastructure identity."""

    low = body.lower()
    for raw in ("<script", "<img", "latitude", "longitude", "polyline",
                "address", "select ", " from "):
        assert raw not in low, raw
    assert api_t.DB_NAME not in body and api_t.TRUSTED_CLIENT_ID not in body


def assert_removed_panels_absent(body):
    for marker in REMOVED_PANEL_MARKERS:
        assert marker not in body, f"deliberately removed panel reappeared: {marker}"


def test_six_section_order_identity_and_audit():
    controller, backend = pages()
    result = call(controller, ranking_group="INCLUDED", page="3", limit="50",
                  sort="eco_driving_score_total", direction="desc")
    body = result.body_html
    assert result.status_code == 200

    # The approved six sections, in the approved order (`EC-14`).
    order = []
    cursor = 0
    while True:
        found = body.find('data-eco-section="', cursor)
        if found == -1:
            break
        start = found + len('data-eco-section="')
        end = body.index('"', start)
        order.append(body[start:end])
        cursor = end
    assert tuple(order) == D.SECTION_ORDER, order

    # The 16 persisted identity fields, with an exclusive end and an opaque ID.
    assert t("eco.detail.identity") in body
    assert t("eco.id.period_end") in body
    assert t("eco.id.assigned_id") in body
    assert body.count('<div class="eco-identity-cell">') >= 16
    # `EC-19`: persisted values are distinguished from the live display name.
    assert "utrwalone" in body and "bieżącej karty kierowcy" in body

    # Ranking state survives the round trip so `D-009` back navigation is exact.
    assert "page=3" in body and "limit=50" in body and "direction=desc" in body

    for metric in REQUIRED_METRICS:
        assert metric not in body  # labels, never raw column keys

    assert [e["event_type"] for e in backend.audit_events][0] == "eco_driving_ranking_entry_viewed"
    assert "assigned_id_digest" in backend.audit_events[0]["metadata"]
    assert "assigned_id" not in backend.audit_events[0]["metadata"]
    assert_removed_panels_absent(body)
    assert_safe(body)


def test_removed_panels_stay_removed_with_every_grant():
    # Even with the trip and route grants — the widest access the module has —
    # neither removed panel may render.
    for flags in ({}, {"trip": True}, {"trip": True, "routes_flag": True}):
        controller, _ = pages(**flags)
        body = call(controller).body_html
        assert_removed_panels_absent(body)


def test_reconciliation_service_contract_survives_panel_removal():
    """The panel is gone; the service behind it is not.

    MATCH, MISMATCH and the sanitized unexpected-error path all still hold, and
    the reconciliation audit event is still written — removing a UI panel must
    not quietly delete a domain capability other consumers rely on.
    """

    svc, backend = service(trip=True)
    result = svc.reconcile_ranking_entry(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN, assigned_id="12345",
    )
    assert result.status_code == 200
    assert result.body["data"]["reconciliation_status"] == "MATCH"

    svc, _ = service(trip=True, responses=api_t._match_responses(totals_overrides={"trips_count": 59}))
    result = svc.reconcile_ranking_entry(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN, assigned_id="12345",
    )
    assert result.body["data"]["reconciliation_status"] == "MISMATCH"
    assert "trips_count" in result.body["data"]["mismatched_fields"]

    svc, backend = service(trip=True)
    original = backend.reader.fetch_one

    def fail_reconstruction(sql, params):
        if backend.reader._kind(sql) == "recon_totals":
            raise RuntimeError("SELECT secret_table FROM private_data")
        return original(sql, params)

    backend.reader.fetch_one = fail_reconstruction
    result = svc.reconcile_ranking_entry(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN, assigned_id="12345",
    )
    assert result.status_code == 500
    body = str(result.body)
    assert "secret_table" not in body and "private_data" not in body
    assert backend.audit_events[-1]["metadata"]["reconciliation_status"] == "UNAVAILABLE"


def test_trip_evidence_permission_boundary_is_explained_in_place():
    # Without the grant the section stays on the page and explains itself.
    controller, backend = pages()
    body = call(controller).body_html
    assert t("eco.detail.trips_denied_title") in body
    assert 'data-eco-section="trips"' in body
    assert backend.reader_opened >= 1

    # The route grant alone changes nothing: it is a different permission.
    controller, _ = pages(routes_flag=True)
    body = call(controller).body_html
    assert t("eco.detail.trips_denied_title") in body

    # With the grant the evidence renders and the full surface is linked.
    controller, _ = pages(trip=True)
    body = call(controller).body_html
    assert t("eco.detail.trips_denied_title") not in body
    assert "ranking-entry/trips" in body


def test_groups_fallbacks_opaque_ids_and_xss():
    for group, included, position in (
        ("INCLUDED", True, 1), ("EXCLUDED", False, 2), ("UNKNOWN_DRIVER", False, None)
    ):
        row = prov.make_entry_row(
            assigned_id="007", ranking_group=group, ranking_included=included,
            ranking_position=position, current_driver_name=None, current_chart_present=False,
            period_label="<script>alert(1)</script>",
            qualification_status='"><img src=x onerror=alert(1)>',
            ecodriving_rating_type="' OR 1=1 --",
        )
        controller, backend = pages(responses=api_t._match_responses(entry_overrides=row))
        result = controller.ranking_entry(
            user=USER, client_code=CC, ranking_family=FAMILY,
            period_key=TOKEN, assigned_id="007", ranking_group=group,
        )
        body = result.body_html
        assert ">007<" in body
        assert D.group_label(group) in body
        assert "&lt;script&gt;" in body and "&lt;img" in body
        assert backend.reader.last_params("single_entry")["assigned_id"] == "007"
        assert_safe(body)

    for malicious in (
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "' OR 1=1 --",
    ):
        controller, backend = pages(
            responses=api_t._match_responses(entry_overrides={"assigned_id": malicious})
        )
        result = controller.ranking_entry(
            user=USER, client_code=CC, ranking_family=FAMILY,
            period_key=TOKEN, assigned_id=malicious,
        )
        assert backend.reader.last_params("single_entry")["assigned_id"] == malicious
        assert malicious not in result.body_html
        assert_safe(result.body_html)

    controller, backend = pages(responses=api_t._match_responses(entry_overrides={"assigned_id": "7"}))
    result = controller.ranking_entry(user=USER, client_code=CC, ranking_family=FAMILY,
                                      period_key=TOKEN, assigned_id="7")
    assert backend.reader.last_params("single_entry")["assigned_id"] == "7"
    assert ">7<" in result.body_html


def test_links_route_errors_and_no_dead_trip_link():
    controller, _ = pages()
    ranking = controller.rankings(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN,
        ranking_group="EXCLUDED", page="2", limit="100",
        sort="assigned_id", direction="asc",
    ).body_html
    assert "/user/eco-driving/ranking-entry?" in ranking
    for fragment in ("ranking_group=EXCLUDED", "page=2", "limit=100",
                     "sort=assigned_id", "direction=asc", "assigned_id=12345"):
        assert fragment in ranking
    assert "/ranking-entry/12345" not in ranking
    assert "ranking-entry/trips" not in ranking

    controller, _ = pages()
    assert call(controller, period_key="bad").status_code == 422

    app = api_t._FakeApp()
    routes.register_eco_driving_page_routes(
        app, pages=controller, require_user=lambda request: USER,
        render=lambda result, user: result,
    )
    assert set(app.routes) == {
        routes.LANDING_PAGE_PATH, routes.RANKINGS_PAGE_PATH, routes.RANKING_ENTRY_PAGE_PATH,
        routes.RANKING_ENTRY_TRIPS_PAGE_PATH,
        routes.RANKING_ENTRY_TRIPS_EXPORT_PATH,
        routes.RANKINGS_EXPORT_PATH, routes.PERIODS_EXPORT_PATH,
        routes.RANKING_ENTRY_EXPORT_PATH,
    }
    assert app.routes[routes.RANKING_ENTRY_PAGE_PATH] is not None


def test_trip_preview_shows_the_plate_and_only_the_plate():
    """`UI-20260820-02` B: the §6 preview and the full trip surface agree.

    The provider trip id is gone from this table and the vehicle plate leads it,
    matching `/user/eco-driving/ranking-entry/trips`. This is a *guard*, not a
    relaxation: the plate is the single authorized vehicle attribute, so every
    other vehicle, route, location and personal field must still be absent, and
    an absent plate must use the page's own placeholder rather than a new one.
    """

    import re

    plated = prov.make_trip_row(vehicle_registration="WZ 425HP")
    unplated = prov.make_trip_row(provider_trip_id=555_111, vehicle_registration=None)
    unplated["client_trip_present"] = False
    responses = api_t._match_responses()
    responses["trips_list"] = [plated, unplated]
    responses["trips_count"] = [{"total_count": 2}]
    controller, _ = pages(trip=True, responses=responses)
    body = call(controller).body_html

    assert t("eco.trip.registration") == "Nr rejestracyjny"
    assert t("eco.trip.registration") in body
    assert t("eco.trip.id") not in body, "provider trip id column reappeared"
    assert "433185849" not in body and "555111" not in body
    assert "WZ 425HP" in body

    # Scope to the trips panel: the detail page renders several tables.
    panel = re.search(
        r'<section class="eco-panel" data-eco-section="trips".*?</section>', body, re.S
    ).group(0)

    # The plate leads the preview table and carries no numeric alignment class.
    thead = re.search(r"<thead>.*?</thead>", panel, re.S).group(0)
    first_th = re.findall(r"<th\b[^>]*>.*?</th>", thead, re.S)[0]
    assert "Nr rejestracyjny" in first_th and 'class="eco-num"' not in first_th

    tbody = re.search(r"<tbody>.*?</tbody>", panel, re.S).group(0)
    rows = re.findall(r"<tr>(.*?)</tr>", tbody, re.S)
    first_cells = [re.findall(r"<td\b[^>]*>.*?</td>", r, re.S)[0] for r in rows]
    assert 'class="eco-num"' not in first_cells[0]
    # Missing plate uses the existing muted em-dash, not an invented placeholder.
    assert "eco-muted" in first_cells[1] and "—" in first_cells[1]
    assert "N/A" not in body and "brak danych" not in body.lower()

    # Still the only vehicle attribute, and nothing personal or locational.
    # Tokens are chosen to be unambiguous: bare "vin"/"model" would collide with
    # ordinary page copy ("Eco Driving" contains "vin").
    assert_safe(body)
    for banned in ("odometer", "driver_tag", "chassis", "vehicle_vin", "@"):
        assert banned not in body.lower(), banned
    # The DTO behind this table exposes exactly one vehicle-named field.
    svc, _ = service(trip=True, responses=responses)
    row = svc.list_contributing_trips(
        user=USER, client_code=CC, ranking_family=FAMILY,
        period_key=TOKEN, assigned_id="12345").body["data"][0]
    assert [k for k in row if "vehicle" in k] == ["vehicle_registration"]



def test_forbidden_admin_and_cross_client_non_disclosure():
    backend = api_t.FakeBackend(access_map={}, responses=api_t._match_responses())
    controller = EcoDrivingPages(EcoDrivingApiService(backend))
    for user in (USER, api_t.ADMIN):
        result = controller.ranking_entry(
            user=user, client_code=CC, ranking_family=FAMILY,
            period_key=TOKEN, assigned_id="' OR 1=1 --",
        )
        assert result.status_code == 403 and backend.reader_opened == 0
        assert "' OR 1=1 --" not in result.body_html


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"OK - Eco Driving driver drill-down tests passed ({len(tests)} cases)")


if __name__ == "__main__":
    main()
