-- Nullable Workflow B report-policy override for trip-metrics postprocessing.
-- Existing policies inherit the client-level selector; no rows are backfilled.

DO $$
DECLARE marker_count INTEGER;
BEGIN
  IF to_regclass('ops_control.environment_identity') IS NULL THEN
    RAISE EXCEPTION 'platform environment identity guard is not installed';
  END IF;
  SELECT count(*) INTO marker_count
    FROM ops_control.environment_identity
   WHERE identity_key = 'primary' AND database_role = 'platform';
  IF marker_count <> 1 THEN
    RAISE EXCEPTION 'platform environment identity guard is not provisioned';
  END IF;
END $$;

ALTER TABLE workflow_b_control.report_type_client_load_policy
  ADD COLUMN IF NOT EXISTS trip_metrics_population_source_override TEXT;

ALTER TABLE workflow_b_control.report_type_client_load_policy
  DROP CONSTRAINT IF EXISTS ck_report_type_client_load_policy_trip_metrics_source_override;

ALTER TABLE workflow_b_control.report_type_client_load_policy
  ADD CONSTRAINT ck_report_type_client_load_policy_trip_metrics_source_override
  CHECK (
    trip_metrics_population_source_override IS NULL
    OR (
      btrim(trip_metrics_population_source_override, E' \t\n\r\f\v') =
        trip_metrics_population_source_override
      AND trip_metrics_population_source_override IN (
        'api_migration',
        'report_207_migration',
        'd105_2_ecodriving_migration',
        'disabled'
      )
    )
  );

COMMENT ON COLUMN workflow_b_control.report_type_client_load_policy.trip_metrics_population_source_override IS
  'Workflow B-only trip-metrics postprocessor selector; NULL inherits the client-level Workflow A selector.';
