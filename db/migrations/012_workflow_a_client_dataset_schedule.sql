-- 012_workflow_a_client_dataset_schedule.sql
-- Workflow A — per-client dataset schedule and sync settings.
--
-- v1 scope:
--   - This table holds the per-client schedule + sync behavior for each
--     dataset listed in dataset_registry (e.g. trips_sync, fuel_daily_aggregation).
--   - The dispatcher that consumes this table lives at
--     `jobs.api.telematics.dispatcher`; migration 014 reclaims
--     `workflow_a_control.client_schedule_run_history` for dispatcher claims.
--
-- The previous experimental table `workflow_a_control.client_schedule` had a
-- cron_expression model that we are abandoning. To keep its data and any FK
-- targets intact (notably `client_schedule_run_history.schedule_id`), we
-- rename it to `client_schedule_legacy` rather than dropping it. Postgres
-- preserves all FK links across a rename.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

-- ---- Rename legacy table (idempotent) -------------------------------------
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'workflow_a_control'
      AND c.relname = 'client_schedule'
      AND c.relkind = 'r'
  ) AND NOT EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'workflow_a_control'
      AND c.relname = 'client_schedule_legacy'
      AND c.relkind = 'r'
  ) THEN
    EXECUTE 'ALTER TABLE workflow_a_control.client_schedule '
            'RENAME TO client_schedule_legacy';
  END IF;
END$$;


-- ---- New table: client_dataset_schedule -----------------------------------

CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_schedule (
  schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES workflow_a_control.client_account (client_id)
                            ON DELETE CASCADE,
  dataset_name TEXT NOT NULL REFERENCES workflow_a_control.dataset_registry (dataset_name)
                              ON UPDATE CASCADE ON DELETE RESTRICT,

  -- Switch the dispatcher on/off without deleting the row.
  enabled BOOLEAN NOT NULL DEFAULT FALSE,

  -- Cadence. day_of_week / day_of_month are only meaningful for weekly/monthly.
  frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
  day_of_week  SMALLINT NULL CHECK (day_of_week  BETWEEN 0 AND 6),    -- 0=Mon..6=Sun
  day_of_month SMALLINT NULL CHECK (day_of_month BETWEEN 1 AND 31),

  -- Local wall-clock fire time interpreted in `timezone`.
  run_time TIME NOT NULL DEFAULT '02:00:00',
  timezone TEXT NOT NULL DEFAULT 'UTC',

  -- How many days back the dispatcher should request as the data window.
  lookback_days INTEGER NOT NULL DEFAULT 7
    CHECK (lookback_days >= 0),

  -- v1 contract: TRUE  -> ON CONFLICT (record_id) DO UPDATE SET ...
  --              FALSE -> ON CONFLICT (record_id) DO NOTHING
  overwrite_existing BOOLEAN NOT NULL DEFAULT TRUE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_client_dataset_schedule UNIQUE (client_id, dataset_name)
);

-- Cadence consistency: weekly needs day_of_week, monthly needs day_of_month,
-- daily uses neither. Enforce in SQL so misconfigured rows are rejected.
ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS ck_client_dataset_schedule_cadence;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT ck_client_dataset_schedule_cadence
  CHECK (
    (frequency = 'daily'   AND day_of_week IS NULL AND day_of_month IS NULL)
    OR (frequency = 'weekly'  AND day_of_week IS NOT NULL AND day_of_month IS NULL)
    OR (frequency = 'monthly' AND day_of_month IS NOT NULL AND day_of_week IS NULL)
  );

CREATE INDEX IF NOT EXISTS idx_client_dataset_schedule_client_enabled
  ON workflow_a_control.client_dataset_schedule (client_id, enabled);

CREATE INDEX IF NOT EXISTS idx_client_dataset_schedule_dataset
  ON workflow_a_control.client_dataset_schedule (dataset_name);
