-- 017_workflow_a_add_client_code_to_control_tables.sql
-- Workflow A — denormalize client_code into schedule and retention control rows.
--
-- client_account.client_code remains nullable in v1, so the new columns are
-- nullable too. When a client has a client_code, this migration backfills it
-- into schedules, schedule run history, and retention policies. A trigger
-- keeps future rows consistent with client_account when a code exists.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD COLUMN IF NOT EXISTS client_code TEXT;

ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD COLUMN IF NOT EXISTS client_code TEXT;

ALTER TABLE workflow_a_control.client_table_retention
  ADD COLUMN IF NOT EXISTS client_code TEXT;

-- Backfill schedules and retention policies directly from the authoritative
-- client account row.
UPDATE workflow_a_control.client_dataset_schedule cds
   SET client_code = ca.client_code
  FROM workflow_a_control.client_account ca
 WHERE cds.client_id = ca.client_id
   AND ca.client_code IS NOT NULL
   AND cds.client_code IS DISTINCT FROM ca.client_code;

UPDATE workflow_a_control.client_table_retention ctr
   SET client_code = ca.client_code
  FROM workflow_a_control.client_account ca
 WHERE ctr.client_id = ca.client_id
   AND ca.client_code IS NOT NULL
   AND ctr.client_code IS DISTINCT FROM ca.client_code;

-- History inherits the schedule's denormalized code first, then falls back to
-- client_account for rows that pre-date the schedule backfill.
UPDATE workflow_a_control.client_schedule_run_history h
   SET client_code = COALESCE(cds.client_code, ca.client_code)
  FROM workflow_a_control.client_dataset_schedule cds
  JOIN workflow_a_control.client_account ca
    ON ca.client_id = cds.client_id
 WHERE h.schedule_id = cds.schedule_id
   AND h.client_id = ca.client_id
   AND COALESCE(cds.client_code, ca.client_code) IS NOT NULL
   AND h.client_code IS DISTINCT FROM COALESCE(cds.client_code, ca.client_code);

-- Composite FK support: client_id remains the primary machine identifier, but
-- when client_code is present the pair must identify the same client_account.
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_account_client_id_code
  ON workflow_a_control.client_account (client_id, client_code);

ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS fk_client_dataset_schedule_client_id_code;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT fk_client_dataset_schedule_client_id_code
  FOREIGN KEY (client_id, client_code)
  REFERENCES workflow_a_control.client_account (client_id, client_code)
  ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS fk_client_schedule_run_history_client_id_code;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT fk_client_schedule_run_history_client_id_code
  FOREIGN KEY (client_id, client_code)
  REFERENCES workflow_a_control.client_account (client_id, client_code)
  ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE workflow_a_control.client_table_retention
  DROP CONSTRAINT IF EXISTS fk_client_table_retention_client_id_code;
ALTER TABLE workflow_a_control.client_table_retention
  ADD CONSTRAINT fk_client_table_retention_client_id_code
  FOREIGN KEY (client_id, client_code)
  REFERENCES workflow_a_control.client_account (client_id, client_code)
  ON UPDATE CASCADE ON DELETE CASCADE;

-- Fill or validate client_code for future inserts/updates. For clients that
-- intentionally do not have client_code, NULL remains allowed.
CREATE OR REPLACE FUNCTION workflow_a_control.set_control_client_code()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_client_code TEXT;
BEGIN
  SELECT client_code
    INTO expected_client_code
    FROM workflow_a_control.client_account
   WHERE client_id = NEW.client_id;

  IF expected_client_code IS NULL THEN
    IF NEW.client_code IS NOT NULL THEN
      RAISE EXCEPTION 'client_code % does not match client_id % with NULL client_account.client_code',
        NEW.client_code, NEW.client_id;
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.client_code IS NULL THEN
    NEW.client_code := expected_client_code;
  ELSIF NEW.client_code <> expected_client_code THEN
    RAISE EXCEPTION 'client_code % does not match client_id % expected client_code %',
      NEW.client_code, NEW.client_id, expected_client_code;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_client_dataset_schedule_client_code
  ON workflow_a_control.client_dataset_schedule;
CREATE TRIGGER trg_client_dataset_schedule_client_code
  BEFORE INSERT OR UPDATE OF client_id, client_code
  ON workflow_a_control.client_dataset_schedule
  FOR EACH ROW
  EXECUTE FUNCTION workflow_a_control.set_control_client_code();

DROP TRIGGER IF EXISTS trg_client_schedule_run_history_client_code
  ON workflow_a_control.client_schedule_run_history;
CREATE TRIGGER trg_client_schedule_run_history_client_code
  BEFORE INSERT OR UPDATE OF client_id, client_code
  ON workflow_a_control.client_schedule_run_history
  FOR EACH ROW
  EXECUTE FUNCTION workflow_a_control.set_control_client_code();

DROP TRIGGER IF EXISTS trg_client_table_retention_client_code
  ON workflow_a_control.client_table_retention;
CREATE TRIGGER trg_client_table_retention_client_code
  BEFORE INSERT OR UPDATE OF client_id, client_code
  ON workflow_a_control.client_table_retention
  FOR EACH ROW
  EXECUTE FUNCTION workflow_a_control.set_control_client_code();

CREATE INDEX IF NOT EXISTS idx_client_dataset_schedule_client_code_enabled
  ON workflow_a_control.client_dataset_schedule (client_code, enabled);

CREATE INDEX IF NOT EXISTS idx_client_schedule_run_history_client_code_fire
  ON workflow_a_control.client_schedule_run_history (client_code, scheduled_fire_ts DESC);

CREATE INDEX IF NOT EXISTS idx_client_table_retention_client_code_enabled
  ON workflow_a_control.client_table_retention (client_code, enabled);
