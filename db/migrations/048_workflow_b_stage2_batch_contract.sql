-- Structured Workflow B Stage 2 finalization state. No historical backfill.
DO $$
DECLARE marker_count INTEGER;
BEGIN
  IF to_regclass('ops_control.environment_identity') IS NULL THEN
    RAISE EXCEPTION 'platform environment identity guard is not installed';
  END IF;
  SELECT count(*) INTO marker_count FROM ops_control.environment_identity
   WHERE identity_key = 'primary' AND database_role = 'platform';
  IF marker_count <> 1 THEN
    RAISE EXCEPTION 'platform environment identity guard is not provisioned';
  END IF;
END $$;

ALTER TABLE ingest.raw_file
  ADD COLUMN IF NOT EXISTS stage2_outcome_category TEXT,
  ADD COLUMN IF NOT EXISTS stage2_retryable BOOLEAN,
  ADD COLUMN IF NOT EXISTS stage2_cleaned_artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_raw_file_stage2_batch_eligibility
  ON ingest.raw_file (status, stage2_status, stage2_retryable)
  WHERE status = 'NORMALIZED';

COMMENT ON COLUMN ingest.raw_file.stage2_outcome_category IS
  'Stable machine-readable Stage 2 outcome/reason category; historical rows remain NULL.';
COMMENT ON COLUMN ingest.raw_file.stage2_retryable IS
  'Whether automatic Stage 2 retry is safe; NULL means unclassified historical state.';
COMMENT ON COLUMN ingest.raw_file.stage2_cleaned_artifact_id IS
  'Canonical idempotent cleaned artifact used to finalize successful Stage 2 processing.';
