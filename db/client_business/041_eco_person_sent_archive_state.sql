-- 041_eco_person_sent_archive_state.sql
-- Preserve exact sent MIME bytes and track Sent-folder archiving independently
-- from SMTP delivery status for Eco Driving Person email jobs.

ALTER TABLE IF EXISTS public.eco_person_weekly_email_send_log
  ADD COLUMN IF NOT EXISTS sent_mime_bytes BYTEA NULL,
  ADD COLUMN IF NOT EXISTS sent_mime_sha256 TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_status TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_mailbox TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_message_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_attempted_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_verified_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_error TEXT NULL;

ALTER TABLE IF EXISTS public.eco_person_monthly_email_send_log
  ADD COLUMN IF NOT EXISTS sent_mime_bytes BYTEA NULL,
  ADD COLUMN IF NOT EXISTS sent_mime_sha256 TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_status TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_mailbox TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_message_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_attempted_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_verified_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sent_archive_error TEXT NULL;

ALTER TABLE IF EXISTS public.eco_person_weekly_email_send_log
  DROP CONSTRAINT IF EXISTS chk_eco_person_weekly_email_sent_archive_status,
  ADD CONSTRAINT chk_eco_person_weekly_email_sent_archive_status
  CHECK (
    sent_archive_status IS NULL
    OR sent_archive_status IN ('pending','appended','already_present','failed')
  );

ALTER TABLE IF EXISTS public.eco_person_monthly_email_send_log
  DROP CONSTRAINT IF EXISTS chk_eco_person_monthly_email_sent_archive_status,
  ADD CONSTRAINT chk_eco_person_monthly_email_sent_archive_status
  CHECK (
    sent_archive_status IS NULL
    OR sent_archive_status IN ('pending','appended','already_present','failed')
  );

CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_sent_archive_status
  ON public.eco_person_weekly_email_send_log (sent_archive_status, sent_archive_attempted_at)
  WHERE sent_archive_status IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_sent_archive_message_id
  ON public.eco_person_weekly_email_send_log (sent_archive_message_id)
  WHERE sent_archive_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_sent_archive_status
  ON public.eco_person_monthly_email_send_log (sent_archive_status, sent_archive_attempted_at)
  WHERE sent_archive_status IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_sent_archive_message_id
  ON public.eco_person_monthly_email_send_log (sent_archive_message_id)
  WHERE sent_archive_message_id IS NOT NULL;

COMMENT ON COLUMN public.eco_person_weekly_email_send_log.sent_mime_bytes IS
  'Exact RFC 5322 MIME bytes accepted by SMTP; used only for Sent-folder archive retry without SMTP resend.';
COMMENT ON COLUMN public.eco_person_weekly_email_send_log.sent_archive_status IS
  'Independent Sent-folder archive outcome after SMTP success: pending, appended, already_present, or failed.';
COMMENT ON COLUMN public.eco_person_monthly_email_send_log.sent_mime_bytes IS
  'Exact RFC 5322 MIME bytes accepted by SMTP; reserved for Sent-folder archive retry without SMTP resend.';
COMMENT ON COLUMN public.eco_person_monthly_email_send_log.sent_archive_status IS
  'Independent Sent-folder archive outcome after SMTP success: pending, appended, already_present, or failed.';
