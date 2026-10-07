-- 025_artifact_explorer_rbac.sql
-- Local Artifact Explorer users, roles, and allow-only artifact permissions.

CREATE TABLE IF NOT EXISTS artifact_users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  is_admin BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_users_active
  ON artifact_users (is_active);

CREATE TABLE IF NOT EXISTS artifact_roles (
  role_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  role_name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifact_user_roles (
  user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  role_id UUID NOT NULL REFERENCES artifact_roles(role_id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_user_roles_role
  ON artifact_user_roles (role_id);

CREATE TABLE IF NOT EXISTS artifact_role_permissions (
  permission_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  role_id UUID NOT NULL REFERENCES artifact_roles(role_id) ON DELETE CASCADE,
  can_view BOOLEAN NOT NULL DEFAULT true,
  can_preview BOOLEAN NOT NULL DEFAULT true,
  can_download BOOLEAN NOT NULL DEFAULT false,
  can_edit_annotations BOOLEAN NOT NULL DEFAULT false,
  workflow_name TEXT,
  stage_name TEXT,
  artifact_role TEXT,
  report_type TEXT,
  client_code TEXT,
  file_ext TEXT,
  layout_version INTEGER,
  tag TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_role_permissions_role
  ON artifact_role_permissions (role_id);
