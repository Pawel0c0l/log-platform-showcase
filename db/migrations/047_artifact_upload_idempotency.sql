-- 047_artifact_upload_idempotency.sql
-- Optional machine-token-scoped idempotency identity for artifact uploads.

DO $$
DECLARE
  marker_count INTEGER;
BEGIN
  IF to_regclass('ops_control.environment_identity') IS NULL THEN
    RAISE EXCEPTION 'platform environment identity guard is not installed';
  END IF;
  SELECT count(*) INTO marker_count
    FROM ops_control.environment_identity
   WHERE identity_key = 'primary' AND database_role = 'platform';
  IF marker_count <> 1 THEN
    RAISE EXCEPTION 'platform environment identity guard is not provisioned';
  END IF;
END
$$;

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS idempotency_scope VARCHAR(128),
  ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256);

ALTER TABLE artifacts
  ADD CONSTRAINT ck_artifacts_idempotency_pair
  CHECK ((idempotency_scope IS NULL) = (idempotency_key IS NULL)),
  ADD CONSTRAINT ck_artifacts_idempotency_scope_not_blank
  CHECK (idempotency_scope IS NULL OR btrim(idempotency_scope, E' \t\n\r\f\v') <> ''),
  ADD CONSTRAINT ck_artifacts_idempotency_key_not_blank
  CHECK (idempotency_key IS NULL OR btrim(idempotency_key, E' \t\n\r\f\v') <> '');

CREATE UNIQUE INDEX uq_artifacts_idempotency_identity
  ON artifacts (idempotency_scope, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

COMMENT ON COLUMN artifacts.idempotency_scope IS
  'Opaque non-secret namespace within the shared machine write-token authorization boundary.';
COMMENT ON COLUMN artifacts.idempotency_key IS
  'Opaque non-secret retry identity; never include customer data or credentials.';
