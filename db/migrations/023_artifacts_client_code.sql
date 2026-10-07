-- 023_artifacts_client_code.sql
-- Add optional operator-friendly client_code metadata to artifacts.
-- Existing artifact rows remain valid with NULL client_code.

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS client_code TEXT;

CREATE INDEX IF NOT EXISTS idx_artifacts_client_code
  ON artifacts (client_code);
