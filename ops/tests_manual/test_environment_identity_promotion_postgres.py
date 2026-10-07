#!/usr/bin/env python3
"""Disposable-PostgreSQL integration tests for environment promotion.

Requires ENV_PROMOTION_TEST_ADMIN_DSN pointing at a throwaway PostgreSQL server.
The script creates randomly named databases and always drops only those names.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ops import environment_identity_promotion as promotion

REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
ALPHA_UUID = "5879ec53-e3f6-4a46-89cf-eaae9f57b27e"
BRAVO_UUID = "a41f7fe6-e113-42f7-8789-2dc20b2091d7"
ALPHA_CLIENT_ID = "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"
BRAVO_CLIENT_ID = "6018be20-5faa-41b6-89c9-fe2b54a8283e"


def dsn_for(admin_dsn: str, dbname: str) -> str:
    params = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    params["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**params)


def execute_script(conn, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def create_database(admin, name: str) -> None:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(admin, name: str) -> None:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def create_platform_fixture(conn, platform_db: str, client_dbs: dict[str, str]) -> None:
    conn.execute("CREATE SCHEMA workflow_a_control")
    conn.execute(
        """CREATE TABLE workflow_a_control.client_account (
             client_id UUID PRIMARY KEY,
             client_code TEXT NOT NULL UNIQUE,
             enabled BOOLEAN NOT NULL,
             client_db_host TEXT NOT NULL,
             client_db_port INTEGER NOT NULL,
             client_db_name TEXT NOT NULL,
             client_db_user TEXT NOT NULL,
             client_db_password_secret_ref TEXT NOT NULL,
             client_db_environment TEXT,
             client_db_identity_id UUID
           )"""
    )
    execute_script(conn, REPO_ROOT / "db/migrations/042_platform_environment_identity.sql")
    conn.execute(
        """INSERT INTO ops_control.environment_identity
           (identity_key, environment, database_identity_id, database_role, database_name, client_code, provisioned_by)
           VALUES ('primary','local_dev',%s::uuid,'platform',%s,NULL,'test')""",
        (PLATFORM_UUID, platform_db),
    )
    rows = [
        (ALPHA_CLIENT_ID, "ALPHA00001", client_dbs["ALPHA00001"], ALPHA_UUID),
        (BRAVO_CLIENT_ID, "BRAVO00016", client_dbs["BRAVO00016"], BRAVO_UUID),
    ]
    for client_id, code, dbname, database_uuid in rows:
        conn.execute(
            """INSERT INTO workflow_a_control.client_account
               (client_id,client_code,enabled,client_db_host,client_db_port,client_db_name,
                client_db_user,client_db_password_secret_ref,client_db_environment,client_db_identity_id)
               VALUES (%s::uuid,%s,true,'127.0.0.1',5432,%s,'postgres','TEST_PASSWORD','local_dev',%s::uuid)""",
            (client_id, code, dbname, database_uuid),
        )
    conn.commit()


def create_client_fixture(conn, dbname: str, code: str, database_uuid: str) -> None:
    execute_script(conn, REPO_ROOT / "db/client_business/038_environment_identity.sql")
    conn.execute(
        """INSERT INTO ops_control.environment_identity
           (identity_key,environment,database_identity_id,database_role,database_name,client_code,provisioned_by)
           VALUES ('primary','local_dev',%s::uuid,'client_business',%s,%s,'test')""",
        (database_uuid, dbname, code),
    )
    conn.commit()


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


def client_plan(client_id, code, dbname, database_uuid):
    return promotion.ClientPlan(
        client_id=client_id,
        client_code=code,
        database_name=dbname,
        database_user="postgres",
        database_host="127.0.0.1",
        database_port=5432,
        database_uuid=database_uuid,
        password_secret_ref="TEST_PASSWORD",
    )


def test_migration_apply_rerun_rollback(platform_conn):
    migration = (REPO_ROOT / "db/migrations/053_environment_identity_promotion_journal.sql").read_text(encoding="utf-8")
    resume_migration = (REPO_ROOT / "db/migrations/054_environment_identity_resume_contract.sql").read_text(encoding="utf-8")
    with platform_conn.transaction():
        platform_conn.execute(migration)
        assert platform_conn.execute("SELECT to_regclass('ops_control.environment_identity_promotion')::text").fetchone().popitem()[1]
        raise psycopg.Rollback()
    assert platform_conn.execute("SELECT to_regclass('ops_control.environment_identity_promotion')::text").fetchone().popitem()[1] is None
    platform_conn.execute(migration)
    platform_conn.commit()
    platform_conn.execute(migration)
    platform_conn.commit()
    platform_conn.execute(resume_migration)
    platform_conn.commit()
    platform_conn.execute(resume_migration)
    platform_conn.commit()
    assert platform_conn.execute("SELECT to_regclass('ops_control.environment_identity_promotion')::text").fetchone().popitem()[1]
    platform_conn.rollback()


def test_marker_and_control_plane(platform_conn, alpha_conn, bravo_conn, platform_db, client_dbs):
    alpha = client_plan(ALPHA_CLIENT_ID, "ALPHA00001", client_dbs["ALPHA00001"], ALPHA_UUID)
    bravo = client_plan(BRAVO_CLIENT_ID, "BRAVO00016", client_dbs["BRAVO00016"], BRAVO_UUID)
    expect_code(
        "MARKER_IDENTITY_MISMATCH",
        lambda: promotion.update_marker_environment(
            alpha_conn,
            expected_database=alpha.database_name,
            expected_uuid=BRAVO_UUID,
            expected_role="client_business",
            expected_client_code="ALPHA00001",
            source="local_dev",
            target="production",
        ),
    )
    assert alpha_conn.execute("SELECT environment FROM ops_control.environment_identity").fetchone().popitem()[1] == "local_dev"
    alpha_conn.rollback()
    promotion.update_marker_environment(
        alpha_conn,
        expected_database=alpha.database_name,
        expected_uuid=ALPHA_UUID,
        expected_role="client_business",
        expected_client_code="ALPHA00001",
        source="local_dev",
        target="production",
    )
    row = alpha_conn.execute("SELECT environment,database_identity_id::text,client_code FROM ops_control.environment_identity").fetchone()
    assert tuple(row.values()) == ("production", ALPHA_UUID, "ALPHA00001")
    assert bravo_conn.execute("SELECT environment FROM ops_control.environment_identity").fetchone().popitem()[1] == "local_dev"
    bravo_conn.rollback()
    promotion.update_control_plane(platform_conn, [alpha], source="local_dev", target="production")
    rows = platform_conn.execute(
        "SELECT client_id::text,client_code,client_db_environment,client_db_identity_id::text FROM workflow_a_control.client_account ORDER BY client_code"
    ).fetchall()
    assert tuple(rows[0].values()) == (BRAVO_CLIENT_ID, "BRAVO00016", "local_dev", BRAVO_UUID)
    assert tuple(rows[1].values()) == (ALPHA_CLIENT_ID, "ALPHA00001", "production", ALPHA_UUID)
    platform_conn.rollback()
    alpha_conn.rollback()
    # Idempotent resume does not repeat a completed update.
    assert promotion.update_control_plane(platform_conn, [alpha], source="local_dev", target="production") == "already_completed"
    assert promotion.update_marker_environment(
        alpha_conn,
        expected_database=alpha.database_name,
        expected_uuid=ALPHA_UUID,
        expected_role="client_business",
        expected_client_code="ALPHA00001",
        source="local_dev",
        target="production",
    ) == "already_completed"


def test_wrong_source_and_transaction_rollback(bravo_conn, client_dbs):
    bravo = client_plan(BRAVO_CLIENT_ID, "BRAVO00016", client_dbs["BRAVO00016"], BRAVO_UUID)
    expect_code(
        "MARKER_SOURCE_MISMATCH",
        lambda: promotion.update_marker_environment(
            bravo_conn,
            expected_database=bravo.database_name,
            expected_uuid=BRAVO_UUID,
            expected_role="client_business",
            expected_client_code="BRAVO00016",
            source="staging",
            target="production",
        ),
    )
    bravo_conn.execute(
        """CREATE FUNCTION ops_control.force_staging() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN NEW.environment := 'staging'; RETURN NEW; END $$"""
    )
    bravo_conn.execute(
        "CREATE TRIGGER force_staging BEFORE UPDATE OF environment ON ops_control.environment_identity FOR EACH ROW EXECUTE FUNCTION ops_control.force_staging()"
    )
    bravo_conn.commit()
    expect_code(
        "MARKER_POSTWRITE_VERIFY",
        lambda: promotion.update_marker_environment(
            bravo_conn,
            expected_database=bravo.database_name,
            expected_uuid=BRAVO_UUID,
            expected_role="client_business",
            expected_client_code="BRAVO00016",
            source="local_dev",
            target="production",
        ),
    )
    row = bravo_conn.execute("SELECT environment,database_identity_id::text FROM ops_control.environment_identity").fetchone()
    assert tuple(row.values()) == ("local_dev", BRAVO_UUID)
    bravo_conn.execute("DROP TRIGGER force_staging ON ops_control.environment_identity")
    bravo_conn.execute("DROP FUNCTION ops_control.force_staging()")
    bravo_conn.commit()


def test_journal_resume_and_concurrency(platform_dsn, platform_conn, platform_db, client_dbs):
    plan = {
        "contract_version": 5,
        "promotion_plan_contract_version": 5,
        "operation_identity": {"host": "test-host"},
        "repository_head": "a" * 40,
        "source_environment": "local_dev",
        "target_environment": "production",
        "platform_uuid": PLATFORM_UUID,
        "platform_database": platform_db,
        "runtime_environment_file": "/tmp/runtime.env",
        "runtime_file_before_sha256": "a" * 64,
        "backup_reference": "/tmp/checkpoint.json",
        "clients": [
            {"client_id": ALPHA_CLIENT_ID, "client_code": "ALPHA00001", "database_name": client_dbs["ALPHA00001"], "database_user": "postgres", "database_host": "127.0.0.1", "database_port": 5432, "database_uuid": ALPHA_UUID},
        ],
        "steps": ["client_marker:ALPHA00001", "platform_control_plane", "platform_marker", "runtime_environment_file", "runtime_reload_required", "runtime_processes_verified", "final_verification"],
        "uuid_policy": "preserve_existing_database_uuids",
    }
    attestation = promotion.promotion_attestation(plan)
    with psycopg.connect(platform_dsn, row_factory=dict_row, autocommit=True) as lock_conn, \
         psycopg.connect(platform_dsn, row_factory=dict_row, autocommit=True) as second_lock:
        assert promotion.try_promotion_lock(lock_conn)
        assert promotion.try_promotion_lock(second_lock) is False
        # Inspection remains possible through a separate read session while the lock is held.
        with psycopg.connect(platform_dsn, row_factory=dict_row) as inspector:
            assert inspector.execute("SELECT count(*) FROM ops_control.environment_identity_promotion").fetchone().popitem()[1] == 0
        promotion.release_promotion_lock(lock_conn)
    promotion_id, journal = promotion.create_or_resume_journal(
        platform_conn,
        promotion_id=None,
        plan=plan,
        attestation=attestation,
        backup_reference="/tmp/checkpoint.json",
    )
    try:
        platform_conn.execute(
            "UPDATE ops_control.environment_identity_promotion SET target_environment='staging' WHERE promotion_id=%s::uuid",
            (promotion_id,),
        )
    except psycopg.errors.RaiseException:
        platform_conn.rollback()
    else:
        raise AssertionError("immutable journal plan update must fail")
    assert promotion.inspect_journals(platform_conn, promotion_id=promotion_id)[0]["target_environment"] == "production"
    platform_conn.rollback()
    promotion.journal_step(platform_conn, promotion_id, current_step="client_marker:ALPHA00001")
    promotion.journal_step(platform_conn, promotion_id, current_step=None, completed_step="client_marker:ALPHA00001")
    promotion.journal_step(platform_conn, promotion_id, current_step=None, completed_step="client_marker:ALPHA00001")
    row = promotion.inspect_journals(platform_conn, promotion_id=promotion_id)[0]
    assert row["completed_steps"] == ["client_marker:ALPHA00001"]
    platform_conn.rollback()
    promotion.journal_fail(platform_conn, promotion_id, current_step="platform_control_plane", error="simulated failure")
    resumed_id, resumed = promotion.create_or_resume_journal(
        platform_conn,
        promotion_id=promotion_id,
        plan=plan,
        attestation=attestation,
        backup_reference="/tmp/checkpoint.json",
    )
    assert resumed_id == promotion_id and resumed["state"] == "in_progress"
    wrong = dict(plan)
    wrong["target_environment"] = "staging"
    expect_code(
        "PROMOTION_PLAN_MISMATCH",
        lambda: promotion.create_or_resume_journal(
            platform_conn,
            promotion_id=promotion_id,
            plan=wrong,
            attestation=promotion.promotion_attestation(wrong),
            backup_reference="/tmp/checkpoint.json",
        ),
    )
    promotion.journal_fail(platform_conn, promotion_id, current_step="platform_control_plane", error="test cleanup")


def test_failure_journal_after_every_stage(platform_conn, platform_db, client_dbs):
    steps = [
        "client_marker:ALPHA00001",
        "platform_control_plane",
        "platform_marker",
        "runtime_environment_file",
        "runtime_reload_required",
        "runtime_processes_verified",
        "final_verification",
    ]
    for failure_index, failed_step in enumerate(steps):
        plan = {
            "contract_version": 5, "promotion_plan_contract_version": 5,
            "operation_identity": {"host": "disposable-test-host"},
            "repository_head": "a" * 40,
            "source_environment": "local_dev",
            "target_environment": "production",
            "platform_uuid": PLATFORM_UUID,
            "platform_database": platform_db,
            "runtime_environment_file": f"/tmp/runtime-{failure_index}.env",
            "runtime_file_before_sha256": f"{failure_index + 1:064x}",
            "backup_reference": f"/tmp/checkpoint-{failure_index}.json",
            "clients": [
                {"client_id": ALPHA_CLIENT_ID, "client_code": "ALPHA00001", "database_name": client_dbs["ALPHA00001"], "database_user": "postgres", "database_host": "127.0.0.1", "database_port": 5432, "database_uuid": ALPHA_UUID},
            ],
            "steps": steps,
            "uuid_policy": "preserve_existing_database_uuids",
        }
        attestation = promotion.promotion_attestation(plan)
        promotion_id, _ = promotion.create_or_resume_journal(
            platform_conn,
            promotion_id=None,
            plan=plan,
            attestation=attestation,
            backup_reference=plan["backup_reference"],
        )
        for completed_step in steps[:failure_index]:
            promotion.journal_step(platform_conn, promotion_id, current_step=completed_step)
            promotion.journal_step(platform_conn, promotion_id, current_step=None, completed_step=completed_step)
        promotion.journal_fail(platform_conn, promotion_id, current_step=failed_step, error=f"simulated failure after stage {failure_index}")
        failed = promotion.inspect_journals(platform_conn, promotion_id=promotion_id)[0]
        platform_conn.rollback()
        assert failed["state"] == "failed"
        assert failed["current_step"] == failed_step
        assert failed["completed_steps"] == steps[:failure_index]
        _, resumed = promotion.create_or_resume_journal(
            platform_conn,
            promotion_id=promotion_id,
            plan=plan,
            attestation=attestation,
            backup_reference=plan["backup_reference"],
        )
        assert resumed["completed_steps"] == steps[:failure_index]
        promotion.journal_fail(platform_conn, promotion_id, current_step=failed_step, error="test cleanup")


def main():
    admin_dsn = os.environ.get("ENV_PROMOTION_TEST_ADMIN_DSN")
    if not admin_dsn:
        raise SystemExit("ENV_PROMOTION_TEST_ADMIN_DSN is required")
    suffix = uuid4().hex[:10]
    platform_db = f"promotion_platform_{suffix}"
    client_dbs = {"ALPHA00001": f"promotion_alpha_{suffix}", "BRAVO00016": f"promotion_bravo_{suffix}"}
    names = [platform_db, *client_dbs.values()]
    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        for name in names:
            create_database(admin, name)
        platform_dsn = dsn_for(admin_dsn, platform_db)
        alpha_dsn = dsn_for(admin_dsn, client_dbs["ALPHA00001"])
        bravo_dsn = dsn_for(admin_dsn, client_dbs["BRAVO00016"])
        with psycopg.connect(platform_dsn, row_factory=dict_row) as platform_conn, \
             psycopg.connect(alpha_dsn, row_factory=dict_row) as alpha_conn, \
             psycopg.connect(bravo_dsn, row_factory=dict_row) as bravo_conn:
            create_platform_fixture(platform_conn, platform_db, client_dbs)
            create_client_fixture(alpha_conn, client_dbs["ALPHA00001"], "ALPHA00001", ALPHA_UUID)
            create_client_fixture(bravo_conn, client_dbs["BRAVO00016"], "BRAVO00016", BRAVO_UUID)
            test_migration_apply_rerun_rollback(platform_conn)
            test_marker_and_control_plane(platform_conn, alpha_conn, bravo_conn, platform_db, client_dbs)
            test_wrong_source_and_transaction_rollback(bravo_conn, client_dbs)
            test_journal_resume_and_concurrency(platform_dsn, platform_conn, platform_db, client_dbs)
            test_failure_journal_after_every_stage(platform_conn, platform_db, client_dbs)
        print("environment identity promotion PostgreSQL tests: OK")
    finally:
        for name in reversed(names):
            drop_database(admin, name)
        admin.close()


if __name__ == "__main__":
    main()
