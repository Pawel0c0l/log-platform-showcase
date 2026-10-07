ALTER TABLE ingest.raw_file
  DROP CONSTRAINT IF EXISTS ux_raw_file_account_sha256;

DROP INDEX IF EXISTS ingest.ux_raw_file_account_sha256;

CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_file_account_sha256_not_duplicate
  ON ingest.raw_file (account, sha256)
  WHERE sha256 IS NOT NULL
    AND status <> 'DUPLICATE_CONTENT';
