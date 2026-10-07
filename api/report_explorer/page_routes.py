"""FastAPI adapter for the Report Explorer pages.

Thin registration layer, mirroring the Eco Driving adapter. It attaches the
detail and file-delivery routes to the shared app, resolves the portal user
through an injected callback (reusing the existing session convention),
delegates to the framework-independent :class:`ReportExplorerPages` controller,
and renders or streams through injected callbacks. No access, query or storage
logic lives here.

The library route itself is **not** registered here: `/user/reports` is an
existing portal path and is rewired in `api/main.py` at its original definition,
so there is never a moment where two handlers answer the `Raporty` nav slot.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Request

from .pages import PageResult, ReportExplorerPages

INSTANCE_PAGE_PATH = "/user/reports/instances/{instance_ref}"
FILE_PREVIEW_PATH = "/user/reports/instances/{instance_ref}/files/{member_ref}/preview"
FILE_DOWNLOAD_PATH = "/user/reports/instances/{instance_ref}/files/{member_ref}/download"
FILE_DOWNLOAD_ALL_PATH = "/user/reports/instances/{instance_ref}/files/download-all"


def _query_params(request) -> dict:
    """The request's query parameters, or an empty mapping.

    Every S15 parameter is optional — the library and the detail page render
    their default state without one — so a request object that carries none is
    not an error condition.
    """
    return getattr(request, "query_params", None) or {}


def register_report_explorer_page_routes(
    app: Any,
    *,
    pages: ReportExplorerPages,
    require_user: Callable[[Request], Any],
    render: Callable[[PageResult, dict], Any],
    serve_file: Callable[..., Any],
    serve_archive: Callable[..., Any],
) -> None:
    """Attach the Report Explorer detail and delivery routes to a FastAPI app.

    Registration is skipped when the app object does not support
    ``add_api_route`` (import-only unit tests injecting a minimal stub app).
    ``require_user`` returns either a user dict or a portal Response; anything
    that is not a ``dict`` is returned to the client unchanged, preserving the
    established unauthenticated-redirect convention.
    """

    if not hasattr(app, "add_api_route"):
        return

    async def report_instance_page(instance_ref: str, request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        result = pages.detail(
            user=user, instance_ref=instance_ref, params=_query_params(request)
        )
        return render(result, user)

    async def report_file_preview(instance_ref: str, member_ref: str, request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        resolved, denied = pages.resolve_file(
            user=user,
            instance_ref=instance_ref,
            member_ref=member_ref,
            params=_query_params(request),
            # The INLINE decision is made by the service from the stored
            # object's own content type, independently of the detail page's
            # rendering choices. A direct request to this route therefore gets
            # the same answer the page would have, not a weaker one.
            for_preview=True,
        )
        if denied is not None:
            return render(denied, user)
        # `RP-11`/`RP-12`: the preview is embedded in the detail page, so the
        # bytes are served inline into that frame rather than as a download.
        return serve_file(resolved, disposition="inline", user=user, request=request)

    async def report_file_download(instance_ref: str, member_ref: str, request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        resolved, denied = pages.resolve_file(
            user=user,
            instance_ref=instance_ref,
            member_ref=member_ref,
            params=_query_params(request),
        )
        if denied is not None:
            return render(denied, user)
        return serve_file(resolved, disposition="attachment", user=user, request=request)

    async def report_files_download_all(instance_ref: str, request: Request):
        user = require_user(request)
        if not isinstance(user, dict):
            return user
        instance, resolved, denied = pages.resolve_all_files(
            user=user, instance_ref=instance_ref, params=_query_params(request)
        )
        if denied is not None:
            return render(denied, user)
        return serve_archive(instance, resolved, user=user, request=request)

    app.add_api_route(FILE_DOWNLOAD_ALL_PATH, report_files_download_all, methods=["GET"])
    app.add_api_route(FILE_PREVIEW_PATH, report_file_preview, methods=["GET"])
    app.add_api_route(FILE_DOWNLOAD_PATH, report_file_download, methods=["GET"])
    app.add_api_route(INSTANCE_PAGE_PATH, report_instance_page, methods=["GET"])
