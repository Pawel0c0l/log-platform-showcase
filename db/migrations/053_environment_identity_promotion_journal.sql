-- 053_environment_identity_promotion_journal.sql
-- Durable journal for staged, cross-database environment identity promotion.
-- The journal records immutable plans and progress; it never stores secrets.

CREATE SCHEMA IF NOT EXISTS ops_control;

CREATE TABLE IF NOT EXISTS ops_control.environment_identity_promotion (
  promotion_id UUID PRIMARY KEY,
  source_environment TEXT NOT NULL,
  target_environment TEXT NOT NULL,
  platform_identity_id UUID NOT NULL,
  selected_clients JSONB NOT NULL,
  immutable_plan_json JSONB NOT NULL,
  plan_sha256 TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'planned',
  started_at TIMESTAMPTZ NULL,
  completed_at TIMESTAMPTZ NULL,
  failed_at TIMESTAMPTZ NULL,
  current_step TEXT NULL,
  completed_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
  error TEXT NULL,
  operator_attestation_hash TEXT NOT NULL,
  backup_reference TEXT NOT NULL,
  runtime_file_backup_path TEXT NULL,
  runtime_file_before_sha256 TEXT NULL,
  runtime_file_after_sha256 TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_environment_identity_promotion_source
    CHECK (source_environment IN ('local_dev', 'staging', 'production')),
  CONSTRAINT ck_environment_identity_promotion_target
    CHECK (target_environment IN ('local_dev', 'staging', 'production')),
  CONSTRAINT ck_environment_identity_promotion_distinct
    CHECK (source_environment <> target_environment),
  CONSTRAINT ck_environment_identity_promotion_state
    CHECK (state IN ('planned', 'in_progress', 'completed', 'failed', 'rolled_back')),
  CONSTRAINT ck_environment_identity_promotion_clients
    CHECK (jsonb_typeof(selected_clients) = 'array' AND jsonb_array_length(selected_clients) > 0),
  CONSTRAINT ck_environment_identity_promotion_plan
    CHECK (jsonb_typeof(immutable_plan_json) = 'object'),
  CONSTRAINT ck_environment_identity_promotion_plan_hash
    CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_environment_identity_promotion_attestation_hash
    CHECK (operator_attestation_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_environment_identity_promotion_state_created
  ON ops_control.environment_identity_promotion (state, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_environment_identity_promotion_active
  ON ops_control.environment_identity_promotion ((true))
  WHERE state IN ('planned', 'in_progress');


CREATE OR REPLACE FUNCTION ops_control.reject_environment_identity_promotion_plan_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
       NEW.promotion_id,
       NEW.source_environment,
       NEW.target_environment,
       NEW.platform_identity_id,
       NEW.selected_clients,
       NEW.immutable_plan_json,
       NEW.plan_sha256,
       NEW.operator_attestation_hash,
       NEW.backup_reference,
       NEW.created_at
     ) IS DISTINCT FROM ROW(
       OLD.promotion_id,
       OLD.source_environment,
       OLD.target_environment,
       OLD.platform_identity_id,
       OLD.selected_clients,
       OLD.immutable_plan_json,
       OLD.plan_sha256,
       OLD.operator_attestation_hash,
       OLD.backup_reference,
       OLD.created_at
     ) THEN
    RAISE EXCEPTION 'environment identity promotion plan is immutable';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_environment_identity_promotion_plan_immutable
  ON ops_control.environment_identity_promotion;
CREATE TRIGGER trg_environment_identity_promotion_plan_immutable
BEFORE UPDATE ON ops_control.environment_identity_promotion
FOR EACH ROW
EXECUTE FUNCTION ops_control.reject_environment_identity_promotion_plan_update();
COMMENT ON TABLE ops_control.environment_identity_promotion
  IS 'Secret-free durable journal for staged and resumable environment classification promotion; database UUIDs remain immutable.';

COMMENT ON COLUMN ops_control.environment_identity_promotion.immutable_plan_json
  IS 'Canonical immutable plan containing selected surfaces, expected identities, and step order; no credentials or raw environment-file contents.';
