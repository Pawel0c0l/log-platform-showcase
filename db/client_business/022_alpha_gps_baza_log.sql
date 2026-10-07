-- 022_alpha_gps_baza_log.sql
-- ALPHA00001 / Alpha GPS XLSM replace-all import target.

CREATE SCHEMA IF NOT EXISTS telematics_reports;

CREATE TABLE IF NOT EXISTS telematics_reports."Alpha_GPS_Baza_LOG" (
    source_id TEXT,
    registration TEXT,
    assignment_date DATE,
    csv_filename TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_sha256 TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    raw_row_json JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_source_sha256
    ON telematics_reports."Alpha_GPS_Baza_LOG" (source_sha256);

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_registration
    ON telematics_reports."Alpha_GPS_Baza_LOG" (registration);

CREATE TABLE IF NOT EXISTS telematics_reports.alpha_gps_baza_log_import_runs (
    import_run_id UUID PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_size_bytes BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    rows_loaded INTEGER DEFAULT 0,
    error_message TEXT,
    metadata_json JSONB DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alpha_gps_baza_log_success_sha256
    ON telematics_reports.alpha_gps_baza_log_import_runs (source_sha256)
    WHERE status = 'SUCCESS';

CREATE INDEX IF NOT EXISTS idx_alpha_gps_baza_log_import_runs_started_at
    ON telematics_reports.alpha_gps_baza_log_import_runs (started_at DESC);

COMMENT ON TABLE telematics_reports."Alpha_GPS_Baza_LOG" IS
    'Replace-all import target for the ALPHA00001 Alpha GPS_baza LOG worksheet.';

COMMENT ON TABLE telematics_reports.alpha_gps_baza_log_import_runs IS
    'Import history for ALPHA00001 Alpha GPS_baza LOG XLSM loads. One successful row per source sha256; force reimports supersede the previous success.';

DO $$
DECLARE
    grant_row record;
BEGIN
    -- Mirror DML grantees from public.client_trips so the configured
    -- client_db_user can run the import after this admin-applied migration.
    FOR grant_row IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('INSERT', 'UPDATE', 'SELECT')
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', 'telematics_reports', grant_row.grantee);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
            'telematics_reports',
            'Alpha_GPS_Baza_LOG',
            grant_row.grantee
        );
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
            'telematics_reports',
            'alpha_gps_baza_log_import_runs',
            grant_row.grantee
        );
    END LOOP;
END
$$;

