-- Phase 3C portal audit foundation for database exports and future portal events.

CREATE TABLE IF NOT EXISTS portal_audit_events (
  audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,
  actor_user_id UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  client_code TEXT REFERENCES portal_clients(client_code) ON DELETE SET NULL,
  dataset_id UUID REFERENCES portal_database_datasets(dataset_id) ON DELETE SET NULL,
  report_folder_id UUID REFERENCES portal_report_folders(folder_id) ON DELETE SET NULL,
  artifact_id UUID,
  ip_address TEXT,
  user_agent TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (btrim(event_type) <> ''),
  CHECK (jsonb_typeof(metadata_json) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_portal_audit_events_created_at
  ON portal_audit_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_portal_audit_events_actor
  ON portal_audit_events (actor_user_id);

CREATE INDEX IF NOT EXISTS idx_portal_audit_events_type
  ON portal_audit_events (event_type);

CREATE INDEX IF NOT EXISTS idx_portal_audit_events_client
  ON portal_audit_events (client_code);

CREATE INDEX IF NOT EXISTS idx_portal_audit_events_dataset
  ON portal_audit_events (dataset_id);
