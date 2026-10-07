-- 045_environment_identity_promotion_primitive.sql
-- Least-privilege, UUID-preserving client environment promotion primitive.

CREATE OR REPLACE FUNCTION ops_control.promote_environment_identity_v1(
    expected_database_uuid uuid,
    expected_current_environment text,
    target_environment text,
    expected_database_role text,
    promotion_id uuid DEFAULT NULL,
    attestation_hash text DEFAULT NULL
)
RETURNS TABLE (
    database_uuid uuid,
    old_environment text,
    new_environment text,
    database_name text,
    changed_row_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ops_control
AS $$
DECLARE
    marker ops_control.environment_identity%ROWTYPE;
    changed integer := 0;
BEGIN
    IF expected_current_environment NOT IN ('local_dev', 'staging', 'production')
       OR target_environment NOT IN ('local_dev', 'staging', 'production') THEN
        RAISE EXCEPTION 'unsupported environment identity value';
    END IF;
    IF expected_current_environment = target_environment THEN
        RAISE EXCEPTION 'source and target environments must differ';
    END IF;
    IF expected_database_role <> 'client_business' THEN
        RAISE EXCEPTION 'expected database role must be client_business';
    END IF;
    IF attestation_hash IS NOT NULL AND attestation_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'attestation hash must be lowercase SHA-256';
    END IF;

    SELECT * INTO STRICT marker
      FROM ops_control.environment_identity
     WHERE identity_key = 'primary'
     FOR UPDATE;

    IF marker.database_role <> 'client_business'
       OR marker.database_role <> expected_database_role
       OR marker.database_name <> current_database()
       OR marker.database_identity_id <> expected_database_uuid THEN
        RAISE EXCEPTION 'client database identity mismatch';
    END IF;
    IF marker.environment = target_environment THEN
        RETURN QUERY SELECT marker.database_identity_id, marker.environment,
                            marker.environment, marker.database_name, 0;
        RETURN;
    END IF;
    IF marker.environment <> expected_current_environment THEN
        RAISE EXCEPTION 'client database source environment mismatch';
    END IF;

    UPDATE ops_control.environment_identity AS identity
       SET environment = target_environment
     WHERE identity.identity_key = 'primary'
       AND identity.database_identity_id = expected_database_uuid
       AND identity.environment = expected_current_environment;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 1 THEN
        RAISE EXCEPTION 'client environment promotion changed % rows', changed;
    END IF;

    SELECT * INTO STRICT marker
      FROM ops_control.environment_identity
     WHERE identity_key = 'primary';
    IF marker.database_identity_id <> expected_database_uuid
       OR marker.environment <> target_environment
       OR marker.database_name <> current_database()
       OR marker.database_role <> 'client_business' THEN
        RAISE EXCEPTION 'client environment promotion verification failed';
    END IF;
    RETURN QUERY SELECT marker.database_identity_id, expected_current_environment,
                        marker.environment, marker.database_name, changed;
END;
$$;

REVOKE ALL ON FUNCTION ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text) FROM PUBLIC;

-- Existing-client migrations have no role placeholder. Mirror the established
-- repository convention: the intended runtime grantee is the role already
-- trusted to SELECT the singleton marker. Remove all direct marker UPDATE and
-- grant only this function.
DO $$
DECLARE
    runtime_grantee text;
BEGIN
    FOR runtime_grantee IN
        SELECT DISTINCT grantee
          FROM information_schema.role_table_grants
         WHERE table_schema = 'ops_control'
           AND table_name = 'environment_identity'
           AND privilege_type = 'SELECT'
           AND grantee <> 'PUBLIC'
           AND grantee <> current_user
    LOOP
        EXECUTE format(
            'REVOKE UPDATE ON TABLE ops_control.environment_identity FROM %I',
            runtime_grantee
        );
        EXECUTE format(
            'GRANT USAGE ON SCHEMA ops_control TO %I',
            runtime_grantee
        );
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text) TO %I',
            runtime_grantee
        );
    END LOOP;
END $$;

COMMENT ON FUNCTION ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)
  IS 'Version 1 least-privilege client environment relabel operation. UUID and all non-environment marker fields remain unchanged.';
