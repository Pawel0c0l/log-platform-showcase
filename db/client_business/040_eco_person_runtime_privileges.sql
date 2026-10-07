-- 040_eco_person_runtime_privileges.sql
-- Workflow A - least-privilege runtime grants for Eco Driving Person jobs.
--
-- Migration 039 creates the isolated eco_person_* objects as the migration
-- owner. Existing-client migrations do not receive a client_db_user placeholder,
-- so this migration mirrors the established repository pattern: discover the
-- client runtime grantees from public.client_trips and grant only the extra
-- privileges the Eco Driving Person runtime jobs require.
--
-- ALPHA eco_driving_* objects are intentionally excluded.

DO $$
DECLARE
    grant_row record;
BEGIN
    FOR grant_row IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', grant_row.grantee);

        -- The jobs read these views directly; table grants from migration 039
        -- do not imply direct SELECT on views.
        EXECUTE format(
            'GRANT SELECT ON TABLE public.eco_person_driver_mappings_view TO %I',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT SELECT ON TABLE public.eco_person_people_email_view TO %I',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT SELECT ON TABLE public.eco_person_weekly_trends_view TO %I',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT SELECT ON TABLE public.eco_person_monthly_trends_view TO %I',
            grant_row.grantee
        );

        -- Aggregation recalculation can delete previously calculated stats
        -- before rewriting them. No other eco_person_* table needs DELETE.
        EXECUTE format(
            'GRANT DELETE ON TABLE public.eco_person_weekly_stats TO %I',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT DELETE ON TABLE public.eco_person_monthly_stats TO %I',
            grant_row.grantee
        );

        -- Expression indexes and the mapping trigger use these functions when
        -- runtime jobs insert/update people or mappings.
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION public.eco_person_normalize_driver_name(TEXT) TO %I',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION public.eco_person_driver_mappings_normalize_trigger() TO %I',
            grant_row.grantee
        );
    END LOOP;
END $$;
