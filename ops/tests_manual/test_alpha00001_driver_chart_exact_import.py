#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.postprocess import (  # noqa: E402
    job_alpha00001_driver_chart_exact_import as job,
)


HEADER = "driver_id,driver_name,email,ranking_included,is_active,notes,source,effective_from,effective_to\n"


def _write_csv(body: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8", delete=False)
    with handle:
        handle.write(body)
    return Path(handle.name)


def _row(
    driver_id: str,
    *,
    name: str = "Driver Name",
    email: str = "driver@example.test",
    ranking: str = "true",
    active: str = "true",
) -> str:
    return f"{driver_id},{name},{email},{ranking},{active},,authoritative roster,,\n"


def _request(path: Path, **overrides):
    params = {
        "client_code": "ALPHA00001",
        "input_path": str(path),
        "driver_ids": ["82122", "323715"],
    }
    params.update(overrides)
    return job._parse_request(params)


def _config():
    return job.shared.ClientDbConfig(
        client_code="ALPHA00001",
        client_id="11111111-1111-1111-1111-111111111111",
        client_db_host="localhost",
        client_db_port=5432,
        client_db_name="alpha_main",
        client_db_user="alpha",
        client_db_password_secret_ref="ALPHA_DB_PASSWORD",
    )


class FakeCursor:
    def __init__(self):
        self.statements: list[str] = []
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.statements.append(str(sql))
        self.current = None

    def fetchone(self):
        return self.current

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_dry_run_does_not_mutate_db() -> None:
    path = _write_csv(HEADER + _row("82122"))
    roster = job._read_roster(path, allow_identical_duplicates=False)
    conn = FakeConn()
    original_validate = job._validate_chart_schema
    original_fetch = job._fetch_existing_rows
    try:
        job._validate_chart_schema = lambda _cur: None
        job._fetch_existing_rows = lambda *_args, **_kwargs: {}
        summary = job._execute_import(
            conn,
            config=_config(),
            request=_request(path),
            roster=roster,
        )
    finally:
        job._validate_chart_schema = original_validate
        job._fetch_existing_rows = original_fetch
        path.unlink()
    sql = "\n".join(conn.cursor_obj.statements).upper()
    assert summary["proposed_insert_driver_ids"] == ["82122"]
    assert "INSERT INTO" not in sql and "UPDATE PUBLIC" not in sql
    assert conn.commits == 0 and conn.rollbacks == 1


def test_exact_allowlist_and_extra_ids() -> None:
    path = _write_csv(HEADER + _row("82122") + _row("99999"))
    roster = job._read_roster(path, allow_identical_duplicates=False)
    conn = FakeConn()
    original_validate = job._validate_chart_schema
    original_fetch = job._fetch_existing_rows
    try:
        job._validate_chart_schema = lambda _cur: None
        job._fetch_existing_rows = lambda *_args, **_kwargs: {}
        summary = job._execute_import(
            conn,
            config=_config(),
            request=_request(path, driver_ids=["82122"]),
            roster=roster,
        )
    finally:
        job._validate_chart_schema = original_validate
        job._fetch_existing_rows = original_fetch
        path.unlink()
    assert summary["proposed_insert_driver_ids"] == ["82122"]
    assert summary["extra_input_ids"] == ["99999"]
    assert summary["extra_ids_skipped"] == ["99999"]
    assert summary["eligible_driver_ids"] == ["82122"]


def test_missing_required_columns_rejected() -> None:
    path = _write_csv("driver_id,driver_name,email,is_active\n82122,A,a@example.test,true\n")
    try:
        job._read_roster(path, allow_identical_duplicates=False)
    except ValueError as exc:
        assert "ranking_included" in str(exc)
    else:
        raise AssertionError("expected missing required column to fail")
    finally:
        path.unlink()


def test_missing_ranking_and_invalid_email_rejected() -> None:
    path = _write_csv(
        HEADER
        + _row("82122", ranking="")
        + _row("323715", email="not-an-email")
    )
    roster = job._read_roster(path, allow_identical_duplicates=False)
    path.unlink()
    reasons = {
        rejected["driver_id"]: rejected["reasons"] for rejected in roster.rejected_rows
    }
    assert "INVALID_OR_MISSING_RANKING_INCLUDED" in reasons["82122"]
    assert "INVALID_EMAIL" in reasons["323715"]
    assert not roster.valid_rows


def test_duplicate_driver_id_rejected_unless_identical_and_allowed() -> None:
    body = HEADER + _row("82122") + _row("82122")
    path = _write_csv(body)
    rejected = job._read_roster(path, allow_identical_duplicates=False)
    allowed = job._read_roster(path, allow_identical_duplicates=True)
    path.unlink()
    assert not rejected.valid_rows
    assert len(rejected.rejected_rows) == 2
    assert [row.driver_id for row in allowed.valid_rows] == ["82122"]
    assert len(allowed.identical_duplicates_collapsed) == 1


def test_allow_updates_false_protects_existing_rows() -> None:
    path = _write_csv(HEADER + _row("82122", name="New Name"))
    roster = job._read_roster(path, allow_identical_duplicates=False)
    row = roster.valid_rows[0]
    existing = {
        "82122": {
            "driver_id": "82122",
            "driver_name": "Existing Name",
            "email": row.email,
            "ranking_included": row.ranking_included,
            "is_active": row.is_active,
            "metadata_json": row.metadata,
        }
    }
    protected = job._plan_changes([row], existing, allow_updates=False)
    allowed = job._plan_changes([row], existing, allow_updates=True)
    path.unlink()
    assert not protected.updates
    assert protected.protected_existing[0]["driver_id"] == "82122"
    assert allowed.updates[0]["changed_fields"] == ["driver_name"]


def test_existing_lookup_is_client_and_exact_id_scoped() -> None:
    cursor = FakeCursor()
    job._fetch_existing_rows(
        cursor,
        client_id="11111111-1111-1111-1111-111111111111",
        driver_ids=["82122", "323715"],
        lock_rows=True,
    )
    sql = cursor.statements[-1]
    assert "WHERE client_id = %s" in sql
    assert sql.count("driver_id = ANY(%s)") == 1
    assert "FOR UPDATE" in sql


def test_expected_count_mismatch_aborts_real_run() -> None:
    path = _write_csv(HEADER + _row("82122"))
    roster = job._read_roster(path, allow_identical_duplicates=False)
    conn = FakeConn()
    original_validate = job._validate_chart_schema
    original_fetch = job._fetch_existing_rows
    original_apply = job._apply_changes
    applied = []
    try:
        job._validate_chart_schema = lambda _cur: None
        job._fetch_existing_rows = lambda *_args, **_kwargs: {}
        job._apply_changes = lambda *_args, **_kwargs: applied.append(True)
        request = _request(
            path,
            dry_run=False,
            expected_insert_count=0,
            expected_update_count=0,
        )
        try:
            job._execute_import(conn, config=_config(), request=request, roster=roster)
        except ValueError as exc:
            assert "expected_insert_count" in str(exc)
        else:
            raise AssertionError("expected count mismatch to fail")
    finally:
        job._validate_chart_schema = original_validate
        job._fetch_existing_rows = original_fetch
        job._apply_changes = original_apply
        path.unlink()
    assert not applied and conn.commits == 0


def test_idempotence_after_insert() -> None:
    path = _write_csv(HEADER + _row("82122"))
    roster = job._read_roster(path, allow_identical_duplicates=False)
    row = roster.valid_rows[0]
    desired = {
        "driver_id": row.driver_id,
        "driver_name": row.driver_name,
        "email": row.email,
        "ranking_included": row.ranking_included,
        "is_active": row.is_active,
        "metadata_json": row.metadata,
    }
    plan = job._plan_changes([row], {"82122": desired}, allow_updates=False)
    path.unlink()
    assert not plan.inserts and not plan.updates
    assert plan.unchanged_ids == ("82122",)


def test_apply_changes_only_uses_planned_exact_ids() -> None:
    cursor = FakeCursor()
    cursor_results = iter([
        {"driver_id": "82122"},
        {"driver_id": "323715"},
    ])
    cursor.fetchone = lambda: next(cursor_results)
    plan = job.ChangePlan(
        inserts=(
            {
                "driver_id": "82122",
                "driver_name": "A",
                "email": "a@example.test",
                "ranking_included": True,
                "is_active": True,
                "metadata_json": {},
            },
        ),
        updates=(
            {
                "driver_id": "323715",
                "driver_name": "B",
                "email": "b@example.test",
                "ranking_included": False,
                "is_active": True,
                "metadata_json": {},
                "changed_fields": ["driver_name"],
            },
        ),
        unchanged_ids=(),
        protected_existing=(),
    )
    inserted, updated = job._apply_changes(
        cursor,
        client_id="11111111-1111-1111-1111-111111111111",
        plan=plan,
    )
    sql = "\n".join(cursor.statements)
    assert inserted == ["82122"] and updated == ["323715"]
    assert "eco_drivers_id_chart" in sql
    assert "client_trips" not in sql and "eco_driver_weekly_stats" not in sql


def main() -> None:
    test_dry_run_does_not_mutate_db()
    test_exact_allowlist_and_extra_ids()
    test_missing_required_columns_rejected()
    test_missing_ranking_and_invalid_email_rejected()
    test_duplicate_driver_id_rejected_unless_identical_and_allowed()
    test_allow_updates_false_protects_existing_rows()
    test_existing_lookup_is_client_and_exact_id_scoped()
    test_expected_count_mismatch_aborts_real_run()
    test_idempotence_after_insert()
    test_apply_changes_only_uses_planned_exact_ids()
    print("OK - ALPHA00001 exact driver chart import tests passed")


if __name__ == "__main__":
    main()
