"""FastAPI adapter for the server-rendered Eco Driving Explorer pages (Stages 3-5).

Thin registration layer, mirroring ``http.py`` for the JSON API. It attaches the
four portal page routes to the shared app, extracts query parameters, resolves the
portal user via an injected callback (reusing the existing session convention),
delegates to the framework-independent :class:`EcoDrivingPages` controller, and
renders the result through an injected ``render`` callback (which applies the
shared ``_portal_layout`` chrome). No provider/access/query logic lives here.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Request

from .pages import EcoDrivingPages, PageResult, _ExportFile

LANDING_PAGE_PATH = "/user/eco-driving"
RANKINGS_PAGE_PATH = "/user/eco-driving/rankings"
RANKING_ENTRY_PAGE_PATH = "/user/eco-driving/ranking-entry"
RANKING_ENTRY_TRIPS_PAGE_PATH = "/user/eco-driving/ranking-entry/trips"
RANKING_ENTRY_TRIPS_EXPORT_PATH = "/user/eco-driving/ranking-entry/trips/export"
RANKINGS_EXPORT_PATH = "/user/eco-driving/rankings/export"
PERIODS_EXPORT_PATH = "/user/eco-driving/periods/export"
RANKING_ENTRY_EXPORT_PATH = "/user/eco-driving/ranking-entry/export"


def _q(request: Request, name: str) -> Optional[str]:
    return request.query_params.get(name)



_FILENAME_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def _export_filename(scope, extension: str, *, prefix: str = "przejazdy") -> str:
    """An ASCII filename built from the RESOLVED SCOPE, never from the query.

    Taking the request was the defect. The contents resolve `month` over
    `period_key`, and a filename rebuilt from the raw query had no way to know
    that: given both, it emitted both and led with a period key naming a period
    the file contained nothing from. It now receives what the controller
    actually resolved, so the name cannot describe a different scope than the
    bytes.

    Content-Disposition and non-ASCII are a long-standing mess across browsers,
    and the parts here (a numeric assignment id, a period token, a month) are
    ASCII anyway. Anything outside the safe set is dropped rather than escaped —
    a filename is not a place to be clever with untrusted input.

    `prefix` names WHICH table the file came from. Two Eco tables now export,
    and a directory of files all called `przejazdy_*` would make the ranking and
    the trips indistinguishable once they leave the browser.
    """

    parts = [prefix]
    for name, raw in scope or ():
        raw = "" if raw is None else str(raw)
        if name == "weeks":
            # `1,2` must not sanitise to `12`, which names a different single
            # week. The separator becomes a safe one rather than being dropped,
            # so two weeks stay two weeks in the filename.
            raw = raw.replace(",", "-")
            if raw:
                raw = f"w{raw}"
        clean = "".join(c for c in raw if c in _FILENAME_SAFE)[:40]
        if clean:
            parts.append(clean)
    return "_".join(parts) + "." + extension


def _download(export: "_ExportFile", filename: str):
    """The bytes, as a download rather than something the browser renders."""

    from fastapi import Response

    return Response(
        content=export.body,
        media_type=export.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The view is filtered and permission-scoped; a shared cache holding
            # it would serve one client's rows to the next request.
            "Cache-Control": "no-store",
        },
    )

def register_eco_driving_page_routes(
    app: Any,
    *,
    pages: EcoDrivingPages,
    require_user: Callable[[Request], Any],
    render: Callable[[PageResult, dict], Any],
) -> None:
    """Attach the Eco Driving portal page routes to an existing FastAPI app.

    Registration is skipped when the app object does not support
    ``add_api_route`` (e.g. import-only unit tests injecting a minimal stub app).
    ``require_user`` returns either a user dict or a portal Response (redirect /
    forbidden); anything that is not a ``dict`` is returned to the client as-is,
    so the existing unauthenticated-redirect convention is preserved.
    """

    if not hasattr(app, "add_api_route"):
        return

    async def eco_driving_landing(request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.landing(
            user=user,
            request=request,
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_type=_q(request, "period_type"),
            year=_q(request, "year"),
            month=_q(request, "month"),
            unit=_q(request, "unit"),
        )
        return render(result, user)

    async def eco_driving_rankings(request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.rankings(
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
            unit=_q(request, "unit"),
            search=_q(request, "search"),
            month=_q(request, "month"),
            weeks=_q(request, "weeks"),
        )
        return render(result, user)

    async def eco_driving_ranking_entry(request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.ranking_entry(
            user=user, request=request,
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_key=_q(request, "period_key"),
            assigned_id=_q(request, "assigned_id"),
            ranking_group=_q(request, "ranking_group"),
            page=_q(request, "page"), limit=_q(request, "limit"),
            sort=_q(request, "sort"), direction=_q(request, "direction"),
            unit=_q(request, "unit"),
            month=_q(request, "month"),
            weeks=_q(request, "weeks"),
        )
        return render(result, user)

    async def eco_driving_ranking_entry_trips(request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.ranking_entry_trips(
            user=user, request=request,
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_key=_q(request, "period_key"),
            assigned_id=_q(request, "assigned_id"),
            page=_q(request, "page"), limit=_q(request, "limit"),
            sort=_q(request, "sort"), direction=_q(request, "direction"),
            trip_start_from=_q(request, "trip_start_from"),
            trip_start_to=_q(request, "trip_start_to"),
            provider_trip_id=_q(request, "provider_trip_id"),
            min_distance_meters=_q(request, "min_distance_meters"),
            max_distance_meters=_q(request, "max_distance_meters"),
            has_scoring_events=_q(request, "has_scoring_events"),
            ranking_group=_q(request, "ranking_group"),
            ranking_page=_q(request, "ranking_page"),
            ranking_limit=_q(request, "ranking_limit"),
            ranking_sort=_q(request, "ranking_sort"),
            ranking_direction=_q(request, "ranking_direction"),
            unit=_q(request, "unit"),
            month=_q(request, "month"),
            weeks=_q(request, "weeks"),
        )
        return render(result, user)


    async def eco_driving_periods_export(request: Request):
        """The landing page's period index, as a file."""
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.periods_export(
            user=user, request=request,
            export_format=(_q(request, "format") or "xlsx"),
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_type=_q(request, "period_type"),
            year=_q(request, "year"), month=_q(request, "month"),
        )
        if isinstance(result, _ExportFile):
            return _download(result, _export_filename(result.scope, result.extension, prefix="okresy"))
        return render(result, user)


    async def eco_driving_ranking_entry_export(request: Request):
        """One table of the driver-detail page, named by `table`.

        `unit` is deliberately absent: the detail tables print both readings of
        every metric side by side, and the export carries both as columns. There
        is nothing for a unit to select.
        """
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.ranking_entry_export(
            user=user, request=request,
            export_format=(_q(request, "format") or "xlsx"),
            table=_q(request, "table"),
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_key=_q(request, "period_key"),
            assigned_id=_q(request, "assigned_id"),
            month=_q(request, "month"), weeks=_q(request, "weeks"),
        )
        if isinstance(result, _ExportFile):
            return _download(result, _export_filename(result.scope, result.extension, prefix="kierowca"))
        return render(result, user)


    async def eco_driving_rankings_export(request: Request):
        """The ranking table, as a file.

        It reads the parameters the RANKING page reads — including `unit`,
        which the trips export excludes. There the unit shapes the screen a
        person came from; here it decides what every metric column contains, so
        excluding it would silently export one reading of the table while the
        other was on screen.

        `page`/`limit` stay out by intent: an export is the whole filtered view.
        """
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.rankings_export(
            user=user, request=request,
            export_format=(_q(request, "format") or "xlsx"),
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_key=_q(request, "period_key"),
            ranking_group=_q(request, "ranking_group"),
            sort=_q(request, "sort"), direction=_q(request, "direction"),
            unit=_q(request, "unit"), search=_q(request, "search"),
            month=_q(request, "month"), weeks=_q(request, "weeks"),
        )
        if isinstance(result, _ExportFile):
            return _download(result, _export_filename(result.scope, result.extension, prefix="ranking"))
        return render(result, user)


    async def eco_driving_ranking_entry_trips_export(request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        # The filters the trips query takes, PLUS `month`/`weeks`.
        #
        # Those two were previously refused here, on the reasoning that the
        # ranking-sidebar parameters shape the page a person came from rather
        # than the rows. That is true of `ranking_*` and `unit`, and it is false
        # of `month`/`weeks`: on the ranking-basis page they ARE the row set --
        # they are what its table was fetched with. Excluding them left that
        # page's export with no scope to send at all, so its buttons downloaded
        # nothing and the endpoint refused them.
        #
        # `ranking_*` and `unit` stay out, for the original and still-correct
        # reason. `page`/`limit` stay out by intent: an export is the whole
        # filtered view.
        result = pages.ranking_entry_trips_export(
            user=user, request=request,
            export_format=(_q(request, "format") or "xlsx"),
            month=_q(request, "month"), weeks=_q(request, "weeks"),
            client_code=_q(request, "client_code"),
            ranking_family=_q(request, "ranking_family"),
            period_key=_q(request, "period_key"),
            assigned_id=_q(request, "assigned_id"),
            sort=_q(request, "sort"), direction=_q(request, "direction"),
            trip_start_from=_q(request, "trip_start_from"),
            trip_start_to=_q(request, "trip_start_to"),
            provider_trip_id=_q(request, "provider_trip_id"),
            min_distance_meters=_q(request, "min_distance_meters"),
            max_distance_meters=_q(request, "max_distance_meters"),
            has_scoring_events=_q(request, "has_scoring_events"),
        )
        if isinstance(result, _ExportFile):
            return _download(result, _export_filename(result.scope, result.extension))
        # A refusal or the too-many-rows page: ordinary chrome, so the person sees
        # WHY in the interface they clicked from rather than as a raw body.
        return render(result, user)

    app.add_api_route(LANDING_PAGE_PATH, eco_driving_landing, methods=["GET"])
    app.add_api_route(RANKINGS_PAGE_PATH, eco_driving_rankings, methods=["GET"])
    app.add_api_route(RANKING_ENTRY_PAGE_PATH, eco_driving_ranking_entry, methods=["GET"])
    app.add_api_route(RANKING_ENTRY_TRIPS_PAGE_PATH, eco_driving_ranking_entry_trips, methods=["GET"])
    app.add_api_route(RANKING_ENTRY_TRIPS_EXPORT_PATH, eco_driving_ranking_entry_trips_export, methods=["GET"])
    app.add_api_route(RANKINGS_EXPORT_PATH, eco_driving_rankings_export, methods=["GET"])
    app.add_api_route(PERIODS_EXPORT_PATH, eco_driving_periods_export, methods=["GET"])
    app.add_api_route(RANKING_ENTRY_EXPORT_PATH, eco_driving_ranking_entry_export, methods=["GET"])
