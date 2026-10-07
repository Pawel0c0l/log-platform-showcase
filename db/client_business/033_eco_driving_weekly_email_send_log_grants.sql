-- 033_eco_driving_weekly_email_send_log_grants.sql
-- Workflow A — additive weekly email send-log audit columns and grants.

ALTER TABLE IF EXISTS public.eco_driving_weekly_email_send_log
  ADD COLUMN IF NOT EXISTS qualification_status TEXT NULL,
  ADD COLUMN IF NOT EXISTS ranking_included BOOLEAN NULL,
  ADD COLUMN IF NOT EXISTS template_variant TEXT NULL;

ALTER TABLE IF EXISTS public.eco_driving_weekly_email_send_log
  DROP CONSTRAINT IF EXISTS chk_eco_driving_weekly_email_send_log_template_variant,
  ADD CONSTRAINT chk_eco_driving_weekly_email_send_log_template_variant
    CHECK (
      template_variant IS NULL
      OR template_variant IN ('ranked', 'norank', 'low_distance')
    );

COMMENT ON COLUMN public.eco_driving_weekly_email_send_log.qualification_status IS
  'Eco Driving stats qualification_status used when selecting the weekly notification template.';

COMMENT ON COLUMN public.eco_driving_weekly_email_send_log.ranking_included IS
  'Current eco_drivers_id_chart.ranking_included value used when selecting ranked versus no-ranking templates.';

COMMENT ON COLUMN public.eco_driving_weekly_email_send_log.template_variant IS
  'Weekly email template selection branch: ranked, norank, or low_distance.';

DO $$
DECLARE
    grant_row record;
BEGIN
    -- Mirror standard client-business DML grantees from public.client_trips.
    -- Existing-client migrations run as admin and do not receive the
    -- client_db_user name as a placeholder.
    FOR grant_row IN
        SELECT grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
        FROM (
            SELECT DISTINCT grantee, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_schema = 'public'
              AND table_name = 'client_trips'
              AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
        ) AS grants
        GROUP BY grantee
    LOOP
        EXECUTE format(
            'GRANT %s ON TABLE public.eco_driving_weekly_email_send_log TO %I',
            grant_row.privileges,
            grant_row.grantee
        );
    END LOOP;
END $$;
