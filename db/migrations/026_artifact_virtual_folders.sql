CREATE TABLE IF NOT EXISTS artifact_virtual_folders (
  folder_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_folder_id UUID REFERENCES artifact_virtual_folders(folder_id) ON DELETE CASCADE,
  folder_name TEXT NOT NULL,
  slug TEXT NOT NULL,
  description TEXT,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (btrim(folder_name) <> ''),
  CHECK (btrim(slug) <> ''),
  CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,127}$')
);

CREATE INDEX IF NOT EXISTS idx_artifact_virtual_folders_parent
  ON artifact_virtual_folders (parent_folder_id);

CREATE INDEX IF NOT EXISTS idx_artifact_virtual_folders_slug
  ON artifact_virtual_folders (slug);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_virtual_folders_root_slug_unique
  ON artifact_virtual_folders (slug)
  WHERE parent_folder_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_virtual_folders_child_slug_unique
  ON artifact_virtual_folders (parent_folder_id, slug)
  WHERE parent_folder_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_virtual_folders_root_name_unique
  ON artifact_virtual_folders (lower(folder_name))
  WHERE parent_folder_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_virtual_folders_child_name_unique
  ON artifact_virtual_folders (parent_folder_id, lower(folder_name))
  WHERE parent_folder_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_virtual_folder_items (
  folder_id UUID NOT NULL REFERENCES artifact_virtual_folders(folder_id) ON DELETE CASCADE,
  artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  added_by TEXT,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (folder_id, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_virtual_folder_items_artifact
  ON artifact_virtual_folder_items (artifact_id);
