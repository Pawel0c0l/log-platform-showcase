#!/usr/bin/env python3
"""Manual unit-style checks for the Eco Driving Explorer read API (Stage 2).

No database and no HTTP client are required. The ``EcoDrivingApiService``
controller is exercised directly against a fake backend that returns canned
access decisions and a synthetic client-database reader (reused from the Stage 1
provider tests). A minimal FastAPI-route registration check confirms the thin
HTTP adapter wires paths and produces JSON envelopes for authentication.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_explorer_api.py
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Reuse the Stage 1 fakes and synthetic-row builders.
import test_eco_driving_explorer_provider as prov  # noqa: E402

from api.eco_driving_explorer.access import EcoDrivingClientAccess, merge_eco_access, no_eco_access  # noqa: E402
from api.eco_driving_explorer.backend import ResolvedEcoClient, assigned_id_digest  # noqa: E402
from api.eco_driving_explorer.errors import (  # noqa: E402
    EnvironmentClientMismatchError,
    PeriodNotFoundError,
    ProviderNotFoundError,
    RankingEntryNotFoundError,
    ReconstructionUnavailableError,
)
from api.eco_driving_explorer.models import LineageQuality  # noqa: E402
from api.eco_driving_explorer.service import ApiResult, EcoDrivingApiService  # noqa: E402
from api.eco_driving_explorer import http as eco_http  # noqa: E402

CLIENT_CODE = "ALPHA00001"
FAMILY = "driver"
TRUSTED_CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
DB_NAME = "alpha_main"

USER = {"user_id": "u-1", "username": "operator", "is_admin": False}
ADMIN = {"user_id": "admin-1", "username": "root", "is_admin": True}

WEEKLY_TOKEN = prov.WEEKLY_KEY.token


def _access(**flags) -> EcoDrivingClientAccess:
    return EcoDrivingClientAccess(
        client_code=CLIENT_CODE,
        can_view_eco_ranking=flags.get("ranking", False),
        can_view_eco_trip_details=flags.get("trip", False),
        can_view_eco_trip_routes=flags.get("routes", False),
        client_is_active=flags.get("active", True),
        sources=flags.get("sources", ()),
    )


class FakeBackend:
    def __init__(self, *, access_map=None, responses=None, resolve_error=None):
        self.access_map = access_map or {}
        self.reader = prov.FakeRowReader(responses or {})
        self.resolve_error = resolve_error
        self.audit_events: list[dict] = []
        self.reader_opened = 0
        self.last_binding: ResolvedEcoClient | None = None

    def fetch_access(self, user_id, client_code):
        return self.access_map.get((user_id, client_code)) or no_eco_access(client_code)

    def resolve_binding(self, client_code, ranking_family):
        if self.resolve_error is not None:
            raise self.resolve_error
        # client_id always comes from trusted config, never the request.
        return ResolvedEcoClient(
            client_code=client_code,
            ranking_family=ranking_family,
            client_id=TRUSTED_CLIENT_ID,
            database_name=DB_NAME,
        )

    @contextmanager
    def open_reader(self, binding):
        self.reader_opened += 1
        self.last_binding = binding
        yield self.reader

    def record_audit(self, *, event_type, actor_user_id, client_code, request, metadata):
        self.audit_events.append(
            {
                "event_type": event_type,
                "actor_user_id": actor_user_id,
                "client_code": client_code,
                "metadata": metadata,
            }
        )


def _service(backend: FakeBackend) -> EcoDrivingApiService:
    return EcoDrivingApiService(backend)


def _weekly_period_rows():
    return [
        {
            "month_start_date": date(2026, 7, 1),
            "period_start_date": date(2026, 7, 1),
            "period_end_date": date(2026, 7, 6),
            "period_sequence_in_month": 1,
            "period_label": "2026-07-W1",
            "is_partial_period": True,
            "included_count": 30,
            "excluded_count": 1000,
            "unknown_count": 70,
            "source_calculated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        },
        {
            "month_start_date": date(2026, 7, 1),
            "period_start_date": date(2026, 7, 1),
            "period_end_date": date(2026, 8, 1),
            "period_sequence_in_month": 5,
            "period_label": "2026-07-W5",
            "is_partial_period": True,
            "included_count": 32,
            "excluded_count": 1100,
            "unknown_count": 90,
            "source_calculated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        },
    ]


def _match_responses(entry_overrides=None, totals_overrides=None):
    entry = prov.make_entry_row(**(entry_overrides or {}))
    return {
        "entries_list": [entry],
        "entries_count": [{"total_count": 1}],
        "single_entry": [entry],
        "trips_list": [prov.make_trip_row() for _ in range(60)],
        "trips_count": [{"total_count": 60}],
        "recon_totals": [prov.make_totals_row(**(totals_overrides or {}))],
        "window_diag": [prov.make_window_diag()],
        "weekly_periods": _weekly_period_rows(),
        "monthly_periods": [],
    }


# --- authentication ----------------------------------------------------------

def test_unauthenticated_returns_401():
    svc = _service(FakeBackend())
    res = svc.list_providers(user=None)
    assert res.status_code == 401
    assert res.body["error"]["code"] == "UNAUTHENTICATED"
    assert res.body["data"] is None
    res = svc.list_periods(user=None, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 401
    print("PASS: unauthenticated requests return JSON 401")


def test_authenticated_user_resolved_via_session_only():
    # The service never reads a client_id/db-name from the caller; only the
    # user dict (from the existing session lookup) and safe query params.
    import inspect

    for name in ("list_periods", "list_ranking_entries", "get_ranking_entry",
                 "list_contributing_trips", "reconcile_ranking_entry"):
        params = set(inspect.signature(getattr(EcoDrivingApiService, name)).parameters)
        assert "client_id" not in params, name
        assert "database_name" not in params, name
    print("PASS: API accepts no client_id/database_name; identity comes from session + trusted config")


# --- effective RBAC ----------------------------------------------------------

def test_effective_access_union_pure():
    # direct false + group true -> access
    a = merge_eco_access(CLIENT_CODE, direct={"can_view_eco_ranking": False},
                         group={"can_view_eco_ranking": True}, client_is_active=True)
    assert a.can_view_eco_ranking is True and "group" in a.sources
    # direct true + group false -> access (one FALSE cannot override a TRUE)
    b = merge_eco_access(CLIENT_CODE, direct={"can_view_eco_trip_details": True},
                         group={"can_view_eco_trip_details": False}, client_is_active=True)
    assert b.can_view_eco_trip_details is True and "direct" in b.sources
    # missing direct and group -> no access
    c = merge_eco_access(CLIENT_CODE, direct=None, group=None, client_is_active=True)
    assert c.any_access is False and c.sources == ()
    print("PASS: effective access is the additive union of direct OR active-group flags")


def test_ranking_flag_grants_ranking_endpoints():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
        responses=_match_responses(),
    )
    svc = _service(backend)
    res = svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                   period_key=WEEKLY_TOKEN, ranking_group="INCLUDED")
    assert res.status_code == 200
    print("PASS: can_view_eco_ranking grants ranking list access")


def test_group_grant_grants_access():
    access = merge_eco_access(CLIENT_CODE, direct=None, group={"can_view_eco_ranking": True}, client_is_active=True)
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): access}, responses=_match_responses())
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 200
    print("PASS: group-only can_view_eco_ranking grants access")


def test_missing_access_returns_403_and_does_not_disclose():
    backend = FakeBackend(access_map={}, responses=_match_responses())
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 403
    assert res.body["error"]["code"] == "FORBIDDEN"
    # never opened a client DB reader or resolved the provider
    assert backend.reader_opened == 0
    assert backend.audit_events == []
    print("PASS: no access -> 403 without disclosing provider existence or auditing")


def test_ranking_access_alone_does_not_grant_trip_details():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=False)},
        responses=_match_responses(),
    )
    svc = _service(backend)
    trips = svc.list_contributing_trips(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                        period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert trips.status_code == 403
    recon = svc.reconcile_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                        period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert recon.status_code == 403
    print("PASS: ranking access alone does not grant trip-detail/reconciliation endpoints")


def test_trip_flag_grants_trip_details():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses=_match_responses(),
    )
    res = _service(backend).list_contributing_trips(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    print("PASS: can_view_eco_trip_details grants trip-detail access")


def test_routes_flag_does_not_expose_route_data():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True, routes=True)},
        responses=_match_responses(),
    )
    res = _service(backend).list_contributing_trips(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    blob = json.dumps(res.body).lower()
    for banned in ("latitude", "longitude", "polyline", "address", "email", "route"):
        assert banned not in blob, banned
    print("PASS: can_view_eco_trip_routes exposes no route/location data in this stage")


def test_admin_does_not_bypass_client_grants():
    # No grant for admin -> 403, matching the existing portal convention.
    backend = FakeBackend(access_map={}, responses=_match_responses())
    res = _service(backend).list_periods(user=ADMIN, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 403
    print("PASS: administrators do not bypass Eco client grants (portal convention preserved)")


# --- provider list -----------------------------------------------------------

def test_providers_list_only_authorized():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)})
    res = _service(backend).list_providers(user=USER)
    assert res.status_code == 200
    data = res.body["data"]
    assert len(data) == 1
    p = data[0]
    assert p["client_code"] == CLIENT_CODE and p["provider_key"] == "alpha00001_driver"
    assert p["capabilities"] == {"can_view_ranking": True, "can_view_trip_details": True, "can_view_trip_routes": False}
    assert p["lineage_modes"] == ["RECONSTRUCTED_CURRENT_STATE"]
    # no leaked infra fields
    blob = json.dumps(p).lower()
    for banned in ("database", "client_id", "uuid", "host", "table", "credential", "password"):
        assert banned not in blob, banned
    print("PASS: providers list returns only authorized providers without infra metadata")


def test_providers_list_unauthorized_omitted():
    backend = FakeBackend(access_map={})  # no grant
    res = _service(backend).list_providers(user=USER)
    assert res.status_code == 200
    assert res.body["data"] == []  # omitted, not allowed=false
    print("PASS: unauthorized provider is omitted from the list, never returned as allowed=false")


# --- period API --------------------------------------------------------------

def test_periods_weekly_w5_and_exclusive_end():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"weekly_periods": _weekly_period_rows()})
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 200
    labels = [p["period_label"] for p in res.body["data"]]
    seqs = [p["period_sequence_in_month"] for p in res.body["data"]]
    assert "2026-07-W5" in labels and 5 in seqs
    w5 = next(p for p in res.body["data"] if p["period_sequence_in_month"] == 5)
    assert w5["period_end_date_exclusive"] == "2026-08-01"
    assert w5["is_partial_period"] is True
    assert w5["lineage_quality"] == "RECONSTRUCTED_CURRENT_STATE"
    print("PASS: weekly periods preserve W5, exclusive end, partial flag, and lineage")


def test_periods_monthly_empty_returns_200_empty_list():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"monthly_periods": []})
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="monthly")
    assert res.status_code == 200
    assert res.body["data"] == []
    assert res.body["error"] is None
    print("PASS: monthly zero-row state returns HTTP 200 with an empty data list")


def test_periods_malformed_type_422():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)})
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="daily")
    assert res.status_code == 422 and res.body["error"]["code"] == "UNSUPPORTED_PERIOD_TYPE"
    print("PASS: malformed period_type returns 422")


# --- ranking API -------------------------------------------------------------

def test_ranking_groups_supported():
    for group in ("INCLUDED", "EXCLUDED", "UNKNOWN_DRIVER"):
        backend = FakeBackend(
            access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
            responses={"entries_list": [prov.make_entry_row(ranking_group=group)], "entries_count": [{"total_count": 1}]},
        )
        res = _service(backend).list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                     period_key=WEEKLY_TOKEN, ranking_group=group)
        assert res.status_code == 200
        assert res.body["data"][0]["ranking_group"] == group
    print("PASS: INCLUDED / EXCLUDED / UNKNOWN_DRIVER ranking groups all work")


def test_persisted_group_not_replaced_by_current_chart():
    # Persisted EXCLUDED entry whose current chart says included=True stays EXCLUDED.
    row = prov.make_entry_row(ranking_group="EXCLUDED", ranking_included=False,
                              current_chart_present=True, current_chart_ranking_included=True)
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"single_entry": [row]})
    res = _service(backend).get_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                              period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    body = res.body["data"]
    assert body["ranking_group"] == "EXCLUDED" and body["ranking_included"] is False
    assert body["current_chart"]["current_chart_ranking_included"] is True
    print("PASS: persisted ranking group is not overwritten by current-chart state")


def test_missing_chart_row_keeps_entry():
    row = prov.make_entry_row(current_chart_present=False, current_driver_name=None, current_chart_ranking_included=None)
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"single_entry": [row]})
    res = _service(backend).get_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                              period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    assert res.body["data"]["current_chart"]["driver_metadata_source"] == "NONE"
    print("PASS: missing current-chart row does not remove the ranking entry")


def test_ranking_pagination_meta():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"entries_list": [prov.make_entry_row()], "entries_count": [{"total_count": 250}]})
    res = _service(backend).list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                 period_key=WEEKLY_TOKEN, ranking_group="INCLUDED", page="2", limit="100")
    meta = res.body["meta"]
    assert meta["page"] == 2 and meta["limit"] == 100 and meta["total_count"] == 250
    assert meta["has_next"] is True and meta["ranking_group"] == "INCLUDED"
    print("PASS: ranking pagination metadata is deterministic")


def test_ranking_invalid_group_and_sort_and_limit():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)}, responses=_match_responses())
    svc = _service(backend)
    r1 = svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                  period_key=WEEKLY_TOKEN, ranking_group="BOGUS")
    assert r1.status_code == 422 and r1.body["error"]["code"] == "INVALID_RANKING_GROUP"
    r2 = svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                  period_key=WEEKLY_TOKEN, ranking_group="INCLUDED", sort="salary")
    assert r2.status_code == 422 and r2.body["error"]["code"] == "INVALID_SORT_FIELD"
    r3 = svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                  period_key=WEEKLY_TOKEN, ranking_group="INCLUDED", limit="501")
    assert r3.status_code == 422 and r3.body["error"]["code"] == "INVALID_PAGINATION"
    r4 = svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                  period_key="not-a-token", ranking_group="INCLUDED")
    assert r4.status_code == 422 and r4.body["error"]["code"] == "MALFORMED_PERIOD_TOKEN"
    print("PASS: invalid group/sort/limit/period-token all return 422 with stable codes")


# --- opaque assigned id ------------------------------------------------------

def test_assigned_id_opaque_and_bound():
    for aid in ("007", "7", "O'Brien;DROP TABLE eco--", "0x1F"):
        backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                              responses={"single_entry": [prov.make_entry_row(assigned_id=aid)]})
        res = _service(backend).get_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                  period_key=WEEKLY_TOKEN, assigned_id=aid)
        assert res.status_code == 200
        sql = backend.reader.last_sql("single_entry")
        # passed verbatim as a bound parameter, never normalized/cast/stripped
        assert backend.reader.last_params("single_entry")["assigned_id"] == aid
        # assigned_id is always a bound placeholder, never interpolated
        assert "%(assigned_id)s" in sql
        # injection-like text never appears inside the SQL string itself
        if any(ch in aid for ch in " ';-"):
            assert aid not in sql
        # audit stores only a redacted digest, never the raw id
        meta = backend.audit_events[-1]["metadata"]
        assert meta["assigned_id_digest"] == assigned_id_digest(aid)
        assert "assigned_id" not in meta  # no raw key
        assert all(str(v) != aid for v in meta.values())  # raw id never stored as a value
        if any(ch in aid for ch in " ';-"):
            assert aid not in json.dumps(meta)
    # 007 and 7 remain distinct opaque identifiers
    assert assigned_id_digest("007") != assigned_id_digest("7")
    print("PASS: assigned_id is opaque, bound, injection-safe, and audited only as a digest")


def test_assigned_id_not_in_path():
    paths = [
        eco_http.PROVIDERS_PATH, eco_http.PERIODS_PATH, eco_http.RANKING_ENTRIES_PATH,
        eco_http.RANKING_ENTRY_PATH, eco_http.RANKING_ENTRY_TRIPS_PATH,
        eco_http.RANKING_ENTRY_RECONCILIATION_PATH,
    ]
    for p in paths:
        assert "{" not in p and "assigned_id" not in p
    print("PASS: assigned_id is never placed in a path segment")


# --- trip details ------------------------------------------------------------

def test_trip_details_safe_fields_only():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses={"trips_list": [prov.make_trip_row()], "trips_count": [{"total_count": 1}]},
    )
    res = _service(backend).list_contributing_trips(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    trip = res.body["data"][0]
    expected = {
        "provider_trip_id", "trip_start_ts", "trip_end_ts", "assigned_id", "assignment_source",
        "trip_distance_meters", "distance_kilometers", "total_scoring_events",
        "source_trip_present", "aggregation_included", "is_private_trip", "exclusion_reason",
        "event_counts", "client_trip_present",
        # `UI-20260820-01`: the vehicle plate is an authorized addition to this
        # DTO. This set stays exhaustive on purpose — it is the fail-closed
        # guard against any further widening.
        "vehicle_registration",
    }
    assert set(trip.keys()) == expected
    assert trip["vehicle_registration"] == "WX 1234A"
    assert res.body["meta"]["lineage_quality"] == "RECONSTRUCTED_CURRENT_STATE"
    print("PASS: contributing trips expose only safe scoring fields with reconstructed lineage")


# --- reconciliation ----------------------------------------------------------

def test_reconciliation_match():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses=_match_responses(),
    )
    res = _service(backend).reconcile_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 200
    body = res.body["data"]
    assert body["reconciliation_status"] == "MATCH"
    assert body["mismatched_fields"] == []
    assert body["lineage_quality"] == "RECONSTRUCTED_CURRENT_STATE"
    assert body["persisted"]["eco_driving_score_total"] == "100.00"
    assert "score_definition" in body and body["score_definition"]["ranking_family"] == "driver"
    blob = json.dumps(res.body).lower()
    assert "select" not in blob and "from public." not in blob
    print("PASS: reconciliation MATCH serializes with precise Decimal, score definition, and no SQL")


def test_reconciliation_mismatch_lists_fields():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses=_match_responses(totals_overrides={"trips_count": 59}),
    )
    res = _service(backend).reconcile_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    body = res.body["data"]
    assert body["reconciliation_status"] == "MISMATCH"
    assert "trips_count" in body["mismatched_fields"]
    print("PASS: reconciliation MISMATCH lists mismatched field names")


def test_reconciliation_unavailable_maps_409():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses={"single_entry": [prov.make_entry_row()]},  # totals row missing -> unavailable
    )
    res = _service(backend).reconcile_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                                    period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert res.status_code == 409 and res.body["error"]["code"] == "RECONSTRUCTION_UNAVAILABLE"
    print("PASS: missing reconstruction totals map to 409 RECONSTRUCTION_UNAVAILABLE")


# --- cross-client isolation --------------------------------------------------

def test_isolation_permission_scoped_per_client():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
        responses=_match_responses(),
    )
    svc = _service(backend)
    # access for ALPHA00001 does not grant a different client code
    res = svc.list_periods(user=USER, client_code="OTHER00002", ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 403
    print("PASS: permission for one client does not grant another client")


def test_isolation_provider_always_receives_trusted_client_id():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses=_match_responses(),
    )
    svc = _service(backend)
    svc.get_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                          period_key=WEEKLY_TOKEN, assigned_id="12345")
    assert backend.reader.last_params("single_entry")["client_id"] == TRUSTED_CLIENT_ID
    assert backend.last_binding.client_id == TRUSTED_CLIENT_ID
    print("PASS: every provider query receives the trusted server-side client_id")


def test_isolation_provider_not_found_maps_404():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          resolve_error=ProviderNotFoundError("nope"))
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 404 and res.body["error"]["code"] == "PROVIDER_NOT_FOUND"
    print("PASS: unknown provider fails closed with 404")


def test_environment_mismatch_maps_503():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          resolve_error=EnvironmentClientMismatchError("mismatch"))
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert res.status_code == 503 and res.body["error"]["code"] == "ENVIRONMENT_CLIENT_MISMATCH"
    print("PASS: environment/client mismatch fails closed with 503")


# --- audit -------------------------------------------------------------------

def test_audit_events_created_and_sanitized():
    backend = FakeBackend(
        access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True, trip=True)},
        responses=_match_responses(),
    )
    svc = _service(backend)
    svc.list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                             period_key=WEEKLY_TOKEN, ranking_group="INCLUDED")
    svc.get_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                          period_key=WEEKLY_TOKEN, assigned_id="12345")
    svc.list_contributing_trips(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                period_key=WEEKLY_TOKEN, assigned_id="12345")
    svc.reconcile_ranking_entry(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                period_key=WEEKLY_TOKEN, assigned_id="12345")
    types = [e["event_type"] for e in backend.audit_events]
    assert "eco_driving_ranking_viewed" in types
    assert "eco_driving_ranking_entry_viewed" in types
    assert "eco_driving_trip_details_viewed" in types
    assert "eco_driving_reconciliation_viewed" in types
    for e in backend.audit_events:
        meta = e["metadata"]
        assert "assigned_id" not in meta  # only assigned_id_digest may be present
        assert all(str(v) != "12345" for v in meta.values())  # raw assigned id never logged
        blob = json.dumps(meta).lower()
        for banned in ("select", "from public.", "driver_name", "email", "latitude", "longitude", "cookie", "password"):
            assert banned not in blob, banned
    print("PASS: ranking/entry/trip/reconciliation audits are created and sanitized (digest only)")


def test_failed_authorization_is_not_audited():
    backend = FakeBackend(access_map={}, responses=_match_responses())
    _service(backend).list_ranking_entries(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY,
                                           period_key=WEEKLY_TOKEN, ranking_group="INCLUDED")
    assert backend.audit_events == []
    print("PASS: failed authorization does not log request values")


# --- serialization envelope --------------------------------------------------

def test_envelope_shape():
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"weekly_periods": _weekly_period_rows()})
    res = _service(backend).list_periods(user=USER, client_code=CLIENT_CODE, ranking_family=FAMILY, period_type="weekly")
    assert set(res.body.keys()) == {"data", "meta", "error"}
    assert isinstance(res.body["data"], list)
    print("PASS: responses use the {data, meta, error} envelope")


# --- HTTP adapter ------------------------------------------------------------

class _FakeApp:
    def __init__(self):
        self.routes: dict[str, object] = {}

    def add_api_route(self, path, handler, methods=None, name=None, include_in_schema=True):
        self.routes[path] = handler


def _make_request(query: str = ""):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/user/eco-driving/api/providers",
        "headers": [],
        "query_string": query.encode("ascii"),
    }
    return Request(scope)


def test_http_adapter_registers_routes_and_returns_json_401():
    app = _FakeApp()
    backend = FakeBackend(access_map={(USER["user_id"], CLIENT_CODE): _access(ranking=True)},
                          responses={"weekly_periods": _weekly_period_rows()})
    svc = _service(backend)

    # Unauthenticated: session lookup returns None.
    eco_http.register_eco_driving_routes(app, service=svc, get_current_user=lambda req: None)
    assert set(app.routes.keys()) == {
        eco_http.PROVIDERS_PATH, eco_http.PERIODS_PATH, eco_http.RANKING_ENTRIES_PATH,
        eco_http.RANKING_ENTRY_PATH, eco_http.RANKING_ENTRY_TRIPS_PATH,
        eco_http.RANKING_ENTRY_RECONCILIATION_PATH,
    }
    handler = app.routes[eco_http.PROVIDERS_PATH]
    resp = asyncio.new_event_loop().run_until_complete(handler(_make_request()))
    assert resp.status_code == 401
    payload = json.loads(bytes(resp.body))
    assert payload["error"]["code"] == "UNAUTHENTICATED"

    # Authenticated: session lookup returns the test user.
    app2 = _FakeApp()
    eco_http.register_eco_driving_routes(app2, service=svc, get_current_user=lambda req: USER)
    periods_handler = app2.routes[eco_http.PERIODS_PATH]
    req = _make_request("client_code=ALPHA00001&ranking_family=driver&period_type=weekly")
    resp2 = asyncio.new_event_loop().run_until_complete(periods_handler(req))
    assert resp2.status_code == 200
    payload2 = json.loads(bytes(resp2.body))
    assert any(p["period_label"] == "2026-07-W5" for p in payload2["data"])
    print("PASS: HTTP adapter registers 6 routes and returns JSON envelopes via the session lookup")


def main() -> None:
    test_unauthenticated_returns_401()
    test_authenticated_user_resolved_via_session_only()
    test_effective_access_union_pure()
    test_ranking_flag_grants_ranking_endpoints()
    test_group_grant_grants_access()
    test_missing_access_returns_403_and_does_not_disclose()
    test_ranking_access_alone_does_not_grant_trip_details()
    test_trip_flag_grants_trip_details()
    test_routes_flag_does_not_expose_route_data()
    test_admin_does_not_bypass_client_grants()
    test_providers_list_only_authorized()
    test_providers_list_unauthorized_omitted()
    test_periods_weekly_w5_and_exclusive_end()
    test_periods_monthly_empty_returns_200_empty_list()
    test_periods_malformed_type_422()
    test_ranking_groups_supported()
    test_persisted_group_not_replaced_by_current_chart()
    test_missing_chart_row_keeps_entry()
    test_ranking_pagination_meta()
    test_ranking_invalid_group_and_sort_and_limit()
    test_assigned_id_opaque_and_bound()
    test_assigned_id_not_in_path()
    test_trip_details_safe_fields_only()
    test_reconciliation_match()
    test_reconciliation_mismatch_lists_fields()
    test_reconciliation_unavailable_maps_409()
    test_isolation_permission_scoped_per_client()
    test_isolation_provider_always_receives_trusted_client_id()
    test_isolation_provider_not_found_maps_404()
    test_environment_mismatch_maps_503()
    test_audit_events_created_and_sanitized()
    test_failed_authorization_is_not_audited()
    test_envelope_shape()
    test_http_adapter_registers_routes_and_returns_json_401()
    print("OK - Eco Driving Explorer read API tests passed")


if __name__ == "__main__":
    main()
