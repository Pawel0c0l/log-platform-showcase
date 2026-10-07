"""FastAPI adapter for the Eco Driving Explorer read API.

Thin integration layer only: it extracts query parameters from the request,
resolves the current portal user with the injected session lookup, delegates to
``EcoDrivingApiService``, and turns the returned ``ApiResult`` into a JSON
response. No Eco Driving business logic lives here, and this module never
imports ``api.main`` (the wiring is one-directional: ``api.main`` imports and
calls :func:`register_eco_driving_routes`).

All query parameters are read as optional strings and validated inside the
service so every endpoint returns the same JSON envelope for authentication,
authorization, validation, and domain errors.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Request

from .service import ApiResult, EcoDrivingApiService

# Route paths kept clearly separate from portal HTML routes.
PROVIDERS_PATH = "/user/eco-driving/api/providers"
PERIODS_PATH = "/user/eco-driving/api/periods"
RANKING_ENTRIES_PATH = "/user/eco-driving/api/ranking-entries"
RANKING_ENTRY_PATH = "/user/eco-driving/api/ranking-entry"
RANKING_ENTRY_TRIPS_PATH = "/user/eco-driving/api/ranking-entry/trips"
RANKING_ENTRY_RECONCILIATION_PATH = "/user/eco-driving/api/ranking-entry/reconciliation"


def _q(request: Request, name: str) -> Optional[str]:
    return request.query_params.get(name)


def register_eco_driving_routes(
    app: Any,
    *,
    service: EcoDrivingApiService,
    get_current_user: Callable[[Request], Optional[dict]],
) -> None:
    """Attach the Eco Driving read API routes to an existing FastAPI app.

    ``JSONResponse`` is imported lazily and registration is skipped when the app
    object does not support ``add_api_route`` (e.g. import-only portal unit tests
    that inject a minimal stub app / stubbed FastAPI without ``JSONResponse``).
    """

    if not hasattr(app, "add_api_route"):
        return

    from fastapi.responses import JSONResponse

    def _json(result: ApiResult):
        return JSONResponse(content=result.body, status_code=result.status_code)

    async def providers(request: Request):
        user = get_current_user(request)
        return _json(service.list_providers(user=user, request=request))

    async def periods(request: Request):
        user = get_current_user(request)
        return _json(
            service.list_periods(
                user=user,
                request=request,
                client_code=_q(request, "client_code"),
                ranking_family=_q(request, "ranking_family"),
                period_type=_q(request, "period_type"),
                year=_q(request, "year"),
                month=_q(request, "month"),
            )
        )

    async def ranking_entries(request: Request):
        user = get_current_user(request)
        return _json(
            service.list_ranking_entries(
                user=user,
                request=request,
                client_code=_q(request, "client_code"),
                ranking_family=_q(request, "ranking_family"),
                period_key=_q(request, "period_key"),
                ranking_group=_q(request, "ranking_group"),
                page=_q(request, "page"),
                limit=_q(request, "limit"),
                sort=_q(request, "sort"),
                direction=_q(request, "direction"),
            )
        )

    async def ranking_entry(request: Request):
        user = get_current_user(request)
        return _json(
            service.get_ranking_entry(
                user=user,
                request=request,
                client_code=_q(request, "client_code"),
                ranking_family=_q(request, "ranking_family"),
                period_key=_q(request, "period_key"),
                assigned_id=_q(request, "assigned_id"),
            )
        )

    async def ranking_entry_trips(request: Request):
        user = get_current_user(request)
        return _json(
            service.list_contributing_trips(
                user=user,
                request=request,
                client_code=_q(request, "client_code"),
                ranking_family=_q(request, "ranking_family"),
                period_key=_q(request, "period_key"),
                assigned_id=_q(request, "assigned_id"),
                page=_q(request, "page"),
                limit=_q(request, "limit"),
                sort=_q(request, "sort"),
                direction=_q(request, "direction"),
                trip_start_from=_q(request, "trip_start_from"),
                trip_start_to=_q(request, "trip_start_to"),
                provider_trip_id=_q(request, "provider_trip_id"),
                min_distance_meters=_q(request, "min_distance_meters"),
                max_distance_meters=_q(request, "max_distance_meters"),
                has_scoring_events=_q(request, "has_scoring_events"),
            )
        )

    async def ranking_entry_reconciliation(request: Request):
        user = get_current_user(request)
        return _json(
            service.reconcile_ranking_entry(
                user=user,
                request=request,
                client_code=_q(request, "client_code"),
                ranking_family=_q(request, "ranking_family"),
                period_key=_q(request, "period_key"),
                assigned_id=_q(request, "assigned_id"),
            )
        )

    routes = (
        (PROVIDERS_PATH, providers, "eco_driving_api_providers"),
        (PERIODS_PATH, periods, "eco_driving_api_periods"),
        (RANKING_ENTRIES_PATH, ranking_entries, "eco_driving_api_ranking_entries"),
        (RANKING_ENTRY_PATH, ranking_entry, "eco_driving_api_ranking_entry"),
        (RANKING_ENTRY_TRIPS_PATH, ranking_entry_trips, "eco_driving_api_ranking_entry_trips"),
        (
            RANKING_ENTRY_RECONCILIATION_PATH,
            ranking_entry_reconciliation,
            "eco_driving_api_ranking_entry_reconciliation",
        ),
    )
    for path, handler, name in routes:
        app.add_api_route(path, handler, methods=["GET"], name=name, include_in_schema=False)
