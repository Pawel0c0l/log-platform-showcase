-- Workflow B target table for ALPHA00001 Alpha GPS XLSM LOG imports.
-- Applied to the client business database (alpha_main).

CREATE SCHEMA IF NOT EXISTS telematics_reports;

CREATE TABLE IF NOT EXISTS telematics_reports."Alpha_GPS_Baza_LOG" (
    source_id TEXT,
    registration TEXT,
    assignment_date DATE,
    csv_filename TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    workflow_run_id UUID NULL,
    raw_file_id UUID NULL,
    source_artifact_id UUID NULL,
    normalized_artifact_id UUID NULL,
    cleaned_artifact_id UUID NULL,
    source_sha256 TEXT NULL,
    source_row_number INTEGER NULL,
    raw_row_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_registration
    ON telematics_reports."Alpha_GPS_Baza_LOG" (registration);

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_assignment_date
    ON telematics_reports."Alpha_GPS_Baza_LOG" (assignment_date);

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_source_sha256
    ON telematics_reports."Alpha_GPS_Baza_LOG" (source_sha256);

COMMENT ON TABLE telematics_reports."Alpha_GPS_Baza_LOG" IS
    'Replace-all Workflow B load target for the Alpha GPS XLSM LOG output.';
COMMENT ON COLUMN telematics_reports."Alpha_GPS_Baza_LOG".workflow_run_id IS
    'Workflow B Stage 3 platform run_id that loaded this replacement snapshot.';
COMMENT ON COLUMN telematics_reports."Alpha_GPS_Baza_LOG".raw_file_id IS
    'Workflow B ingest.raw_file id for the source email attachment.';
COMMENT ON COLUMN telematics_reports."Alpha_GPS_Baza_LOG".source_artifact_id IS
    'Stage 1 raw XLSM artifact id when available.';
COMMENT ON COLUMN telematics_reports."Alpha_GPS_Baza_LOG".normalized_artifact_id IS
    'Stage 1 normalized CSV artifact id when available.';
COMMENT ON COLUMN telematics_reports."Alpha_GPS_Baza_LOG".cleaned_artifact_id IS
    'Stage 2 cleaned CSV artifact id loaded by Stage 3.';

DO $$
DECLARE
    grantee_name TEXT;
BEGIN
    FOR grantee_name IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type = 'SELECT'
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA telematics_reports TO %I', grantee_name);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE telematics_reports."Alpha_GPS_Baza_LOG" TO %I',
            grantee_name
        );
    END LOOP;
END $$;
