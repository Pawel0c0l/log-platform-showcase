"""Eco Driving Explorer internal read-model foundation (Stage 1).

Read-only domain provider/service layer for browsing persisted Eco Driving
ranking periods, entries, and reconstructing contributing trips. This package
is intentionally decoupled from FastAPI routes, HTML rendering, exports, RBAC,
and lineage persistence — those belong to later stages.

Reconstructed trip membership is always classified
``RECONSTRUCTED_CURRENT_STATE``; this package must never claim immutable
historical lineage.
"""

from __future__ import annotations

from .errors import (
    EcoDrivingExplorerError,
    EnvironmentClientMismatchError,
    InvalidPaginationError,
    InvalidRankingGroupError,
    InvalidSortFieldError,
    PeriodNotFoundError,
    ProviderNotFoundError,
    RankingEntryNotFoundError,
    ReconstructionUnavailableError,
    UnsupportedPeriodTypeError,
)
from .models import (
    DriverMetadataSource,
    LineageQuality,
    Page,
    PeriodType,
    ProviderIdentity,
    RankingEntry,
    RankingGroup,
    RankingPeriod,
    RankingPeriodKey,
    ReconciliationField,
    ReconciliationResult,
    ReconciliationStatus,
    ScoreDefinition,
    SortDirection,
    TripContribution,
    TripRow,
)
from .provider import (
    EcoDrivingExplorerProvider,
    classify_trip_contribution,
)
from .queries import ClientDatabaseReader, RowReader
from .registry import (
    available_provider_identities,
    get_provider,
    is_registered,
)
from .access import (
    ECO_ACCESS_FLAGS,
    EcoDrivingClientAccess,
    merge_eco_access,
    no_eco_access,
)
from .backend import (
    PortalEcoDrivingBackend,
    ResolvedEcoClient,
    assigned_id_digest,
)
from .service import ApiResult, EcoDrivingApiService

# NOTE: ``api.eco_driving_explorer.http`` (FastAPI adapter) is intentionally not
# imported here so this package stays importable without FastAPI. Import
# ``register_eco_driving_routes`` from that submodule directly at wiring time.

__all__ = [
    "EcoDrivingExplorerError",
    "EnvironmentClientMismatchError",
    "InvalidPaginationError",
    "InvalidRankingGroupError",
    "InvalidSortFieldError",
    "PeriodNotFoundError",
    "ProviderNotFoundError",
    "RankingEntryNotFoundError",
    "ReconstructionUnavailableError",
    "UnsupportedPeriodTypeError",
    "DriverMetadataSource",
    "LineageQuality",
    "Page",
    "PeriodType",
    "ProviderIdentity",
    "RankingEntry",
    "RankingGroup",
    "RankingPeriod",
    "RankingPeriodKey",
    "ReconciliationField",
    "ReconciliationResult",
    "ReconciliationStatus",
    "ScoreDefinition",
    "SortDirection",
    "TripContribution",
    "TripRow",
    "EcoDrivingExplorerProvider",
    "classify_trip_contribution",
    "ClientDatabaseReader",
    "RowReader",
    "available_provider_identities",
    "get_provider",
    "is_registered",
    "ECO_ACCESS_FLAGS",
    "EcoDrivingClientAccess",
    "merge_eco_access",
    "no_eco_access",
    "PortalEcoDrivingBackend",
    "ResolvedEcoClient",
    "assigned_id_digest",
    "ApiResult",
    "EcoDrivingApiService",
]
