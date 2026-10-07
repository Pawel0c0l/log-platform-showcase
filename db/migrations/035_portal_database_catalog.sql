-- Phase 3A portal database catalog foundation.
-- Catalog entries are allowlisted references only; this migration does not enable row browsing or SQL execution.

CREATE TABLE IF NOT EXISTS portal_database_datasets (
  dataset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_code TEXT NOT NULL REFERENCES portal_clients(client_code) ON DELETE CASCADE,
  dataset_name TEXT NOT NULL,
  slug TEXT NOT NULL,
  description TEXT,
  schema_name TEXT NOT NULL,
  table_name TEXT NOT NULL,
  default_date_column TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  updated_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (client_code, slug),
  UNIQUE (client_code, schema_name, table_name),
  CHECK (btrim(dataset_name) <> ''),
  CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
  CHECK (schema_name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'),
  CHECK (table_name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'),
  CHECK (default_date_column IS NULL OR default_date_column ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$')
);

CREATE INDEX IF NOT EXISTS idx_portal_database_datasets_client
  ON portal_database_datasets (client_code);

CREATE INDEX IF NOT EXISTS idx_portal_database_datasets_active
  ON portal_database_datasets (is_active);

CREATE TABLE IF NOT EXISTS portal_database_dataset_columns (
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
  column_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  data_type TEXT,
  is_visible BOOLEAN NOT NULL DEFAULT true,
  is_filterable BOOLEAN NOT NULL DEFAULT true,
  is_sortable BOOLEAN NOT NULL DEFAULT true,
  is_default_date_column BOOLEAN NOT NULL DEFAULT false,
  display_order INTEGER NOT NULL DEFAULT 100,
  PRIMARY KEY (dataset_id, column_name),
  CHECK (column_name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'),
  CHECK (btrim(display_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_portal_database_dataset_columns_dataset
  ON portal_database_dataset_columns (dataset_id);

CREATE TABLE IF NOT EXISTS portal_database_dataset_users (
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  can_view_rows BOOLEAN NOT NULL DEFAULT true,
  can_filter_rows BOOLEAN NOT NULL DEFAULT true,
  can_export_rows BOOLEAN NOT NULL DEFAULT false,
  granted_by UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_portal_database_dataset_users_user
  ON portal_database_dataset_users (user_id);

CREATE INDEX IF NOT EXISTS idx_portal_database_dataset_users_dataset
  ON portal_database_dataset_users (dataset_id);
