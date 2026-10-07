-- 024_artifact_annotations.sql
-- Add editable, human-facing artifact annotations without changing stored objects.

CREATE TABLE IF NOT EXISTS artifact_metadata_overrides (
  artifact_id UUID PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  description TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_metadata_overrides_updated_at
  ON artifact_metadata_overrides (updated_at DESC);

CREATE TABLE IF NOT EXISTS artifact_tags (
  artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (artifact_id, tag),
  CHECK (tag = lower(tag)),
  CHECK (char_length(tag) BETWEEN 1 AND 64),
  CHECK (tag ~ '^[a-z0-9][a-z0-9_.:-]{0,63}$')
);

CREATE INDEX IF NOT EXISTS idx_artifact_tags_tag
  ON artifact_tags (tag);
