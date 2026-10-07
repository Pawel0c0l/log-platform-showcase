-- 053_write_test_snapshot_retention_privileges.sql
-- The two grants migration 052 could not have known about.
--
-- WHY A SECOND FILE. `052_retention_runtime_privileges.sql` is applied to all
-- five client databases and is immutable, so the relations discovered after it
-- shipped get their grants here. Nothing in 052 is changed, revoked or
-- restated; `GRANT` is additive and idempotent, and this file is a strict
-- extension of the same least-privilege list.
--
-- WHICH RELATIONS, AND HOW THEY WERE FOUND. Two pre-write snapshots an operator
-- took in `alpha_main` on 2026-06-19 before a D105.2 EcoDriving write test:
--
--   telematics_reports.backup_client_trips_d105_2_write_test_20260619_100539
--   telematics_reports.backup_d105_2_ecodriving_write_test_20260619_100539
--
-- The first is a full copy of customer trip rows and is read by
-- `ops/reports/d105_2_ecodriving_alpha00001_local_write_test_rollback_20260619_100539.sql`;
-- the second is a copy of the Stage 3 report table. No DDL in this repository
-- creates either, so the migration-parsing coverage check could never see them
-- — a live `python -m ops.retention_registry --coverage --clients` census did.
-- They are now governed by `client_db.legacy_backup_tables`, the same
-- deprecated-copy policy that already governs the migration 020/021 leftovers,
-- at the global 13-calendar-month ceiling. No new policy, no new exemption.
--
-- WHAT IT GRANTS, AND NOTHING MORE. `SELECT, DELETE`, for exactly the reasons
-- 052 states: the sweep must count eligible rows and read `ctid` before it
-- removes anything, and the batch is a single
-- `DELETE … WHERE ctid IN (SELECT … LIMIT n)`. No INSERT, no UPDATE, no
-- TRUNCATE, no ownership, and no `GRANT … ON ALL TABLES IN SCHEMA` — that last
-- one would silently widen to every future relation in a schema that also holds
-- operator and forensic material.
--
-- BY EXACT NAME, NOT BY PATTERN. A `backup_%` match over `telematics_reports`
-- would confer destructive rights on relations nobody decided to govern. The
-- array below is two names.
--
-- THE GPS ASSIGNMENT LOG IS STILL ABSENT, for the reason 052 gives: it is the
-- platform's one owner-approved exemption from age-based retention, the sweep
-- plans no DELETE and runs no query there, so it needs no privilege there.
--
-- ALREADY TRUE IN PRODUCTION. Read-only verification on 2026-08-29 shows
-- `alpha_user` already holding SELECT and DELETE on both relations, so
-- applying this file changes nothing on the current fleet. It exists so a
-- rebuilt or re-provisioned client database is correct by construction rather
-- than by accident, and so the repository invariant "every relation the sweep
-- touches is granted" holds in the file rather than only in the live database.
--
-- IDEMPOTENT AND ADDITIVE. Every statement is guarded by `to_regclass`, so the
-- four clients that do not have these relations skip them. No data is read,
-- written or deleted by this file.

DO $write_test_snapshot_privileges$
DECLARE
    grantee_row  record;
    relation     text;
    relations text[] := ARRAY[
        'telematics_reports.backup_client_trips_d105_2_write_test_20260619_100539',
        'telematics_reports.backup_d105_2_ecodriving_write_test_20260619_100539'
    ];
BEGIN
    -- The same grantee discovery migrations 040 and 052 use: the roles holding
    -- runtime privileges on `public.client_trips` are by definition this
    -- client's runtime grantees. Existing-client migrations get no
    -- `client_db_user` placeholder, so the grantee is derived, never named.
    FOR grantee_row IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
    LOOP
        IF to_regnamespace('telematics_reports') IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA telematics_reports TO %I',
                           grantee_row.grantee);
        END IF;

        FOREACH relation IN ARRAY relations LOOP
            IF to_regclass(relation) IS NOT NULL THEN
                EXECUTE format('GRANT SELECT, DELETE ON TABLE %s TO %I',
                               relation, grantee_row.grantee);
            END IF;
        END LOOP;
    END LOOP;
END $write_test_snapshot_privileges$;
