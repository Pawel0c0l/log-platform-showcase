-- 032_eco_driving_weekly_email_notifications.sql
-- Workflow A — Eco Driving weekly notification email send log.

CREATE TABLE IF NOT EXISTS public.eco_driving_weekly_email_send_log (
  send_log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  run_id TEXT NULL,
  assigned_id TEXT NOT NULL,
  recipient_email TEXT NOT NULL,
  original_recipient_email TEXT NULL,
  ranking_type TEXT NULL,
  report_type TEXT NOT NULL DEFAULT 'weekly',
  template_type TEXT NOT NULL,
  template_filename TEXT NOT NULL,
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

  CONSTRAINT chk_eco_driving_weekly_email_send_log_report_type
    CHECK (report_type = 'weekly'),
  CONSTRAINT chk_eco_driving_weekly_email_send_log_period
    CHECK (period_end_date > period_start_date),
  CONSTRAINT chk_eco_driving_weekly_email_send_log_status
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
  CONSTRAINT chk_eco_driving_weekly_email_send_log_sent_at
    CHECK (
      (status = 'sent' AND sent_at IS NOT NULL)
      OR (status <> 'sent' AND sent_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_eco_driving_weekly_email_send_log_client_period
  ON public.eco_driving_weekly_email_send_log (
    client_id,
    period_start_date,
    period_end_date
  );

CREATE INDEX IF NOT EXISTS idx_eco_driving_weekly_email_send_log_assigned_period
  ON public.eco_driving_weekly_email_send_log (
    client_id,
    assigned_id,
    period_start_date,
    period_end_date
  );

CREATE INDEX IF NOT EXISTS idx_eco_driving_weekly_email_send_log_status
  ON public.eco_driving_weekly_email_send_log (status, attempted_at);

-- Normal successful sends are unique for idempotency. Explicit force_resend
-- attempts are stored as new sent rows with metadata_json.force_resend=true.
CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_driving_weekly_email_send_log_sent_once
  ON public.eco_driving_weekly_email_send_log (
    client_id,
    assigned_id,
    report_type,
    period_start_date,
    period_end_date,
    template_type
  )
  WHERE status = 'sent'
    AND metadata_json->>'force_resend' IS DISTINCT FROM 'true';

COMMENT ON TABLE public.eco_driving_weekly_email_send_log IS
  'Audit/idempotency log for Workflow A Eco Driving weekly driver notification emails. status=sent means the SMTP server accepted the message without raising an exception; it does not guarantee inbox delivery.';

COMMENT ON COLUMN public.eco_driving_weekly_email_send_log.original_recipient_email IS
  'Original driver email from eco_drivers_id_chart when test_recipient_email redirects delivery.';

COMMENT ON INDEX public.uq_eco_driving_weekly_email_send_log_sent_once IS
  'Prevents duplicate normal successful sends for one client/driver/weekly period/template. Rows with metadata_json.force_resend=true are intentional resend attempts.';
