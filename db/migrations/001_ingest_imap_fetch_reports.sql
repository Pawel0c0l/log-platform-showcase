CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS ingest;

CREATE TABLE IF NOT EXISTS ingest.imap_message (
  id BIGSERIAL PRIMARY KEY,
  account TEXT NOT NULL,
  mailbox TEXT NOT NULL,
  uidvalidity BIGINT NOT NULL,
  uid BIGINT NOT NULL,
  message_id TEXT,
  from_addr TEXT,
  subject TEXT,
  internal_date TIMESTAMPTZ,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  run_id UUID
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_imap_message_account_mailbox_uidvalidity_uid
  ON ingest.imap_message (account, mailbox, uidvalidity, uid);

CREATE TABLE IF NOT EXISTS ingest.raw_file (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  imap_message_id BIGINT NOT NULL REFERENCES ingest.imap_message(id) ON DELETE CASCADE,
  account TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  content_type TEXT,
  size_bytes BIGINT NOT NULL,
  raw_path TEXT NOT NULL,
  normalized_csv_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  CONSTRAINT ck_raw_file_status CHECK (status IN ('NEW', 'NORMALIZED', 'FAILED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_file_account_sha256
  ON ingest.raw_file (account, sha256);

CREATE INDEX IF NOT EXISTS idx_raw_file_status
  ON ingest.raw_file (status);
