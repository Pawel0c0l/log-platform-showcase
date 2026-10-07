-- 035_eco_driving_monthly_email_notifications.sql
-- Workflow A — Eco Driving monthly notification email send log.

CREATE TABLE IF NOT EXISTS public.eco_driving_monthly_email_send_log (
  send_log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  run_id TEXT NULL,
  assigned_id TEXT NOT NULL,
  recipient_email TEXT NOT NULL,
  original_recipient_email TEXT NULL,
  ranking_type TEXT NULL,
  report_type TEXT NOT NULL DEFAULT 'monthly',
  template_type TEXT NOT NULL,
  template_filename TEXT NOT NULL,
  qualification_status TEXT NULL,
  ranking_included BOOLEAN NULL,
  template_variant TEXT NULL,
  period_start_date DATE NOT NULL,
  period_end_date DATE NOT NULL,
  ecodriving_rating_type TEXT NOT NULL,
  email_subject TEXT NOT NULL,
  status TEXT NOT NULL,
  smtp_message_id TEXT NULL,
  provider_response TEXT NULL,
  error_message TEXT NULL,
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,

  CONSTRAINT chk_eco_driving_monthly_email_send_log_report_type
    CHECK (report_type = 'monthly'),
  CONSTRAINT chk_eco_driving_monthly_email_send_log_period
    CHECK (period_end_date > period_start_date),
  CONSTRAINT chk_eco_driving_monthly_email_send_log_status
    CHECK (
      status IN (
        'pending',
        'skipped_already_sent',
        'skipped_missing_email',
        'skipped_unknown_rating_type',
        'dry_run_rendered',
        'sent',
        'failed'
      )
    ),
  CONSTRAINT chk_eco_driving_monthly_email_send_log_sent_at
    CHECK (
      (status = 'sent' AND sent_at IS NOT NULL)
      OR (status <> 'sent' AND sent_at IS NULL)
    ),
  CONSTRAINT chk_eco_driving_monthly_email_send_log_template_variant
    CHECK (
      template_variant IS NULL
      OR template_variant IN ('ranked', 'norank', 'low_distance')
    )
);

CREATE INDEX IF NOT EXISTS idx_eco_driving_monthly_email_send_log_client_period
  ON public.eco_driving_monthly_email_send_log (
    client_id,
    period_start_date,
    period_end_date
  );

CREATE INDEX IF NOT EXISTS idx_eco_driving_monthly_email_send_log_assigned_period
  ON public.eco_driving_monthly_email_send_log (
    client_id,
    assigned_id,
    period_start_date,
    period_end_date
  );

CREATE INDEX IF NOT EXISTS idx_eco_driving_monthly_email_send_log_status
  ON public.eco_driving_monthly_email_send_log (status, attempted_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_driving_monthly_email_send_log_sent_once
  ON public.eco_driving_monthly_email_send_log (
    client_id,
    assigned_id,
    report_type,
    period_start_date,
    period_end_date,
    template_type
  )
  WHERE status = 'sent'
    AND metadata_json->>'force_resend' IS DISTINCT FROM 'true';

COMMENT ON TABLE public.eco_driving_monthly_email_send_log IS
  'Audit/idempotency log for Workflow A Eco Driving monthly driver notification emails. status=sent means the SMTP server accepted the message without raising an exception; it does not guarantee inbox delivery.';

COMMENT ON COLUMN public.eco_driving_monthly_email_send_log.original_recipient_email IS
  'Original driver email from eco_drivers_id_chart when test_recipient_email redirects delivery.';

COMMENT ON COLUMN public.eco_driving_monthly_email_send_log.qualification_status IS
  'Eco Driving stats qualification_status used when selecting the monthly notification template.';

COMMENT ON COLUMN public.eco_driving_monthly_email_send_log.ranking_included IS
  'Current eco_drivers_id_chart.ranking_included value used when selecting ranked versus no-ranking templates.';

COMMENT ON COLUMN public.eco_driving_monthly_email_send_log.template_variant IS
  'Monthly email template selection branch: ranked, norank, or low_distance.';

COMMENT ON INDEX public.uq_eco_driving_monthly_email_send_log_sent_once IS
  'Prevents duplicate normal successful sends for one client/driver/monthly period/template. Rows with metadata_json.force_resend=true are intentional resend attempts.';

DO $$
DECLARE
    grant_row record;
BEGIN
    -- Mirror standard client-business DML grantees from public.client_trips.
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
            'GRANT %s ON TABLE public.eco_driving_monthly_email_send_log TO %I',
            grant_row.privileges,
            grant_row.grantee
        );
    END LOOP;
END $$;
