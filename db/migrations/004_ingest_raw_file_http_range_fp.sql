ALTER TABLE ingest.raw_file
  ADD COLUMN IF NOT EXISTS http_range_fp TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_file_http_range_fp
  ON ingest.raw_file (account, http_range_fp)
  WHERE http_range_fp IS NOT NULL;
