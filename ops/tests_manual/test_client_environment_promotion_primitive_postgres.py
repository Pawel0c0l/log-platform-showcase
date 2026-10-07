#!/usr/bin/env python3
"""Disposable PostgreSQL security tests for client migration 045."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from ops import environment_identity_promotion as promotion

REPO_ROOT = Path(__file__).resolve().parents[2]
M038 = (REPO_ROOT / "db/client_business/038_environment_identity.sql").read_text()
M045 = (REPO_ROOT / "db/client_business/045_environment_identity_promotion_primitive.sql").read_text()
DATABASE_UUID = "a41f7fe6-e113-42f7-8789-2dc20b2091d7"


def dsn_for(admin_dsn: str, dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    values = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    values["dbname"] = dbname
    if user is not None:
        values["user"] = user
        values["password"] = password or ""
    return psycopg.conninfo.make_conninfo(**values)


def expect_db_error(callback) -> None:
    try:
        callback()
    except psycopg.Error:
        return
    raise AssertionError("expected PostgreSQL failure")


def main() -> None:
    admin_dsn = os.environ.get("ENV_PROMOTION_TEST_ADMIN_DSN")
    if not admin_dsn:
        raise SystemExit("ENV_PROMOTION_TEST_ADMIN_DSN is required")
    suffix = uuid4().hex[:10]
    dbname = f"promotion_primitive_{suffix}"
    runtime_role = f"promotion_runtime_{suffix}"
    runtime_password = uuid4().hex
    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(runtime_role), sql.Literal(runtime_password)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(runtime_role)))
        db_dsn = dsn_for(admin_dsn, dbname)
        with psycopg.connect(db_dsn, row_factory=dict_row) as owner:
            owner.execute(M038)
            owner.execute(
                """INSERT INTO ops_control.environment_identity
                   (identity_key,environment,database_identity_id,database_role,database_name,client_code,provisioned_by)
                   VALUES ('primary','local_dev',%s::uuid,'client_business',%s,'BRAVO00016','test')""",
                (DATABASE_UUID, dbname),
            )
            owner.execute(sql.SQL("GRANT USAGE ON SCHEMA ops_control TO {}").format(sql.Identifier(runtime_role)))
            owner.execute(sql.SQL("GRANT SELECT ON ops_control.environment_identity TO {}").format(sql.Identifier(runtime_role)))
            owner.commit()

            # Transaction rollback leaves no partial function/grant.
            try:
                with owner.transaction():
                    owner.execute(M045)
                    assert owner.execute("SELECT to_regprocedure('ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)') IS NOT NULL").fetchone().popitem()[1]
                    raise psycopg.Rollback()
            except psycopg.Rollback:
                pass
            assert owner.execute("SELECT to_regprocedure('ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)') IS NULL").fetchone().popitem()[1]
            owner.execute(M045)
            owner.commit()
            owner.execute(M045)
            owner.commit()
            metadata = owner.execute(
                """SELECT p.prosecdef, p.proconfig, p.proowner=to_regrole(current_user)::oid AS owner_ok,
                          coalesce(array_to_string(p.proacl,','),'') AS acl
                     FROM pg_proc p
                    WHERE p.oid='ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)'::regprocedure"""
            ).fetchone()
            assert metadata["prosecdef"] is True and metadata["owner_ok"] is True
            assert "search_path=pg_catalog, ops_control" in metadata["proconfig"]
            assert not any(item.startswith("=") for item in metadata["acl"].split(","))  # PUBLIC execute is absent.

        runtime_dsn = dsn_for(admin_dsn, dbname, user=runtime_role, password=runtime_password)
        with psycopg.connect(runtime_dsn, row_factory=dict_row) as runtime:
            expect_db_error(lambda: runtime.execute("UPDATE ops_control.environment_identity SET environment='production'"))
            runtime.rollback()
            assert runtime.execute("SELECT has_table_privilege(current_user,'ops_control.environment_identity','UPDATE')").fetchone().popitem()[1] is False
            assert runtime.execute("SELECT has_function_privilege(current_user,'ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)','EXECUTE')").fetchone().popitem()[1] is True
            capability = promotion.client_promotion_capability(runtime, expected_user=runtime_role)
            assert capability["available"] is True and capability["least_privilege_safe"] is True
            expect_db_error(lambda: runtime.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',NULL,NULL)",
                (str(uuid4()),),
            ))
            runtime.rollback()
            expect_db_error(lambda: runtime.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'staging','production','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            ))
            runtime.rollback()
            expect_db_error(lambda: runtime.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','prod','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            ))
            runtime.rollback()
            runtime.execute("CREATE TEMP TABLE environment_identity(environment text)")
            result = runtime.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',%s::uuid,%s)",
                (DATABASE_UUID, str(uuid4()), "a" * 64),
            ).fetchone()
            runtime.commit()
            assert str(result["database_uuid"]) == DATABASE_UUID
            assert result["old_environment"] == "local_dev" and result["new_environment"] == "production"
            assert result["database_name"] == dbname and result["changed_row_count"] == 1
            marker = runtime.execute("SELECT environment,database_identity_id::text,client_code,database_name FROM ops_control.environment_identity").fetchone()
            assert tuple(marker.values()) == ("production", DATABASE_UUID, "BRAVO00016", dbname)
            repeated = runtime.execute(
                "SELECT changed_row_count FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            ).fetchone()
            runtime.commit()
            assert repeated["changed_row_count"] == 0

        with psycopg.connect(db_dsn) as owner:
            owner.execute("UPDATE ops_control.environment_identity SET environment='local_dev'")
            owner.commit()
        with psycopg.connect(runtime_dsn, row_factory=dict_row) as runtime:
            assert promotion.promote_client_marker(
                runtime, expected_database=dbname, expected_uuid=DATABASE_UUID,
                expected_user=runtime_role, source="local_dev", target="production",
                promotion_id=str(uuid4()), attestation_hash="b" * 64,
            ) == "updated"
        with psycopg.connect(runtime_dsn, row_factory=dict_row) as fresh_runtime:
            marker = fresh_runtime.execute(
                "SELECT environment,database_identity_id::text FROM ops_control.environment_identity"
            ).fetchone()
            assert tuple(marker.values()) == ("production", DATABASE_UUID)

        # Serialize concurrent calls on the exact primary row.
        with psycopg.connect(db_dsn) as owner:
            owner.execute("UPDATE ops_control.environment_identity SET environment='local_dev'")
            owner.commit()
        first = psycopg.connect(runtime_dsn)
        second = psycopg.connect(runtime_dsn)
        try:
            first.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            )
            second.execute("SET statement_timeout='300ms'")
            expect_db_error(lambda: second.execute(
                "SELECT * FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            ))
            second.rollback()
            first.commit()
            assert second.execute(
                "SELECT changed_row_count FROM ops_control.promote_environment_identity_v1(%s::uuid,'local_dev','production','client_business',NULL,NULL)",
                (DATABASE_UUID,),
            ).fetchone()[0] == 0
            second.commit()
        finally:
            first.close()
            second.close()
        print("client environment promotion primitive PostgreSQL tests: OK")
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role)))
        admin.close()


if __name__ == "__main__":
    main()
