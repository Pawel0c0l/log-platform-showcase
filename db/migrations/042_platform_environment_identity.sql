-- 042_platform_environment_identity.sql
-- Fail-closed environment identity structure for guarded operational jobs.
-- Deployment-specific marker rows are provisioned separately; this migration
-- intentionally does not infer or insert local, staging, or production values.

CREATE SCHEMA IF NOT EXISTS ops_control;

CREATE TABLE IF NOT EXISTS ops_control.environment_identity (
  identity_key TEXT PRIMARY KEY,
  environment TEXT NOT NULL,
  database_identity_id UUID NOT NULL UNIQUE,
  database_role TEXT NOT NULL,
  database_name TEXT NOT NULL,
  client_code TEXT NULL,
  provisioned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  provisioned_by TEXT NOT NULL,
  notes TEXT NULL,
  CONSTRAINT ck_environment_identity_singleton
    CHECK (identity_key = 'primary'),
  CONSTRAINT ck_environment_identity_environment
    CHECK (environment IN ('local_dev', 'staging', 'production')),
  CONSTRAINT ck_environment_identity_database_role
    CHECK (database_role IN ('platform', 'client_business')),
  CONSTRAINT ck_environment_identity_client_code
    CHECK (
      (database_role = 'platform' AND client_code IS NULL)
      OR
      (
        database_role = 'client_business'
        AND client_code IS NOT NULL
        AND btrim(client_code) <> ''
      )
    )
);

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS client_db_environment TEXT NULL;

ALTER TABLE workflow_a_control.client_account
  ADD COLUMN IF NOT EXISTS client_db_identity_id UUID NULL;

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_client_db_environment;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_client_db_environment
  CHECK (
    client_db_environment IS NULL
    OR client_db_environment IN ('local_dev', 'staging', 'production')
  );

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_client_db_identity_pair;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_client_db_identity_pair
  CHECK (
    (client_db_environment IS NULL AND client_db_identity_id IS NULL)
    OR
    (client_db_environment IS NOT NULL AND client_db_identity_id IS NOT NULL)
  );

COMMENT ON TABLE ops_control.environment_identity
  IS 'Deployment-provisioned singleton identity marker. Runtime jobs read and compare it but must not create or repair marker rows.';

COMMENT ON COLUMN workflow_a_control.client_account.client_db_environment
  IS 'Expected environment identity of the client business database for fail-closed guarded operations.';

COMMENT ON COLUMN workflow_a_control.client_account.client_db_identity_id
  IS 'Expected stable UUID of the client business database identity marker for fail-closed guarded operations.';
