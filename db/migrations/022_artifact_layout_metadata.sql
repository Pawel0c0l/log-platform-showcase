-- 022_artifact_layout_metadata.sql
-- Add semantic metadata for transparent artifact organization.
-- Existing artifact rows remain valid; new layout-aware uploads use layout_version=2.

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS workflow_name TEXT,
  ADD COLUMN IF NOT EXISTS stage_name TEXT,
  ADD COLUMN IF NOT EXISTS artifact_role TEXT,
  ADD COLUMN IF NOT EXISTS report_type TEXT,
  ADD COLUMN IF NOT EXISTS display_filename TEXT,
  ADD COLUMN IF NOT EXISTS original_filename TEXT,
  ADD COLUMN IF NOT EXISTS file_ext TEXT,
  ADD COLUMN IF NOT EXISTS layout_version INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_artifacts_workflow_stage
  ON artifacts (workflow_name, stage_name);

CREATE INDEX IF NOT EXISTS idx_artifacts_report_type
  ON artifacts (report_type);

CREATE INDEX IF NOT EXISTS idx_artifacts_artifact_role
  ON artifacts (artifact_role);

CREATE INDEX IF NOT EXISTS idx_artifacts_layout_version
  ON artifacts (layout_version);

