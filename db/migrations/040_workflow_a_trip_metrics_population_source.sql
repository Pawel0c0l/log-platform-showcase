-- 040_workflow_a_trip_metrics_population_source.sql
-- Workflow A/B control-plane selector for mutually-exclusive client_trips
-- trip metrics population.
--
-- The selector is per client account / client business DB and controls the
-- combined trip metrics group: speeding buckets plus HIGH_RPM/OVERREV counts.
-- Existing clients default to api_migration to preserve current Workflow A
-- sync behavior unless an operator explicitly selects a report-backed source.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS trip_metrics_population_source TEXT;

UPDATE workflow_a_control.client_account
   SET trip_metrics_population_source = 'api_migration'
 WHERE trip_metrics_population_source IS NULL;

ALTER TABLE workflow_a_control.client_account
  ALTER COLUMN trip_metrics_population_source SET DEFAULT 'api_migration';

ALTER TABLE workflow_a_control.client_account
  ALTER COLUMN trip_metrics_population_source SET NOT NULL;

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trip_metrics_population_source;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trip_metrics_population_source
  CHECK (
    trip_metrics_population_source IN (
      'api_migration',
      'report_207_migration',
      'd105_2_ecodriving_migration',
      'disabled'
    )
  );

COMMENT ON COLUMN workflow_a_control.client_account.trip_metrics_population_source
  IS 'Source of truth for event-derived client_trips trip metrics: speeding buckets and HIGH_RPM/OVERREV counters.';
