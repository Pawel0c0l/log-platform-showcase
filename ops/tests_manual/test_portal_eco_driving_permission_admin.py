#!/usr/bin/env python3
"""Focused Stage 6 tests for Eco Driving permission administration.

Pure validation/CSRF/route checks always run. Transactional checks use a fresh
throwaway PostgreSQL database and are skipped when local Docker PostgreSQL is
unavailable. The active platform database is never migrated or written.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.eco_driving_explorer.admin_models import (
    EcoAdminConflict,
    EcoAdminCsrfError,
    EcoAdminRequestTooLarge,
    EcoAdminValidationError,
    EcoPermissionState,
    normalize_eco_permission_transition,
    normalize_eco_permissions,
)
from api.eco_driving_explorer.admin_pages import EcoDrivingPermissionAdminPages

# The repository manual-test interpreter may not include the API container FastAPI dependency.
if "fastapi" not in sys.modules:
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        fastapi_stub = types.ModuleType("fastapi")
        fastapi_stub.Request = type("Request", (), {})
        sys.modules["fastapi"] = fastapi_stub
from api.eco_driving_explorer.admin_routes import register_eco_driving_admin_routes
from api.eco_driving_explorer.admin_service import (
    EcoAdminCsrfProtector,
    EcoDrivingPermissionAdminService,
    MAX_ADMIN_CLIENT_ROWS,
    MAX_ADMIN_FIELD_NAME_LENGTH,
    MAX_ADMIN_FIELD_VALUE_LENGTH,
    MAX_ADMIN_FORM_BODY_BYTES,
    MAX_ADMIN_FORM_FIELDS,
)


ADMIN = "10000000-0000-0000-0000-000000000001"
USER = "10000000-0000-0000-0000-000000000002"
GROUP = "20000000-0000-0000-0000-000000000001"


def test_normalization_contract() -> None:
    expected = {
        (0, 0, 0): (0, 0, 0), (1, 0, 0): (1, 0, 0),
        (1, 1, 0): (1, 1, 0), (1, 1, 1): (1, 1, 1),
        (0, 1, 0): (1, 1, 0), (0, 0, 1): (1, 1, 1),
        (1, 0, 1): (1, 1, 1), (0, 1, 1): (1, 1, 1),
    }
    for raw, result in expected.items():
        normalized = normalize_eco_permissions(*map(bool, raw))
        assert tuple(map(int, normalized.value.as_dict().values())) == result
        assert normalized.changed is (raw != result)
    print("PASS: all valid and invalid permission states normalize consistently")


def test_stateful_transition_contract() -> None:
    cases = (
        ((0, 0, 0), (0, 1, 0), (1, 1, 0), False),
        ((0, 0, 0), (0, 0, 1), (1, 1, 1), False),
        ((0, 0, 0), (1, 0, 1), (1, 1, 1), False),
        ((0, 0, 0), (0, 1, 1), (1, 1, 1), False),
        ((1, 1, 1), (0, 1, 1), (0, 0, 0), True),
        ((1, 1, 1), (1, 0, 1), (1, 0, 0), True),
        ((1, 1, 1), (1, 1, 0), (1, 1, 0), False),
        ((1, 1, 1), (1, 0, 0), (1, 0, 0), True),
        ((1, 1, 1), (0, 0, 0), (0, 0, 0), True),
        ((1, 1, 0), (0, 1, 0), (0, 0, 0), True),
        ((1, 1, 0), (1, 0, 0), (1, 0, 0), True),
        ((1, 0, 0), (1, 1, 0), (1, 1, 0), False),
        ((1, 0, 0), (1, 1, 1), (1, 1, 1), False),
    )
    valid = {(0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 1, 1)}
    for before_raw, submitted_raw, expected_raw, cascaded in cases:
        before = EcoPermissionState(*map(bool, before_raw))
        submitted = EcoPermissionState(*map(bool, submitted_raw))
        transition = normalize_eco_permission_transition(before, submitted)
        actual = tuple(map(int, transition.value.as_dict().values()))
        assert actual == expected_raw and actual in valid
        assert transition.revoke_cascade is cascaded
    print("PASS: state-aware grant promotion and revoke cascades cover every required transition")


def test_csrf_contract() -> None:
    now = [1_700_000_000]
    protector = EcoAdminCsrfProtector(lambda: "test-session-secret", ttl_seconds=60, clock=lambda: now[0])
    token = protector.issue(ADMIN, f"eco:user:{USER}")
    protector.validate(token, ADMIN, f"eco:user:{USER}")
    for actor, scope, candidate in (
        (USER, f"eco:user:{USER}", token),
        (ADMIN, f"eco:group:{GROUP}", token),
        (ADMIN, f"eco:user:{USER}", token + "x"),
        (ADMIN, f"eco:user:{USER}", "malformed"),
    ):
        try:
            protector.validate(candidate, actor, scope)
        except EcoAdminCsrfError:
            pass
        else:
            raise AssertionError("invalid CSRF token accepted")
    now[0] += 61
    try:
        protector.validate(token, ADMIN, f"eco:user:{USER}")
    except EcoAdminCsrfError:
        pass
    else:
        raise AssertionError("expired CSRF token accepted")
    assert "test-session-secret" not in token and ADMIN not in token
    print("PASS: CSRF is signed, scoped, actor-bound and expiring")


class _FakeApp:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []

    def get(self, path: str, **kwargs):
        return self._register("GET", path)

    def post(self, path: str, **kwargs):
        return self._register("POST", path)

    def _register(self, method: str, path: str):
        def decorator(handler):
            self.routes.append((method, path, handler))
            return handler
        return decorator


class _NoopPages:
    service = object()


def test_route_registration() -> None:
    app = _FakeApp()
    register_eco_driving_admin_routes(
        app, pages=_NoopPages(), require_admin=lambda request: {},
        render=lambda result, user: result, redirect=lambda location: location,
    )
    expected = {
        ("GET", "/admin/client-access/eco-driving"),
        ("GET", "/admin/client-access/eco-driving/users/{user_id}"),
        ("POST", "/admin/client-access/eco-driving/users/{user_id}"),
        ("GET", "/admin/client-access/eco-driving/groups/{group_id}"),
        ("POST", "/admin/client-access/eco-driving/groups/{group_id}"),
    }
    actual = [(method, path) for method, path, _ in app.routes]
    assert set(actual) == expected and len(actual) == len(expected)
    assert all(actual.count(item) == 1 for item in expected)
    assert all("email" not in path and "client" not in path.rsplit("/", 1)[-1] for _, path in actual)
    print("PASS: Stage 6 admin routes are registered exactly once")


def test_form_allowlist_and_duplicates() -> None:
    base = [("csrf_token", "x"), ("version_token", "y"), ("client_code", "ALPHA00001")]
    valid = base + [("clients[ALPHA00001][can_view_eco_trip_routes]", "true")]
    _, _, states, normalized = EcoDrivingPermissionAdminService.parse_form(valid)
    assert states["ALPHA00001"] == EcoPermissionState(False, False, True) and normalized
    invalid = (
        base + [("client_code", "ALPHA00001")],
        base + [("clients[UNKNOWN][can_view_eco_ranking]", "true")],
        base + [("clients[ALPHA00001][can_view_database]", "true")],
        base + [("clients[ALPHA00001][can_view_eco_ranking]", "1")],
        base + [("clients[ALPHA00001][can_view_eco_ranking]", "true"), ("clients[ALPHA00001][can_view_eco_ranking]", "true")],
        base + [("user_id", USER)],
        base + [("client_id", USER)],
        base + [("database_name", "other")],
    )
    for pairs in invalid:
        try:
            EcoDrivingPermissionAdminService.parse_form(pairs)
        except (EcoAdminValidationError, EcoAdminCsrfError):
            pass
        else:
            raise AssertionError("unsupported form input accepted")
    oversized = (
        [("csrf_token", "x"), ("version_token", "y")]
        + [("client_code", f"C{i:04d}") for i in range(MAX_ADMIN_CLIENT_ROWS + 1)]
    )
    for pairs in (
        oversized,
        base + [("x" * (MAX_ADMIN_FIELD_NAME_LENGTH + 1), "true")],
        base + [("clients[ALPHA00001][can_view_eco_ranking]", "x" * (MAX_ADMIN_FIELD_VALUE_LENGTH + 1))],
        [("csrf_token", "x"), ("version_token", "y")] + [(f"x{i}", "v") for i in range(MAX_ADMIN_FORM_FIELDS + 1)],
    ):
        try:
            EcoDrivingPermissionAdminService.parse_form(pairs)
        except (EcoAdminRequestTooLarge, EcoAdminValidationError):
            pass
        else:
            raise AssertionError("oversized permission form accepted")
    print("PASS: form parser rejects duplicates, unknown clients, malformed fields and bounded-limit violations")


def _docker_container() -> str | None:
    try:
        out = subprocess.run(["docker", "compose", "ps", "-q", "postgres"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return (out.stdout or "").strip() or None


class _Psql:
    def __init__(self, cid: str, user: str) -> None:
        self.cid, self.user = cid, user

    def run(self, database: str, sql: str, *, tuples: bool = False) -> str:
        args = ["docker", "exec", "-i", self.cid, "psql", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", database]
        if tuples:
            args.append("-tA")
        proc = subprocess.run(args + ["-c", sql], capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise RuntimeError("isolated psql command failed")
        return proc.stdout

    def stdin(self, database: str, sql: str) -> None:
        args = ["docker", "exec", "-i", self.cid, "psql", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", database]
        proc = subprocess.run(args, input=sql, capture_output=True, text=True, timeout=120)
        if proc.returncode:
            raise RuntimeError("isolated psql script failed")


_SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE artifact_users (user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, display_name TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE, is_admin BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE portal_clients (client_code TEXT PRIMARY KEY, display_name TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE, description TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE portal_user_clients (user_id UUID NOT NULL REFERENCES artifact_users(user_id), client_code TEXT NOT NULL REFERENCES portal_clients(client_code), can_view_database BOOLEAN NOT NULL DEFAULT TRUE, can_view_reports BOOLEAN NOT NULL DEFAULT TRUE, can_export_database BOOLEAN NOT NULL DEFAULT FALSE, granted_by UUID REFERENCES artifact_users(user_id), granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(user_id,client_code));
CREATE TABLE portal_groups (group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), group_name TEXT NOT NULL UNIQUE, description TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by UUID REFERENCES artifact_users(user_id), updated_by UUID REFERENCES artifact_users(user_id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE portal_group_users (group_id UUID NOT NULL REFERENCES portal_groups(group_id), user_id UUID NOT NULL REFERENCES artifact_users(user_id), granted_by UUID REFERENCES artifact_users(user_id), granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(group_id,user_id));
CREATE TABLE portal_group_clients (group_id UUID NOT NULL REFERENCES portal_groups(group_id), client_code TEXT NOT NULL REFERENCES portal_clients(client_code), can_view_database BOOLEAN NOT NULL DEFAULT TRUE, can_view_reports BOOLEAN NOT NULL DEFAULT TRUE, can_export_database BOOLEAN NOT NULL DEFAULT FALSE, granted_by UUID REFERENCES artifact_users(user_id), granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(group_id,client_code));
CREATE TABLE portal_audit_events (audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), event_type TEXT NOT NULL, actor_user_id UUID REFERENCES artifact_users(user_id), client_code TEXT REFERENCES portal_clients(client_code), dataset_id UUID, report_folder_id UUID, artifact_id UUID, ip_address TEXT, user_agent TEXT, metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


def _form(service: EcoDrivingPermissionAdminService, editor, values: dict[str, EcoPermissionState], actor: str) -> list[tuple[str, str]]:
    pairs = [
        ("csrf_token", service.csrf.issue(actor, service.csrf_scope(editor.subject_type, editor.subject_id))),
        ("version_token", editor.version_token),
    ]
    for row in editor.clients:
        pairs.append(("client_code", row.client_code))
        state = values[row.client_code]
        for flag, enabled in state.as_dict().items():
            if enabled:
                pairs.append((f"clients[{row.client_code}][{flag}]", "true"))
    return pairs


def test_isolated_transactional_writes() -> None:
    cid = _docker_container()
    if not cid:
        print("SKIP: Docker PostgreSQL unavailable; isolated Stage 6 write test skipped")
        return
    try:
        from dotenv import load_dotenv
        import psycopg
        from psycopg.rows import dict_row
    except Exception:
        print("SKIP: psycopg/dotenv unavailable; isolated Stage 6 write test skipped")
        return
    load_dotenv(ROOT / ".env")
    db_user = os.getenv("POSTGRES_USER", "loguser")
    admin_db = os.getenv("POSTGRES_DB", "logdb")
    password = os.getenv("POSTGRES_PASSWORD", "logpass")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    if host in {"postgres", "db"}:
        host = "127.0.0.1"
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    psql = _Psql(cid, db_user)
    database = f"eco_permission_admin_test_{os.getpid()}"
    psql.run(admin_db, f"DROP DATABASE IF EXISTS {database}")
    psql.run(admin_db, f"CREATE DATABASE {database}")
    try:
        psql.stdin(database, _SCHEMA)
        psql.stdin(database, (ROOT / "db/migrations/051_portal_eco_driving_permissions.sql").read_text())
        psql.stdin(database, f"""
          INSERT INTO artifact_users(user_id,username,password_hash,display_name,is_admin) VALUES
            ('{ADMIN}','admin','x','Administrator',TRUE),
            ('{USER}','malicious-user','x','<script>alert(1)</script>',FALSE);
          INSERT INTO portal_clients(client_code,display_name) VALUES ('ALPHA00001','\"><img src=x onerror=alert(1)>'),('SAFE0002','Second client');
          INSERT INTO portal_groups(group_id,group_name,is_active) VALUES ('{GROUP}','<script>group</script>',TRUE);
          INSERT INTO portal_group_users(group_id,user_id,granted_by) VALUES ('{GROUP}','{USER}','{ADMIN}');
          INSERT INTO portal_user_clients(user_id,client_code,can_view_database,can_view_reports,can_export_database,can_view_eco_ranking,granted_by)
            VALUES ('{USER}','ALPHA00001',TRUE,FALSE,TRUE,TRUE,'{ADMIN}');
          INSERT INTO portal_group_clients(group_id,client_code,can_view_database,can_view_reports,can_export_database,can_view_eco_ranking,can_view_eco_trip_details,granted_by)
            VALUES ('{GROUP}','ALPHA00001',FALSE,TRUE,FALSE,TRUE,TRUE,'{ADMIN}');
        """)

        def db_conn():
            return psycopg.connect(dbname=database, user=db_user, password=password, host=host, port=port, row_factory=dict_row)

        service = EcoDrivingPermissionAdminService(
            db_conn=db_conn,
            csrf=EcoAdminCsrfProtector(lambda: "isolated-test-secret"),
            audit_sanitizer=lambda value: value,
        )
        landing_html = EcoDrivingPermissionAdminPages(service).landing().body_html
        for heading in ("Users", "Groups", "Configured Eco Driving clients"):
            assert f">{heading}<" in landing_html
        assert "ALPHA00001" in landing_html and "Registered" in landing_html and "driver" in landing_html
        assert "SAFE0002" in landing_html and "Not registered" in landing_html and "Permission remains configurable" in landing_html
        assert "database_name" not in landing_html and "client_id" not in landing_html
        assert "<script>" not in landing_html and "<img" not in landing_html

        transition_cases = (
            ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
            ((0, 0, 0), (0, 0, 1), (1, 1, 1)),
            ((0, 0, 0), (1, 0, 1), (1, 1, 1)),
            ((0, 0, 0), (0, 1, 1), (1, 1, 1)),
            ((1, 1, 1), (0, 1, 1), (0, 0, 0)),
            ((1, 1, 1), (1, 0, 1), (1, 0, 0)),
            ((1, 1, 1), (1, 1, 0), (1, 1, 0)),
            ((1, 1, 1), (1, 0, 0), (1, 0, 0)),
            ((1, 1, 1), (0, 0, 0), (0, 0, 0)),
            ((1, 1, 0), (0, 1, 0), (0, 0, 0)),
            ((1, 1, 0), (1, 0, 0), (1, 0, 0)),
            ((1, 0, 0), (1, 1, 0), (1, 1, 0)),
            ((1, 0, 0), (1, 1, 1), (1, 1, 1)),
        )
        valid_states = {"000", "100", "110", "111"}
        subject_specs = (
            ("user", "portal_user_clients", "user_id", USER, service.user_editor, service.update_user),
            ("group", "portal_group_clients", "group_id", GROUP, service.group_editor, service.update_group),
        )
        for subject_type, table, id_col, subject_id, load_editor, update in subject_specs:
            for before_raw, submitted_raw, expected_raw in transition_cases:
                before_sql = ",".join("TRUE" if value else "FALSE" for value in before_raw)
                psql.run(database, f"UPDATE {table} SET (can_view_eco_ranking,can_view_eco_trip_details,can_view_eco_trip_routes)=({before_sql}) WHERE {id_col}='{subject_id}' AND client_code='ALPHA00001'")
                psql.run(database, "DELETE FROM portal_audit_events")
                transition_editor = load_editor(subject_id)
                desired_transition = {row.client_code: row.direct for row in transition_editor.clients}
                desired_transition["ALPHA00001"] = EcoPermissionState(*map(bool, submitted_raw))
                transition_result = update(
                    subject_id,
                    actor_user_id=ADMIN,
                    pairs=_form(service, transition_editor, desired_transition, ADMIN),
                    ip_address=None,
                    user_agent=None,
                )
                stored = psql.run(database, f"SELECT can_view_eco_ranking::int::text||can_view_eco_trip_details::int::text||can_view_eco_trip_routes::int::text FROM {table} WHERE {id_col}='{subject_id}' AND client_code='ALPHA00001'", tuples=True).strip()
                expected = "".join(map(str, expected_raw))
                assert stored == expected and stored in valid_states, (subject_type, before_raw, submitted_raw, stored)
                assert len(transition_result.changes) == 1
                audit_transition = json.loads(psql.run(database, "SELECT metadata_json::text FROM portal_audit_events", tuples=True).strip())
                change = audit_transition["changes"][0]
                flag_order = ("can_view_eco_ranking", "can_view_eco_trip_details", "can_view_eco_trip_routes")
                assert tuple(int(change["before"][flag]) for flag in flag_order) == before_raw
                assert tuple(int(change["submitted"][flag]) for flag in flag_order) == submitted_raw
                assert tuple(int(change["normalized_after"][flag]) for flag in flag_order) == expected_raw
                expected_cascade = before_raw[0] == 1 and submitted_raw[0] == 0 or (before_raw[1] == 1 and submitted_raw[0] == 1 and submitted_raw[1] == 0)
                assert audit_transition.get("revoke_cascade", False) is expected_cascade
                preserved = psql.run(database, f"SELECT can_view_database,can_view_reports,can_export_database FROM {table} WHERE {id_col}='{subject_id}' AND client_code='ALPHA00001'", tuples=True).strip()
                assert preserved == ("t|f|t" if subject_type == "user" else "f|t|f")

        psql.run(database, f"UPDATE portal_user_clients SET can_view_eco_ranking=TRUE,can_view_eco_trip_details=FALSE,can_view_eco_trip_routes=FALSE WHERE user_id='{USER}' AND client_code='ALPHA00001'")
        psql.run(database, f"UPDATE portal_group_clients SET can_view_eco_ranking=TRUE,can_view_eco_trip_details=TRUE,can_view_eco_trip_routes=FALSE WHERE group_id='{GROUP}' AND client_code='ALPHA00001'")
        psql.run(database, "DELETE FROM portal_audit_events")

        editor = service.user_editor(USER)
        assert len(editor.clients) == 2
        alpha = next(row for row in editor.clients if row.client_code == "ALPHA00001")
        assert alpha.direct == EcoPermissionState(True, False, False)
        assert alpha.inherited.value == EcoPermissionState(True, True, False)
        assert alpha.effective == EcoPermissionState(True, True, False)

        html_body = EcoDrivingPermissionAdminPages(service).user_editor(USER, actor_user_id=ADMIN).body_html
        assert "<script>alert(1)</script>" not in html_body and "&lt;script&gt;" in html_body
        assert "<img" not in html_body and "&lt;img" in html_body
        assert "database_name" not in html_body and "password" not in html_body.lower()

        desired = {"ALPHA00001": EcoPermissionState(True, False, True), "SAFE0002": EcoPermissionState(True, False, False)}
        result = service.update_user(USER, actor_user_id=ADMIN, pairs=_form(service, editor, desired, ADMIN), ip_address="127.0.0.1", user_agent="stage6-test")
        assert len(result.changes) == 2 and result.dependency_normalized is True
        state = psql.run(database, f"SELECT can_view_database,can_view_reports,can_export_database,can_view_eco_ranking,can_view_eco_trip_details,can_view_eco_trip_routes FROM portal_user_clients WHERE user_id='{USER}' AND client_code='ALPHA00001'", tuples=True).strip()
        assert state == "t|f|t|t|t|t", state
        inserted = psql.run(database, f"SELECT can_view_database,can_view_reports,can_export_database,can_view_eco_ranking FROM portal_user_clients WHERE user_id='{USER}' AND client_code='SAFE0002'", tuples=True).strip()
        assert inserted == "f|f|f|t", inserted
        audit = json.loads(psql.run(database, "SELECT metadata_json::text FROM portal_audit_events WHERE event_type='eco_driving_user_permissions_updated'", tuples=True).strip())
        assert audit["clients_changed"] == 2 and audit["dependency_normalized"] is True
        assert audit["changes"][0]["submitted"] and audit["changes"][0]["normalized_after"]
        audit_blob = json.dumps(audit).lower()
        for secret in ("csrf", "version", "password", "sql", "session"):
            assert secret not in audit_blob

        stale_pairs = _form(service, editor, desired, ADMIN)
        try:
            service.update_user(USER, actor_user_id=ADMIN, pairs=stale_pairs, ip_address=None, user_agent=None)
        except EcoAdminConflict:
            pass
        else:
            raise AssertionError("stale version was accepted")
        assert psql.run(database, "SELECT count(*) FROM portal_audit_events", tuples=True).strip() == "1"

        group_editor = service.group_editor(GROUP)
        group_values = {row.client_code: EcoPermissionState(False, False, row.client_code == "SAFE0002") for row in group_editor.clients}
        group_result = service.update_group(GROUP, actor_user_id=ADMIN, pairs=_form(service, group_editor, group_values, ADMIN), ip_address=None, user_agent=None)
        assert group_result.affected_active_member_count == 1 and group_result.dependency_normalized is True
        assert psql.run(database, "SELECT count(*) FROM portal_audit_events WHERE event_type='eco_driving_group_permissions_updated'", tuples=True).strip() == "1"

        # Existing Stage 2 effective union reflects direct/group writes and the
        # user-facing admin flag is not consulted by this service.
        refreshed = service.user_editor(USER)
        safe = next(row for row in refreshed.clients if row.client_code == "SAFE0002")
        assert safe.direct == EcoPermissionState(True, False, False)
        assert safe.inherited.value == EcoPermissionState(True, True, True)
        assert safe.effective == EcoPermissionState(True, True, True)

        # No-op emits no misleading update audit.
        no_op_values = {row.client_code: row.direct for row in refreshed.clients}
        no_op = service.update_user(USER, actor_user_id=ADMIN, pairs=_form(service, refreshed, no_op_values, ADMIN), ip_address=None, user_agent=None)
        assert not no_op.changes
        assert psql.run(database, "SELECT count(*) FROM portal_audit_events WHERE event_type='eco_driving_user_permissions_updated'", tuples=True).strip() == "1"

        # Revoking an Eco-only row keeps the row and its unrelated flags false.
        revoke_editor = service.user_editor(USER)
        revoke_values = {row.client_code: row.direct for row in revoke_editor.clients}
        revoke_values["SAFE0002"] = EcoPermissionState()
        service.update_user(USER, actor_user_id=ADMIN, pairs=_form(service, revoke_editor, revoke_values, ADMIN), ip_address=None, user_agent=None)
        eco_only = psql.run(database, f"SELECT count(*),bool_or(can_view_database),bool_or(can_view_reports),bool_or(can_export_database),bool_or(can_view_eco_ranking) FROM portal_user_clients WHERE user_id='{USER}' AND client_code='SAFE0002'", tuples=True).strip()
        assert eco_only == "1|f|f|f|f"

        # A failure on the later client must roll back the earlier update.
        psql.run(database, f"UPDATE portal_user_clients SET can_view_eco_ranking=TRUE WHERE user_id='{USER}' AND client_code='SAFE0002'")
        psql.stdin(database, """
          CREATE FUNCTION reject_safe_client_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.client_code='SAFE0002' AND NEW.can_view_eco_ranking IS FALSE THEN RAISE EXCEPTION 'later client rejected'; END IF;
            RETURN NEW;
          END $$;
          CREATE TRIGGER reject_safe_client_update BEFORE UPDATE ON portal_user_clients FOR EACH ROW EXECUTE FUNCTION reject_safe_client_update();
        """)
        before_later_failure = service.user_editor(USER)
        later_failure_values = {row.client_code: EcoPermissionState(True, False, False) for row in before_later_failure.clients}
        later_failure_values["SAFE0002"] = EcoPermissionState()
        try:
            service.update_user(USER, actor_user_id=ADMIN, pairs=_form(service, before_later_failure, later_failure_values, ADMIN), ip_address=None, user_agent=None)
        except Exception:
            pass
        else:
            raise AssertionError("later-client failure did not abort the submission")
        after_later_failure = service.user_editor(USER)
        assert [row.direct for row in after_later_failure.clients] == [row.direct for row in before_later_failure.clients]
        psql.stdin(database, "DROP TRIGGER reject_safe_client_update ON portal_user_clients; DROP FUNCTION reject_safe_client_update();")

        statuses = psql.run(database, f"SELECT (SELECT is_active FROM artifact_users WHERE user_id='{USER}'),(SELECT is_active FROM portal_groups WHERE group_id='{GROUP}'),(SELECT count(*) FROM portal_group_users WHERE group_id='{GROUP}' AND user_id='{USER}'),(SELECT count(*) FROM portal_clients WHERE is_active IS TRUE)", tuples=True).strip()
        assert statuses == "t|t|1|2"

        # An audit failure must roll back the authorization update.
        psql.stdin(database, """
          CREATE FUNCTION reject_eco_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit rejected'; END $$;
          CREATE TRIGGER reject_eco_audit BEFORE INSERT ON portal_audit_events FOR EACH ROW EXECUTE FUNCTION reject_eco_audit();
        """)
        before_failure = service.user_editor(USER)
        failed_values = {row.client_code: EcoPermissionState(False, False, False) for row in before_failure.clients}
        try:
            service.update_user(USER, actor_user_id=ADMIN, pairs=_form(service, before_failure, failed_values, ADMIN), ip_address=None, user_agent=None)
        except Exception:
            pass
        else:
            raise AssertionError("audit failure did not abort the write")
        after_failure = service.user_editor(USER)
        assert [row.direct for row in after_failure.clients] == [row.direct for row in before_failure.clients]
        print("PASS: isolated writes are atomic, audited, stale-safe and preserve unrelated permissions")
    finally:
        psql.run(admin_db, f"DROP DATABASE IF EXISTS {database}")


def _request(method: str, path: str, *, body: bytes = b"", auth: str | None = None, content_length: int | None = None):
    from fastapi import Request
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if auth:
        headers.append((b"x-test-auth", auth.encode("ascii")))
    declared = len(body) if content_length is None else content_length
    headers.append((b"content-length", str(declared).encode("ascii")))
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    scope = {
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 12345), "server": ("test", 80),
    }
    return Request(scope, receive)


def test_http_admin_workflow() -> None:
    from urllib.parse import urlencode

    class HttpService:
        csrf = EcoAdminCsrfProtector(lambda: "http-test-secret")
        @staticmethod
        def csrf_scope(subject_type, subject_id):
            return f"eco-driving-permissions:{subject_type}:{subject_id}"
        def _update(self, subject_type, subject_id, **kwargs):
            csrf, version, states, _ = EcoDrivingPermissionAdminService.parse_form(kwargs["pairs"])
            self.csrf.validate(csrf, kwargs["actor_user_id"], self.csrf_scope(subject_type, subject_id))
            if version == "stale":
                raise EcoAdminConflict()
            if version == "internal-error":
                raise RuntimeError("synthetic database detail must not render")
            return SimpleNamespace(changes=(() if version == "current-noop" else (object(),)))
        def update_user(self, subject_id, **kwargs): return self._update("user", subject_id, **kwargs)
        def update_group(self, subject_id, **kwargs): return self._update("group", subject_id, **kwargs)

    class HttpPages:
        def __init__(self): self.service = HttpService()
        @staticmethod
        def landing(**kwargs): return SimpleNamespace(title="landing", body_html="landing", status_code=200)
        @staticmethod
        def user_editor(subject_id, **kwargs): return SimpleNamespace(title="user", body_html=kwargs.get("error") or "user", status_code=kwargs.get("status_code", 200))
        @staticmethod
        def group_editor(subject_id, **kwargs): return SimpleNamespace(title="group", body_html=kwargs.get("error") or "group", status_code=kwargs.get("status_code", 200))
        @staticmethod
        def error_page(title, message, status_code): return SimpleNamespace(title=title, body_html=message, status_code=status_code)

    app = _FakeApp()
    pages = HttpPages()
    def require_admin(request):
        auth = request.headers.get("x-test-auth")
        if auth == "admin": return {"user_id": ADMIN, "is_admin": True}
        return "LOGIN_REDIRECT" if not auth else "FORBIDDEN_403"
    register_eco_driving_admin_routes(
        app, pages=pages, require_admin=require_admin,
        render=lambda result, user: (result.status_code, result.body_html),
        redirect=lambda location: (303, location),
    )
    handlers = {(method, path): handler for method, path, handler in app.routes}
    landing = handlers[("GET", "/admin/client-access/eco-driving")]
    assert landing(_request("GET", "/admin/client-access/eco-driving")) == "LOGIN_REDIRECT"
    assert landing(_request("GET", "/admin/client-access/eco-driving", auth="eco-non-admin")) == "FORBIDDEN_403"
    assert landing(_request("GET", "/admin/client-access/eco-driving", auth="admin"))[0] == 200

    post = handlers[("POST", "/admin/client-access/eco-driving/users/{user_id}")]
    token = pages.service.csrf.issue(ADMIN, pages.service.csrf_scope("user", USER))
    def encoded(version="current", extra=()):
        values = [("csrf_token", token), ("version_token", version), ("client_code", "ALPHA00001"), *extra]
        return urlencode(values).encode("utf-8")
    response = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded(), auth="admin")))
    assert response == (303, f"/admin/client-access/eco-driving/users/{USER}?result=updated")
    no_op = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded("current-noop"), auth="admin")))
    assert no_op == (303, f"/admin/client-access/eco-driving/users/{USER}?result=no-change")
    stale = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded("stale"), auth="admin")))
    assert stale[0] == 409 and "Permissions changed" in stale[1]
    invalid = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded(extra=(("can_view_database", "true"),)), auth="admin")))
    assert invalid[0] == 422
    internal = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded("internal-error"), auth="admin")))
    assert internal[0] == 500 and "synthetic database detail" not in internal[1]
    missing_csrf = urlencode((("version_token", "current"), ("client_code", "ALPHA00001"))).encode()
    denied = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=missing_csrf, auth="admin")))
    assert denied[0] == 403
    too_large = asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=b"x=1", auth="admin", content_length=MAX_ADMIN_FORM_BODY_BYTES + 1)))
    assert too_large[0] == 413
    assert asyncio.run(post(USER, _request("POST", "/admin/client-access/eco-driving/users/x", body=encoded(), auth="eco-non-admin"))) == "FORBIDDEN_403"
    print("PASS: HTTP adapter enforces admin auth, bounded body, 403/409/413/422 and PRG outcomes")


def test_static_scope_and_security() -> None:
    service = (ROOT / "api/eco_driving_explorer/admin_service.py").read_text()
    routes = (ROOT / "api/eco_driving_explorer/admin_routes.py").read_text()
    main = (ROOT / "api/main.py").read_text()
    assert "SELECT *" not in service and "DELETE FROM portal_user_clients" not in service
    assert "DELETE FROM portal_group_clients" not in service
    assert "can_view_database, can_view_reports, can_export_database" in service
    assert "request.stream()" in routes and "_require_portal_admin" not in routes
    assert main.count("/admin/client-access/eco-driving\"") == 1  # navigation only; routes live outside main
    assert "EcoDrivingPermissionAdminService" in main
    assert "PRIMARY KEY (user_id, client_code)" in (ROOT / "db/migrations/033_portal_client_access.sql").read_text()
    assert "PRIMARY KEY (group_id, client_code)" in (ROOT / "db/migrations/037_portal_groups.sql").read_text()
    for forbidden in ("client database credentials", "automatic grants", "EXACT_SNAPSHOT"):
        assert forbidden not in service
    print("PASS: write surface is explicit, bounded and isolated from api/main.py")


def main() -> None:
    test_normalization_contract()
    test_stateful_transition_contract()
    test_csrf_contract()
    test_route_registration()
    test_form_allowlist_and_duplicates()
    test_isolated_transactional_writes()
    test_http_admin_workflow()
    test_static_scope_and_security()
    print("OK - Eco Driving permission admin tests passed")


if __name__ == "__main__":
    main()
