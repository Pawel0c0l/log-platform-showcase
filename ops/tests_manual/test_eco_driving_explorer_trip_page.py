#!/usr/bin/env python3
"""Focused Stage 5 checks for paginated contributing-trip evidence."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_eco_driving_explorer_api as api_t
import test_eco_driving_explorer_provider as prov

from html import escape as _escape  # noqa: E402
from api.eco_driving_explorer import html as H  # noqa: E402
from api.portal_ui.i18n import t as _t  # noqa: E402
from api.eco_driving_explorer import page_routes, queries
from api.eco_driving_explorer.models import EVENT_METRIC_COLUMNS, TripFilters
from api.eco_driving_explorer.pages import EcoDrivingPages
from api.eco_driving_explorer.service import EcoDrivingApiService
from api.eco_driving_explorer import trip_view_models as T_VM


def _thead_of(body: str) -> str:
    import re
    match = re.search(r"<thead>.*?</thead>", body, re.S)
    return match.group(0) if match else ""

CC, FAMILY, TOKEN, USER = api_t.CLIENT_CODE, api_t.FAMILY, api_t.WEEKLY_TOKEN, api_t.USER


def make_pages(*, ranking=True, trip=True, routes=False, responses=None):
    backend = api_t.FakeBackend(
        access_map={(USER["user_id"], CC): api_t._access(ranking=ranking, trip=trip, routes=routes)},
        responses=responses or api_t._match_responses(),
    )
    return EcoDrivingPages(EcoDrivingApiService(backend)), backend


def call(controller, **extra):
    params = dict(user=USER, client_code=CC, ranking_family=FAMILY,
                  period_key=TOKEN, assigned_id="12345")
    params.update(extra)
    return controller.ranking_entry_trips(**params)


def test_route_permissions_and_detail_link():
    controller, _ = make_pages()
    app = api_t._FakeApp()
    page_routes.register_eco_driving_page_routes(
        app, pages=controller, require_user=lambda request: USER,
        render=lambda result, user: result,
    )
    assert list(app.routes).count(page_routes.RANKING_ENTRY_TRIPS_PAGE_PATH) == 1
    assert {page_routes.LANDING_PAGE_PATH, page_routes.RANKINGS_PAGE_PATH,
            page_routes.RANKING_ENTRY_PAGE_PATH}.issubset(app.routes)
    assert "{" not in page_routes.RANKING_ENTRY_TRIPS_PAGE_PATH

    allowed, _ = make_pages()
    detail = allowed.ranking_entry(
        user=USER, client_code=CC, ranking_family=FAMILY, period_key=TOKEN,
        assigned_id="12345", ranking_group="EXCLUDED", page="2", limit="100",
        sort="assigned_id", direction="desc",
    )
    assert _t("eco.detail.trips_open") in detail.body_html
    for value in ("ranking_group=EXCLUDED", "ranking_page=2", "ranking_limit=100",
                  "ranking_sort=assigned_id", "ranking_direction=desc"):
        assert value in detail.body_html

    denied, backend = make_pages(trip=False)
    detail = denied.ranking_entry(user=USER, client_code=CC, ranking_family=FAMILY,
                                  period_key=TOKEN, assigned_id="12345")
    assert _t("eco.detail.trips_open") not in detail.body_html
    # The section itself must stay on the page and explain the missing grant.
    assert _t("eco.detail.trips_denied_title") in detail.body_html
    opened_before = backend.reader_opened
    result = call(denied)
    # The denied trip page opens no reader at all: authorization fails before
    # any client-database work. The earlier opens belong to the detail page.
    assert result.status_code == 403 and backend.reader_opened == opened_before

    for ranking, trip, routes in ((False, True, False), (False, False, True), (False, False, False)):
        controller, backend = make_pages(ranking=ranking, trip=trip, routes=routes)
        assert call(controller).status_code == 403
        assert backend.reader_opened == 0

    admin_backend = api_t.FakeBackend(access_map={}, responses=api_t._match_responses())
    admin_pages = EcoDrivingPages(EcoDrivingApiService(admin_backend))
    assert admin_pages.ranking_entry_trips(
        user=api_t.ADMIN, client_code=CC, ranking_family=FAMILY,
        period_key=TOKEN, assigned_id="12345").status_code == 403


def test_shared_filters_bounds_sort_and_audit():
    controller, backend = make_pages()
    result = call(
        controller, trip_start_from="2026-07-02", trip_start_to="2026-07-20T12:30:00+02:00",
        provider_trip_id="433185849", min_distance_meters="1000",
        max_distance_meters="50000", has_scoring_events="true",
        page="2", limit="25", sort="total_scoring_events", direction="desc",
        ranking_group="INCLUDED", ranking_page="3", ranking_limit="100",
        ranking_sort="trips_count", ranking_direction="asc",
    )
    assert result.status_code == 200
    list_sql = backend.reader.last_sql("trips_list")
    count_sql = next(sql for sql, _ in backend.reader.calls if backend.reader._kind(sql) == "trips_count" and "provider_trip_id = %(provider_trip_id)s" in sql)
    for fragment in ("a.provider_trip_id = %(provider_trip_id)s",
                     "a.trip_distance_meters >= %(min_distance_meters)s",
                     "a.trip_distance_meters <= %(max_distance_meters)s"):
        assert fragment in list_sql and fragment in count_sql
    assert "> 0" in list_sql and "> 0" in count_sql
    params = backend.reader.last_params("trips_list")
    assert params["provider_trip_id"] == 433185849
    assert params["min_distance_meters"] == 1000 and params["max_distance_meters"] == 50000
    assert params["offset"] == 25
    body = result.body_html
    for value in ("trip_start_from=2026-07-02", "provider_trip_id=433185849",
                  "ranking_page=3", "ranking_limit=100"):
        assert value in body
    assert "page=1" in body  # filter/sort/size controls reset pagination
    audit = [e for e in backend.audit_events if e["event_type"] == "eco_driving_trip_details_viewed"]
    assert len(audit) == 1
    meta = audit[0]["metadata"]
    assert meta["provider_trip_filter_used"] is True
    assert 433185849 not in meta.values()
    assert "assigned_id" not in meta and "assigned_id_digest" in meta


def test_every_sort_direction_and_pagination_contract():
    provider = prov._provider()
    expected_fragments = {
        "trip_start_ts": "a.trip_start_ts",
        "trip_end_ts": "a.trip_end_ts",
        "trip_distance_meters": "a.trip_distance_meters",
        "provider_trip_id": "a.provider_trip_id",
        "total_scoring_events": "COALESCE(a.overrev_events_count, 0)",
    }
    for field, fragment in expected_fragments.items():
        for direction in ("asc", "desc"):
            reader = prov.FakeRowReader({
                "trips_list": [prov.make_trip_row()],
                "trips_count": [{"total_count": 60}],
            })
            page = provider.list_contributing_trips(
                reader, prov.WEEKLY_KEY, "12345", sort_field=field,
                direction=direction, page=2, limit=25,
            )
            sql = reader.last_sql("trips_list")
            assert fragment in sql and f" {direction.upper()} " in sql
            assert "a.provider_trip_id ASC" in sql
            assert reader.last_params("trips_list")["offset"] == 25
            assert page.page == 2 and page.limit == 25 and page.has_next is True

    reader = prov.FakeRowReader({
        "trips_list": [prov.make_trip_row()],
        "trips_count": [{"total_count": 1}],
    })
    filters = TripFilters(
        trip_start_from=queries.business_local_midnight(prov.WEEKLY_KEY.period_start_date),
        trip_start_to=queries.business_local_midnight(prov.WEEKLY_KEY.period_end_date),
        has_scoring_events=False,
    )
    provider.list_contributing_trips(reader, prov.WEEKLY_KEY, "12345", filters=filters)
    assert "= 0" in reader.last_sql("trips_list")
    assert any("= 0" in sql for sql, _ in reader.calls if reader._kind(sql) == "trips_count")


def test_validation_and_exact_identifiers():
    controller, backend = make_pages()
    invalid = (
        {"trip_start_from": "2026-07-20", "trip_start_to": "2026-07-19"},
        {"min_distance_meters": "5", "max_distance_meters": "4"},
        {"provider_trip_id": "' OR 1=1 --"},
        {"has_scoring_events": "maybe"},
        {"sort": "trip_start_ts; DROP TABLE x"},
        {"direction": "sideways"},
        {"page": "0"}, {"limit": "501"},
    )
    for values in invalid:
        before = backend.reader_opened
        result = call(controller, **values)
        assert result.status_code == 422
        assert "Traceback" not in result.body_html and "DROP TABLE" not in result.body_html
        if "provider_trip_id" in values:
            assert backend.reader_opened == before
    svc = controller._service
    for assigned in ("007", "7"):
        res = svc.list_contributing_trips(
            user=USER, client_code=CC, ranking_family=FAMILY,
            period_key=TOKEN, assigned_id=assigned)
        assert res.status_code == 200
        assert backend.reader.last_params("trips_list")["assigned_id"] == assigned


def test_membership_left_join_missing_source_and_safe_contract():
    malicious_id = "<script>alert(1)</script>"
    trip = prov.make_trip_row(assigned_id=malicious_id)
    trip["client_trip_present"] = False
    trip["assignment_source"] = 'DRIVER_RESTRICTIONS"><img src=x onerror=alert(1)>'
    responses = api_t._match_responses(entry_overrides={"assigned_id": malicious_id})
    responses["trips_list"] = [trip]
    responses["trips_count"] = [{"total_count": 1}]
    controller, backend = make_pages(routes=True, responses=responses)
    result = call(controller, assigned_id=malicious_id)
    assert result.status_code == 200
    body = result.body_html
    assert malicious_id not in body and "&lt;script&gt;" in body
    assert backend.reader.last_params("trips_list")["assigned_id"] == malicious_id
    sql = backend.reader.last_sql("trips_list")
    assert "LEFT JOIN public.client_trips" in sql
    # The inclusion clause is the driver family's own, copied verbatim from its
    # aggregation job. The person family's clause is deliberately different and
    # is asserted in the arbitrary-week suite.
    for fragment in ("a.client_id = %(client_id)s::uuid", "a.assigned_id = %(assigned_id)s",
                     "a.trip_start_ts >=", "a.trip_start_ts <",
                     "a.aggregation_included IS TRUE", "a.is_private_trip IS FALSE"):
        assert fragment in sql
    assert "brak bieżącego wiersza client_trips" in body
    assert "&lt;img" in body and "<img" not in body
    for label in (_t("eco.metric.overrev_short"), _t("eco.metric.harsh_braking_short"),
                  _t("eco.metric.harsh_acceleration_short"), _t("eco.metric.harsh_turning_short"),
                  _t("eco.metric.idle_short"), _t("eco.metric.speeding_140_short"),
                  _t("eco.metric.speeding_160_short"), _t("eco.metric.speeding_170_short")):
        # `> 140` and friends are HTML-escaped in the rendered header.
        assert _escape(label, quote=True) in body
    # Trip-level values are sums for that one trip and say so; there is no
    # invented per-trip contribution-to-score column (`EC-21` scope note).
    assert _t("eco.trip.sum_note") in body
    assert "Wkład w wynik" not in body
    assert len(EVENT_METRIC_COLUMNS) == 8
    assert _t("eco.detail.trips") in body
    assert H.LINEAGE_LABEL in body
    assert "niezmiennym zapisem" in body
    low = body.lower()
    for banned in ("latitude", "longitude", "polyline", "email",
                   "start address", "end address"):
        assert banned not in low
    # `UI-20260820-01`: the vehicle plate is the one authorized vehicle
    # attribute on this surface, and it replaced the provider trip id.
    assert T_VM.REGISTRATION_LABEL in body
    assert "ID przejazdu" not in _thead_of(body)
    assert api_t.DB_NAME not in body and api_t.TRUSTED_CLIENT_ID not in body


def test_api_meta_pagination_empty_and_safe_fields():
    backend = api_t.FakeBackend(
        access_map={(USER["user_id"], CC): api_t._access(ranking=True, trip=True)},
        responses={"trips_list": [], "trips_count": [{"total_count": 0}]},
    )
    res = EcoDrivingApiService(backend).list_contributing_trips(
        user=USER, client_code=CC, ranking_family=FAMILY,
        period_key=TOKEN, assigned_id="12345", page="4", limit="50",
        trip_start_from="2026-06-01", trip_start_to="2026-08-01",
        has_scoring_events="false",
    )
    assert res.status_code == 200 and res.body["data"] == []
    meta = res.body["meta"]
    assert meta["page"] == 4 and meta["limit"] == 50
    assert meta["sort"] == "trip_start_ts" and meta["direction"] == "ASC"
    assert meta["filters_active"] is True
    assert meta["lineage_quality"] == "RECONSTRUCTED_CURRENT_STATE"
    assert meta["filters"]["effective_trip_start_from"].startswith("2026-07-01")
    assert meta["filters"]["effective_trip_start_to_exclusive"].startswith("2026-07-27")

    row_backend = api_t.FakeBackend(
        access_map={(USER["user_id"], CC): api_t._access(ranking=True, trip=True)},
        responses={"trips_list": [prov.make_trip_row()], "trips_count": [{"total_count": 1}]},
    )
    row = EcoDrivingApiService(row_backend).list_contributing_trips(
        user=USER, client_code=CC, ranking_family=FAMILY,
        period_key=TOKEN, assigned_id="12345").body["data"][0]
    for key in ("provider_trip_id", "trip_start_ts", "trip_end_ts", "trip_distance_meters",
                "distance_kilometers", "assignment_source", "source_trip_present",
                "event_counts", "total_scoring_events"):
        assert key in row
    assert row["distance_kilometers"] == "36.000"
    assert set(row["event_counts"]) == set(EVENT_METRIC_COLUMNS)
    blob = json.dumps(row).lower()
    for banned in ("latitude", "longitude", "address", "route", "polyline",
                   "email", "driver_tag"):
        assert banned not in blob
    # The plate is present and is the only vehicle attribute in the DTO.
    assert row["vehicle_registration"] == "WX 1234A"
    assert [k for k in row if "vehicle" in k] == ["vehicle_registration"]


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("PASS:", test.__name__)
    print(f"Eco Driving contributing-trip page tests passed ({len(tests)} cases)")


if __name__ == "__main__":
    main()
