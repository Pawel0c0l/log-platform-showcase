"""Effective Eco Driving portal access model.

Mirrors the existing additive portal access model (``portal_user_clients`` OR
active ``portal_group_clients``). The union is purely additive: a capability is
effective when it is TRUE on the direct grant OR on any active-group grant. A
missing direct row and no matching group row means no access. One ``FALSE``
source never overrides another source's ``TRUE``.

Administrators do NOT bypass client grants here: this matches the existing
portal convention where ``artifact_users.is_admin`` only unlocks the ``/admin``
surface and still requires direct/group grants for client data.

This module has no I/O; the SQL-backed lookup lives in ``backend.py`` and simply
feeds ``merge_eco_access`` two boolean maps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Canonical order of the three Eco Driving capability flags.
ECO_ACCESS_FLAGS: tuple[str, ...] = (
    "can_view_eco_ranking",
    "can_view_eco_trip_details",
    "can_view_eco_trip_routes",
)


@dataclass(frozen=True)
class EcoDrivingClientAccess:
    """Typed effective access for one user + client_code.

    ``sources`` records which grant sources contributed (``"direct"`` and/or
    ``"group"``). It is intended for diagnostics/audit only and must not be
    exposed to ordinary API callers as internal IDs; it never contains IDs.
    """

    client_code: str
    can_view_eco_ranking: bool
    can_view_eco_trip_details: bool
    can_view_eco_trip_routes: bool
    client_is_active: bool
    sources: tuple[str, ...] = ()

    @property
    def any_access(self) -> bool:
        return (
            self.can_view_eco_ranking
            or self.can_view_eco_trip_details
            or self.can_view_eco_trip_routes
        )

    def has(self, flag: str) -> bool:
        if flag not in ECO_ACCESS_FLAGS:
            raise KeyError(flag)
        return bool(getattr(self, flag))


def _flag(source: Mapping[str, object] | None, name: str) -> bool:
    if not source:
        return False
    return bool(source.get(name))


def merge_eco_access(
    client_code: str,
    *,
    direct: Mapping[str, object] | None,
    group: Mapping[str, object] | None,
    client_is_active: bool,
) -> EcoDrivingClientAccess:
    """Return the additive union of direct and active-group Eco flags."""

    direct = direct or {}
    group = group or {}
    flags = {name: (_flag(direct, name) or _flag(group, name)) for name in ECO_ACCESS_FLAGS}

    sources: list[str] = []
    if any(_flag(direct, name) for name in ECO_ACCESS_FLAGS):
        sources.append("direct")
    if any(_flag(group, name) for name in ECO_ACCESS_FLAGS):
        sources.append("group")

    return EcoDrivingClientAccess(
        client_code=str(client_code or ""),
        can_view_eco_ranking=flags["can_view_eco_ranking"],
        can_view_eco_trip_details=flags["can_view_eco_trip_details"],
        can_view_eco_trip_routes=flags["can_view_eco_trip_routes"],
        client_is_active=bool(client_is_active),
        sources=tuple(sources),
    )


def no_eco_access(client_code: str) -> EcoDrivingClientAccess:
    return EcoDrivingClientAccess(
        client_code=str(client_code or ""),
        can_view_eco_ranking=False,
        can_view_eco_trip_details=False,
        can_view_eco_trip_routes=False,
        client_is_active=False,
        sources=(),
    )
