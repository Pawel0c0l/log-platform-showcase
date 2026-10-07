#!/usr/bin/env python3
"""Manual regressions for Workflow B Stage 3 permission bootstrap.

Run:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_stage3_permissions.py
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage3 import permissions  # noqa: E402
from ops import grant_workflow_b_stage3_permissions as grant_script  # noqa: E402


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, rows=None):
        self.cursor_obj = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj


class Patch:
    def __init__(self, obj, **attrs):
        self.obj = obj
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(self.obj, name)
            setattr(self.obj, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(self.obj, name, value)


def test_sql_generation() -> None:
    role_sql = permissions.build_ensure_loader_role_sql()
    assert "SELECT 1 FROM pg_roles" in role_sql
    assert 'CREATE ROLE "workflow_b_stage3_loader"' in role_sql
    assert "SUPERUSER" not in role_sql.upper()
    assert "CREATEDB" not in role_sql.upper()
    assert "CREATEROLE" not in role_sql.upper()

    grant_sql = permissions.build_stage3_permission_sql(
        client_db_name="alpha_main",
        client_db_user="alpha_user",
    )
    assert 'GRANT "workflow_b_stage3_loader" TO "alpha_user";' in grant_sql
    assert 'GRANT CONNECT ON DATABASE "alpha_main" TO "workflow_b_stage3_loader";' in grant_sql
    assert not any('GRANT CREATE' in statement for statement in grant_sql)
    print("PASS: SQL generation creates loader role with CONNECT but no CREATE grant")


def test_target_filtering() -> None:
    rows = [
        {"client_code": "A", "client_db_name": "a_db", "client_db_user": "a_user", "enabled": True},
        {"client_code": "B", "client_db_name": "b_db", "client_db_user": "b_user", "enabled": False},
        {"client_code": "C", "client_db_name": "", "client_db_user": "c_user", "enabled": True},
        {"client_code": "D", "client_db_name": "d_db", "client_db_user": "", "enabled": True},
    ]
    targets, skipped = permissions.rows_to_permission_targets(rows)
    assert [target.client_code for target in targets] == ["A"]
    assert {row["reason"] for row in skipped} == {"disabled", "missing_client_db_name_or_user"}

    filtered, _ = permissions.rows_to_permission_targets(rows, client_code="A")
    assert [target.client_code for target in filtered] == ["A"]
    print("PASS: disabled and incomplete clients are skipped, and client_code filter works")


def test_identifier_safety() -> None:
    assert permissions.quote_ident('a"b') == '"a""b"'
    try:
        permissions.validate_role_name("bad-role;drop")
    except ValueError as exc:
        assert "Unsafe" in str(exc)
    else:
        raise AssertionError("expected unsafe role name to fail")
    print("PASS: identifiers are quoted and role names are strictly validated")


def test_dry_run_script_prints_sql_without_execution() -> None:
    rows = [
        {
            "client_code": "ALPHA00001",
            "client_db_host": "127.0.0.1",
            "client_db_port": 5432,
            "client_db_name": "alpha_main",
            "client_db_user": "alpha_user",
            "enabled": True,
        }
    ]
    fake_platform = FakeConn(rows)
    out = io.StringIO()
    with Patch(grant_script, _platform_conn=lambda: fake_platform), contextlib.redirect_stdout(out):
        old_argv = sys.argv
        sys.argv = ["grant_workflow_b_stage3_permissions.py"]
        try:
            rc = grant_script.main()
        finally:
            sys.argv = old_argv
    text = out.getvalue()
    assert rc == 0
    assert "Mode: DRY-RUN" in text
    assert 'GRANT CONNECT ON DATABASE "alpha_main"' in text
    assert fake_platform.cursor_obj.executed and "SELECT" in fake_platform.cursor_obj.executed[0][0]
    print("PASS: dry-run script prints SQL and does not open admin apply connections")


def test_apply_script_path_can_be_mocked() -> None:
    rows = [
        {
            "client_code": "ALPHA00001",
            "client_db_host": "127.0.0.1",
            "client_db_port": 5432,
            "client_db_name": "alpha_main",
            "client_db_user": "alpha_user",
            "enabled": True,
        }
    ]
    fake_platform = FakeConn(rows)
    applied = []

    def fake_admin_pg_conn(*, host, port, dbname):
        applied.append((host, port, dbname))
        return FakeConn([])

    with (
        Patch(grant_script, _platform_conn=lambda: fake_platform),
        Patch(permissions, admin_pg_conn=fake_admin_pg_conn),
    ):
        old_argv = sys.argv
        sys.argv = ["grant_workflow_b_stage3_permissions.py", "--apply"]
        try:
            rc = grant_script.main()
        finally:
            sys.argv = old_argv
    assert rc == 0
    assert applied == [("127.0.0.1", 5432, "postgres")]
    print("PASS: --apply execution path can be mocked")


def main() -> int:
    test_sql_generation()
    test_target_filtering()
    test_identifier_safety()
    test_dry_run_script_prints_sql_without_execution()
    test_apply_script_path_can_be_mocked()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
