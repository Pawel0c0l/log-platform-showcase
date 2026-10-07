-- 013_workflow_a_client_table_retention.sql
-- Workflow A — per-client, per-table retention policy.
--
-- Granularity is intentionally per TABLE (not per dataset) because retention
-- is a property of the data, not of how it gets produced. For example,
-- `client_speeding_notifications` and the per-vehicle daily aggregates may
-- have very different retention windows even though the same job writes them.
--
-- v1 scope:
--   - The retention worker that consumes this table lives at
--     `jobs.api.telematics.retention_purge`.
--   - The cutoff timestamp is computed in Python (UTC) per (client, table)
--     and passed to SQL as a parameter. The SQL itself NEVER computes
--     `NOW() - INTERVAL '… days'` dynamically; the worker resolves identifiers
--     (schema/table/retention_key_column) only via the registry allowlist.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

CREATE TABLE IF NOT EXISTS workflow_a_control.client_table_retention (
  retention_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES workflow_a_control.client_account (client_id)
                            ON DELETE CASCADE,
  table_name TEXT NOT NULL REFERENCES workflow_a_control.table_registry (table_name)
                            ON UPDATE CASCADE ON DELETE RESTRICT,

  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  retention_days INTEGER NOT NULL DEFAULT 365
    CHECK (retention_days > 0),

  -- Audit columns
  last_purge_run_at TIMESTAMPTZ NULL,
  last_purge_cutoff_ts TIMESTAMPTZ NULL,
  last_purge_deleted_count BIGINT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_client_table_retention UNIQUE (client_id, table_name)
);

CREATE INDEX IF NOT EXISTS idx_client_table_retention_client_enabled
  ON workflow_a_control.client_table_retention (client_id, enabled);

CREATE INDEX IF NOT EXISTS idx_client_table_retention_table
  ON workflow_a_control.client_table_retention (table_name);
