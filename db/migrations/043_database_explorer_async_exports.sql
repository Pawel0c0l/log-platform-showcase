-- Database Explorer asynchronous exports.
-- Adds a durable queue plus requester-owned, expiring artifact metadata.

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES artifact_users(user_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_created
  ON artifacts (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_expires_at
  ON artifacts (expires_at);

DO $$
BEGIN
  ALTER TABLE artifacts
    ADD CONSTRAINT artifacts_owner_user_id_fkey
    FOREIGN KEY (owner_user_id) REFERENCES artifact_users(user_id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS database_export_jobs (
  job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  requested_by_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE RESTRICT,
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE RESTRICT,
  requested_format TEXT NOT NULL,
  request_snapshot_json JSONB NOT NULL,
  status TEXT NOT NULL,
  queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,
  claim_token UUID,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  row_count BIGINT,
  artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  object_key TEXT,
  attempt_object_key TEXT,
  safe_error_code TEXT,
  safe_error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (requested_format IN ('csv', 'xlsx')),
  CHECK (status IN ('queued', 'running', 'completed', 'failed', 'expired')),
  CHECK (jsonb_typeof(request_snapshot_json) = 'object'),
  CHECK (attempt_count >= 0),
  CHECK (row_count IS NULL OR row_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_queue
  ON database_export_jobs (queued_at ASC, job_id ASC)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_running_lease
  ON database_export_jobs (lease_expires_at)
  WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_running_claim
  ON database_export_jobs (job_id, claim_token)
  WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_requester
  ON database_export_jobs (requested_by_user_id, queued_at DESC);

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_expiry
  ON database_export_jobs (expires_at)
  WHERE status = 'completed';

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_artifact
  ON database_export_jobs (artifact_id)
  WHERE artifact_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_database_export_jobs_attempt_object
  ON database_export_jobs (attempt_object_key)
  WHERE attempt_object_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS database_export_attempt_objects (
  attempt_object_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES database_export_jobs(job_id) ON DELETE CASCADE,
  claim_token UUID NOT NULL,
  object_key TEXT NOT NULL,
  state TEXT NOT NULL,
  cleanup_requested_at TIMESTAMPTZ,
  last_cleanup_attempt_at TIMESTAMPTZ,
  last_cleanup_success_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (object_key <> ''),
  CHECK (state IN ('active', 'published', 'cleanup_pending')),
  UNIQUE (job_id, claim_token),
  UNIQUE (object_key)
);

CREATE INDEX IF NOT EXISTS idx_database_export_attempt_objects_job
  ON database_export_attempt_objects (job_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_database_export_attempt_objects_cleanup
  ON database_export_attempt_objects (COALESCE(last_cleanup_attempt_at, cleanup_requested_at, created_at), attempt_object_id)
  WHERE state = 'cleanup_pending';

CREATE INDEX IF NOT EXISTS idx_database_export_attempt_objects_active
  ON database_export_attempt_objects (job_id, claim_token, object_key)
  WHERE state = 'active';
