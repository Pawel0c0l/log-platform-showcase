-- 010_add_client_code.sql
-- Adds optional client_code to Workflow A control-plane client_account.
--
-- client_code: human-readable identifier for operators and UIs (e.g. DELTA00001),
-- distinct from client_id (UUID). Nullable in v1; when set, values must be unique.

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS client_code TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_client_account_client_code
  ON workflow_a_control.client_account (client_code);
