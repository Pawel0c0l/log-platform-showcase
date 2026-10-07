"""The normalized domain the Report Explorer read path speaks.

`docs/40` §10 defines ONE product interface with TWO providers: generated
reports, which own the three new relations, and completed Database Explorer
background exports, which are adapted read-only over `database_export_jobs` and
are never copied into generated-report storage (`DB-53`, `docs/40` §10.2).

Everything below is provider-independent on purpose. The page renders these
objects and never reaches into a provider, so a provider cannot smuggle a
storage detail — a path, a job column, an artifact id — into the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Provider identifiers. They are also the URL prefix of an instance reference,
# which is what makes one detail route able to resolve both providers without
# guessing (`docs/40` §10.1 — provider-scoped stable identity).
PROVIDER_GENERATED = "gen"
PROVIDER_DATABASE_EXPORT = "exp"

# The four approved statuses (`PBC` §3.3). Stored nowhere: every one of them is
# derived from (was it published?) x (is any file available?) — see
# `docs/40` §3.2.
STATUS_READY = "ready"
STATUS_GENERATING = "generating"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_ORDER = (STATUS_READY, STATUS_GENERATING, STATUS_FAILED, STATUS_EXPIRED)

# Semantic file roles (`PBC` §3.5). The approved Polish copy lives in `html.py`;
# these are the machine values the member row stores.
ROLE_MAIN = "main_document"
ROLE_DETAILED = "detailed_data"
ROLE_RAW = "raw_data"

CADENCE_CLASSES = ("weekly", "monthly", "quarterly", "on_demand")


@dataclass(frozen=True)
class ReportTypeView:
    """A rail entry: one report type in the context of one client."""

    type_key: str
    display_name: str
    description: str
    cadence_class: str
    cadence_detail: str
    provider: str
    instance_count: int = 0
    # `PBC` §3.1: "an attention dot when the latest instance failed". It reads
    # the PRESENTED status of the newest instance, so it fires on a genuinely
    # file-less failure and stays quiet when a retry failed over files that are
    # still downloadable (`docs/40` §3.2).
    needs_attention: bool = False


@dataclass(frozen=True)
class ReportFileView:
    """One file of one report instance."""

    member_ref: str
    display_filename: str
    file_format: str
    content_type: str
    size_bytes: int
    is_previewable: bool
    semantic_role: str
    # `RP-13`. An explicit flag; never position, filename or extension.
    is_main_file: bool
    is_available: bool
    content_metric_kind: str | None = None
    content_metric_value: int | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ReportSourceBinding:
    """`RP-18` provenance, resolved and snapshotted at generation time.

    `dataset_id` may be null after the dataset is deleted; the descriptive
    fields keep the record readable, and the read path degrades to a truthful
    unavailable state rather than to a silently wrong link (`docs/40` §12).
    """

    dataset_id: str | None
    dataset_slug: str
    dataset_name: str
    date_column: str
    applied_from: date | None
    applied_to_exclusive: date | None


@dataclass(frozen=True)
class ReportInstanceView:
    """One report occurrence, as the library and the detail page see it."""

    instance_ref: str
    provider: str
    client_code: str
    client_display_name: str
    type_key: str
    type_label: str
    type_description: str
    cadence_class: str
    cadence_detail: str
    display_name: str
    status: str
    library_timestamp: datetime
    generation_started_at: datetime | None = None
    generation_finished_at: datetime | None = None
    period_key: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_kind: str | None = None
    row_count: int | None = None
    retention_until: datetime | None = None
    safe_error_code: str | None = None
    files: tuple[ReportFileView, ...] = ()
    source: ReportSourceBinding | None = None
    # `docs/40` §10.1 capabilities. A provider that has no cyclical period
    # declares so, and the detail page omits period navigation and the history
    # panel rather than inventing them (`docs/40` §11.3, §13.1 = A).
    supports_periods: bool = True
    supports_history: bool = True

    @property
    def available_files(self) -> tuple[ReportFileView, ...]:
        return tuple(f for f in self.files if f.is_available)

    @property
    def main_file(self) -> ReportFileView | None:
        for candidate in self.available_files:
            if candidate.is_main_file:
                return candidate
        return None

    @property
    def total_available_bytes(self) -> int:
        return sum(f.size_bytes for f in self.available_files)


@dataclass(frozen=True)
class ReportHistoryEntry:
    """One row of `Historia tego raportu` (`RP-15`).

    Built from the instance record alone — never from its files — which is what
    lets an expired period still render its period, status and generation time
    with `0` files (`docs/40` §11.2).
    """

    instance_ref: str
    period_key: str
    period_label: str
    status: str
    library_timestamp: datetime
    file_count: int
    is_current: bool


@dataclass(frozen=True)
class LibraryGroup:
    """Instances that share a generation month (`RP-3`, `RP-6`)."""

    key: str
    heading: str
    instances: tuple[ReportInstanceView, ...]

    @property
    def count(self) -> int:
        return len(self.instances)


@dataclass
class LibraryQuery:
    """Canonical, URL-driven library state.

    `SH-13` requires returning from the detail page to the same list state, so
    the query IS the state: there is no server-side cursor and no session
    memory, and a detail link simply carries this query back (`docs/40` §10).
    """

    client_code: str = ""
    type_key: str = ""
    year: int | None = None
    status: str = ""
    search: str = ""
    page: int = 1
    limit: int = 100
    scroll: int = 0


@dataclass(frozen=True)
class LibraryPage:
    """One rendered page of the library, plus the counts the footer states."""

    groups: tuple[LibraryGroup, ...]
    filtered_total: int
    library_total: int
    page: int
    limit: int
    types: tuple[ReportTypeView, ...] = ()
    oldest_library_timestamp: datetime | None = None
    available_years: tuple[int, ...] = ()

    @property
    def page_count(self) -> int:
        if self.limit <= 0:
            return 1
        return max(1, (self.filtered_total + self.limit - 1) // self.limit)

    @property
    def rendered_count(self) -> int:
        return sum(group.count for group in self.groups)


@dataclass(frozen=True)
class ClientContext:
    """The client the context bar names, and the ones the selector offers."""

    client_code: str
    display_name: str
    available: tuple[tuple[str, str], ...] = field(default=())
