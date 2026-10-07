-- 031_workflow_b_ensure_alpha_gps_baza_log_registry.sql
-- Idempotently repair the Workflow B control-plane registry row and load
-- policy for ALPHA00001 Alpha GPS XLSM LOG imports.

INSERT INTO workflow_b_control.report_type_registry (
    report_type,
    display_name,
    enabled,
    cleaner_module,
    cleaner_function,
    detection_module,
    detection_rules,
    required_columns,
    optional_columns,
    min_detection_score,
    priority,
    implementation_status,
    notes,
    detection_rules_schema_version,
    column_types,
    multi_table,
    cleaner_entrypoint,
    id_sync_column_name,
    record_id_ingredients
)
VALUES (
    'Alpha_GPS_Baza_LOG',
    'Alpha GPS baza LOG',
    true,
    'jobs.reports.stage2.types.alpha_gps_baza_log',
    'AlphaGPSBazaLog.clean',
    'jobs.reports.stage2.types.alpha_gps_baza_log.AlphaGPSBazaLog.detect',
    '{
      "strategy": "header_signature_then_log_output_section",
      "file_extensions": [".xlsm", ".csv"],
      "source_sheet": "LOG",
      "required_header_labels": [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "RFID",
        "PRYW",
        "EDYS",
        "OPTIMA",
        "OTK"
      ],
      "output_header_labels": [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "Nazwa Pliku csv"
      ],
      "stop_before_header_labels": [
        "ID",
        "Data przydziału",
        "PRYW stary",
        "PRYW aktualny"
      ],
      "output_columns": [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "Nazwa Pliku csv"
      ],
      "column_mapping": {
        "ID": "source_id",
        "Nr rejestracyjny": "registration",
        "Data przydziału": "assignment_date",
        "Nazwa Pliku csv": "csv_filename"
      },
      "date_columns": ["Data przydziału"],
      "date_parse_policy": "fail_fast",
      "target_client_code": "ALPHA00001",
      "target_database": "alpha_main",
      "target_schema": "telematics_reports",
      "target_table": "Alpha_GPS_Baza_LOG",
      "load_strategy": "replace_all",
      "data_overwrite": true,
      "term_groups": [
        ["ID", "Nr rejestracyjny", "Data przydziału", "RFID", "PRYW", "EDYS", "OPTIMA", "OTK"]
      ]
    }'::jsonb,
    '[
      "ID",
      "Nr rejestracyjny",
      "Data przydziału",
      "Nazwa Pliku csv"
    ]'::jsonb,
    '[]'::jsonb,
    0.70,
    34,
    'implemented',
    'Email-driven Workflow B import for the macro-enabled Alpha GPS workbook. Stage 1 normalizes the LOG worksheet from XLSM without executing VBA; Stage 2 detects Alpha_GPS_Baza_LOG; Stage 3 replaces alpha_main.telematics_reports."Alpha_GPS_Baza_LOG" transactionally.',
    1,
    '{
      "ID": "text",
      "Nr rejestracyjny": "text",
      "Data przydziału": "date",
      "Nazwa Pliku csv": "text"
    }'::jsonb,
    false,
    'jobs.reports.stage2.types.alpha_gps_baza_log:AlphaGPSBazaLog.clean',
    NULL,
    NULL
)
ON CONFLICT (report_type)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    enabled = EXCLUDED.enabled,
    cleaner_module = EXCLUDED.cleaner_module,
    cleaner_function = EXCLUDED.cleaner_function,
    detection_module = EXCLUDED.detection_module,
    detection_rules = EXCLUDED.detection_rules,
    required_columns = EXCLUDED.required_columns,
    optional_columns = EXCLUDED.optional_columns,
    min_detection_score = EXCLUDED.min_detection_score,
    priority = EXCLUDED.priority,
    implementation_status = EXCLUDED.implementation_status,
    notes = EXCLUDED.notes,
    detection_rules_schema_version = EXCLUDED.detection_rules_schema_version,
    column_types = EXCLUDED.column_types,
    multi_table = EXCLUDED.multi_table,
    cleaner_entrypoint = EXCLUDED.cleaner_entrypoint,
    id_sync_column_name = EXCLUDED.id_sync_column_name,
    record_id_ingredients = EXCLUDED.record_id_ingredients;

INSERT INTO workflow_b_control.report_type_client_load_policy (
    client_code,
    report_type,
    data_overwrite
)
VALUES (
    'ALPHA00001',
    'Alpha_GPS_Baza_LOG',
    true
)
ON CONFLICT (client_code, report_type)
DO UPDATE SET
    data_overwrite = EXCLUDED.data_overwrite;
