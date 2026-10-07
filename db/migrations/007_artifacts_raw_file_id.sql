-- 007_artifacts_raw_file_id.sql
-- Optional link from artifact to ingest raw_file (e.g. Stage 2 cleaned CSV from a specific raw file).
-- Table artifacts is created by the API at startup; this migration assumes it exists.

ALTER TABLE artifacts
  ADD COLUMN raw_file_id UUID REFERENCES ingest.raw_file(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_raw_file_id
  ON artifacts (raw_file_id);
