-- 014_workflow_a_dispatcher_v1.sql
-- Workflow A — dispatcher (v1) data-model bring-up.
--
-- Adds the minimum DB shape that `jobs.api.telematics.dispatcher` needs:
--
-- 1) `client_dataset_schedule` learns to encode "monthly on the last day"
--    via a new `day_of_month_last BOOLEAN` companion to `day_of_month`.
--    The cadence CHECK is rewritten so the row is consistent for daily,
--    weekly and monthly. `day_of_month` is also tightened to 1..28 so we
--    never have to coerce February-29-style dates.
--
-- 2) `client_schedule_run_history` is reclaimed for the dispatcher:
--      * its FK was previously to `workflow_a_control.client_schedule`
--        (renamed by 012 to `client_schedule_legacy`); we re-target it
--        to `workflow_a_control.client_dataset_schedule`,
--      * `status` adds the RUNNING state used as the strict-queue marker,
--      * adds `dataset_name`, `started_at`, `finished_at`,
--      * uniqueness shifts from (schedule_id, window_*) to
--        (schedule_id, scheduled_fire_ts) so the dispatcher claim is one
--        row per logical fire,
--      * a partial index supports the SELECT COUNT(*) WHERE status='RUNNING'
--        guard the dispatcher uses on every tick.
--
-- The table is intentionally TRUNCATEd here. `CURRENT_TASK_CONTEXT.md` and
-- `docs/10_scheduler_design.md` document `client_schedule_run_history` as
-- "untouched in v1 / reserved for the dispatcher", so it is empty in every
-- known environment. Re-keying it without truncating would leave orphan
-- rows pointing at the legacy schedule_id namespace.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;


-- ---------------------------------------------------------------------------
-- 1) client_dataset_schedule: support monthly "last day of month"
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD COLUMN IF NOT EXISTS day_of_month_last BOOLEAN NOT NULL DEFAULT FALSE;

-- Tighten day_of_month to 1..28 (every month has these days, no coercion).
ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS client_dataset_schedule_day_of_month_check;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT client_dataset_schedule_day_of_month_check
  CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 28);

-- Replace cadence CHECK so monthly accepts either a numeric day or "last".
ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS ck_client_dataset_schedule_cadence;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT ck_client_dataset_schedule_cadence
  CHECK (
    (frequency = 'daily'
       AND day_of_week IS NULL
       AND day_of_month IS NULL
       AND day_of_month_last = false)
    OR (frequency = 'weekly'
       AND day_of_week IS NOT NULL
       AND day_of_month IS NULL
       AND day_of_month_last = false)
    OR (frequency = 'monthly'
       AND day_of_week IS NULL
       AND (
         (day_of_month IS NOT NULL AND day_of_month_last = false)
         OR (day_of_month IS NULL AND day_of_month_last = true)
       ))
  );


-- ---------------------------------------------------------------------------
-- 2) client_schedule_run_history: reclaim for the dispatcher
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'workflow_a_control'
      AND table_name   = 'client_schedule_run_history'
  ) THEN
    -- Empty in v1; safe to truncate before retargeting the FK namespace.
    TRUNCATE workflow_a_control.client_schedule_run_history;

    -- Drop the legacy FK (was created against client_schedule, now renamed
    -- to client_schedule_legacy by migration 012). The constraint name is
    -- the Postgres auto-generated one; guard with IF EXISTS.
    ALTER TABLE workflow_a_control.client_schedule_run_history
      DROP CONSTRAINT IF EXISTS client_schedule_run_history_schedule_id_fkey;
    ALTER TABLE workflow_a_control.client_schedule_run_history
      DROP CONSTRAINT IF EXISTS fk_run_history_schedule;
    ALTER TABLE workflow_a_control.client_schedule_run_history
      ADD CONSTRAINT fk_run_history_schedule
      FOREIGN KEY (schedule_id)
      REFERENCES workflow_a_control.client_dataset_schedule (schedule_id)
      ON DELETE CASCADE;
  END IF;
END$$;

ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS client_schedule_run_history_status_check;
ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS ck_run_history_status;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT ck_run_history_status
  CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED'));

ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD COLUMN IF NOT EXISTS dataset_name TEXT;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

-- Replace the old window-keyed UNIQUE with a fire-keyed one. The dispatcher
-- claim must be one row per (schedule, scheduled_fire_ts).
ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS uq_client_schedule_run_history_window;
ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS uq_run_history_schedule_fire;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT uq_run_history_schedule_fire
  UNIQUE (schedule_id, scheduled_fire_ts);

-- The dispatcher does `SELECT COUNT(*) ... WHERE status='RUNNING'` on every
-- tick. A partial index keeps that query O(rows_currently_running).
CREATE INDEX IF NOT EXISTS idx_run_history_status_running
  ON workflow_a_control.client_schedule_run_history (status)
  WHERE status = 'RUNNING';

-- Helpful for ad-hoc audit ("what fired in the last day?")
CREATE INDEX IF NOT EXISTS idx_run_history_fire_desc
  ON workflow_a_control.client_schedule_run_history (scheduled_fire_ts DESC);
