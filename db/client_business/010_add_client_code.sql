-- 010_add_client_code.sql
-- Workflow A v1 extension: add human-readable client_code alongside canonical client_id.
-- client_code (e.g. DELTA00001) is operator-friendly and does not replace UUID client_id.

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS client_code TEXT;

ALTER TABLE IF EXISTS public.client_speeding_notifications
  ADD COLUMN IF NOT EXISTS client_code TEXT;

