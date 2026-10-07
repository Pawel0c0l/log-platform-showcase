-- User-visible system folders for async Database Explorer exports.
-- One stable, system-managed "Database Exports" folder is provisioned per owner.

CREATE TABLE IF NOT EXISTS database_export_system_folders (
  folder_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  system_key TEXT NOT NULL,
  folder_name TEXT NOT NULL,
  slug TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (system_key = 'database_exports'),
  CHECK (btrim(folder_name) <> ''),
  CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
  UNIQUE (owner_user_id, system_key),
  UNIQUE (folder_id, owner_user_id)
);

CREATE INDEX IF NOT EXISTS idx_database_export_system_folders_owner
  ON database_export_system_folders (owner_user_id);

ALTER TABLE database_export_jobs
  ADD COLUMN IF NOT EXISTS system_folder_id UUID;

DO $$
BEGIN
  ALTER TABLE database_export_jobs
    ADD CONSTRAINT database_export_jobs_system_folder_id_fkey
    FOREIGN KEY (system_folder_id, requested_by_user_id)
    REFERENCES database_export_system_folders(folder_id, owner_user_id)
    ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_system_folder
  ON database_export_jobs (system_folder_id, queued_at DESC);
