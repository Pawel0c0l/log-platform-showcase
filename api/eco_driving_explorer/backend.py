"""Trusted, server-side resolution + platform I/O for the Eco Driving read API.

This module owns everything that touches the platform database and the client
business database, but it does not build any HTTP responses (that is the
service/http layer). It is constructed with narrow callbacks injected from
``api.main`` so no authentication, session, audit, or client-connection logic is
re-implemented here:

* ``db_conn``           -> platform (log DB) connection factory (context manager);
* ``connect_client_db`` -> ``_connect_portal_client_database`` (read-only, timeout);
* ``audit``             -> ``_portal_audit_event_safe``.

Client / provider resolution is strictly server-side and trusted:

    client_code
      -> authorized portal client (portal_clients, active)
      -> enabled control-plane record (workflow_a_control.client_account, enabled)
      -> verified platform client_id (UUID, from the control plane, never the request)
      -> configured client database (cross-checked against the portal mapping)
      -> registered provider factory (explicit allowlist, not table existence)

The request never supplies client_id, database name, schema, table, provider
class, or SQL.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .access import EcoDrivingClientAccess, merge_eco_access, no_eco_access
from .errors import (
    EnvironmentClientMismatchError,
    ProviderNotFoundError,
)
from .queries import ClientDatabaseReader, RowReader
from .registry import is_registered


@dataclass(frozen=True)
class ResolvedEcoClient:
    """Trusted binding produced entirely from server-side configuration."""

    client_code: str
    ranking_family: str
    client_id: str
    database_name: str


def _is_missing_eco_rbac(exc: Exception) -> bool:
    """True when an exception indicates the Eco RBAC columns/tables are absent.

    Detected via SQLSTATE (undefined_column 42703 / undefined_table 42P01,
    exposed as ``sqlstate`` on psycopg3 or ``pgcode`` on psycopg2), with a
    conservative message fallback scoped to the Eco columns only.
    """

    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if code in ("42703", "42P01"):
        return True
    text = str(exc).lower()
    return "can_view_eco" in text and ("does not exist" in text or "undefined" in text)


def assigned_id_digest(assigned_id: str) -> str:
    """Deterministic, non-reversible short digest of an opaque assigned_id.

    Used only for audit metadata so the raw identifier is never persisted.
    """

    return hashlib.sha256(str(assigned_id).encode("utf-8")).hexdigest()[:16]


_DIRECT_ACCESS_SQL = """
    SELECT bool_or(can_view_eco_ranking) AS can_view_eco_ranking,
           bool_or(can_view_eco_trip_details) AS can_view_eco_trip_details,
           bool_or(can_view_eco_trip_routes) AS can_view_eco_trip_routes
    FROM portal_user_clients
    WHERE user_id = %s AND client_code = %s
"""

_GROUP_ACCESS_SQL = """
    SELECT bool_or(pgc.can_view_eco_ranking) AS can_view_eco_ranking,
           bool_or(pgc.can_view_eco_trip_details) AS can_view_eco_trip_details,
           bool_or(pgc.can_view_eco_trip_routes) AS can_view_eco_trip_routes
    FROM portal_group_users pgu
    JOIN portal_groups pg ON pg.group_id = pgu.group_id AND pg.is_active IS TRUE
    JOIN portal_group_clients pgc ON pgc.group_id = pg.group_id
    WHERE pgu.user_id = %s AND pgc.client_code = %s
"""


class PortalEcoDrivingBackend:
    """Production backend backed by the platform + client databases."""

    def __init__(
        self,
        *,
        db_conn: Callable[[], Any],
        connect_client_db: Callable[..., Any],
        audit: Callable[..., None],
        statement_timeout_ms: int = 15000,
    ) -> None:
        self._db_conn = db_conn
        self._connect_client_db = connect_client_db
        self._audit = audit
        self._statement_timeout_ms = int(statement_timeout_ms)

    # -- effective access -----------------------------------------------------

    def fetch_access(self, user_id: str, client_code: str) -> EcoDrivingClientAccess:
        if not user_id or not client_code:
            return no_eco_access(client_code)
        try:
            with self._db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT is_active FROM portal_clients WHERE client_code = %s",
                        (client_code,),
                    )
                    client_row = cur.fetchone()
                    client_active = bool(client_row and client_row.get("is_active"))
                    cur.execute(_DIRECT_ACCESS_SQL, (user_id, client_code))
                    direct = cur.fetchone() or {}
                    cur.execute(_GROUP_ACCESS_SQL, (user_id, client_code))
                    group = cur.fetchone() or {}
        except Exception as exc:
            # If the Eco RBAC columns are not provisioned yet (migration 051 not
            # applied), fail closed to "no access" so the surrounding portal stays
            # healthy instead of erroring on every render. Any other error
            # propagates unchanged.
            if _is_missing_eco_rbac(exc):
                return no_eco_access(client_code)
            raise
        return merge_eco_access(
            client_code,
            direct=direct,
            group=group,
            client_is_active=client_active,
        )

    # -- trusted client + provider resolution ---------------------------------

    def resolve_binding(self, client_code: str, ranking_family: str) -> ResolvedEcoClient:
        code = str(client_code or "")
        family = str(ranking_family or "")
        if not code or not family:
            raise ProviderNotFoundError("unknown Eco Driving provider")

        with self._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_active, database_name FROM portal_clients WHERE client_code = %s",
                    (code,),
                )
                portal_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT client_id::text AS client_id, client_db_name
                    FROM workflow_a_control.client_account
                    WHERE client_code = %s AND enabled = TRUE
                    LIMIT 1
                    """,
                    (code,),
                )
                control_row = cur.fetchone()

        # Fail closed and do not disclose which of these is missing.
        if not portal_row or not portal_row.get("is_active"):
            raise ProviderNotFoundError("unknown Eco Driving provider")
        if not control_row or not control_row.get("client_id"):
            raise ProviderNotFoundError("unknown Eco Driving provider")
        if not is_registered(code, family):
            raise ProviderNotFoundError("unknown Eco Driving provider")

        portal_db = str(portal_row.get("database_name") or "").strip()
        control_db = str(control_row.get("client_db_name") or "").strip()
        if not portal_db or not control_db:
            raise EnvironmentClientMismatchError("client database mapping is incomplete")
        if portal_db.lower() != control_db.lower():
            # Portal mapping and control-plane config disagree: refuse to guess.
            raise EnvironmentClientMismatchError("client database mapping is inconsistent")

        return ResolvedEcoClient(
            client_code=code,
            ranking_family=family,
            client_id=str(control_row["client_id"]),
            database_name=portal_db,
        )

    # -- read-only client database reader -------------------------------------

    @contextmanager
    def open_reader(self, binding: ResolvedEcoClient) -> Iterator[RowReader]:
        conn = self._connect_client_db(binding.database_name)
        try:
            yield ClientDatabaseReader(conn, statement_timeout_ms=self._statement_timeout_ms)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- audit ----------------------------------------------------------------

    def record_audit(
        self,
        *,
        event_type: str,
        actor_user_id: str | None,
        client_code: str | None,
        request: Any,
        metadata: dict,
    ) -> None:
        self._audit(
            event_type=event_type,
            actor_user_id=actor_user_id,
            client_code=client_code,
            request=request,
            metadata=metadata,
        )
