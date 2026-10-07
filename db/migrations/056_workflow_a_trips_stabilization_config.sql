-- Inert per-client stabilization configuration for future Telematics /trips
-- compatibility windows. No runtime path applies these values in C2.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS trips_stabilization_delay_seconds INTEGER;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS trips_overlap_seconds INTEGER;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS trips_max_recovery_span_seconds INTEGER;

DO $$
DECLARE
  field_name TEXT;
  actual_type TEXT;
BEGIN
  FOREACH field_name IN ARRAY ARRAY[
    'trips_stabilization_delay_seconds',
    'trips_overlap_seconds',
    'trips_max_recovery_span_seconds'
  ]
  LOOP
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO actual_type
      FROM pg_attribute a
     WHERE a.attrelid = 'workflow_a_control.client_account'::regclass
       AND a.attname = field_name
       AND a.attnum > 0
       AND NOT a.attisdropped;
    IF actual_type IS DISTINCT FROM 'integer' THEN
      RAISE EXCEPTION 'workflow_a_control.client_account.% has incompatible type %, expected integer',
        field_name, coalesce(actual_type, '<missing>');
    END IF;
  END LOOP;
END;
$$;

UPDATE workflow_a_control.client_account
   SET trips_stabilization_delay_seconds = 10800
 WHERE trips_stabilization_delay_seconds IS NULL;

UPDATE workflow_a_control.client_account
   SET trips_overlap_seconds = 3600
 WHERE trips_overlap_seconds IS NULL;

UPDATE workflow_a_control.client_account
   SET trips_max_recovery_span_seconds = 2678400
 WHERE trips_max_recovery_span_seconds IS NULL;

ALTER TABLE workflow_a_control.client_account
  ALTER COLUMN trips_stabilization_delay_seconds SET DEFAULT 10800,
  ALTER COLUMN trips_stabilization_delay_seconds SET NOT NULL,
  ALTER COLUMN trips_overlap_seconds SET DEFAULT 3600,
  ALTER COLUMN trips_overlap_seconds SET NOT NULL,
  ALTER COLUMN trips_max_recovery_span_seconds SET DEFAULT 2678400,
  ALTER COLUMN trips_max_recovery_span_seconds SET NOT NULL;

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_stabilization_delay_seconds;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_stabilization_delay_seconds
  CHECK (trips_stabilization_delay_seconds >= 0);

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_overlap_seconds;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_overlap_seconds
  CHECK (trips_overlap_seconds >= 0);

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_max_recovery_span_seconds;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_max_recovery_span_seconds
  CHECK (
    trips_max_recovery_span_seconds > 0
    AND trips_max_recovery_span_seconds <= 2678400
  );

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_overlap_within_recovery_span;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_overlap_within_recovery_span
  CHECK (trips_overlap_seconds <= trips_max_recovery_span_seconds);
