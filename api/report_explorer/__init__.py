"""Report Explorer (`S15`): the client-facing library of reports the platform
generates, and the read path, publication boundary and pages that serve it.

Layering, outermost last:

* `models` — the normalized, provider-independent domain the pages render;
* `periods` — declared reporting-period vocabulary and its presentation;
* `errors` — the failure vocabulary, every member of which is a renderable state;
* `access` — the one authorization gate (`docs/40` §9);
* `store` — the read path over the three generated-report relations;
* `exports_adapter` — the read-only `DB-53` provider over `database_export_jobs`;
* `publication` — the write boundary a generator publishes through;
* `service` — authorization first, then the two providers, then one library;
* `html` / `pages` / `page_routes` — presentation and the FastAPI adapter.

Nothing in this package imports `api.main`; the integration layer injects the
portal's canonical helpers (effective client access, the Database Explorer
dataset gate, artifact delivery) so there is exactly one implementation of each.
"""
from __future__ import annotations

from . import access, errors, exports_adapter, html, models, periods, publication, store
from .errors import (
    EmptyPublicationError,
    ReportAccessDeniedError,
    ReportClientNotFoundError,
    ReportDefinitionNotFoundError,
    ReportExplorerError,
    ReportFileNotFoundError,
    ReportFileUnavailableError,
    ReportInstanceNotFoundError,
    ReportPublicationError,
    ReportSchemaUnavailableError,
    StaleAttemptError,
)
from .html import REPORT_EXPLORER_PAGE_ASSETS
from .models import (
    PROVIDER_DATABASE_EXPORT,
    PROVIDER_GENERATED,
    ROLE_DETAILED,
    ROLE_MAIN,
    ROLE_RAW,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_READY,
    LibraryQuery,
)
from .pages import PageResult, ReportExplorerPages
from .page_routes import register_report_explorer_page_routes
from .periods import DEFAULT_PERIOD_TIMEZONE, ReportingPeriod, canonical_period
from .publication import (
    DefinitionSpec,
    GenerationAttempt,
    PublishedFile,
    ReportPublicationService,
    SourceSnapshot,
    generated_report_schema_available,
)
from .service import ReportExplorerService

__all__ = [
    "DEFAULT_PERIOD_TIMEZONE",
    "DefinitionSpec",
    "EmptyPublicationError",
    "GenerationAttempt",
    "LibraryQuery",
    "PROVIDER_DATABASE_EXPORT",
    "PROVIDER_GENERATED",
    "PageResult",
    "PublishedFile",
    "REPORT_EXPLORER_PAGE_ASSETS",
    "ROLE_DETAILED",
    "ROLE_MAIN",
    "ROLE_RAW",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_GENERATING",
    "STATUS_READY",
    "ReportAccessDeniedError",
    "ReportClientNotFoundError",
    "ReportDefinitionNotFoundError",
    "ReportExplorerError",
    "ReportExplorerPages",
    "ReportExplorerService",
    "ReportFileNotFoundError",
    "ReportFileUnavailableError",
    "ReportInstanceNotFoundError",
    "ReportPublicationError",
    "ReportPublicationService",
    "ReportSchemaUnavailableError",
    "ReportingPeriod",
    "SourceSnapshot",
    "StaleAttemptError",
    "access",
    "canonical_period",
    "errors",
    "exports_adapter",
    "generated_report_schema_available",
    "html",
    "models",
    "periods",
    "publication",
    "register_report_explorer_page_routes",
    "store",
]
