#!/usr/bin/env python3
"""Disposable-PostgreSQL tests for the complete migration-054 contract."""
from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as cli
from ops import resume_plan_v2 as resume
from ops.tests_manual.test_resume_plan_v2 import fixture_plan
from ops.tests_manual.test_resume_v2_remote_binding import repositories, run as git_run


ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "RESUME_V2_TEST_DSN"
FORBIDDEN_DATABASES = {"logdb", "telematics_main", "alpha_main", "postgres"}


def execute_file(conn, relative: str) -> None:
    with conn.cursor() as cur:
        cur.execute((ROOT / relative).read_text(encoding="utf-8"))
    conn.commit()


def expect_schema_drift(fn) -> None:
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == "RESUME_SCHEMA_CONTRACT_DRIFT", exc
        assert exc.details["writes_performed"] is False
    else:
        raise AssertionError("expected RESUME_SCHEMA_CONTRACT_DRIFT")


def collect(
    conn, database: str, database_uuid: str, target_promotion_id: str,
) -> dict[str, object]:
    return resume.migration_054_schema_contract(
        conn,
        expected_database_name=database,
        expected_database_uuid=database_uuid,
        target_promotion_id=target_promotion_id,
    )


def mutate_and_refuse(
    conn, sql: str, database: str, database_uuid: str, target_promotion_id: str, *, label: str,
    expected_codes: tuple[str, ...] = ("RESUME_SCHEMA_CONTRACT_DRIFT",),
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        try:
            collect(conn, database, database_uuid, target_promotion_id)
        except promotion.PromotionError as exc:
            assert exc.code in expected_codes, (label, exc.code)
            assert exc.details["writes_performed"] is False
        else:
            raise AssertionError("schema mutation was accepted")
    except Exception as exc:
        raise AssertionError(
            f"schema mutation failed before drift classification: {label}"
        ) from exc
    finally:
        conn.rollback()


def main() -> None:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is required; no repository or production DSN fallback is allowed")
    configured = conninfo_to_dict(dsn)
    configured_name = configured.get("dbname", "")
    if configured_name in FORBIDDEN_DATABASES or not configured_name.startswith("resume_v2_test"):
        raise SystemExit(f"refusing non-disposable database name: {configured_name!r}")

    conn = psycopg.connect(
        dsn, row_factory=dict_row,
        options="-c application_name=resume_v2_schema_contract_disposable_test",
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS database")
            database = str(cur.fetchone()["database"])
        conn.rollback()
        assert database == configured_name
        assert database.startswith("resume_v2_test") and database not in FORBIDDEN_DATABASES

        # This test owns the explicitly disposable database and starts from a
        # deterministic catalog. It never loads .env or another DSN.
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
            cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
            cur.execute(
                """DROP ROLE IF EXISTS resume_v2_changed_owner;
                   CREATE SCHEMA ops_control;
                   CREATE TABLE ops_control.environment_identity (
                     identity_key text PRIMARY KEY,
                     environment text NOT NULL,
                     database_identity_id uuid NOT NULL UNIQUE,
                     database_role text NOT NULL,
                     database_name text NOT NULL,
                     client_code text NULL
                   );
                   CREATE TABLE public.schema_migrations (
                     filename text PRIMARY KEY,
                     applied_at timestamptz NOT NULL DEFAULT now()
                   );
                   CREATE ROLE resume_v2_changed_owner NOLOGIN"""
            )
        conn.commit()
        execute_file(conn, "db/migrations/053_environment_identity_promotion_journal.sql")
        execute_file(conn, "db/migrations/054_environment_identity_resume_contract.sql")

        database_uuid = str(uuid4())
        historical_promotion_id = str(uuid4())
        target_promotion_id = str(uuid4())
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ops_control.environment_identity
                     (identity_key,environment,database_identity_id,database_role,database_name,client_code)
                   VALUES ('primary','production',%s::uuid,'platform',%s,NULL)""",
                (database_uuid, database),
            )
            cur.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                (resume.RESUME_SCHEMA_MIGRATION,),
            )
            cur.execute(
                """INSERT INTO ops_control.environment_identity_promotion (
                     promotion_id,source_environment,target_environment,platform_identity_id,
                     selected_clients,immutable_plan_json,plan_sha256,state,started_at,
                     completed_at,current_step,completed_steps,operator_attestation_hash,
                     backup_reference,resume_contract,resume_plan_sha256)
                   VALUES
                     (%s::uuid,'local_dev','production',%s::uuid,'[{}]'::jsonb,
                      '{}'::jsonb,%s,'completed',now(),now(),NULL,'[]'::jsonb,%s,
                      '/disposable/historical',NULL,NULL),
                     (%s::uuid,'local_dev','production',%s::uuid,'[{}]'::jsonb,
                      '{}'::jsonb,%s,'in_progress',now(),NULL,'runtime_reload_required',
                      '[]'::jsonb,%s,'/disposable/target',NULL,NULL)""",
                (
                    historical_promotion_id, database_uuid, "1" * 64, "2" * 64,
                    target_promotion_id, database_uuid, "3" * 64, "4" * 64,
                ),
            )
        conn.commit()

        clean_before_history = collect(
            conn, database, database_uuid, target_promotion_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE ops_control.environment_identity_promotion
                      SET resume_contract='resume-v2', resume_plan_sha256=%s
                    WHERE promotion_id=%s::uuid""",
                ("5" * 64, historical_promotion_id),
            )
        conn.commit()
        contract = collect(conn, database, database_uuid, target_promotion_id)
        conn.rollback()
        assert contract == clean_before_history
        assert contract["schema_migration"]["filename_count"] == 1
        assert contract["schema_migration"]["ceiling"] == resume.RESUME_SCHEMA_MIGRATION
        assert [row["attnum"] for row in contract["columns"]] == [22, 23]
        assert contract["trigger_function"]["prosrc_sha256"] == resume.RESUME_TRIGGER_FUNCTION_PROSRC_SHA256
        assert contract["trigger"]["enabled"] == "O"
        assert contract["trigger"]["timing"] == "BEFORE"
        assert contract["trigger"]["events"] == ["UPDATE"]
        assert contract["trigger"]["row_level"] is True
        assert contract["target_promotion_resume_audit_state"] == {
            "promotion_id": target_promotion_id,
            "non_null_resume_audit_row_count": 0,
        }
        assert contract["comment_policy"] == "exact_text_required_block_on_absent_or_changed"
        assert contract["volatile_catalog_oids_bound"] is False
        for row in contract["columns"]:
            assert row["identity_kind"] == resume.CANONICAL_SCHEMA_ABSENCE["identity_kind"]
            assert row["generated_kind"] == resume.CANONICAL_SCHEMA_ABSENCE["generated_kind"]
            assert row["default_expression"] == resume.CANONICAL_SCHEMA_ABSENCE["default_expression"]
        assert (
            contract["trigger_function"]["identity_arguments"]
            == resume.CANONICAL_SCHEMA_ABSENCE["identity_arguments"]
        )
        assert (
            contract["trigger_function"]["proconfig"]
            == resume.CANONICAL_SCHEMA_ABSENCE["proconfig"]
        )

        # Use the exact real schema collector output and exact real remote
        # collector output in the real plan builder. No collector structure is
        # reconstructed or replaced with a hand-written binding.
        with tempfile.TemporaryDirectory() as directory:
            _bare, work = repositories(Path(directory))
            head = git_run(work, "rev-parse", "HEAD")
            remote_binding = resume.remote_repository_binding(
                work, local_head=head, local_branch="main",
            )
            fixture = fixture_plan()
            approval = {
                **fixture["approval_identity"],
                "resume_implementation_head": head,
                "promotion_id": target_promotion_id,
            }
            journal_state = {
                **fixture["journal_state"],
                "promotion_id": target_promotion_id,
            }
            plan = resume.build_plan(
                approval_identity=approval,
                remote_repository_binding=remote_binding,
                migration_054_schema_contract=contract,
                journal_state=journal_state,
                persistent_state=fixture["persistent_state"],
                runtime_convergence=fixture["runtime_convergence"],
                systemd_runtime=fixture["systemd_runtime"],
                docker_runtime=fixture["docker_runtime"],
                security_and_recovery=fixture["security_and_recovery"],
                promotion_id=target_promotion_id,
            )
            resume.validate_plan(plan)
            canonical = promotion.canonical_json(plan)
            digest = promotion.plan_hash(plan)
            attestation = resume.attestation(plan)
            assert len(canonical) > 0 and len(digest) == 64
            assert f"resume_plan_sha256={digest}" in attestation
            assert plan["contract_version"] == 3
            assert plan["attestation_contract"] == "resume-v2"
            assert plan["migration_054_schema_contract"][
                "target_promotion_resume_audit_state"
            ]["promotion_id"] == plan["journal_state"]["promotion_id"]

            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ops_control.environment_identity_promotion WHERE promotion_id=%s::uuid",
                    (historical_promotion_id,),
                )
            without_history = collect(
                conn, database, database_uuid, target_promotion_id,
            )
            assert without_history == contract
            assert promotion.plan_hash({
                "schema": without_history,
            }) == promotion.plan_hash({"schema": contract})
            conn.rollback()

            for replacement in (None, ""):
                changed = deepcopy(plan)
                changed["migration_054_schema_contract"]["columns"][0][
                    "default_expression"
                ] = replacement
                try:
                    resume.validate_plan(changed)
                except promotion.PromotionError as exc:
                    assert exc.code == "RESUME_IMMUTABLE_BINDING_DRIFT"
                else:
                    raise AssertionError("null/empty collector evidence was accepted")
            changed = deepcopy(plan)
            del changed["migration_054_schema_contract"]["columns"][0][
                "default_expression"
            ]
            expect_schema_drift(lambda: resume.validate_plan(changed))

        expect_schema_drift(
            lambda: collect(
                conn, database + "_wrong", database_uuid, target_promotion_id,
            )
        )
        expect_schema_drift(
            lambda: collect(
                conn, database, str(uuid4()), target_promotion_id,
            )
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """UPDATE ops_control.environment_identity_promotion
                          SET resume_contract=NULL, resume_plan_sha256=NULL
                        WHERE promotion_id=%s::uuid""",
                    (historical_promotion_id,),
                )
            except psycopg.Error:
                conn.rollback()
            else:
                raise AssertionError("completed historical resume evidence was cleared")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ops_control.environment_identity_promotion WHERE promotion_id=%s::uuid",
                (historical_promotion_id,),
            )
        conn.commit()

        migration_053 = (
            ROOT / "db/migrations/053_environment_identity_promotion_journal.sql"
        ).read_text(encoding="utf-8")
        mutations = (
            ("migration history row missing",
             "DELETE FROM public.schema_migrations WHERE filename='054_environment_identity_resume_contract.sql'"),
            ("migration ceiling changed",
             "INSERT INTO public.schema_migrations(filename) VALUES ('055_unapproved.sql')"),
            ("one column missing",
             "ALTER TABLE ops_control.environment_identity_promotion DROP COLUMN resume_contract CASCADE"),
            ("wrong column type",
             """ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_plan_hash;
                ALTER TABLE ops_control.environment_identity_promotion
                  ALTER COLUMN resume_plan_sha256 TYPE bigint
                  USING NULLIF(resume_plan_sha256,'')::bigint"""),
            ("unexpected default",
             "ALTER TABLE ops_control.environment_identity_promotion ALTER COLUMN resume_contract SET DEFAULT 'resume-v2'"),
            ("identity column unexpectedly configured",
             """ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_contract;
                ALTER TABLE ops_control.environment_identity_promotion
                  ALTER COLUMN resume_contract TYPE bigint USING 1;
                ALTER TABLE ops_control.environment_identity_promotion
                  ALTER COLUMN resume_contract SET NOT NULL;
                ALTER TABLE ops_control.environment_identity_promotion
                  ALTER COLUMN resume_contract ADD GENERATED BY DEFAULT AS IDENTITY"""),
            ("generated column unexpectedly configured",
             """ALTER TABLE ops_control.environment_identity_promotion
                  DROP COLUMN resume_contract CASCADE;
                ALTER TABLE ops_control.environment_identity_promotion
                  ADD COLUMN resume_contract text
                  GENERATED ALWAYS AS ('resume-v2'::text) STORED"""),
            ("wrong nullability",
             """TRUNCATE ops_control.environment_identity_promotion;
                ALTER TABLE ops_control.environment_identity_promotion ALTER COLUMN resume_contract SET NOT NULL"""),
            ("one constraint missing",
             "ALTER TABLE ops_control.environment_identity_promotion DROP CONSTRAINT ck_environment_identity_promotion_resume_contract"),
            ("same-named constraint wrong predicate",
             """ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_contract;
                ALTER TABLE ops_control.environment_identity_promotion
                  ADD CONSTRAINT ck_environment_identity_promotion_resume_contract
                  CHECK (resume_contract IS NULL OR resume_contract IN ('resume-v2','other'))"""),
            ("constraint not valid",
             """ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_contract;
                ALTER TABLE ops_control.environment_identity_promotion
                  ADD CONSTRAINT ck_environment_identity_promotion_resume_contract
                  CHECK (resume_contract IS NULL OR resume_contract = 'resume-v2') NOT VALID"""),
            ("trigger function body changed",
             """CREATE OR REPLACE FUNCTION ops_control.reject_environment_identity_promotion_plan_update()
                  RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"""),
            ("function owner changed",
             "ALTER FUNCTION ops_control.reject_environment_identity_promotion_plan_update() OWNER TO resume_v2_changed_owner"),
            ("function security mode changed",
             "ALTER FUNCTION ops_control.reject_environment_identity_promotion_plan_update() SECURITY DEFINER"),
            ("function volatility changed",
             "ALTER FUNCTION ops_control.reject_environment_identity_promotion_plan_update() STABLE"),
            ("function search path changed",
             "ALTER FUNCTION ops_control.reject_environment_identity_promotion_plan_update() SET search_path=pg_catalog,ops_control"),
            ("trigger function name overloaded with arguments",
             """CREATE FUNCTION ops_control.reject_environment_identity_promotion_plan_update(unexpected text)
                  RETURNS text LANGUAGE sql IMMUTABLE AS 'SELECT unexpected'"""),
            ("trigger disabled",
             "ALTER TABLE ops_control.environment_identity_promotion DISABLE TRIGGER trg_environment_identity_promotion_plan_immutable"),
            ("trigger rebound to another function",
             """CREATE FUNCTION ops_control.alternate_resume_trigger() RETURNS trigger
                  LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;
                DROP TRIGGER trg_environment_identity_promotion_plan_immutable
                  ON ops_control.environment_identity_promotion;
                CREATE TRIGGER trg_environment_identity_promotion_plan_immutable
                  BEFORE UPDATE ON ops_control.environment_identity_promotion
                  FOR EACH ROW EXECUTE FUNCTION ops_control.alternate_resume_trigger()"""),
            ("trigger definition changed",
             """DROP TRIGGER trg_environment_identity_promotion_plan_immutable
                  ON ops_control.environment_identity_promotion;
                CREATE TRIGGER trg_environment_identity_promotion_plan_immutable
                  AFTER UPDATE ON ops_control.environment_identity_promotion
                  FOR EACH ROW EXECUTE FUNCTION ops_control.reject_environment_identity_promotion_plan_update()"""),
            ("blocking comment changed",
             """COMMENT ON COLUMN ops_control.environment_identity_promotion.resume_contract
                  IS 'advisory-only changed comment'"""),
            ("columns and constraints with migration-053 function", migration_053),
            ("columns zero constraints function-053 no history", f"""
                DELETE FROM public.schema_migrations
                 WHERE filename='054_environment_identity_resume_contract.sql';
                ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_contract;
                ALTER TABLE ops_control.environment_identity_promotion
                  DROP CONSTRAINT ck_environment_identity_promotion_resume_plan_hash;
                {migration_053}
            """),
            ("columns two constraints function-053 no history", f"""
                DELETE FROM public.schema_migrations
                 WHERE filename='054_environment_identity_resume_contract.sql';
                {migration_053}
            """),
        )
        for label, sql in mutations:
            mutate_and_refuse(
                conn, sql, database, database_uuid, target_promotion_id,
                label=label,
                expected_codes=(
                    ("ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE",)
                    if label == "one column missing"
                    else ("RESUME_SCHEMA_CONTRACT_DRIFT",)
                ),
            )

        for resume_contract, resume_plan_sha256 in (
            ("resume-v2", None),
            (None, "6" * 64),
            ("resume-v2", "7" * 64),
        ):
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE ops_control.environment_identity_promotion
                          SET resume_contract=%s, resume_plan_sha256=%s
                        WHERE promotion_id=%s::uuid""",
                    (resume_contract, resume_plan_sha256, target_promotion_id),
                )
            try:
                collect(conn, database, database_uuid, target_promotion_id)
            except promotion.PromotionError as exc:
                assert exc.code == "RESUME_SCHEMA_CONTRACT_DRIFT"
                assert str(exc) == (
                    "RESUME_SCHEMA_CONTRACT_DRIFT: target promotion resume audit "
                    "columns already contain values"
                )
                assert exc.details == {
                    "writes_performed": False,
                    "promotion_id": target_promotion_id,
                }
            else:
                raise AssertionError("target resume audit state was accepted")
            finally:
                conn.rollback()

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ops_control.environment_identity_promotion WHERE promotion_id=%s::uuid",
                (target_promotion_id,),
            )
        missing_contract = collect(
            conn, database, database_uuid, target_promotion_id,
        )
        assert missing_contract["target_promotion_resume_audit_state"][
            "non_null_resume_audit_row_count"
        ] == 0
        try:
            cli._resume_plan(
                SimpleNamespace(promotion_id=target_promotion_id),
                conn,
                {},
                SimpleNamespace(),
            )
        except promotion.PromotionError as exc:
            assert exc.code == "PROMOTION_NOT_FOUND"
        else:
            raise AssertionError("missing target journal reached plan construction")
        conn.rollback()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                    (resume.RESUME_SCHEMA_MIGRATION,),
                )
        except psycopg.errors.UniqueViolation:
            conn.rollback()
        else:
            raise AssertionError("duplicate migration-history row was reachable")

        # Every observed stable field participates in canonical plan bytes.
        baseline = promotion.plan_hash({"schema": contract})
        for section, key, value in (
            ("schema_migration", "ceiling", "055_unapproved.sql"),
            ("columns", None, [
                {**contract["columns"][0], "data_type": "integer"},
                *contract["columns"][1:],
            ]),
            ("constraints", None, [
                {**contract["constraints"][0], "validated": False},
                *contract["constraints"][1:],
            ]),
            ("trigger_function", "prosrc_sha256", "0" * 64),
            ("trigger_function", "security_definer", True),
            ("trigger", "enabled", "D"),
            ("trigger", "definition", "changed"),
            ("target_promotion_resume_audit_state", None, {
                "promotion_id": target_promotion_id,
                "non_null_resume_audit_row_count": 1,
            }),
        ):
            changed = {
                **contract,
                section: value if key is None else {**contract[section], key: value},
            }
            assert promotion.plan_hash({"schema": changed}) != baseline
        print("resume-v2 migration-054 schema contract tests: OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
