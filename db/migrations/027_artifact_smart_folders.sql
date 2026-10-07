-- Smart / search folders: saved filter criteria (JSON), dynamic membership.
-- Manual folders continue to use artifact_virtual_folder_items.

ALTER TABLE artifact_virtual_folders
  ADD COLUMN IF NOT EXISTS folder_type TEXT NOT NULL DEFAULT 'manual',
  ADD COLUMN IF NOT EXISTS search_query_json JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE artifact_virtual_folders
SET folder_type = 'manual'
WHERE folder_type IS NULL;

ALTER TABLE artifact_virtual_folders DROP CONSTRAINT IF EXISTS artifact_virtual_folders_folder_type_check;
ALTER TABLE artifact_virtual_folders
  ADD CONSTRAINT artifact_virtual_folders_folder_type_check
  CHECK (folder_type IN ('manual', 'smart'));

ALTER TABLE artifact_virtual_folders DROP CONSTRAINT IF EXISTS artifact_virtual_folders_search_query_json_object_check;
ALTER TABLE artifact_virtual_folders
  ADD CONSTRAINT artifact_virtual_folders_search_query_json_object_check
  CHECK (jsonb_typeof(search_query_json) = 'object');
