"""Thin FastAPI adapters for Eco Driving permission administration."""

from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import parse_qsl, quote

from fastapi import Request

from .admin_models import (
    EcoAdminError,
    EcoAdminRequestTooLarge,
    EcoAdminValidationError,
)
from .admin_pages import AdminPageResult, EcoDrivingPermissionAdminPages
from .admin_service import (
    MAX_ADMIN_FORM_BODY_BYTES,
    MAX_ADMIN_FORM_FIELDS,
)


LOGGER = logging.getLogger(__name__)


def _request_ip(request: Request) -> str | None:
    return str(request.client.host) if request.client and request.client.host else None


def _request_user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return str(value)[:500] if value else None


async def _bounded_form_pairs(request: Request) -> list[tuple[str, str]]:
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise EcoAdminValidationError("The permission form encoding is invalid.")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise EcoAdminValidationError("The permission form length is invalid.") from exc
        if declared_length < 0:
            raise EcoAdminValidationError("The permission form length is invalid.")
        if declared_length > MAX_ADMIN_FORM_BODY_BYTES:
            raise EcoAdminRequestTooLarge()

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_ADMIN_FORM_BODY_BYTES:
            raise EcoAdminRequestTooLarge()
    if body and body.count(b"&") + 1 > MAX_ADMIN_FORM_FIELDS:
        raise EcoAdminRequestTooLarge()
    try:
        decoded = bytes(body).decode("utf-8", errors="strict")
        return parse_qsl(decoded, keep_blank_values=True, max_num_fields=MAX_ADMIN_FORM_FIELDS)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EcoAdminValidationError("The permission form is malformed.") from exc


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError) as exc:
        raise ValueError from exc
    if parsed < 1:
        raise ValueError
    return parsed


def register_eco_driving_admin_routes(
    app: Any,
    *,
    pages: EcoDrivingPermissionAdminPages,
    require_admin: Callable[[Request], Any],
    render: Callable[[AdminPageResult, dict], Any],
    redirect: Callable[[str], Any],
) -> None:
    def authorized(request: Request) -> tuple[dict | None, Any | None]:
        user = require_admin(request)
        if not isinstance(user, dict):
            return None, user
        return user, None

    @app.get("/admin/client-access/eco-driving", name="admin_eco_driving_permissions")
    def landing(request: Request):
        user, denied = authorized(request)
        if denied is not None:
            return denied
        try:
            result = pages.landing(
                page=_positive_int(request.query_params.get("page"), 1),
                limit=_positive_int(request.query_params.get("limit"), 50),
            )
        except (ValueError, EcoAdminError) as exc:
            message = getattr(exc, "safe_message", "Invalid admin pagination.")
            result = pages.error_page("Invalid request", message, getattr(exc, "status_code", 422))
        except Exception as exc:
            LOGGER.error("Eco permission admin landing failed: %s", type(exc).__name__)
            result = pages.error_page("Eco Driving permissions unavailable", "Eco Driving permission administration is currently unavailable.", 500)
        return render(result, user)

    @app.get("/admin/client-access/eco-driving/users/{user_id}", name="admin_eco_driving_user_permissions")
    def user_editor(user_id: str, request: Request):
        return _get_editor("user", user_id, request)

    @app.get("/admin/client-access/eco-driving/groups/{group_id}", name="admin_eco_driving_group_permissions")
    def group_editor(group_id: str, request: Request):
        return _get_editor("group", group_id, request)

    def _get_editor(subject_type: str, subject_id: str, request: Request):
        user, denied = authorized(request)
        if denied is not None:
            return denied
        actor = str(user.get("user_id") or "")
        try:
            outcome = request.query_params.get("result")
            if outcome not in {"updated", "no-change"}:
                outcome = None
            result = (
                pages.user_editor(subject_id, actor_user_id=actor, outcome=outcome)
                if subject_type == "user"
                else pages.group_editor(subject_id, actor_user_id=actor, outcome=outcome)
            )
        except EcoAdminError as exc:
            result = pages.error_page("Permission editor unavailable", exc.safe_message, exc.status_code)
        except Exception as exc:
            LOGGER.error("Eco permission editor failed: %s", type(exc).__name__)
            result = pages.error_page("Permission editor unavailable", "Eco Driving permission administration is currently unavailable.", 500)
        return render(result, user)

    @app.post("/admin/client-access/eco-driving/users/{user_id}", name="admin_update_eco_driving_user_permissions")
    async def update_user(user_id: str, request: Request):
        return await _post_editor("user", user_id, request)

    @app.post("/admin/client-access/eco-driving/groups/{group_id}", name="admin_update_eco_driving_group_permissions")
    async def update_group(group_id: str, request: Request):
        return await _post_editor("group", group_id, request)

    async def _post_editor(subject_type: str, subject_id: str, request: Request):
        user, denied = authorized(request)
        if denied is not None:
            return denied
        actor = str(user.get("user_id") or "")
        try:
            pairs = await _bounded_form_pairs(request)
            kwargs = {
                "actor_user_id": actor,
                "pairs": pairs,
                "ip_address": _request_ip(request),
                "user_agent": _request_user_agent(request),
            }
            if subject_type == "user":
                update = pages.service.update_user(subject_id, **kwargs)
                subject_path = f"users/{quote(subject_id, safe='')}"
            else:
                update = pages.service.update_group(subject_id, **kwargs)
                subject_path = f"groups/{quote(subject_id, safe='')}"
            outcome = "updated" if update.changes else "no-change"
            location = f"/admin/client-access/eco-driving/{subject_path}?result={outcome}"
            return redirect(location)
        except EcoAdminError as exc:
            # Security and conflict errors do not echo the submitted form or its
            # tokens. Validation errors re-render current trusted state.
            try:
                result = (
                    pages.user_editor(subject_id, actor_user_id=actor, error=exc.safe_message, status_code=exc.status_code)
                    if subject_type == "user"
                    else pages.group_editor(subject_id, actor_user_id=actor, error=exc.safe_message, status_code=exc.status_code)
                )
            except Exception:
                result = pages.error_page("Permission update rejected", exc.safe_message, exc.status_code)
            return render(result, user)
        except Exception as exc:
            LOGGER.error("Eco permission update failed: %s", type(exc).__name__)
            return render(pages.error_page("Permission update failed", "Eco Driving permissions were not changed.", 500), user)
