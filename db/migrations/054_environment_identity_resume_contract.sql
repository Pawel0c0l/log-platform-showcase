-- 054_environment_identity_resume_contract.sql
-- Additive audit binding for journal-only resume-v2 approvals.

ALTER TABLE ops_control.environment_identity_promotion
  ADD COLUMN IF NOT EXISTS resume_contract TEXT NULL,
  ADD COLUMN IF NOT EXISTS resume_plan_sha256 TEXT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'ops_control.environment_identity_promotion'::regclass
       AND conname = 'ck_environment_identity_promotion_resume_contract'
  ) THEN
    ALTER TABLE ops_control.environment_identity_promotion
      ADD CONSTRAINT ck_environment_identity_promotion_resume_contract
      CHECK (resume_contract IS NULL OR resume_contract = 'resume-v2');
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'ops_control.environment_identity_promotion'::regclass
       AND conname = 'ck_environment_identity_promotion_resume_plan_hash'
  ) THEN
    ALTER TABLE ops_control.environment_identity_promotion
      ADD CONSTRAINT ck_environment_identity_promotion_resume_plan_hash
      CHECK (resume_plan_sha256 IS NULL OR resume_plan_sha256 ~ '^[0-9a-f]{64}$');
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ops_control.reject_environment_identity_promotion_plan_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
       NEW.promotion_id, NEW.source_environment, NEW.target_environment,
       NEW.platform_identity_id, NEW.selected_clients, NEW.immutable_plan_json,
       NEW.plan_sha256, NEW.operator_attestation_hash, NEW.backup_reference,
       NEW.started_at, NEW.created_at
     ) IS DISTINCT FROM ROW(
       OLD.promotion_id, OLD.source_environment, OLD.target_environment,
       OLD.platform_identity_id, OLD.selected_clients, OLD.immutable_plan_json,
       OLD.plan_sha256, OLD.operator_attestation_hash, OLD.backup_reference,
       OLD.started_at, OLD.created_at
     ) THEN
    RAISE EXCEPTION 'environment identity promotion plan is immutable';
  END IF;

  IF OLD.resume_contract IS NOT NULL
     AND NEW.resume_contract IS DISTINCT FROM OLD.resume_contract THEN
    RAISE EXCEPTION 'environment identity resume contract is write-once';
  END IF;
  IF OLD.resume_plan_sha256 IS NOT NULL
     AND NEW.resume_plan_sha256 IS DISTINCT FROM OLD.resume_plan_sha256 THEN
    RAISE EXCEPTION 'environment identity resume plan hash is write-once';
  END IF;

  IF OLD.runtime_file_backup_path IS NOT NULL
     AND NEW.runtime_file_backup_path IS DISTINCT FROM OLD.runtime_file_backup_path THEN
    RAISE EXCEPTION 'runtime file backup path is write-once';
  END IF;
  IF OLD.runtime_file_before_sha256 IS NOT NULL
     AND NEW.runtime_file_before_sha256 IS DISTINCT FROM OLD.runtime_file_before_sha256 THEN
    RAISE EXCEPTION 'runtime file before hash is write-once';
  END IF;
  IF OLD.runtime_file_after_sha256 IS NOT NULL
     AND NEW.runtime_file_after_sha256 IS DISTINCT FROM OLD.runtime_file_after_sha256 THEN
    RAISE EXCEPTION 'runtime file after hash is write-once';
  END IF;
  RETURN NEW;
END;
$$;

COMMENT ON COLUMN ops_control.environment_identity_promotion.resume_contract
  IS 'Write-once executable resume approval contract; only resume-v2 is accepted.';
COMMENT ON COLUMN ops_control.environment_identity_promotion.resume_plan_sha256
  IS 'Write-once SHA-256 of the exact canonical resume-v2 plan approved for this completion attempt.';
