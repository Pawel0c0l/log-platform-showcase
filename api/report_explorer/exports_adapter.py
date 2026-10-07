"""`DB-53` — completed background exports, adapted, never duplicated.

A completed Database Explorer background export must be reachable from Report
Explorer as the system report type `Eksporty danych` (`DB-53`, `PBC` §2.13). It
reaches it through THIS adapter, which reads `database_export_jobs` as it stands
and normalizes it into the same domain objects the generated-report provider
produces.

WHY NOT COPY THE ROWS.
    An export already has a first-class identity, a status lifecycle, a row
    count, an artifact and an expiry (`db/migrations/043`, `065`). Copying it
    into generated-report storage would create two identities and two lifecycles
    for one thing, and a synchronization problem for zero product gain
    (`docs/40` §10.2). Nothing in this module writes.

WHAT AN EXPORT IS NOT.
    It is not cyclical, and no reporting period is invented for it. Owner
    decision `docs/40` §13.1 = **A**: `Okres raportowania` and `Numer okresu`
    render `nie dotyczy`, the type declares `na żądanie · systemowy`, ordering is
    by completion time, and the provider declares period siblings and history as
    unsupported — so the detail page omits those panels rather than fabricating
    an adjacency that does not exist.

SCOPE.
    COMPLETED exports only, including completed-then-expired. `queued`,
    `running`, `failed` and `cancelled` stay on the export management surface
    (`DB-007`), which keeps its own lifecycle actions and is a different product
    purpose.

VISIBILITY.
    Owner-scoped by `requested_by_user_id` — an export belongs to the account
    that requested it — **and** additionally gated on effective `can_view_reports`
    for the dataset's client, applied by the caller before this adapter is
    consulted.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import (
    PROVIDER_DATABASE_EXPORT,
    ROLE_MAIN,
    STATUS_EXPIRED,
    STATUS_READY,
    ReportFileView,
    ReportInstanceView,
    ReportSourceBinding,
    ReportTypeView,
)

EXPORT_TYPE_KEY = "database_exports"
EXPORT_TYPE_LABEL = "Eksporty danych"
EXPORT_TYPE_DESCRIPTION = (
    "Eksporty w tle zlecone w Przeglądarce danych. Powstają na żądanie Twojego konta "
    "i są widoczne tylko dla Ciebie."
)
EXPORT_CADENCE_CLASS = "on_demand"
EXPORT_CADENCE_DETAIL = "systemowy"

# The single member an export publishes. A fixed token rather than the artifact
# id, so no storage identity ever appears in a URL.
EXPORT_MEMBER_REF = "main"

_FORMAT_BY_EXTENSION = {"csv": "CSV", "xlsx": "XLSX", "json": "JSON", "txt": "TXT"}
_CONTENT_TYPE_BY_FORMAT = {
    "CSV": "text/csv",
    "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "JSON": "application/json",
    "TXT": "text/plain",
}


def export_instance_ref(job_id) -> str:
    return f"{PROVIDER_DATABASE_EXPORT}-{job_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(row, now: datetime) -> bool:
    if str(row.get("status") or "") == "expired":
        return True
    if row.get("artifact_expired_at"):
        return True
    if not row.get("artifact_id"):
        return True
    expires_at = row.get("expires_at")
    return bool(expires_at and expires_at <= now)


class DatabaseExportProvider:
    """Read-only view of `database_export_jobs` as one Report Explorer type."""

    def __init__(self, connection_factory, *, schema_available) -> None:
        self._connect = connection_factory
        self._schema_available = schema_available

    def available(self) -> bool:
        try:
            return bool(self._schema_available())
        except Exception:  # noqa: BLE001 - an unavailable probe is an absent type
            return False

    # -- the SQL this provider contributes to a merged library page ----------
    #
    # `DB-53` exports and generated reports are ONE product list ordered by one
    # axis, and an independent review found the previous implementation kept
    # only the newest 500 exports before merging, so an older export became
    # unreachable while the footer still counted it. A truthful merged page
    # cannot be built by truncating a provider in Python.
    #
    # This provider therefore contributes a SELECT with its own scoping,
    # ordering key and filter semantics, which the service composes into the
    # merged page query. It stays a read-only adapter: it reads
    # `database_export_jobs` as it stands, writes nothing, and copies nothing
    # into generated-report storage (`docs/40` §10.2).

    # The derived `Gotowy` / `Pliki wygasły` status, in SQL, matching
    # `_is_expired` exactly so the two never disagree about one export.
    STATUS_SQL = """
        CASE
          WHEN dej.status = 'expired'
            OR a.expired_at IS NOT NULL
            OR dej.artifact_id IS NULL
            OR (dej.expires_at IS NOT NULL AND dej.expires_at <= now())
            THEN 'expired'
          ELSE 'ready'
        END
    """

    # The rendered instance label, in SQL. `RP-4` requires the search filter to
    # match what is actually on screen, so the pattern is applied to the same
    # string the page shows.
    DISPLAY_NAME_SQL = """
        COALESCE(pdd.dataset_name, 'Eksport') || ' · ' || (
          CASE lower(COALESCE(dej.requested_format, ''))
            WHEN 'xlsx' THEN 'XLSX'
            WHEN 'json' THEN 'JSON'
            WHEN 'txt'  THEN 'TXT'
            ELSE 'CSV'
          END)
    """

    FROM_SQL = """
        FROM database_export_jobs dej
        JOIN portal_database_datasets pdd ON pdd.dataset_id = dej.dataset_id
        JOIN portal_clients pc ON pc.client_code = pdd.client_code
        LEFT JOIN artifacts a ON a.artifact_id = dej.artifact_id
    """

    def scope_sql(self, *, user_id: str, client_code: str) -> tuple[str, list]:
        """The owner + client + completed-only scope every export read applies."""
        return (
            "dej.requested_by_user_id = %s AND pdd.client_code = %s "
            # `PBC` §2.13 / `DB-53`: a COMPLETED export is what must be
            # reachable. A queued or failed job is export management, not a
            # report.
            "AND dej.status IN ('completed', 'expired') "
            "AND dej.completed_at IS NOT NULL",
            [user_id, client_code],
        )

    def filter_sql(self, *, user_id: str, client_code: str, query) -> tuple[str, list]:
        """Scope plus the active library filters, as one WHERE fragment."""
        clause, params = self.scope_sql(user_id=user_id, client_code=client_code)
        where = [clause]
        if getattr(query, "status", ""):
            where.append(f"({self.STATUS_SQL}) = %s")
            params.append(query.status)
        if getattr(query, "year", None):
            where.append("EXTRACT(YEAR FROM dej.completed_at) = %s")
            params.append(int(query.year))
        if getattr(query, "search", ""):
            where.append(f"({self.DISPLAY_NAME_SQL}) ILIKE %s")
            params.append(f"%{query.search}%")
        return " AND ".join(where), params

    def page_key_sql(self, *, user_id: str, client_code: str, query) -> tuple[str, list]:
        """`(provider, identifier, ordering timestamp)` for the merged page.

        Thin by design: the merged query orders and slices identities only, and
        the rows themselves are hydrated afterwards for the page actually shown.
        """
        clause, params = self.filter_sql(user_id=user_id, client_code=client_code, query=query)
        return (
            f"""
            SELECT '{PROVIDER_DATABASE_EXPORT}'::text AS provider,
                   dej.job_id::text AS identifier,
                   dej.completed_at AS library_timestamp
            {self.FROM_SQL}
            WHERE {clause}
            """,
            params,
        )

    def count_filtered(self, *, user_id: str, client_code: str, query) -> int:
        if not (user_id and client_code) or not self.available():
            return 0
        clause, params = self.filter_sql(user_id=user_id, client_code=client_code, query=query)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*)::int AS n {self.FROM_SQL} WHERE {clause}", params
                )
                return int((cur.fetchone() or {}).get("n") or 0)

    def count_all(self, *, user_id: str, client_code: str) -> int:
        if not (user_id and client_code) or not self.available():
            return 0
        clause, params = self.scope_sql(user_id=user_id, client_code=client_code)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*)::int AS n {self.FROM_SQL} WHERE {clause}", params
                )
                return int((cur.fetchone() or {}).get("n") or 0)

    def library_bounds(self, *, user_id: str, client_code: str) -> tuple[datetime | None, list[int]]:
        """`(oldest completion, years present)` — aggregates, never a row scan."""
        if not (user_id and client_code) or not self.available():
            return None, []
        clause, params = self.scope_sql(user_id=user_id, client_code=client_code)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT min(dej.completed_at) AS oldest {self.FROM_SQL} WHERE {clause}",
                    params,
                )
                oldest = (cur.fetchone() or {}).get("oldest")
                cur.execute(
                    f"""
                    SELECT DISTINCT EXTRACT(YEAR FROM dej.completed_at)::int AS year
                    {self.FROM_SQL} WHERE {clause}
                    """,
                    params,
                )
                years = [int(row["year"]) for row in cur.fetchall() if row.get("year")]
        return oldest, years

    def _rows(self, *, user_id: str, client_code: str, job_id: str | None = None,
              job_ids: list[str] | None = None) -> list[dict]:
        if not (user_id and client_code):
            return []
        clause, params = self.scope_sql(user_id=user_id, client_code=client_code)
        clauses = [clause]
        if job_id is not None:
            clauses.append("dej.job_id = %s")
            params.append(job_id)
        if job_ids is not None:
            if not job_ids:
                return []
            clauses.append("dej.job_id = ANY(%s::uuid[])")
            params.append(list(job_ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT dej.job_id, dej.dataset_id, dej.requested_format, dej.status,
                           dej.completed_at, dej.expires_at, dej.row_count, dej.artifact_id,
                           dej.request_snapshot_json,
                           pdd.dataset_name, pdd.client_code, pdd.slug AS dataset_slug,
                           pc.display_name AS client_display_name,
                           a.display_filename, a.filename, a.size_bytes, a.content_type,
                           a.expired_at AS artifact_expired_at
                    {self.FROM_SQL}
                     WHERE {' AND '.join(clauses)}
                     ORDER BY dej.completed_at DESC, dej.job_id DESC
                    """,
                    params,
                )
                return list(cur.fetchall())

    def hydrate(self, *, user_id: str, client_code: str, job_ids: list[str]) -> dict[str, ReportInstanceView]:
        """The instances for one page's worth of already-ordered identities.

        Re-applies the full owner + client + completed scope rather than trusting
        the identities the merged query produced, so this is not a second, weaker
        read path.
        """
        if not self.available() or not job_ids:
            return {}
        now = _now()
        rows = self._rows(user_id=user_id, client_code=client_code, job_ids=job_ids)
        return {str(row["job_id"]): self._view(row, now) for row in rows}

    def get_instance(self, *, user_id: str, client_code: str, job_id: str) -> ReportInstanceView | None:
        if not self.available():
            return None
        try:
            import uuid as _uuid

            _uuid.UUID(str(job_id))
        except (ValueError, AttributeError, TypeError):
            return None
        rows = self._rows(user_id=user_id, client_code=client_code, job_id=job_id)
        return self._view(rows[0], _now()) if rows else None

    def get_artifact(self, *, user_id: str, client_code: str, job_id: str) -> dict | None:
        """The `artifacts` row behind one export, re-authorized from scratch.

        Owner, client and job are all in the WHERE clause, so a job id belonging
        to another account or another client resolves to nothing.
        """
        rows = self._rows(user_id=user_id, client_code=client_code, job_id=job_id)
        if not rows or not rows[0].get("artifact_id"):
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = %s AND client_code = %s",
                    (rows[0]["artifact_id"], client_code),
                )
                return cur.fetchone()

    def type_view_for_count(self, instance_count: int) -> ReportTypeView:
        """The rail entry from a COUNT, without materialising the population.

        The rail states how many exports exist; it never needed the exports
        themselves, and loading them to call `len()` was what made the 500-row
        cap look harmless.
        """
        return ReportTypeView(
            type_key=EXPORT_TYPE_KEY,
            display_name=EXPORT_TYPE_LABEL,
            description=EXPORT_TYPE_DESCRIPTION,
            cadence_class=EXPORT_CADENCE_CLASS,
            cadence_detail=EXPORT_CADENCE_DETAIL,
            provider=PROVIDER_DATABASE_EXPORT,
            instance_count=int(instance_count),
            # An export cannot fail into this library: only completed jobs are
            # adapted, so there is no failure for an attention dot to report.
            needs_attention=False,
        )

    def _view(self, row, now: datetime) -> ReportInstanceView:
        expired = _is_expired(row, now)
        fmt = _FORMAT_BY_EXTENSION.get(str(row.get("requested_format") or "").lower(), "CSV")
        filename = str(row.get("display_filename") or row.get("filename") or f"eksport.{fmt.lower()}")
        files: tuple[ReportFileView, ...] = ()
        if row.get("artifact_id"):
            files = (
                ReportFileView(
                    member_ref=EXPORT_MEMBER_REF,
                    display_filename=filename,
                    file_format=fmt,
                    content_type=str(row.get("content_type") or _CONTENT_TYPE_BY_FORMAT.get(fmt, "application/octet-stream")),
                    size_bytes=int(row.get("size_bytes") or 0),
                    # `RP-14`: a tabular export is not embeddable, so it offers
                    # download only. A file property, never a permission.
                    is_previewable=False,
                    semantic_role=ROLE_MAIN,
                    is_main_file=True,
                    is_available=not expired,
                    content_metric_kind=("rows" if row.get("row_count") is not None else None),
                    content_metric_value=(int(row["row_count"]) if row.get("row_count") is not None else None),
                    expires_at=row.get("expires_at"),
                ),
            )
        snapshot = row.get("request_snapshot_json") or {}
        if isinstance(snapshot, str):
            import json as _json

            try:
                snapshot = _json.loads(snapshot)
            except ValueError:
                snapshot = {}
        source = ReportSourceBinding(
            dataset_id=str(row["dataset_id"]) if row.get("dataset_id") else None,
            dataset_slug=str(row.get("dataset_slug") or ""),
            dataset_name=str(row.get("dataset_name") or ""),
            date_column="",
            # Owner decision A: an export declares no reporting period, so no
            # period filter is replayed into the source-data link.
            applied_from=None,
            applied_to_exclusive=None,
        )
        completed = row.get("completed_at")
        return ReportInstanceView(
            instance_ref=export_instance_ref(row["job_id"]),
            provider=PROVIDER_DATABASE_EXPORT,
            client_code=str(row["client_code"]),
            client_display_name=str(row.get("client_display_name") or row["client_code"]),
            type_key=EXPORT_TYPE_KEY,
            type_label=EXPORT_TYPE_LABEL,
            type_description=EXPORT_TYPE_DESCRIPTION,
            cadence_class=EXPORT_CADENCE_CLASS,
            cadence_detail=EXPORT_CADENCE_DETAIL,
            display_name=f"{row.get('dataset_name') or 'Eksport'} · {fmt}",
            status=(STATUS_EXPIRED if expired else STATUS_READY),
            library_timestamp=completed,
            generation_started_at=None,
            generation_finished_at=completed,
            period_key=None,
            period_start=None,
            period_end=None,
            period_kind=None,
            row_count=(int(row["row_count"]) if row.get("row_count") is not None else None),
            retention_until=row.get("expires_at"),
            safe_error_code=None,
            files=files,
            source=source,
            supports_periods=False,
            supports_history=False,
        )
