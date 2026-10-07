-- Workflow B Stage 3 load status and per-client/report load policy.
-- Idempotent migration; preserves existing data.

CREATE SCHEMA IF NOT EXISTS workflow_b_control;

CREATE TABLE IF NOT EXISTS workflow_b_control.report_type_client_load_policy (
    client_code TEXT NOT NULL,
    report_type TEXT NOT NULL,
    data_overwrite BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_code, report_type)
);

CREATE OR REPLACE FUNCTION workflow_b_control.set_report_type_client_load_policy_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_report_type_client_load_policy_updated_at
    ON workflow_b_control.report_type_client_load_policy;

CREATE TRIGGER trg_report_type_client_load_policy_updated_at
BEFORE UPDATE ON workflow_b_control.report_type_client_load_policy
FOR EACH ROW
EXECUTE FUNCTION workflow_b_control.set_report_type_client_load_policy_updated_at();

ALTER TABLE ingest.raw_file
    ADD COLUMN IF NOT EXISTS stage3_status TEXT NULL,
    ADD COLUMN IF NOT EXISTS stage3_started_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS stage3_finished_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS stage3_error TEXT NULL,
    ADD COLUMN IF NOT EXISTS stage3_inserted_rows INTEGER NULL,
    ADD COLUMN IF NOT EXISTS stage3_updated_rows INTEGER NULL,
    ADD COLUMN IF NOT EXISTS stage3_skipped_rows INTEGER NULL,
    ADD COLUMN IF NOT EXISTS stage3_destination_schema TEXT NULL,
    ADD COLUMN IF NOT EXISTS stage3_destination_table TEXT NULL,
    ADD COLUMN IF NOT EXISTS stage3_data_overwrite BOOLEAN NULL;

CREATE INDEX IF NOT EXISTS idx_raw_file_stage3_pending
    ON ingest.raw_file (stage2_status, stage3_status)
    WHERE stage2_status = 'OK'
      AND client_code IS NOT NULL
      AND btrim(client_code) <> '';

CREATE INDEX IF NOT EXISTS idx_report_type_client_load_policy_lookup
    ON workflow_b_control.report_type_client_load_policy (client_code, report_type);

COMMENT ON TABLE workflow_b_control.report_type_client_load_policy IS
    'Workflow B Stage 3 per-client/report-type load policy. Missing rows default to data_overwrite=false.';
COMMENT ON COLUMN workflow_b_control.report_type_client_load_policy.data_overwrite IS
    'When true, Stage 3 overwrites matching record_id rows or replaces no-record_id report tables for this client/report type.';
COMMENT ON COLUMN ingest.raw_file.stage3_status IS
    'Workflow B Stage 3 load status: RUNNING, OK, ERROR, SKIPPED_NO_RECORD_ID, or NULL before processing.';
COMMENT ON COLUMN ingest.raw_file.stage3_data_overwrite IS
    'Resolved data_overwrite policy used by Workflow B Stage 3 for this raw file load.';
