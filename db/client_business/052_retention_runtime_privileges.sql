-- 052_retention_runtime_privileges.sql
-- Least-privilege grants that let the hard-retention worker actually govern the
-- registered client-business tables.
--
-- WHY THIS MIGRATION EXISTS. A retention policy a role cannot execute is not a
-- policy. The read-only production rehearsal of `ops/hard_retention.py` found 27
-- (client, table) pairs answering `permission denied` — `eco_driver_weekly_stats`,
-- `eco_driver_monthly_stats`, `eco_trip_assignments`, `eco_drivers_id_chart`,
-- `eco_driving_{weekly,monthly}_email_send_log`, the Workflow B Stage 3 report
-- tables, the GPS import history, the legacy trip backups and the V2 staging
-- tables — across BRAVO00016, FOXTROT00001, DELTA00001 and ECHO00001. Those stores
-- were reported as ungoverned, which is the correct fail-closed answer and not a
-- state anyone should ship. The same gap already applied to
-- `jobs.api.telematics.retention_purge`; it was invisible only because 68 of its
-- 69 policy rows are disabled.
--
-- WHAT IT GRANTS, AND NOTHING MORE.
--
--   SELECT  the sweep must COUNT eligible rows, find the oldest surviving one
--           and read `ctid` before it deletes anything. Dry run needs exactly
--           this and nothing else, which is why SELECT is granted even on
--           tables the worker will never delete from.
--   DELETE  the one mutation the supported cleanup performs. The batch is a
--           single `DELETE … WHERE ctid IN (SELECT … LIMIT n)` statement, which
--           needs exactly `SELECT` and `DELETE` and nothing else.
--
-- WHY THERE IS NO `UPDATE` HERE, AND WHY THE SWEEP GAVE SOMETHING UP FOR THAT.
-- The batch originally used `FOR UPDATE SKIP LOCKED`, so a row held by another
-- transaction was deferred rather than waited on. PostgreSQL requires the
-- UPDATE privilege for ANY row-locking clause, so keeping that behaviour would
-- have meant granting a retention role the ability to rewrite customer trip and
-- driver rows. Deletion is destructive; silent falsification of business
-- records is worse, and it is a power this job has no use for. The locking
-- clause was removed instead. Correctness is unaffected — the subquery and the
-- delete are one statement under one snapshot, so no `ctid` can be reclaimed
-- and reused between choosing and deleting a row — and the contended case is
-- bounded by `lock_timeout` and retried on the next run.
--
-- No INSERT. No UPDATE. No TRUNCATE. No REFERENCES. No ownership, no schema
-- ownership, no `ALL PRIVILEGES`, no `GRANT … ON ALL TABLES IN SCHEMA` — that
-- last one would silently widen to every future table, which is precisely the
-- kind of blanket authority a retention role must not accumulate.
--
-- WHY THE GPS ASSIGNMENT LOG IS NOT IN THE LIST. `ops/retention_registry.py`
-- records `telematics_reports."Alpha_GPS_Baza_LOG"` (and its pre-rename twin) as
-- the platform's one owner-approved exemption from age-based retention:
-- `Mode.OWNER_EXEMPT`, no anchor, no cleanup job. The hard-retention worker
-- plans no DELETE and runs no query there, so it needs no privilege there, and
-- granting one anyway would hand a retention role destructive rights over a
-- store it is forbidden to touch. Least privilege means the grant follows the
-- supported operation, and the supported operation no longer exists.
--
-- This removes nothing from anybody: the file has never been applied, `GRANT`
-- is additive, and the ordinary Workflow B full-replace import — which does
-- `DELETE FROM telematics_reports."Alpha_GPS_Baza_LOG"` before reinserting the
-- workbook — runs as the Stage 3 loader on rights it already holds, not on
-- rights this migration would confer. Its import history
-- (`alpha_gps_baza_log_import_runs`) IS still swept, under
-- `client_db.workflow_b_gps_assignment_import_runs`, and stays in the list.
--
-- WHO IS GRANTED. The same discovery the repository already uses (migration
-- `040`): the roles that hold runtime privileges on `public.client_trips` are
-- by definition this client's runtime grantees. Existing-client migrations get
-- no `client_db_user` placeholder, so the grantee is derived rather than named.
--
-- IDEMPOTENT AND ADDITIVE. `GRANT` is idempotent, every statement is guarded by
-- `to_regclass`, and a client that lacks a relation simply skips it — the five
-- client databases genuinely differ (only BRAVO00016 and ALPHA00001 carry the
-- Stage 3 report tables, only ALPHA00001 and FOXTROT00001 carry the GPS
-- relations). No data is read, written or deleted by this file.

DO $retention_privileges$
DECLARE
    grantee_row  record;
    relation     text;
    -- Every relation named by a `client_db.*` policy in
    -- `ops/retention_registry.py` whose cleanup job is `ops.hard_retention`.
    -- Schema-qualified and quoted exactly as the catalog stores them. A policy
    -- with no cleanup job — lifecycle-bound, not-applicable, or the
    -- owner-exempt GPS assignment log — grants nothing, because nothing runs
    -- against it.
    relations text[] := ARRAY[
        -- Workflow A registered tables (jobs/api/telematics/registry.py)
        'public.client_trips',
        'public.client_speeding_notifications',
        'public.client_vehicle_daily_fuel',
        'public.client_vehicle_driver_daily_fuel',
        'public.eco_trip_assignments',
        'public.eco_driver_weekly_stats',
        'public.eco_driver_monthly_stats',
        'public.eco_person_people',
        'public.eco_person_driver_mappings',
        'public.eco_person_trip_assignments',
        'public.eco_person_weekly_stats',
        'public.eco_person_monthly_stats',
        'public.eco_person_weekly_email_send_log',
        'public.eco_person_monthly_email_send_log',
        -- Registered by the retention registry, absent from registry.TABLES
        'public.eco_driving_weekly_email_send_log',
        'public.eco_driving_monthly_email_send_log',
        'public.eco_drivers_id_chart',
        'public.eco_dashboard_delivery_operation',
        -- Workflow B Stage 3 destinations (created by the loader at runtime)
        'telematics_reports.report_207',
        'telematics_reports.report_d105_2_ecodriving',
        -- The GPS assignment log itself is DELIBERATELY ABSENT. See the
        -- owner-exemption note in the header.
        'telematics_reports.alpha_gps_baza_log_import_runs',
        -- Deprecated copies that still hold customer trip rows. The
        -- `_rebuilt_` names are the transient tables migrations 020/021 build
        -- before renaming them to `_legacy_backup_`; they are listed so a
        -- migration interrupted mid-rename leaves nothing ungovernable.
        'public.client_trips_legacy_backup_020',
        'public.client_trips_legacy_backup_021',
        'public.client_trips_rebuilt_020',
        'public.client_trips_rebuilt_021',
        -- Declared-only V2 staging; empty everywhere, governed so a future
        -- loader cannot start writing into an ungoverned store
        'public.source_trips',
        'public.source_notifications',
        'public.source_fuel_observations'
    ];
BEGIN
    FOR grantee_row IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
    LOOP
        -- USAGE on the schemas the relations live in. Without it a table grant
        -- is unusable, and `telematics_reports`/`telematics_reports` are owned by the
        -- Stage 3 loader rather than by the runtime role.
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', grantee_row.grantee);
        IF to_regnamespace('telematics_reports') IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA telematics_reports TO %I', grantee_row.grantee);
        END IF;
        IF to_regnamespace('telematics_reports') IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SCHEMA telematics_reports TO %I', grantee_row.grantee);
        END IF;

        FOREACH relation IN ARRAY relations LOOP
            -- `to_regclass` answers NULL for a relation this client does not
            -- have, which is the normal case rather than an error: the fleet's
            -- schemas legitimately differ.
            IF to_regclass(relation) IS NOT NULL THEN
                EXECUTE format('GRANT SELECT, DELETE ON TABLE %s TO %I',
                               relation, grantee_row.grantee);
            END IF;
        END LOOP;
    END LOOP;
END $retention_privileges$;
