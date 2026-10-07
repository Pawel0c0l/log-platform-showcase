"""Controlled provider registry for the Eco Driving Explorer.

Providers are resolved only from an explicit ``(client_code, ranking_family)``
allowlist. Presence of database tables never implies a provider. Unknown keys
fail closed with a typed domain error.
"""

from __future__ import annotations

from typing import Callable

from .errors import ProviderNotFoundError
from .models import PeriodType, ProviderIdentity
from .provider import EcoDrivingExplorerProvider
from .bravo_person_provider import (
    CLIENT_CODE as BRAVO_CLIENT_CODE,
    DISPLAY_NAME as BRAVO_DISPLAY_NAME,
    PROVIDER_KEY as BRAVO_PROVIDER_KEY,
    RANKING_FAMILY as BRAVO_RANKING_FAMILY,
    BravoPersonEcoDrivingProvider,
)
from .alpha_driver_provider import (
    CLIENT_CODE as ALPHA_CLIENT_CODE,
    DISPLAY_NAME as ALPHA_DISPLAY_NAME,
    PROVIDER_KEY as ALPHA_PROVIDER_KEY,
    RANKING_FAMILY as ALPHA_RANKING_FAMILY,
    AlphaDriverEcoDrivingProvider,
)

ProviderFactory = Callable[..., EcoDrivingExplorerProvider]

# The allowlist is the only way a provider is reachable. A caller supplies a
# client code and a ranking family as *lookup keys*; an unknown pair fails
# closed, and no pair can select a family whose data contract belongs to a
# different client.
_REGISTRY: dict[tuple[str, str], ProviderFactory] = {
    (ALPHA_CLIENT_CODE, ALPHA_RANKING_FAMILY): AlphaDriverEcoDrivingProvider,
    (BRAVO_CLIENT_CODE, BRAVO_RANKING_FAMILY): BravoPersonEcoDrivingProvider,
}

_IDENTITIES: dict[tuple[str, str], ProviderIdentity] = {
    (ALPHA_CLIENT_CODE, ALPHA_RANKING_FAMILY): ProviderIdentity(
        provider_key=ALPHA_PROVIDER_KEY,
        client_code=ALPHA_CLIENT_CODE,
        ranking_family=ALPHA_RANKING_FAMILY,
        display_name=ALPHA_DISPLAY_NAME,
        supported_period_types=(PeriodType.WEEKLY, PeriodType.MONTHLY),
    ),
    (BRAVO_CLIENT_CODE, BRAVO_RANKING_FAMILY): ProviderIdentity(
        provider_key=BRAVO_PROVIDER_KEY,
        client_code=BRAVO_CLIENT_CODE,
        ranking_family=BRAVO_RANKING_FAMILY,
        display_name=BRAVO_DISPLAY_NAME,
        supported_period_types=(PeriodType.WEEKLY, PeriodType.MONTHLY),
    ),
}


def is_registered(client_code: str, ranking_family: str) -> bool:
    return (client_code, ranking_family) in _REGISTRY


def get_provider(
    client_code: str,
    ranking_family: str,
    *,
    client_id: str,
    statement_timeout_ms: int = 15000,
) -> EcoDrivingExplorerProvider:
    factory = _REGISTRY.get((client_code, ranking_family))
    if factory is None:
        raise ProviderNotFoundError(
            f"no Eco Driving Explorer provider for client {client_code!r} family {ranking_family!r}"
        )
    return factory(client_id=client_id, statement_timeout_ms=statement_timeout_ms)


def available_provider_identities() -> tuple[ProviderIdentity, ...]:
    return tuple(_IDENTITIES.values())
