-- 042_workflow_b_stage3_runtime_schema.sql
-- Workflow B Stage 3 schema preparation owned by the controlled migration path.
-- Recurring Stage 3 and Report 207 postprocessing must perform DML only.

CREATE SCHEMA IF NOT EXISTS telematics_reports;

-- Report 207 has a stable, registered business schema. Upgrade it when the
-- client already has that report target; do not create unused report tables in
-- every client database. A missing target needs an explicitly scoped bootstrap.
ALTER TABLE IF EXISTS telematics_reports.report_207
  ADD COLUMN IF NOT EXISTS "Data i czas" TEXT NULL,
  ADD COLUMN IF NOT EXISTS "Nr rejestracyjny" TEXT NULL,
  ADD COLUMN IF NOT EXISTS "Prędkość" TEXT NULL,
  ADD COLUMN IF NOT EXISTS "Ograniczenie prędkości drogowej" TEXT NULL,
  ADD COLUMN IF NOT EXISTS "Lokalizacja" TEXT NULL,
  ADD COLUMN IF NOT EXISTS record_id TEXT NULL;

DO $$
DECLARE
    report_row record;
    duplicate_sample text;
    index_name text;
BEGIN
    FOR report_row IN
        SELECT c.relname AS table_name
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'telematics_reports'
          AND c.relkind IN ('r', 'p')
          AND c.relname = ANY (ARRAY[
              'd104_1', 'd104_7', 'd105_2', 'eco_driving_driver',
              'eco_driving_vehicle', 'n104_1', 'report_112', 'report_207',
              'report_602', 'report_602_ev', 'report_d105_2_ecodriving'
          ])
        ORDER BY c.relname
    LOOP
        EXECUTE format(
            'ALTER TABLE telematics_reports.%I '
            'ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(), '
            'ADD COLUMN IF NOT EXISTS _raw_file_id TEXT NULL, '
            'ADD COLUMN IF NOT EXISTS _source_artifact_id TEXT NULL, '
            'ADD COLUMN IF NOT EXISTS _source_filename TEXT NULL, '
            'ADD COLUMN IF NOT EXISTS _stage3_run_id TEXT NULL',
            report_row.table_name
        );

        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'telematics_reports'
              AND table_name = report_row.table_name
              AND column_name = 'record_id'
        ) THEN
            EXECUTE format(
                'SELECT string_agg(format(''%%s (%%s)'', record_id, row_count), '', '') '
                'FROM ('
                '  SELECT record_id, count(*) AS row_count '
                '  FROM telematics_reports.%I '
                '  WHERE NULLIF(btrim(record_id), '''') IS NOT NULL '
                '  GROUP BY record_id HAVING count(*) > 1 LIMIT 10'
                ') AS duplicates',
                report_row.table_name
            ) INTO duplicate_sample;
            IF duplicate_sample IS NOT NULL THEN
                RAISE EXCEPTION
                    'Cannot prepare telematics_reports.%: duplicate record_id values: %',
                    report_row.table_name,
                    duplicate_sample;
            END IF;

            index_name := left(report_row.table_name || '__record_id_uidx', 63);
            EXECUTE format(
                'CREATE UNIQUE INDEX IF NOT EXISTS %I ON telematics_reports.%I (record_id) '
                'WHERE NULLIF(btrim(record_id), '''') IS NOT NULL',
                index_name,
                report_row.table_name
            );
        END IF;
    END LOOP;
END $$;

ALTER TABLE IF EXISTS telematics_reports.report_207
  ADD COLUMN IF NOT EXISTS migrated_to_client_db BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS migrated_to_client_db_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS migrated_to_client_trip_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS migrated_to_client_db_error TEXT NULL;

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS speeding_140_160_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS speeding_160_170_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS speeding_170_plus_count INTEGER NOT NULL DEFAULT 0;

-- Existing-client migrations have no client_db_user placeholder. Follow the
-- established repository convention and identify runtime roles from existing
-- SELECT+UPDATE grants on public.client_trips. No ownership or ALL grants are used.
DO $$
DECLARE
    runtime_role record;
    report_row record;
BEGIN
    FOR runtime_role IN
        SELECT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('SELECT', 'UPDATE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee
        HAVING count(DISTINCT privilege_type) = 2
    LOOP
        EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO %I',
            current_database(),
            runtime_role.grantee
        );
        EXECUTE format('GRANT USAGE ON SCHEMA telematics_reports TO %I', runtime_role.grantee);
        EXECUTE format(
            'GRANT SELECT, UPDATE ON TABLE public.client_trips TO %I',
            runtime_role.grantee
        );
        FOR report_row IN
            SELECT c.relname AS table_name
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'telematics_reports'
              AND c.relkind IN ('r', 'p')
              AND c.relname = ANY (ARRAY[
              'd104_1', 'd104_7', 'd105_2', 'eco_driving_driver',
              'eco_driving_vehicle', 'n104_1', 'report_112', 'report_207',
              'report_602', 'report_602_ev', 'report_d105_2_ecodriving'
          ])
        LOOP
            -- DELETE is required only for the existing Stage 3 replace-all policy;
            -- no sequence privilege is needed because Stage 3 tables use no sequence.
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE telematics_reports.%I TO %I',
                report_row.table_name,
                runtime_role.grantee
            );
        END LOOP;
    END LOOP;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workflow_b_stage3_loader') THEN
        EXECUTE format(
            'REVOKE CREATE ON DATABASE %I FROM workflow_b_stage3_loader',
            current_database()
        );
        REVOKE CREATE ON SCHEMA telematics_reports FROM workflow_b_stage3_loader;
    END IF;
END $$;

-- Read-only verification queries (also useful when reviewing a controlled apply).
SELECT n.nspname AS schema_name, c.relname AS table_name, owner.rolname AS table_owner
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_roles AS owner ON owner.oid = c.relowner
WHERE (n.nspname = 'telematics_reports' AND c.relname = ANY (ARRAY['d104_1','d104_7','d105_2','eco_driving_driver','eco_driving_vehicle','n104_1','report_112','report_207','report_602','report_602_ev','report_d105_2_ecodriving']))
   OR (n.nspname = 'public' AND c.relname = 'client_trips')
ORDER BY n.nspname, c.relname;

SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE (table_schema = 'telematics_reports' AND table_name = ANY (ARRAY['d104_1','d104_7','d105_2','eco_driving_driver','eco_driving_vehicle','n104_1','report_112','report_207','report_602','report_602_ev','report_d105_2_ecodriving'])
       AND column_name IN (
           '_loaded_at', '_raw_file_id', '_source_artifact_id',
           '_source_filename', '_stage3_run_id', 'migrated_to_client_db',
           'migrated_to_client_db_at', 'migrated_to_client_trip_id',
           'migrated_to_client_db_error'
       ))
   OR (table_schema = 'public' AND table_name = 'client_trips'
       AND column_name IN (
           'speeding_140_160_count', 'speeding_160_170_count', 'speeding_170_plus_count'
       ))
ORDER BY table_schema, table_name, column_name;

SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'telematics_reports'
  AND indexname LIKE '%\_\_record\_id\_uidx' ESCAPE '\'
ORDER BY tablename, indexname;

SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE (table_schema = 'telematics_reports' AND table_name = ANY (ARRAY['d104_1','d104_7','d105_2','eco_driving_driver','eco_driving_vehicle','n104_1','report_112','report_207','report_602','report_602_ev','report_d105_2_ecodriving']))
   OR (table_schema = 'public' AND table_name = 'client_trips')
ORDER BY grantee, table_schema, table_name, privilege_type;
