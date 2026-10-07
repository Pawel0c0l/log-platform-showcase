ALTER TABLE ingest.raw_file
  ADD COLUMN IF NOT EXISTS report_key TEXT,
  ADD COLUMN IF NOT EXISTS content_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS dedup_basis TEXT;

ALTER TABLE ingest.raw_file
  ALTER COLUMN raw_path DROP NOT NULL;

ALTER TABLE ingest.raw_file
  DROP CONSTRAINT IF EXISTS ck_raw_file_status;

ALTER TABLE ingest.raw_file
  ADD CONSTRAINT ck_raw_file_status
  CHECK (status IN ('NEW', 'NORMALIZED', 'FAILED', 'DUPLICATE_CONTENT'));

DROP INDEX IF EXISTS ingest.ux_raw_file_account_report_key_content_fingerprint;

CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_file_account_report_key_content_fingerprint
  ON ingest.raw_file (account, report_key, content_fingerprint)
  WHERE content_fingerprint IS NOT NULL
    AND report_key IS NOT NULL
    AND status <> 'DUPLICATE_CONTENT';
