-- Phase 4B: portal groups and additive permission inheritance.

CREATE TABLE IF NOT EXISTS portal_groups (
  group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_name TEXT NOT NULL UNIQUE,
  description TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  updated_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (btrim(group_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_portal_groups_active
  ON portal_groups (is_active);

CREATE TABLE IF NOT EXISTS portal_group_users (
  group_id UUID NOT NULL REFERENCES portal_groups(group_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_group_users_user
  ON portal_group_users (user_id);

CREATE INDEX IF NOT EXISTS idx_portal_group_users_group
  ON portal_group_users (group_id);

CREATE TABLE IF NOT EXISTS portal_group_clients (
  group_id UUID NOT NULL REFERENCES portal_groups(group_id) ON DELETE CASCADE,
  client_code TEXT NOT NULL REFERENCES portal_clients(client_code) ON DELETE CASCADE,
  can_view_database BOOLEAN NOT NULL DEFAULT true,
  can_view_reports BOOLEAN NOT NULL DEFAULT true,
  can_export_database BOOLEAN NOT NULL DEFAULT false,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, client_code)
);

CREATE INDEX IF NOT EXISTS idx_portal_group_clients_group
  ON portal_group_clients (group_id);

CREATE INDEX IF NOT EXISTS idx_portal_group_clients_client
  ON portal_group_clients (client_code);

CREATE TABLE IF NOT EXISTS portal_report_folder_groups (
  folder_id UUID NOT NULL REFERENCES portal_report_folders(folder_id) ON DELETE CASCADE,
  group_id UUID NOT NULL REFERENCES portal_groups(group_id) ON DELETE CASCADE,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (folder_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_report_folder_groups_group
  ON portal_report_folder_groups (group_id);

CREATE INDEX IF NOT EXISTS idx_portal_report_folder_groups_folder
  ON portal_report_folder_groups (folder_id);

CREATE TABLE IF NOT EXISTS portal_database_dataset_groups (
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
  group_id UUID NOT NULL REFERENCES portal_groups(group_id) ON DELETE CASCADE,
  can_view_rows BOOLEAN NOT NULL DEFAULT true,
  can_filter_rows BOOLEAN NOT NULL DEFAULT true,
  can_export_rows BOOLEAN NOT NULL DEFAULT false,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_database_dataset_groups_group
  ON portal_database_dataset_groups (group_id);

CREATE INDEX IF NOT EXISTS idx_portal_database_dataset_groups_dataset
  ON portal_database_dataset_groups (dataset_id);
