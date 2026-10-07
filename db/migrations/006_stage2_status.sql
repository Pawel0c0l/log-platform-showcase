ALTER TABLE ingest.raw_file
  ADD COLUMN IF NOT EXISTS stage2_status TEXT,
  ADD COLUMN IF NOT EXISTS stage2_report_type TEXT,
  ADD COLUMN IF NOT EXISTS stage2_scores JSONB,
  ADD COLUMN IF NOT EXISTS stage2_schema_diff JSONB,
  ADD COLUMN IF NOT EXISTS stage2_pending_reason TEXT,
  ADD COLUMN IF NOT EXISTS stage2_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_raw_file_stage2_status
  ON ingest.raw_file (stage2_status);
