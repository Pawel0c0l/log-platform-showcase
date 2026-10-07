"""Read path over the three generated-report relations.

Every statement here is CLIENT-SCOPED FIRST. There is no query in this module
that can return a row for a client the caller did not name, which means the
authorization decision made in `access.py` cannot be bypassed by a crafted
instance id, member id or artifact id: the id is a *filter*, never a lookup key
on its own.

The four approved statuses are derived in SQL rather than in Python, because
`Status` is a toolbar filter and the footer counter must be exact (`RP-5`) —
deriving it per row after fetching would make filtering and counting disagree
with each other.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import (
    PROVIDER_GENERATED,
    ReportFileView,
    ReportHistoryEntry,
    ReportInstanceView,
    ReportSourceBinding,
    ReportTypeView,
)
from .periods import format_period_range

# `docs/40` §3.2, verbatim as a SQL expression. Published-and-available is
# `Gotowy`; published-and-not is `Pliki wygasły`; never-published splits on the
# latest attempt. A failed retry over a published instance therefore stays
# `Gotowy`, which is the rule that keeps working actions on screen.
STATUS_SQL = """
    CASE
      WHEN i.last_published_at IS NOT NULL
           AND i.available_member_count > 0
           AND (i.available_expires_at IS NULL OR i.available_expires_at > now())
        THEN 'ready'
      WHEN i.last_published_at IS NOT NULL THEN 'expired'
      WHEN i.generation_state = 'failed' THEN 'failed'
      ELSE 'generating'
    END
"""

MAX_HISTORY_ENTRIES = 8
PAGE_SIZES = (25, 50, 100, 200, 500)
DEFAULT_PAGE_SIZE = 100


def instance_ref(instance_id) -> str:
    return f"{PROVIDER_GENERATED}-{instance_id}"


def parse_instance_ref(ref: str) -> tuple[str, str] | None:
    """`('gen', uuid)` for a well-formed reference, else ``None``.

    A malformed reference is not an error class of its own: it resolves to
    nothing, exactly like a reference to another client's report, so probing
    cannot distinguish the two (`REP-004`).
    """
    text = str(ref or "").strip()
    prefix, _, rest = text.partition("-")
    if not prefix or not rest:
        return None
    return prefix, rest


def _is_uuid(text: str) -> bool:
    import uuid as _uuid

    try:
        _uuid.UUID(str(text))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class GeneratedReportStore:
    """Provider 1 — the reports the platform generates."""

    def __init__(self, connection_factory) -> None:
        self._connect = connection_factory

    # -- rail ---------------------------------------------------------------

    def list_types(self, client_code: str) -> list[ReportTypeView]:
        """The left rail for one client (`RP-2`, `RP-7`).

        Derived from the instances that exist for THIS client, so a rail entry
        can never advertise a type the client has nothing for, and the per-type
        counts sum to the library total by construction.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH scoped AS (
                      SELECT i.instance_id, i.definition_id, i.library_timestamp,
                             {STATUS_SQL} AS status
                        FROM portal_generated_report_instances i
                       WHERE i.client_code = %s
                    ),
                    newest AS (
                      SELECT DISTINCT ON (definition_id) definition_id, status
                        FROM scoped
                       ORDER BY definition_id, library_timestamp DESC, instance_id DESC
                    )
                    SELECT d.type_key, d.display_name, d.description,
                           d.cadence_class, d.cadence_detail,
                           count(s.instance_id)::int AS instance_count,
                           bool_or(n.status = 'failed') AS needs_attention
                      FROM scoped s
                      JOIN portal_generated_report_definitions d ON d.definition_id = s.definition_id
                      LEFT JOIN newest n ON n.definition_id = s.definition_id
                     GROUP BY d.type_key, d.display_name, d.description, d.cadence_class, d.cadence_detail
                     ORDER BY d.display_name ASC
                    """,
                    (client_code,),
                )
                rows = cur.fetchall()
        return [
            ReportTypeView(
                type_key=str(row["type_key"]),
                display_name=str(row["display_name"]),
                description=str(row.get("description") or ""),
                cadence_class=str(row["cadence_class"]),
                cadence_detail=str(row.get("cadence_detail") or ""),
                provider=PROVIDER_GENERATED,
                instance_count=int(row["instance_count"]),
                needs_attention=bool(row.get("needs_attention")),
            )
            for row in rows
        ]

    # -- library ------------------------------------------------------------

    def _filter_sql(self, client_code: str, query) -> tuple[str, list]:
        where = ["i.client_code = %s"]
        params: list = [client_code]
        if query.type_key:
            where.append("d.type_key = %s")
            params.append(query.type_key)
        if query.year:
            # The `Rok` axis is the library timestamp — the GENERATION year, the
            # same axis the groups and the ordering use (`docs/40` §3.4).
            where.append("EXTRACT(YEAR FROM i.library_timestamp) = %s")
            params.append(int(query.year))
        if query.status:
            where.append(f"({STATUS_SQL}) = %s")
            params.append(query.status)
        if query.search:
            # `Nazwa raportu lub okres` (`PBC` §3.4): the persisted instance
            # label and the persisted period key. Never a filename.
            where.append("(i.display_name ILIKE %s OR i.period_key ILIKE %s)")
            pattern = f"%{query.search}%"
            params.extend([pattern, pattern])
        return " AND ".join(where), params

    def count_library_total(self, client_code: str) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*)::int AS n FROM portal_generated_report_instances WHERE client_code = %s",
                    (client_code,),
                )
                return int((cur.fetchone() or {}).get("n") or 0)

    def count_filtered(self, client_code: str, query) -> int:
        clause, params = self._filter_sql(client_code, query)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT count(*)::int AS n
                      FROM portal_generated_report_instances i
                      JOIN portal_generated_report_definitions d ON d.definition_id = i.definition_id
                     WHERE {clause}
                    """,
                    params,
                )
                return int((cur.fetchone() or {}).get("n") or 0)

    def page_key_sql(self, client_code: str, query) -> tuple[str, list]:
        """`(provider, identifier, ordering timestamp)` for the merged page.

        The generated provider's half of the one merged ordering. It selects
        identities only: a merged page is decided by the ordering axis, and
        hydrating rows that the merge will discard is exactly the wasted work
        the previous windowing did.
        """
        clause, params = self._filter_sql(client_code, query)
        return (
            f"""
            SELECT '{PROVIDER_GENERATED}'::text AS provider,
                   i.instance_id::text AS identifier,
                   i.library_timestamp AS library_timestamp
              FROM portal_generated_report_instances i
              JOIN portal_generated_report_definitions d ON d.definition_id = i.definition_id
             WHERE {clause}
            """,
            params,
        )

    def hydrate(self, client_code: str, instance_ids: list[str]) -> dict[str, ReportInstanceView]:
        """The instances for one page's worth of already-ordered identities.

        Client-scoped in the WHERE clause exactly like every other read here, so
        an identity the merge produced is still re-authorized rather than
        trusted.
        """
        wanted = [str(i) for i in instance_ids if _is_uuid(str(i))]
        if not wanted:
            return {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT i.instance_id, i.client_code, i.display_name,
                           i.period_kind, i.period_key, i.period_start, i.period_end,
                           i.library_timestamp, i.generation_started_at, i.generation_finished_at,
                           i.row_count, i.safe_error_code, i.available_expires_at,
                           d.type_key, d.display_name AS type_label, d.description AS type_description,
                           d.cadence_class, d.cadence_detail, d.retention_months,
                           {STATUS_SQL} AS status,
                           pc.display_name AS client_display_name
                      FROM portal_generated_report_instances i
                      JOIN portal_generated_report_definitions d ON d.definition_id = i.definition_id
                      JOIN portal_clients pc ON pc.client_code = i.client_code
                     WHERE i.client_code = %s
                       AND i.instance_id = ANY(%s::uuid[])
                    """,
                    (client_code, wanted),
                )
                rows = cur.fetchall()
                members = self._load_members(cur, [row["instance_id"] for row in rows])
        return {
            str(row["instance_id"]): self._instance_view(
                row, members.get(str(row["instance_id"]), ())
            )
            for row in rows
        }

    def available_years(self, client_code: str) -> list[int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT EXTRACT(YEAR FROM library_timestamp)::int AS year
                      FROM portal_generated_report_instances
                     WHERE client_code = %s
                     ORDER BY year DESC
                    """,
                    (client_code,),
                )
                return [int(row["year"]) for row in cur.fetchall()]

    def oldest_library_timestamp(self, client_code: str) -> datetime | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT min(library_timestamp) AS oldest FROM portal_generated_report_instances WHERE client_code = %s",
                    (client_code,),
                )
                return (cur.fetchone() or {}).get("oldest")

    # -- detail -------------------------------------------------------------

    def get_instance(self, client_code: str, instance_id: str) -> ReportInstanceView | None:
        """One instance, scoped to the client the caller was authorized for.

        `client_code` is part of the WHERE clause and not a post-fetch check, so
        a foreign instance id returns nothing at all rather than a row that some
        later branch is trusted to reject (`REP-004` — no existence leak).
        """
        if not _is_uuid(instance_id):
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT i.instance_id, i.client_code, i.display_name, i.definition_id,
                           i.period_kind, i.period_key, i.period_start, i.period_end,
                           i.library_timestamp, i.generation_started_at, i.generation_finished_at,
                           i.row_count, i.safe_error_code, i.available_expires_at,
                           i.source_dataset_id, i.source_provenance_json,
                           d.type_key, d.display_name AS type_label, d.description AS type_description,
                           d.cadence_class, d.cadence_detail, d.retention_months,
                           {STATUS_SQL} AS status,
                           pc.display_name AS client_display_name
                      FROM portal_generated_report_instances i
                      JOIN portal_generated_report_definitions d ON d.definition_id = i.definition_id
                      JOIN portal_clients pc ON pc.client_code = i.client_code
                     WHERE i.instance_id = %s AND i.client_code = %s
                     LIMIT 1
                    """,
                    (instance_id, client_code),
                )
                row = cur.fetchone()
                if not row:
                    return None
                members = self._load_members(cur, [row["instance_id"]])
        return self._instance_view(row, members.get(str(row["instance_id"]), ()), with_source=True)

    def get_definition_id(self, client_code: str, instance_id: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT definition_id FROM portal_generated_report_instances "
                    "WHERE instance_id = %s AND client_code = %s",
                    (instance_id, client_code),
                )
                row = cur.fetchone()
        return str(row["definition_id"]) if row else None

    def history(self, client_code: str, instance_id: str, *, limit: int = MAX_HISTORY_ENTRIES) -> list[ReportHistoryEntry]:
        """`Historia tego raportu` — the same type's last periods (`RP-15`).

        Ordered by `period_start`, not by generation time and not by key text:
        the panel is a statement about PERIODS. It reads only instance records,
        so an expired period still renders its status and a file count of zero
        (`docs/40` §11.2).
        """
        definition_id = self.get_definition_id(client_code, instance_id)
        if not definition_id:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT i.instance_id, i.period_key, i.period_start, i.period_end,
                           i.library_timestamp, i.available_member_count,
                           {STATUS_SQL} AS status
                      FROM portal_generated_report_instances i
                     WHERE i.client_code = %s AND i.definition_id = %s
                     ORDER BY i.period_start DESC
                     LIMIT %s
                    """,
                    (client_code, definition_id, int(limit)),
                )
                rows = cur.fetchall()
        return [
            ReportHistoryEntry(
                instance_ref=instance_ref(row["instance_id"]),
                period_key=str(row["period_key"]),
                period_label=format_period_range(row["period_start"], row["period_end"]),
                status=str(row["status"]),
                library_timestamp=row["library_timestamp"],
                file_count=int(row.get("available_member_count") or 0),
                is_current=str(row["instance_id"]) == str(instance_id),
            )
            for row in rows
        ]

    def count_periods(self, client_code: str, instance_id: str) -> int:
        definition_id = self.get_definition_id(client_code, instance_id)
        if not definition_id:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*)::int AS n FROM portal_generated_report_instances "
                    "WHERE client_code = %s AND definition_id = %s",
                    (client_code, definition_id),
                )
                return int((cur.fetchone() or {}).get("n") or 0)

    def siblings(self, client_code: str, instance: ReportInstanceView) -> tuple[dict | None, dict | None]:
        """`(previous, next)` by adjacent reporting period (`RP-16`, `RP-17`).

        Adjacency is decided by `period_start` within one `(client, type)` — the
        migration's alignment and uniqueness rules are what make that a total
        order. At the newest period the forward sibling is simply ``None``, and
        the page renders no control at all rather than a disabled one.
        """
        definition_id = self.get_definition_id(client_code, instance.instance_ref.split("-", 1)[1])
        if not definition_id or instance.period_start is None:
            return None, None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT instance_id, display_name, period_key, period_start, period_end
                      FROM portal_generated_report_instances
                     WHERE client_code = %s AND definition_id = %s AND period_start < %s
                     ORDER BY period_start DESC LIMIT 1
                    """,
                    (client_code, definition_id, instance.period_start),
                )
                previous = cur.fetchone()
                cur.execute(
                    """
                    SELECT instance_id, display_name, period_key, period_start, period_end
                      FROM portal_generated_report_instances
                     WHERE client_code = %s AND definition_id = %s AND period_start > %s
                     ORDER BY period_start ASC LIMIT 1
                    """,
                    (client_code, definition_id, instance.period_start),
                )
                following = cur.fetchone()

        def _sibling(row) -> dict | None:
            if not row:
                return None
            return {
                "instance_ref": instance_ref(row["instance_id"]),
                "display_name": str(row["display_name"]),
                "period_key": str(row["period_key"]),
                "period_label": format_period_range(row["period_start"], row["period_end"]),
            }

        return _sibling(previous), _sibling(following)

    # -- delivery -----------------------------------------------------------

    def get_member_artifact(self, client_code: str, instance_id: str, member_id: str) -> dict | None:
        """The `artifacts` row behind one member, or ``None``.

        The chain is re-walked in ONE statement — client → instance → member →
        artifact — every single time bytes are served. A member id from another
        client's report joins to nothing; an artifact id from another client is
        never an input at all, because the artifact is reached only THROUGH the
        membership, and the extra `artifacts.client_code` equality makes even a
        mis-published membership unservable.
        """
        if not (_is_uuid(instance_id) and _is_uuid(member_id)):
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.member_id, f.display_filename, f.file_format, f.content_type,
                           f.size_bytes, f.is_previewable, f.is_available, f.semantic_role,
                           f.is_main_file, f.expires_at AS member_expires_at,
                           a.artifact_id, a.storage_backend, a.storage_key, a.filename,
                           a.display_filename AS artifact_display_filename,
                           a.content_type AS artifact_content_type,
                           a.expires_at, a.expired_at, a.client_code AS artifact_client_code
                      FROM portal_generated_report_files f
                      JOIN portal_generated_report_instances i ON i.instance_id = f.instance_id
                      LEFT JOIN artifacts a ON a.artifact_id = f.artifact_id
                     WHERE f.member_id = %s
                       AND f.instance_id = %s
                       AND i.client_code = %s
                       AND (a.artifact_id IS NULL OR a.client_code = i.client_code)
                     LIMIT 1
                    """,
                    (member_id, instance_id, client_code),
                )
                return cur.fetchone()

    # -- assembly -----------------------------------------------------------

    def _load_members(self, cur, instance_ids) -> dict[str, tuple[ReportFileView, ...]]:
        if not instance_ids:
            return {}
        cur.execute(
            """
            SELECT member_id, instance_id, display_filename, file_format, content_type,
                   size_bytes, is_previewable, semantic_role, is_main_file, is_available,
                   content_metric_kind, content_metric_value, expires_at, display_order
              FROM portal_generated_report_files
             WHERE instance_id = ANY(%s::uuid[])
             ORDER BY display_order ASC, display_filename ASC
            """,
            ([str(i) for i in instance_ids],),
        )
        grouped: dict[str, list[ReportFileView]] = {}
        for row in cur.fetchall():
            grouped.setdefault(str(row["instance_id"]), []).append(
                ReportFileView(
                    member_ref=str(row["member_id"]),
                    display_filename=str(row["display_filename"]),
                    file_format=str(row["file_format"]),
                    content_type=str(row["content_type"]),
                    size_bytes=int(row["size_bytes"]),
                    is_previewable=bool(row["is_previewable"]),
                    semantic_role=str(row["semantic_role"]),
                    is_main_file=bool(row["is_main_file"]),
                    is_available=bool(row["is_available"]),
                    content_metric_kind=row.get("content_metric_kind"),
                    content_metric_value=row.get("content_metric_value"),
                    expires_at=row.get("expires_at"),
                )
            )
        return {key: tuple(value) for key, value in grouped.items()}

    def _retention_until(self, row, members: tuple[ReportFileView, ...]) -> datetime | None:
        """`Retencja plików`: the latest member expiry, else the declared policy.

        A report whose members are gone still states a retention, because the
        metadata grid renders one for every instance (`docs/40` §11.1).
        """
        expiries = [m.expires_at for m in members if m.expires_at is not None]
        if expiries:
            return max(expiries)
        months = row.get("retention_months")
        anchor = row.get("generation_finished_at") or row.get("library_timestamp")
        if months and anchor:
            # Calendar months as a day count is deliberate: this is a rendered
            # horizon, not a boundary anything is compared against.
            return anchor + timedelta(days=int(months) * 30)
        return None

    def _instance_view(self, row, members, *, with_source: bool = False) -> ReportInstanceView:
        source = None
        if with_source:
            provenance = row.get("source_provenance_json") or {}
            if isinstance(provenance, str):
                import json as _json

                try:
                    provenance = _json.loads(provenance)
                except ValueError:
                    provenance = {}
            if provenance.get("dataset_slug"):
                source = ReportSourceBinding(
                    dataset_id=(str(row["source_dataset_id"]) if row.get("source_dataset_id") else None),
                    dataset_slug=str(provenance.get("dataset_slug") or ""),
                    dataset_name=str(provenance.get("dataset_name") or ""),
                    date_column=str(provenance.get("date_column") or ""),
                    applied_from=_as_date(provenance.get("applied_from")),
                    applied_to_exclusive=_as_date(provenance.get("applied_to_exclusive")),
                )
        return ReportInstanceView(
            instance_ref=instance_ref(row["instance_id"]),
            provider=PROVIDER_GENERATED,
            client_code=str(row["client_code"]),
            client_display_name=str(row.get("client_display_name") or row["client_code"]),
            type_key=str(row["type_key"]),
            type_label=str(row["type_label"]),
            type_description=str(row.get("type_description") or ""),
            cadence_class=str(row["cadence_class"]),
            cadence_detail=str(row.get("cadence_detail") or ""),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
            library_timestamp=row["library_timestamp"],
            generation_started_at=row.get("generation_started_at"),
            generation_finished_at=row.get("generation_finished_at"),
            period_key=str(row["period_key"]),
            period_start=row["period_start"],
            period_end=row["period_end"],
            period_kind=str(row["period_kind"]),
            row_count=(int(row["row_count"]) if row.get("row_count") is not None else None),
            retention_until=self._retention_until(row, members),
            safe_error_code=row.get("safe_error_code"),
            files=members,
            source=source,
            supports_periods=True,
            supports_history=True,
        )


def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
