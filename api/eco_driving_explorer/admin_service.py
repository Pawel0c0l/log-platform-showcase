"""Platform-database service for Eco Driving permission administration.

All writes use fixed SQL, one transaction, a subject-row lock, an optimistic
version token, and an audit insert in the same transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping

from .access import ECO_ACCESS_FLAGS
from .admin_models import (
    EcoAdminConflict,
    EcoAdminCsrfError,
    EcoAdminNotFound,
    EcoAdminRequestTooLarge,
    EcoAdminValidationError,
    EcoClientPermissionRow,
    EcoPermissionChange,
    EcoPermissionEditor,
    EcoPermissionState,
    EcoPermissionUpdateResult,
    InheritedEcoPermissions,
    ProviderAdminInfo,
    normalize_eco_permission_transition,
    normalize_eco_permissions,
    permission_state,
)
from .registry import available_provider_identities


MAX_ADMIN_CLIENT_ROWS = 500
MAX_ADMIN_FORM_FIELDS = MAX_ADMIN_CLIENT_ROWS * 4 + 2
MAX_ADMIN_FIELD_NAME_LENGTH = 160
MAX_ADMIN_FIELD_VALUE_LENGTH = 2048
MAX_ADMIN_FORM_BODY_BYTES = 256 * 1024
AUDIT_CHANGED_CLIENT_LIMIT = 50
ADMIN_STATEMENT_TIMEOUT_MS = 15_000
CSRF_TTL_SECONDS = 30 * 60
_FIELD_RE = re.compile(
    r"^clients\[([^\]]+)\]\[(can_view_eco_ranking|can_view_eco_trip_details|can_view_eco_trip_routes)\]$"
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class EcoAdminCsrfProtector:
    """Short-lived HMAC token bound to administrator identity and action."""

    def __init__(
        self,
        secret_supplier: Callable[[], str],
        *,
        ttl_seconds: int = CSRF_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret_supplier = secret_supplier
        self._ttl_seconds = int(ttl_seconds)
        self._clock = clock

    def issue(self, actor_user_id: str, scope: str) -> str:
        now = int(self._clock())
        payload = {
            "actor": str(actor_user_id),
            "scope": str(scope),
            "iat": now,
            "exp": now + self._ttl_seconds,
        }
        encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = hmac.new(
            self._secret_supplier().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}"

    def validate(self, token: str, actor_user_id: str, scope: str) -> None:
        try:
            encoded, signature = str(token or "").rsplit(".", 1)
            expected = hmac.new(
                self._secret_supplier().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(_b64decode(encoded).decode("utf-8"))
            now = int(self._clock())
            if (
                str(payload.get("actor") or "") != str(actor_user_id)
                or str(payload.get("scope") or "") != str(scope)
                or int(payload.get("iat") or 0) > now + 60
                or int(payload.get("exp") or 0) < now
            ):
                raise ValueError
        except Exception as exc:
            raise EcoAdminCsrfError() from exc


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EcoAdminNotFound() from exc


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
    return dict(zip(columns, value))


class EcoDrivingPermissionAdminService:
    @staticmethod
    def _configure_transaction(cur: Any) -> None:
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(ADMIN_STATEMENT_TIMEOUT_MS),))

    def __init__(
        self,
        *,
        db_conn: Callable[[], Any],
        csrf: EcoAdminCsrfProtector,
        audit_sanitizer: Callable[[Any], Any],
    ) -> None:
        self._db_conn = db_conn
        self.csrf = csrf
        self._audit_sanitizer = audit_sanitizer

    @staticmethod
    def csrf_scope(subject_type: str, subject_id: str) -> str:
        return f"eco-driving-permissions:{subject_type}:{subject_id}"

    @staticmethod
    def parse_form(pairs: Iterable[tuple[str, object]]) -> tuple[str, str, dict[str, EcoPermissionState], bool]:
        csrf_tokens: list[str] = []
        version_tokens: list[str] = []
        clients: list[str] = []
        raw_flags: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        pair_count = 0
        for key_value, raw_value in pairs:
            pair_count += 1
            if pair_count > MAX_ADMIN_FORM_FIELDS:
                raise EcoAdminRequestTooLarge()
            key, value = str(key_value), str(raw_value)
            if len(key) > MAX_ADMIN_FIELD_NAME_LENGTH or len(value) > MAX_ADMIN_FIELD_VALUE_LENGTH:
                raise EcoAdminRequestTooLarge()
            if key == "csrf_token":
                csrf_tokens.append(value)
            elif key == "version_token":
                version_tokens.append(value)
            elif key == "client_code":
                clients.append(value)
            else:
                match = _FIELD_RE.fullmatch(key)
                if not match:
                    raise EcoAdminValidationError("The form contains an unsupported permission field.")
                client_code, flag = match.groups()
                raw_flags[client_code][flag].append(value)

        if len(csrf_tokens) != 1:
            raise EcoAdminCsrfError()
        if len(version_tokens) != 1:
            raise EcoAdminValidationError("The form contains an invalid version token field.")
        if len(clients) > MAX_ADMIN_CLIENT_ROWS:
            raise EcoAdminValidationError("The submitted permission form contains too many clients.")
        if len(set(clients)) != len(clients):
            raise EcoAdminValidationError("The submitted permission form contains a duplicate client.")
        if set(raw_flags) - set(clients):
            raise EcoAdminValidationError("The submitted permission form contains an unknown client row.")

        states: dict[str, EcoPermissionState] = {}
        normalized_any = False
        for client_code in clients:
            values: dict[str, bool] = {}
            for flag in ECO_ACCESS_FLAGS:
                submitted = raw_flags.get(client_code, {}).get(flag, [])
                if len(submitted) > 1 or (submitted and submitted[0] != "true"):
                    raise EcoAdminValidationError("A permission value is malformed.")
                values[flag] = bool(submitted)
            normalized = normalize_eco_permissions(**values)
            states[client_code] = EcoPermissionState(**values)
            normalized_any = normalized_any or normalized.changed
        return csrf_tokens[0], version_tokens[0], states, normalized_any

    def dashboard(self, *, page: int = 1, limit: int = 50) -> dict[str, Any]:
        if page < 1 or limit < 1 or limit > 200:
            raise EcoAdminValidationError("Invalid admin pagination.")
        offset = (page - 1) * limit
        with self._db_conn() as conn:
            with conn.cursor() as cur:
                self._configure_transaction(cur)
                cur.execute("SELECT count(*) AS count FROM artifact_users")
                users_total = int((_row(cur) or {}).get("count") or 0)
                cur.execute(
                    """
                    SELECT u.user_id::text, u.username, u.display_name, u.is_active, u.is_admin,
                      (SELECT count(*) FROM portal_user_clients puc
                       WHERE puc.user_id=u.user_id AND (puc.can_view_eco_ranking OR puc.can_view_eco_trip_details OR puc.can_view_eco_trip_routes)) AS direct_count,
                      (SELECT count(DISTINCT pgc.client_code) FROM portal_group_users pgu
                       JOIN portal_groups pg ON pg.group_id=pgu.group_id AND pg.is_active IS TRUE
                       JOIN portal_group_clients pgc ON pgc.group_id=pg.group_id
                       WHERE pgu.user_id=u.user_id AND (pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes)) AS inherited_count,
                      (SELECT count(*) FROM portal_clients pc WHERE pc.is_active IS TRUE AND (
                         EXISTS (SELECT 1 FROM portal_user_clients puc WHERE puc.user_id=u.user_id AND puc.client_code=pc.client_code AND (puc.can_view_eco_ranking OR puc.can_view_eco_trip_details OR puc.can_view_eco_trip_routes))
                         OR EXISTS (SELECT 1 FROM portal_group_users pgu JOIN portal_groups pg ON pg.group_id=pgu.group_id AND pg.is_active IS TRUE JOIN portal_group_clients pgc ON pgc.group_id=pg.group_id WHERE pgu.user_id=u.user_id AND pgc.client_code=pc.client_code AND (pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes)))) AS effective_count
                    FROM artifact_users u ORDER BY lower(u.username), u.user_id LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                users = _rows(cur)
                cur.execute("SELECT count(*) AS count FROM portal_groups")
                groups_total = int((_row(cur) or {}).get("count") or 0)
                cur.execute(
                    """
                    SELECT pg.group_id::text, pg.group_name, pg.is_active,
                      count(DISTINCT au.user_id) FILTER (WHERE au.is_active IS TRUE) AS active_members,
                      count(DISTINCT pgc.client_code) FILTER (WHERE pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes) AS client_grants
                    FROM portal_groups pg
                    LEFT JOIN portal_group_users pgu ON pgu.group_id=pg.group_id
                    LEFT JOIN artifact_users au ON au.user_id=pgu.user_id
                    LEFT JOIN portal_group_clients pgc ON pgc.group_id=pg.group_id
                    GROUP BY pg.group_id ORDER BY pg.is_active DESC, lower(pg.group_name), pg.group_id LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                groups = _rows(cur)
                cur.execute("SELECT count(*) AS count FROM portal_clients WHERE is_active IS TRUE")
                clients_total = int((_row(cur) or {}).get("count") or 0)
                cur.execute(
                    """
                    SELECT pc.client_code, pc.display_name,
                      (SELECT count(*) FROM portal_user_clients puc
                       WHERE puc.client_code=pc.client_code
                         AND (puc.can_view_eco_ranking OR puc.can_view_eco_trip_details OR puc.can_view_eco_trip_routes)) AS direct_user_grants,
                      (SELECT count(*) FROM portal_group_clients pgc
                       WHERE pgc.client_code=pc.client_code
                         AND (pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes)) AS group_grants,
                      (SELECT count(*) FROM artifact_users u WHERE u.is_active IS TRUE AND (
                         EXISTS (SELECT 1 FROM portal_user_clients puc
                                 WHERE puc.user_id=u.user_id AND puc.client_code=pc.client_code
                                   AND (puc.can_view_eco_ranking OR puc.can_view_eco_trip_details OR puc.can_view_eco_trip_routes))
                         OR EXISTS (SELECT 1 FROM portal_group_users pgu
                                    JOIN portal_groups pg ON pg.group_id=pgu.group_id AND pg.is_active IS TRUE
                                    JOIN portal_group_clients pgc ON pgc.group_id=pg.group_id
                                    WHERE pgu.user_id=u.user_id AND pgc.client_code=pc.client_code
                                      AND (pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes)))) AS effective_users
                    FROM portal_clients pc
                    WHERE pc.is_active IS TRUE
                    ORDER BY lower(pc.display_name), pc.client_code
                    LIMIT %s
                    """,
                    (MAX_ADMIN_CLIENT_ROWS,),
                )
                clients = _rows(cur)
        provider_map = self._provider_map()
        for client in clients:
            client["providers"] = provider_map.get(str(client["client_code"]), ())
        return {
            "users": users,
            "groups": groups,
            "clients": clients,
            "users_total": users_total,
            "groups_total": groups_total,
            "clients_total": clients_total,
            "page": page,
            "limit": limit,
        }

    def user_editor(self, user_id: str) -> EcoPermissionEditor:
        return self._editor("user", _uuid(user_id))

    def group_editor(self, group_id: str) -> EcoPermissionEditor:
        return self._editor("group", _uuid(group_id))

    def _editor(self, subject_type: str, subject_id: str) -> EcoPermissionEditor:
        with self._db_conn() as conn:
            with conn.cursor() as cur:
                self._configure_transaction(cur)
                subject = self._fetch_subject(cur, subject_type, subject_id, lock=False)
                clients = self._fetch_direct_clients(cur, subject_type, subject_id, lock=False)
                inherited = self._fetch_inherited(cur, subject_id) if subject_type == "user" else {}
        return self._build_editor(subject_type, subject, clients, inherited)

    def _fetch_subject(self, cur: Any, subject_type: str, subject_id: str, *, lock: bool) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        if subject_type == "user":
            cur.execute(
                "SELECT user_id::text AS subject_id, username AS subject_name, display_name, is_active FROM artifact_users WHERE user_id=%s" + suffix,
                (subject_id,),
            )
        else:
            cur.execute(
                """SELECT pg.group_id::text AS subject_id, pg.group_name AS subject_name, NULL::text AS display_name, pg.is_active,
                   (SELECT count(*) FROM portal_group_users pgu JOIN artifact_users au ON au.user_id=pgu.user_id WHERE pgu.group_id=pg.group_id AND au.is_active IS TRUE) AS active_member_count
                   FROM portal_groups pg WHERE pg.group_id=%s""" + suffix,
                (subject_id,),
            )
        row = _row(cur)
        if not row:
            raise EcoAdminNotFound()
        return row

    def _fetch_direct_clients(self, cur: Any, subject_type: str, subject_id: str, *, lock: bool) -> list[dict[str, Any]]:
        table = "portal_user_clients" if subject_type == "user" else "portal_group_clients"
        id_col = "user_id" if subject_type == "user" else "group_id"
        if lock:
            cur.execute(
                f"SELECT client_code FROM {table} WHERE {id_col}=%s FOR UPDATE",
                (subject_id,),
            )
            cur.fetchall()
            cur.execute("SELECT client_code FROM portal_clients WHERE is_active IS TRUE ORDER BY client_code FOR SHARE")
            cur.fetchall()
        cur.execute(
            f"""SELECT pc.client_code, pc.display_name,
                COALESCE(g.can_view_eco_ranking,FALSE) AS can_view_eco_ranking,
                COALESCE(g.can_view_eco_trip_details,FALSE) AS can_view_eco_trip_details,
                COALESCE(g.can_view_eco_trip_routes,FALSE) AS can_view_eco_trip_routes,
                (g.client_code IS NOT NULL) AS row_exists
                FROM portal_clients pc LEFT JOIN {table} g ON g.client_code=pc.client_code AND g.{id_col}=%s
                WHERE pc.is_active IS TRUE ORDER BY lower(pc.display_name), pc.client_code""",
            (subject_id,),
        )
        return _rows(cur)

    def _fetch_inherited(self, cur: Any, user_id: str) -> dict[str, list[dict[str, Any]]]:
        cur.execute(
            """SELECT pgc.client_code, pg.group_name, pgc.can_view_eco_ranking,
                      pgc.can_view_eco_trip_details, pgc.can_view_eco_trip_routes
               FROM portal_group_users pgu
               JOIN portal_groups pg ON pg.group_id=pgu.group_id AND pg.is_active IS TRUE
               JOIN portal_group_clients pgc ON pgc.group_id=pg.group_id
               WHERE pgu.user_id=%s AND (pgc.can_view_eco_ranking OR pgc.can_view_eco_trip_details OR pgc.can_view_eco_trip_routes)
               ORDER BY lower(pg.group_name), pg.group_id""",
            (user_id,),
        )
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in _rows(cur):
            result[str(row["client_code"])].append(row)
        return result

    def _provider_map(self) -> dict[str, tuple[ProviderAdminInfo, ...]]:
        values: dict[str, list[ProviderAdminInfo]] = defaultdict(list)
        for identity in available_provider_identities():
            values[identity.client_code].append(ProviderAdminInfo(identity.ranking_family, identity.display_name))
        return {key: tuple(items) for key, items in values.items()}

    def _build_editor(self, subject_type: str, subject: dict[str, Any], clients: list[dict[str, Any]], inherited_rows: dict[str, list[dict[str, Any]]]) -> EcoPermissionEditor:
        provider_map = self._provider_map()
        rendered: list[EcoClientPermissionRow] = []
        for client in clients:
            code = str(client["client_code"])
            direct = permission_state(client)
            sources = inherited_rows.get(code, [])
            names = {
                flag: tuple(str(row["group_name"]) for row in sources if bool(row.get(flag)))
                for flag in ECO_ACCESS_FLAGS
            }
            inherited_state = EcoPermissionState(*(bool(names[flag]) for flag in ECO_ACCESS_FLAGS))
            inherited = InheritedEcoPermissions(inherited_state, names[ECO_ACCESS_FLAGS[0]], names[ECO_ACCESS_FLAGS[1]], names[ECO_ACCESS_FLAGS[2]])
            effective = EcoPermissionState(*(bool(getattr(direct, flag) or getattr(inherited_state, flag)) for flag in ECO_ACCESS_FLAGS))
            rendered.append(EcoClientPermissionRow(code, str(client.get("display_name") or code), direct, inherited, effective, provider_map.get(code, ())))
        version = self._version(subject_type, str(subject["subject_id"]), clients)
        name = str(subject.get("display_name") or subject.get("subject_name") or "User or group")
        return EcoPermissionEditor(subject_type, str(subject["subject_id"]), name, bool(subject.get("is_active")), tuple(rendered), version, int(subject.get("active_member_count") or 0))

    @staticmethod
    def _version(subject_type: str, subject_id: str, clients: list[dict[str, Any]]) -> str:
        canonical = [
            [str(row["client_code"]), *[bool(row.get(flag)) for flag in ECO_ACCESS_FLAGS]]
            for row in clients
        ]
        value = json.dumps([subject_type, subject_id, canonical], separators=(",", ":"), sort_keys=False)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def update_user(self, user_id: str, *, actor_user_id: str, pairs: Iterable[tuple[str, object]], ip_address: str | None, user_agent: str | None) -> EcoPermissionUpdateResult:
        return self._update("user", _uuid(user_id), actor_user_id, pairs, ip_address, user_agent)

    def update_group(self, group_id: str, *, actor_user_id: str, pairs: Iterable[tuple[str, object]], ip_address: str | None, user_agent: str | None) -> EcoPermissionUpdateResult:
        return self._update("group", _uuid(group_id), actor_user_id, pairs, ip_address, user_agent)

    def _update(self, subject_type: str, subject_id: str, actor_user_id: str, pairs: Iterable[tuple[str, object]], ip_address: str | None, user_agent: str | None) -> EcoPermissionUpdateResult:
        csrf_token, submitted_version, submitted, normalized_any = self.parse_form(pairs)
        self.csrf.validate(csrf_token, actor_user_id, self.csrf_scope(subject_type, subject_id))
        with self._db_conn() as conn:
            with conn.cursor() as cur:
                self._configure_transaction(cur)
                subject = self._fetch_subject(cur, subject_type, subject_id, lock=True)
                current = self._fetch_direct_clients(cur, subject_type, subject_id, lock=True)
                current_codes = [str(row["client_code"]) for row in current]
                if set(submitted) != set(current_codes) or len(submitted) != len(current_codes):
                    raise EcoAdminValidationError("The client list changed or contains an unknown client. Reload the page.")
                if not hmac.compare_digest(submitted_version, self._version(subject_type, subject_id, current)):
                    raise EcoAdminConflict()

                changes: list[EcoPermissionChange] = []
                revoke_cascade_any = False
                for row in current:
                    code = str(row["client_code"])
                    before = permission_state(row)
                    requested = submitted[code]
                    transition = normalize_eco_permission_transition(before, requested)
                    after = transition.value
                    normalized_any = normalized_any or transition.changed
                    revoke_cascade_any = revoke_cascade_any or transition.revoke_cascade
                    if before != after:
                        changes.append(EcoPermissionChange(
                            code,
                            before,
                            requested,
                            after,
                            transition.changed,
                            transition.revoke_cascade,
                        ))
                        self._write_flags(cur, subject_type, subject_id, code, after, bool(row.get("row_exists")), actor_user_id)

                active_members = int(subject.get("active_member_count") or 0) if subject_type == "group" else 0
                result = EcoPermissionUpdateResult(
                    subject_type,
                    subject_id,
                    len(submitted),
                    tuple(changes),
                    normalized_any,
                    revoke_cascade_any,
                    active_members,
                )
                if changes:
                    self._insert_audit(cur, actor_user_id, result, ip_address, user_agent)
        return result

    @staticmethod
    def _write_flags(cur: Any, subject_type: str, subject_id: str, client_code: str, value: EcoPermissionState, exists: bool, actor_user_id: str) -> None:
        table = "portal_user_clients" if subject_type == "user" else "portal_group_clients"
        id_col = "user_id" if subject_type == "user" else "group_id"
        flags = tuple(value.as_dict()[name] for name in ECO_ACCESS_FLAGS)
        if exists:
            cur.execute(
                f"UPDATE {table} SET can_view_eco_ranking=%s, can_view_eco_trip_details=%s, can_view_eco_trip_routes=%s, updated_at=now() WHERE {id_col}=%s AND client_code=%s",
                (*flags, subject_id, client_code),
            )
        elif value.any:
            # Historical defaults for Database/Reports are TRUE, so explicitly
            # create an Eco-only row with all unrelated permissions disabled.
            cur.execute(
                f"""INSERT INTO {table}
                    ({id_col}, client_code, can_view_database, can_view_reports, can_export_database,
                     can_view_eco_ranking, can_view_eco_trip_details, can_view_eco_trip_routes, granted_by)
                    VALUES (%s,%s,FALSE,FALSE,FALSE,%s,%s,%s,%s)""",
                (subject_id, client_code, *flags, actor_user_id),
            )

    def _insert_audit(self, cur: Any, actor_user_id: str, result: EcoPermissionUpdateResult, ip_address: str | None, user_agent: str | None) -> None:
        changes = [
            {
                "client_code": item.client_code,
                "before": item.before.as_dict(),
                "submitted": item.submitted.as_dict(),
                "normalized_after": item.after.as_dict(),
                "dependency_normalized": item.dependency_normalized,
                "revoke_cascade": item.revoke_cascade,
            }
            for item in result.changes[:AUDIT_CHANGED_CLIENT_LIMIT]
        ]
        metadata = self._audit_sanitizer({
            "subject_type": result.subject_type,
            "subject_id": result.subject_id,
            "clients_submitted": result.submitted_count,
            "clients_changed": len(result.changes),
            "changed_client_codes": [item.client_code for item in result.changes[:AUDIT_CHANGED_CLIENT_LIMIT]],
            "changes_truncated": len(result.changes) > AUDIT_CHANGED_CLIENT_LIMIT,
            "changes": changes,
            "dependency_normalized": result.dependency_normalized,
            "revoke_cascade": result.revoke_cascade,
            "affected_active_member_count": result.affected_active_member_count if result.subject_type == "group" else None,
            "request_result": "updated",
        })
        cur.execute(
            """INSERT INTO portal_audit_events
               (event_type, actor_user_id, ip_address, user_agent, metadata_json)
               VALUES (%s,%s,%s,%s,%s::jsonb)""",
            (
                "eco_driving_user_permissions_updated" if result.subject_type == "user" else "eco_driving_group_permissions_updated",
                actor_user_id,
                ip_address,
                user_agent,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
