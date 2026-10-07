-- 038_environment_identity.sql
-- Per-client database identity marker structure.
-- Deployment-specific marker rows are provisioned separately; this migration
-- intentionally inserts no environment value or UUID.

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

COMMENT ON TABLE ops_control.environment_identity
  IS 'Deployment-provisioned singleton identity marker. Runtime jobs receive SELECT only and must not create or repair marker rows.';
