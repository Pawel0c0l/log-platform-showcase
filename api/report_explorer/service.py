"""Report Explorer read service: authorization, then the two providers, then one library.

The order matters and is the whole point of this layer. Every entry point
authorizes the CLIENT first, and only then asks the providers for data that is
already scoped to it. No provider is ever consulted with an id the caller
supplied but the account was not authorized for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .access import ReportAccessResolver, ReportClient
from .delivery import may_preview_inline, normalize_content_type
from .errors import (
    ReportFileNotFoundError,
    ReportFilePreviewUnsupportedError,
    ReportFileUnavailableError,
    ReportInstanceNotFoundError,
    ReportSchemaUnavailableError,
)
from .exports_adapter import (
    EXPORT_MEMBER_REF,
    EXPORT_TYPE_KEY,
    DatabaseExportProvider,
)
from .models import (
    PROVIDER_DATABASE_EXPORT,
    PROVIDER_GENERATED,
    STATUS_ORDER,
    ClientContext,
    LibraryGroup,
    LibraryPage,
    LibraryQuery,
    ReportInstanceView,
    ReportTypeView,
)
from .periods import generation_month_key, generation_month_label
from .publication import generated_report_schema_available
from .store import DEFAULT_PAGE_SIZE, PAGE_SIZES, GeneratedReportStore, parse_instance_ref

# THE LIBRARY HAS NO POPULATION CEILING.
#
# The previous implementation kept the newest 500 exports and the newest 5,000
# generated instances before merging them in Python. An independent review
# established the consequence: past those counts an older record was simply
# unreachable, while the footer counter — computed from `count(*)` — still said
# it existed. A page and its total described two different populations.
#
# There is now ONE merged ordering, expressed once in SQL over both providers'
# own scoped SELECTs, and the page is a `LIMIT/OFFSET` over it. Per request the
# database orders `offset + limit` thin identity rows and the service hydrates
# at most `limit` of them; nothing loads a population into Python to merge it,
# and no record is unreachable.
#
# `page` is clamped to the LAST page that the current filter actually has, so a
# hand-typed `page=99999` lands on real data instead of asking for an offset
# beyond the population. That is a bound on nonsense, not on reachability.


@dataclass(frozen=True)
class ReportDetail:
    """Everything `REP-003` renders, already authorized."""

    instance: ReportInstanceView
    previous_period: dict | None
    next_period: dict | None
    history: tuple
    total_periods: int
    # `RP-18`. Present only when the account CURRENTLY has Database Explorer
    # access to the report's dataset. Report access never implies dataset
    # access, so this is resolved on every detail render, not stored.
    source_url: str | None
    source_dataset_name: str


@dataclass(frozen=True)
class ResolvedFile:
    """One authorized member and the artifact row behind it."""

    instance: ReportInstanceView
    member_ref: str
    display_filename: str
    content_type: str
    is_previewable: bool
    artifact_row: dict


class ReportExplorerService:
    def __init__(
        self,
        connection_factory,
        *,
        effective_client_access,
        database_export_schema_available,
        dataset_for_user,
    ) -> None:
        self._connect = connection_factory
        self.access = ReportAccessResolver(
            connection_factory, effective_client_access=effective_client_access
        )
        self.store = GeneratedReportStore(connection_factory)
        self.exports = DatabaseExportProvider(
            connection_factory, schema_available=database_export_schema_available
        )
        # `_get_portal_database_dataset_for_user` — the canonical Database
        # Explorer gate. Injected so `Dane źródłowe` reauthorizes through the
        # same helper the Database Explorer routes use, never a copy of it.
        self._dataset_for_user = dataset_for_user

    # -- availability -------------------------------------------------------

    def schema_available(self) -> bool:
        return generated_report_schema_available(self._connect)

    def _require_schema(self) -> None:
        if not self.schema_available():
            raise ReportSchemaUnavailableError(
                "the generated-report schema is not present in this database"
            )

    # -- client context -----------------------------------------------------

    def client_context(self, user: dict, requested_client: str = "") -> ClientContext:
        """Resolve the active client, or raise the right denial.

        With no client named, the first client the account may see is used — the
        context bar always names a real client, because `SH-6` requires the
        client identity to be visible on every screen including the empty and
        denied ones.
        """
        user_id = str((user or {}).get("user_id") or "")
        clients = self.access.list_report_clients(user_id)
        available = tuple((c.client_code, c.display_name) for c in clients)
        code = str(requested_client or "").strip()
        if code:
            # An explicitly named client is authorized on its own terms, so a
            # deep link into a client the account lost access to produces the
            # approved state rather than silently switching clients.
            resolved: ReportClient = self.access.require_report_access(user, code)
            return ClientContext(resolved.client_code, resolved.display_name, available)
        if not clients:
            from .errors import ReportAccessDeniedError

            raise ReportAccessDeniedError("this account has no report access to any client")
        first = clients[0]
        return ClientContext(first.client_code, first.display_name, available)

    # -- library ------------------------------------------------------------

    def library(self, user: dict, context: ClientContext, query: LibraryQuery) -> LibraryPage:
        self._require_schema()
        user_id = str((user or {}).get("user_id") or "")
        limit = query.limit if query.limit in PAGE_SIZES else DEFAULT_PAGE_SIZE

        wants_exports = query.type_key in ("", EXPORT_TYPE_KEY) and self.exports.available()
        wants_generated = query.type_key != EXPORT_TYPE_KEY

        filtered_total = 0
        if wants_generated:
            filtered_total += self.store.count_filtered(context.client_code, query)
        if wants_exports:
            filtered_total += self.exports.count_filtered(
                user_id=user_id, client_code=context.client_code, query=query
            )

        # `RP-5`: the page and the counter describe the same population, so the
        # page number is resolved against the count that was just taken.
        page_count = max(1, (filtered_total + limit - 1) // limit)
        page = min(max(1, int(query.page or 1)), page_count)
        offset = (page - 1) * limit

        keys = self._merged_page_keys(
            user_id=user_id,
            client_code=context.client_code,
            query=query,
            wants_generated=wants_generated,
            wants_exports=wants_exports,
            offset=offset,
            limit=limit,
        )
        visible = self._hydrate(user_id, context.client_code, keys)

        types = self._library_types(user_id, context.client_code)
        library_total = self.store.count_library_total(context.client_code)
        if self.exports.available():
            library_total += self.exports.count_all(
                user_id=user_id, client_code=context.client_code
            )
        export_oldest, export_years = self.exports.library_bounds(
            user_id=user_id, client_code=context.client_code
        )

        return LibraryPage(
            groups=self._group_by_generation_month(visible),
            filtered_total=filtered_total,
            library_total=library_total,
            page=page,
            limit=limit,
            types=tuple(types),
            oldest_library_timestamp=self._oldest(context.client_code, export_oldest),
            available_years=self._years(context.client_code, export_years),
        )

    def _merged_page_keys(
        self, *, user_id: str, client_code: str, query: LibraryQuery,
        wants_generated: bool, wants_exports: bool, offset: int, limit: int,
    ) -> list[tuple[str, str]]:
        """One page of `(provider, identifier)`, in the one merged order.

        The ordering key is `(library_timestamp DESC, instance_ref DESC)` — the
        same key the page renders by — and `instance_ref` is
        `provider || '-' || identifier`, which is unique across both providers.
        Ties therefore break deterministically rather than by whichever provider
        happened to be read first, and two adjacent pages can neither repeat nor
        skip a record.
        """
        fragments: list[str] = []
        params: list = []
        if wants_generated:
            sql, values = self.store.page_key_sql(client_code, query)
            fragments.append(sql)
            params.extend(values)
        if wants_exports:
            sql, values = self.exports.page_key_sql(
                user_id=user_id, client_code=client_code, query=query
            )
            fragments.append(sql)
            params.extend(values)
        if not fragments:
            return []
        union = " UNION ALL ".join(f"({fragment})" for fragment in fragments)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT provider, identifier
                      FROM ({union}) AS merged
                     ORDER BY library_timestamp DESC,
                              (provider || '-' || identifier) DESC
                     LIMIT %s OFFSET %s
                    """,
                    [*params, int(limit), int(offset)],
                )
                return [(str(row["provider"]), str(row["identifier"])) for row in cur.fetchall()]

    def _hydrate(self, user_id: str, client_code: str, keys) -> list[ReportInstanceView]:
        """Turn one page of ordered identities into rendered instances.

        Each provider re-applies its own full scope while hydrating, so this is
        not a second path that trusts an id the merge produced.
        """
        generated_ids = [ident for provider, ident in keys if provider == PROVIDER_GENERATED]
        export_ids = [ident for provider, ident in keys if provider == PROVIDER_DATABASE_EXPORT]
        generated = self.store.hydrate(client_code, generated_ids) if generated_ids else {}
        exports = (
            self.exports.hydrate(user_id=user_id, client_code=client_code, job_ids=export_ids)
            if export_ids
            else {}
        )
        ordered: list[ReportInstanceView] = []
        for provider, identifier in keys:
            found = (generated if provider == PROVIDER_GENERATED else exports).get(identifier)
            if found is not None:
                ordered.append(found)
        return ordered

    def _library_types(self, user_id: str, client_code: str) -> list[ReportTypeView]:
        """The left rail (`RP-2`): one entry per type this client has records for.

        The export entry's count is a `count(*)`, not the length of a list that
        was loaded to be counted.
        """
        types: list[ReportTypeView] = list(self.store.list_types(client_code))
        export_count = (
            self.exports.count_all(user_id=user_id, client_code=client_code)
            if self.exports.available()
            else 0
        )
        if export_count:
            types.append(self.exports.type_view_for_count(export_count))
        types.sort(key=lambda t: t.display_name)
        return types

    def _oldest(self, client_code: str, export_oldest: datetime | None) -> datetime | None:
        candidates = [self.store.oldest_library_timestamp(client_code), export_oldest]
        present = [c for c in candidates if c is not None]
        return min(present) if present else None

    def _years(self, client_code: str, export_years) -> tuple[int, ...]:
        years = set(self.store.available_years(client_code))
        years.update(int(year) for year in (export_years or ()))
        return tuple(sorted(years, reverse=True))

    def _group_by_generation_month(self, instances: list[ReportInstanceView]) -> tuple[LibraryGroup, ...]:
        """`RP-3` / `RP-6`: groups state their rule, and their counts are real.

        The count is `len()` of the rows actually placed in the group, so a
        heading can never claim a number the list does not render.
        """
        groups: list[LibraryGroup] = []
        current: list[ReportInstanceView] = []
        current_key = ""
        for instance in instances:
            key = generation_month_key(instance.library_timestamp)
            if key != current_key and current:
                groups.append(self._close_group(current_key, current))
                current = []
            current_key = key
            current.append(instance)
        if current:
            groups.append(self._close_group(current_key, current))
        return tuple(groups)

    def _close_group(self, key: str, instances: list[ReportInstanceView]) -> LibraryGroup:
        return LibraryGroup(
            key=key,
            heading=generation_month_label(instances[0].library_timestamp),
            instances=tuple(instances),
        )

    # -- detail -------------------------------------------------------------

    def instance(self, user: dict, context: ClientContext, instance_ref: str) -> ReportInstanceView:
        """One instance the account is CURRENTLY allowed to see, or not found.

        A reference that is malformed, belongs to another client, belongs to
        another account's export or simply does not exist all produce the same
        `ReportInstanceNotFoundError`, so the response cannot be used to probe
        for existence.
        """
        parsed = parse_instance_ref(instance_ref)
        if not parsed:
            raise ReportInstanceNotFoundError("unknown report reference")
        provider, identifier = parsed
        user_id = str((user or {}).get("user_id") or "")
        if provider == PROVIDER_DATABASE_EXPORT:
            found = self.exports.get_instance(
                user_id=user_id, client_code=context.client_code, job_id=identifier
            )
        elif provider == PROVIDER_GENERATED:
            self._require_schema()
            found = self.store.get_instance(context.client_code, identifier)
        else:
            found = None
        if not found:
            raise ReportInstanceNotFoundError("unknown report reference")
        return found

    def detail(self, user: dict, context: ClientContext, instance_ref: str) -> ReportDetail:
        instance = self.instance(user, context, instance_ref)
        previous = following = None
        history: tuple = ()
        total_periods = 0
        if instance.provider == PROVIDER_GENERATED:
            identifier = instance.instance_ref.split("-", 1)[1]
            previous, following = self.store.siblings(context.client_code, instance)
            history = tuple(self.store.history(context.client_code, identifier))
            total_periods = self.store.count_periods(context.client_code, identifier)
        source_url, source_name = self._source_data_link(user, instance)
        return ReportDetail(
            instance=instance,
            previous_period=previous,
            next_period=following,
            history=history,
            total_periods=total_periods,
            source_url=source_url,
            source_dataset_name=source_name,
        )

    def _source_data_link(self, user: dict, instance: ReportInstanceView) -> tuple[str | None, str]:
        """`RP-18`, reauthorized independently every time it is rendered.

        Report access does NOT imply Database Explorer access. The dataset is
        re-resolved through the canonical Database Explorer gate, and when that
        gate says no — or when the dataset was deleted, renamed away or
        deactivated — the action is ABSENT and no dataset metadata is disclosed.
        """
        source = instance.source
        if not source or not source.dataset_id:
            return None, ""
        user_id = str((user or {}).get("user_id") or "")
        try:
            dataset = self._dataset_for_user(source.dataset_id, user_id)
        except Exception:  # noqa: BLE001 - an unreadable gate is a closed gate
            return None, ""
        if not dataset:
            return None, ""
        if str(dataset.get("client_code") or "") != instance.client_code:
            # The dataset was re-pointed at another client since generation. The
            # historical link would now cross a tenant boundary, so it is gone.
            return None, ""
        url = f"/user/database/datasets/{source.dataset_id}"
        if source.date_column and source.applied_from and source.applied_to_exclusive:
            from urllib.parse import urlencode

            url += "?" + urlencode(
                {
                    f"dateop__{source.date_column}": "range",
                    f"date_from__{source.date_column}": source.applied_from.isoformat(),
                    f"date_to__{source.date_column}": source.applied_to_exclusive.isoformat(),
                }
            )
        return url, str(dataset.get("dataset_name") or source.dataset_name)

    # -- file delivery ------------------------------------------------------

    def resolve_file(
        self, user: dict, context: ClientContext, instance_ref: str, member_ref: str,
        *, for_preview: bool = False,
    ) -> ResolvedFile:
        """Walk authorized user → client → instance → member → artifact.

        Every hop is re-established here, on this request. The artifact is
        reached only THROUGH the membership, so an artifact id is never an input
        and an artifact belonging to another client is unreachable even if a
        membership were mis-published.

        `for_preview` adds the INLINE decision, and it is made here rather than
        by the route, from the artifact's OWN content type rather than from the
        member's description of it. An independent review served a member
        labelled `PDF` whose bytes were `text/html` into the embedded preview;
        the answer is that presentation metadata never decides what may be
        embedded — the stored object does, and only for the one content type
        this platform serves inline.
        """
        instance = self.instance(user, context, instance_ref)
        user_id = str((user or {}).get("user_id") or "")
        if instance.provider == PROVIDER_DATABASE_EXPORT:
            if member_ref != EXPORT_MEMBER_REF:
                raise ReportFileNotFoundError("unknown file reference")
            artifact = self.exports.get_artifact(
                user_id=user_id,
                client_code=context.client_code,
                job_id=instance.instance_ref.split("-", 1)[1],
            )
            member = instance.files[0] if instance.files else None
            if not member:
                raise ReportFileNotFoundError("unknown file reference")
            if not artifact or not member.is_available:
                raise ReportFileUnavailableError("the file is no longer available")
            return self._resolved(
                instance=instance,
                member_ref=member.member_ref,
                display_filename=member.display_filename,
                declared_content_type=member.content_type,
                artifact_content_type=artifact.get("content_type"),
                is_previewable=member.is_previewable,
                artifact_row=artifact,
                for_preview=for_preview,
            )

        row = self.store.get_member_artifact(
            context.client_code, instance.instance_ref.split("-", 1)[1], member_ref
        )
        if not row:
            raise ReportFileNotFoundError("unknown file reference")
        if not row.get("is_available") or not row.get("artifact_id") or row.get("expired_at"):
            raise ReportFileUnavailableError("the file is no longer available")
        return self._resolved(
            instance=instance,
            member_ref=str(row["member_id"]),
            display_filename=str(row["display_filename"]),
            declared_content_type=row.get("content_type"),
            artifact_content_type=row.get("artifact_content_type"),
            is_previewable=bool(row.get("is_previewable")),
            artifact_row=row,
            for_preview=for_preview,
        )

    def _resolved(
        self, *, instance: ReportInstanceView, member_ref: str, display_filename: str,
        declared_content_type, artifact_content_type, is_previewable: bool,
        artifact_row: dict, for_preview: bool,
    ) -> ResolvedFile:
        """Bind the served content type to the stored object, then decide inline.

        The ARTIFACT's content type wins whenever it has one. Publication already
        binds the two, and migration `069` constrains the member — this is the
        third layer, and the one that also covers a member written before either
        existed.
        """
        authoritative = normalize_content_type(artifact_content_type) or normalize_content_type(
            declared_content_type
        )
        previewable = may_preview_inline(authoritative, is_previewable=bool(is_previewable))
        if for_preview and not previewable:
            # `RP-14`: a non-previewable member offers download only. The route
            # cannot answer a preview request with bytes it was never allowed to
            # embed, so this is a refusal, not a silent fall back to download.
            raise ReportFilePreviewUnsupportedError(
                "this file is not served as an embedded preview"
            )
        return ResolvedFile(
            instance=instance,
            member_ref=member_ref,
            display_filename=display_filename,
            content_type=authoritative or "application/octet-stream",
            is_previewable=previewable,
            artifact_row=artifact_row,
        )


def normalize_status(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in STATUS_ORDER else ""


def normalize_year(value) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 2000 <= year <= 2100 else None


def normalize_limit(value) -> int:
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return limit if limit in PAGE_SIZES else DEFAULT_PAGE_SIZE


def normalize_page(value) -> int:
    """A page number, or 1.

    Deliberately NOT capped here any more: the library clamps the request to the
    last page the current filter actually has, so reachability is decided by the
    real population and the offset can never exceed it. A cap at this layer was
    a second, invisible ceiling.
    """
    try:
        page = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, page)


def today() -> date:
    return date.today()
