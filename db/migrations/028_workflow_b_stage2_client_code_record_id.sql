-- 028_workflow_b_stage2_client_code_record_id.sql
-- Workflow B Stage 2: operator-configured client detection and deterministic
-- cleaned-report record IDs.

ALTER TABLE workflow_b_control.report_type_registry
  ADD COLUMN IF NOT EXISTS id_sync_column_name TEXT NULL,
  ADD COLUMN IF NOT EXISTS record_id_ingredients TEXT NULL;

COMMENT ON COLUMN workflow_b_control.report_type_registry.id_sync_column_name IS
  'Exact cleaned Stage 2 output column name used to identify the client from Workflow A client_trips values.';

COMMENT ON COLUMN workflow_b_control.report_type_registry.record_id_ingredients IS
  'Comma-separated cleaned Stage 2 output column names, in order, used to compute deterministic row-level record_id values.';

ALTER TABLE ingest.raw_file
  ADD COLUMN IF NOT EXISTS client_code TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_file_client_code
  ON ingest.raw_file (client_code);

COMMENT ON COLUMN ingest.raw_file.client_code IS
  'Operator-friendly client code resolved by Workflow B Stage 2 from the final cleaned report, when unambiguous.';
