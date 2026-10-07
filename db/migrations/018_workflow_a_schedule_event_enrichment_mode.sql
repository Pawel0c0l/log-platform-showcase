-- 018_workflow_a_schedule_event_enrichment_mode.sql
-- Workflow A — per-schedule event enrichment mode.
--
-- `event_enrichment_mode` controls whether scheduled trips_sync runs fetch
-- `/vehicles/events` enrichment or intentionally skip it. The column lives on
-- all client_dataset_schedule rows for a simple schedule schema, but the
-- dispatcher only forwards it to datasets that support the parameter.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD COLUMN IF NOT EXISTS event_enrichment_mode TEXT;

UPDATE workflow_a_control.client_dataset_schedule
   SET event_enrichment_mode = 'enabled'
 WHERE event_enrichment_mode IS NULL;

ALTER TABLE workflow_a_control.client_dataset_schedule
  ALTER COLUMN event_enrichment_mode SET DEFAULT 'enabled';

ALTER TABLE workflow_a_control.client_dataset_schedule
  ALTER COLUMN event_enrichment_mode SET NOT NULL;

ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS ck_client_dataset_schedule_event_enrichment_mode;

ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT ck_client_dataset_schedule_event_enrichment_mode
  CHECK (event_enrichment_mode IN ('enabled', 'disabled'));
