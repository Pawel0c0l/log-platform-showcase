-- 055_workflow_a_trips_pagination_mode.sql
-- Inert per-client configuration contract for future Telematics /trips
-- pagination compatibility. No runtime path consumes this value yet.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS trips_pagination_mode TEXT;

UPDATE workflow_a_control.client_account
   SET trips_pagination_mode = 'strict_meta'
 WHERE trips_pagination_mode IS NULL;

ALTER TABLE workflow_a_control.client_account
  ALTER COLUMN trips_pagination_mode SET DEFAULT 'strict_meta';

ALTER TABLE workflow_a_control.client_account
  ALTER COLUMN trips_pagination_mode SET NOT NULL;

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_pagination_mode;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_pagination_mode
  CHECK (trips_pagination_mode IN ('strict_meta', 'data_invariants_v1'));
