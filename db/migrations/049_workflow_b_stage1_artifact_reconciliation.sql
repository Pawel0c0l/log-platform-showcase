-- Persist Stage 1 normalized-artifact metadata needed for retry reconciliation.
-- Historical rows remain unchanged; no artifact or ingest backfill is performed.
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
  ADD COLUMN IF NOT EXISTS stage1_normalized_artifact_metadata JSONB;

COMMENT ON COLUMN ingest.raw_file.stage1_normalized_artifact_metadata IS
  'Safe Stage 1 normalization diagnostics required to reproduce normalized artifact metadata during reconciliation.';
