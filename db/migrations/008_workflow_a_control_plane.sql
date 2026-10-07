-- 008_workflow_a_control_plane.sql
-- Workflow A v1 control-plane tables (platform DB).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

-- ---- client_account ----
CREATE TABLE IF NOT EXISTS workflow_a_control.client_account (
  client_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_name TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,

  -- Provider API config (v1: single integration type)
  provider_type TEXT NOT NULL,
  provider_base_url TEXT NOT NULL,
  provider_basic_auth_username TEXT NOT NULL,
  provider_basic_auth_password_secret_ref TEXT NOT NULL,

  -- Target client business DB connection (v1 deployment model: separate DB per client)
  client_db_host TEXT NOT NULL,
  client_db_port INT NOT NULL,
  client_db_name TEXT NOT NULL,
  client_db_user TEXT NOT NULL,
  client_db_password_secret_ref TEXT NOT NULL,
  client_db_schema TEXT NOT NULL DEFAULT 'public',

  -- TEXT-based operator-managed token filter for selecting “speeding incidents”
  speed_trigger_filter_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_client_account_enabled
  ON workflow_a_control.client_account (enabled);

-- ---- client_schedule ----
CREATE TABLE IF NOT EXISTS workflow_a_control.client_schedule (
  schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES workflow_a_control.client_account (client_id) ON DELETE CASCADE,

  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  timezone TEXT NOT NULL DEFAULT 'UTC',

  -- Strict classic 5-field cron expression interpreted in `client_schedule.timezone`.
  cron_expression TEXT NOT NULL,
  window_preset TEXT NOT NULL CHECK (window_preset IN ('last_week', 'last_month')),

  schedule_kind TEXT NOT NULL DEFAULT 'DEFAULT_OR_EXTRA'
);

-- Basic cron expression shape check (v1; parser semantics are not enforced in SQL).
ALTER TABLE workflow_a_control.client_schedule
  DROP CONSTRAINT IF EXISTS ck_client_schedule_cron_expression;
ALTER TABLE workflow_a_control.client_schedule
  ADD CONSTRAINT ck_client_schedule_cron_expression
  CHECK (cron_expression ~ '^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]*$');

CREATE INDEX IF NOT EXISTS idx_client_schedule_client_enabled
  ON workflow_a_control.client_schedule (client_id, enabled);
CREATE INDEX IF NOT EXISTS idx_client_schedule_enabled_cron
  ON workflow_a_control.client_schedule (enabled, cron_expression);

-- ---- client_sync_state ----
-- Optional/lightweight dataset checkpointing (not required as the primary v1 execution scope).
CREATE TABLE IF NOT EXISTS workflow_a_control.client_sync_state (
  client_id UUID NOT NULL REFERENCES workflow_a_control.client_account (client_id) ON DELETE CASCADE,
  dataset TEXT NOT NULL CHECK (dataset IN ('trips', 'speeding_notifications')),
  last_success_window_end_ts TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, dataset)
);

-- ---- client_schedule_run_history ----
CREATE TABLE IF NOT EXISTS workflow_a_control.client_schedule_run_history (
  run_history_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id UUID NOT NULL REFERENCES workflow_a_control.client_schedule (schedule_id) ON DELETE CASCADE,
  client_id UUID NOT NULL REFERENCES workflow_a_control.client_account (client_id) ON DELETE CASCADE,

  -- Execution data scope (what the pipeline syncs/recomputes)
  window_start_ts TIMESTAMPTZ NOT NULL,
  window_end_ts TIMESTAMPTZ NOT NULL,

  -- Dispatcher-determined logical due time for audit/lag analysis
  scheduled_fire_ts TIMESTAMPTZ NOT NULL,

  status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
  platform_run_id UUID NULL,
  error_summary TEXT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_client_schedule_run_history_window
    UNIQUE (schedule_id, window_start_ts, window_end_ts)
);

CREATE INDEX IF NOT EXISTS idx_client_schedule_run_history_client_created
  ON workflow_a_control.client_schedule_run_history (client_id, created_at DESC);

