-- Phase 2B portal client access foundation.
-- Portal clients are separate from technical Artifact Explorer RBAC roles.

CREATE TABLE IF NOT EXISTS portal_clients (
  client_code TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (client_code ~ '^[A-Z0-9_:-]{2,64}$'),
  CHECK (btrim(display_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_portal_clients_active
  ON portal_clients (is_active);

CREATE TABLE IF NOT EXISTS portal_user_clients (
  user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  client_code TEXT NOT NULL REFERENCES portal_clients(client_code) ON DELETE CASCADE,
  can_view_database BOOLEAN NOT NULL DEFAULT true,
  can_view_reports BOOLEAN NOT NULL DEFAULT true,
  can_export_database BOOLEAN NOT NULL DEFAULT false,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, client_code)
);

CREATE INDEX IF NOT EXISTS idx_portal_user_clients_user
  ON portal_user_clients (user_id);

CREATE INDEX IF NOT EXISTS idx_portal_user_clients_client
  ON portal_user_clients (client_code);
