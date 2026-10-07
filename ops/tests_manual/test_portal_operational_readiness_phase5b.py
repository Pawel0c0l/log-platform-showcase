#!/usr/bin/env python3
"""Manual readiness smoke tests for Phase 5B portal operations.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_operational_readiness_phase5b.py

These checks use import stubs and do not require a live database.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _App:
    def __init__(self, *args, **kwargs):
        self.routes = []

    def get(self, *args, **kwargs):
        self.routes.append(("GET", args[0] if args else ""))
        return lambda fn: fn

    def post(self, *args, **kwargs):
        self.routes.append(("POST", args[0] if args else ""))
        return lambda fn: fn

    def patch(self, *args, **kwargs):
        self.routes.append(("PATCH", args[0] if args else ""))
        return lambda fn: fn

    def delete(self, *args, **kwargs):
        self.routes.append(("DELETE", args[0] if args else ""))
        return lambda fn: fn

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = b"".join(body)
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


def _identity_default(default=None, *args, **kwargs):
    return default


def _install_import_stubs() -> None:
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = _identity_default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = _identity_default
    fastapi.Form = _identity_default
    fastapi.Query = _identity_default
    fastapi.Body = _identity_default
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = _HTMLResponse
    responses.StreamingResponse = _StreamingResponse

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: None

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *args, **kwargs: None
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402
import scripts.bootstrap_portal_admin as bootstrap_admin  # noqa: E402


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_portal_ready = _load_module(REPO_ROOT / "ops/checks/check_portal_ready.py", "check_portal_ready_phase5b")


class _FakeUrl:
    def __init__(self, path="/", query=""):
        self.path = path
        self.query = query


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, *, path="/", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}
        self.headers = {"user-agent": "manual-test"}
        self.client = _FakeClient()


def _html(response) -> str:
    return response.body.decode("utf-8")


def _assert_clean_response(response) -> str:
    html = _html(response)
    lowered = html.lower()
    for marker in ["traceback", "password_hash", "api_write_token", "api_read_token", "super-secret", "select *"]:
        assert marker not in lowered, marker
    return html


def _portal_user(*, admin=False):
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "roles": [],
        "permissions": [],
    }


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def test_login_and_unauthorized_portal_routes_are_clean():
    login = api_main.artifact_explorer_login_page(_FakeRequest(path="/artifact-explorer/login"), next="/admin")
    assert login.status_code == 200, login.status_code
    html = _assert_clean_response(login)
    assert "Log in" in html and "/artifact-explorer/login" in html, html

    user_home = api_main.user_portal_home(_FakeRequest(path="/user"))
    assert user_home.status_code == 303, user_home.status_code
    assert "/artifact-explorer/login" in user_home.headers.get("Location", ""), user_home.headers

    admin_home = api_main.admin_portal_home(_FakeRequest(path="/admin"))
    assert admin_home.status_code == 303, admin_home.status_code
    assert "/artifact-explorer/login" in admin_home.headers.get("Location", ""), admin_home.headers
    print("PASS: login and unauthenticated portal routes are clean")


def test_admin_audit_is_admin_only_without_traceback():
    old = _patch("get_current_artifact_user", lambda request: _portal_user(admin=False))
    try:
        denied = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit"))
    finally:
        setattr(api_main, "get_current_artifact_user", old)
    assert denied.status_code == 403, denied.status_code
    html = _assert_clean_response(denied)
    assert "Only admin users" in html, html
    print("PASS: /admin/audit is admin-only and renders a clean forbidden page")


def test_portal_readiness_contract_lists_required_tables():
    required = set(check_portal_ready.REQUIRED_TABLES)
    for table in [
        "artifact_users",
        "portal_clients",
        "portal_user_clients",
        "portal_report_folders",
        "portal_database_datasets",
        "portal_audit_events",
        "portal_groups",
        "portal_group_clients",
        "portal_report_folder_groups",
        "portal_database_dataset_groups",
    ]:
        assert table in required, table
    assert len(required) == len(check_portal_ready.REQUIRED_TABLES), "duplicate readiness table names"
    print("PASS: readiness check covers required portal tables")


def test_readiness_db_failure_does_not_print_secret_values():
    original_connect = check_portal_ready.psycopg.connect
    check_portal_ready.psycopg.connect = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("password=super-secret"))
    try:
        results = check_portal_ready.run_checks()
    finally:
        check_portal_ready.psycopg.connect = original_connect
    assert len(results) == 1 and not results[0].ok, results
    assert "RuntimeError" in results[0].message, results[0].message
    assert "super-secret" not in results[0].message and "password=" not in results[0].message, results[0].message
    print("PASS: readiness DB failure output is sanitized")


def test_bootstrap_admin_password_inputs_are_safe_and_idempotent_shape():
    parser = bootstrap_admin.build_parser()
    args = parser.parse_args(["--username", "admin", "--password-env", "PORTAL_ADMIN_PASSWORD"])
    os.environ["PORTAL_ADMIN_PASSWORD"] = "temporary-password"
    try:
        assert bootstrap_admin._password_from_args(args) == "temporary-password"
    finally:
        os.environ.pop("PORTAL_ADMIN_PASSWORD", None)

    empty_args = parser.parse_args(["--username", "admin", "--password", ""])
    try:
        bootstrap_admin._password_from_args(empty_args)
    except SystemExit as exc:
        assert "Password must not be empty" in str(exc), exc
    else:
        raise AssertionError("empty password was accepted")

    password_hash = bootstrap_admin.hash_artifact_password("temporary-password")
    assert password_hash.startswith("pbkdf2_sha256$"), password_hash
    assert "temporary-password" not in password_hash, password_hash
    print("PASS: bootstrap admin password handling uses env/prompt-safe hashing and rejects empty passwords")


def main() -> int:
    test_login_and_unauthorized_portal_routes_are_clean()
    test_admin_audit_is_admin_only_without_traceback()
    test_portal_readiness_contract_lists_required_tables()
    test_readiness_db_failure_does_not_print_secret_values()
    test_bootstrap_admin_password_inputs_are_safe_and_idempotent_shape()
    print("PASS: Phase 5B portal operational readiness checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
