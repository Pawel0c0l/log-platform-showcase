"""Typed models and validation for Eco Driving permission administration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .access import ECO_ACCESS_FLAGS


@dataclass(frozen=True)
class EcoPermissionState:
    can_view_eco_ranking: bool = False
    can_view_eco_trip_details: bool = False
    can_view_eco_trip_routes: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in ECO_ACCESS_FLAGS}

    @property
    def any(self) -> bool:
        return any(self.as_dict().values())


@dataclass(frozen=True)
class NormalizedEcoPermissions:
    requested: EcoPermissionState
    value: EcoPermissionState
    changed: bool
    revoke_cascade: bool = False


def normalize_eco_permissions(
    can_view_eco_ranking: bool,
    can_view_eco_trip_details: bool,
    can_view_eco_trip_routes: bool,
) -> NormalizedEcoPermissions:
    """Normalize the dependency chain routes -> details -> ranking.

    Checked child capabilities grant their prerequisites. Unchecking a parent
    while leaving no checked child revokes all dependent capabilities.
    """

    requested = EcoPermissionState(
        bool(can_view_eco_ranking),
        bool(can_view_eco_trip_details),
        bool(can_view_eco_trip_routes),
    )
    routes = requested.can_view_eco_trip_routes
    details = requested.can_view_eco_trip_details or routes
    ranking = requested.can_view_eco_ranking or details
    value = EcoPermissionState(ranking, details, routes)
    return NormalizedEcoPermissions(requested, value, requested != value)


def normalize_eco_permission_transition(
    before: EcoPermissionState,
    submitted: EcoPermissionState,
) -> NormalizedEcoPermissions:
    """Normalize one edit while preserving explicit parent-revoke intent."""

    if before.can_view_eco_ranking and not submitted.can_view_eco_ranking:
        value = EcoPermissionState()
        return NormalizedEcoPermissions(submitted, value, submitted != value, True)
    if (
        before.can_view_eco_trip_details
        and submitted.can_view_eco_ranking
        and not submitted.can_view_eco_trip_details
    ):
        value = EcoPermissionState(True, False, False)
        return NormalizedEcoPermissions(submitted, value, submitted != value, True)
    promoted = normalize_eco_permissions(**submitted.as_dict())
    return NormalizedEcoPermissions(submitted, promoted.value, promoted.changed, False)


def permission_state(row: Mapping[str, object] | None) -> EcoPermissionState:
    row = row or {}
    return EcoPermissionState(*(bool(row.get(name)) for name in ECO_ACCESS_FLAGS))


@dataclass(frozen=True)
class ProviderAdminInfo:
    ranking_family: str
    display_name: str


@dataclass(frozen=True)
class InheritedEcoPermissions:
    value: EcoPermissionState
    ranking_groups: tuple[str, ...] = ()
    detail_groups: tuple[str, ...] = ()
    route_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class EcoClientPermissionRow:
    client_code: str
    display_name: str
    direct: EcoPermissionState
    inherited: InheritedEcoPermissions
    effective: EcoPermissionState
    providers: tuple[ProviderAdminInfo, ...] = ()


@dataclass(frozen=True)
class EcoPermissionEditor:
    subject_type: str
    subject_id: str
    subject_name: str
    subject_active: bool
    clients: tuple[EcoClientPermissionRow, ...]
    version_token: str
    active_member_count: int = 0


@dataclass(frozen=True)
class EcoPermissionChange:
    client_code: str
    before: EcoPermissionState
    submitted: EcoPermissionState
    after: EcoPermissionState
    dependency_normalized: bool
    revoke_cascade: bool


@dataclass(frozen=True)
class EcoPermissionUpdateResult:
    subject_type: str
    subject_id: str
    submitted_count: int
    changes: tuple[EcoPermissionChange, ...]
    dependency_normalized: bool
    revoke_cascade: bool
    affected_active_member_count: int = 0


class EcoAdminError(Exception):
    status_code = 500
    safe_message = "Eco Driving permissions are currently unavailable."


class EcoAdminNotFound(EcoAdminError):
    status_code = 404
    safe_message = "The requested user or group is no longer available."


class EcoAdminValidationError(EcoAdminError):
    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.safe_message = message


class EcoAdminConflict(EcoAdminError):
    status_code = 409
    safe_message = "Permissions changed since this page was opened. Reload and review the current state."


class EcoAdminRequestTooLarge(EcoAdminError):
    status_code = 413
    safe_message = "The submitted permission form is too large."


class EcoAdminCsrfError(EcoAdminError):
    status_code = 403
    safe_message = "The security token is missing, invalid, or expired. Reload the page and try again."
