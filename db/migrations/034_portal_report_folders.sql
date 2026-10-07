-- Phase 2C portal report folders.
-- Customer-facing report folders are separate from technical Artifact Explorer virtual folders.

CREATE TABLE IF NOT EXISTS portal_report_folders (
  folder_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_code TEXT NOT NULL REFERENCES portal_clients(client_code) ON DELETE CASCADE,
  folder_name TEXT NOT NULL,
  slug TEXT NOT NULL,
  description TEXT,
  search_query_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT true,
  can_preview BOOLEAN NOT NULL DEFAULT true,
  can_download BOOLEAN NOT NULL DEFAULT true,
  created_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  updated_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (client_code, slug),
  CHECK (btrim(folder_name) <> ''),
  CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
  CHECK (jsonb_typeof(search_query_json) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_portal_report_folders_client
  ON portal_report_folders (client_code);

CREATE INDEX IF NOT EXISTS idx_portal_report_folders_active
  ON portal_report_folders (is_active);

CREATE TABLE IF NOT EXISTS portal_report_folder_users (
  folder_id UUID NOT NULL REFERENCES portal_report_folders(folder_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (folder_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_report_folder_users_user
  ON portal_report_folder_users (user_id);

CREATE INDEX IF NOT EXISTS idx_portal_report_folder_users_folder
  ON portal_report_folder_users (folder_id);
