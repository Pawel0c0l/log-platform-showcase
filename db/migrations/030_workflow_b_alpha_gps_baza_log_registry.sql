-- Register ALPHA00001 Alpha GPS XLSM LOG import in Workflow B control-plane metadata.
-- Stage 2 runtime remains Python-registry driven; this row documents the
-- implemented detector/cleaner and the Stage 3 replace-all load policy.

CREATE SCHEMA IF NOT EXISTS workflow_b_control;

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
  cleaner_entrypoint
)
VALUES (
  'Alpha_GPS_Baza_LOG',
  'ALPHA00001 Alpha GPS XLSM LOG',
  true,
  'jobs.reports.stage2.types.alpha_gps_baza_log',
  'AlphaGPSBazaLog.clean',
  'jobs.reports.stage2.types.alpha_gps_baza_log.AlphaGPSBazaLog.detect',
  '{
    "schema_version": 1,
    "text_scope": {"table_scope": "first_table", "mode": "all_rows"},
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
    "load_strategy": "replace_all",
    "target_schema": "telematics_reports",
    "target_table": "Alpha_GPS_Baza_LOG",
    "term_groups": [
      {
        "name": "main_gps_base_header",
        "scope": "row_cells",
        "op": "contains_all_exact_normalized_cells",
        "values": [
          "ID",
          "Nr rejestracyjny",
          "Data przydziału",
          "RFID",
          "PRYW",
          "EDYS",
          "OPTIMA",
          "OTK"
        ],
        "weight": 1.0
      }
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
  'Email-driven Workflow B import for the macro-enabled Alpha GPS workbook. Stage 1 normalizes the LOG worksheet from XLSM without executing VBA; Stage 3 replaces telematics_reports."Alpha_GPS_Baza_LOG" transactionally.',
  1,
  '{
    "ID": "string",
    "Nr rejestracyjny": "string",
    "Data przydziału": "date",
    "Nazwa Pliku csv": "string"
  }'::jsonb,
  false,
  'jobs.reports.stage2.types.alpha_gps_baza_log:AlphaGPSBazaLog.clean'
)
ON CONFLICT (report_type) DO UPDATE
  SET display_name = EXCLUDED.display_name,
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
      cleaner_entrypoint = EXCLUDED.cleaner_entrypoint;

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
ON CONFLICT (client_code, report_type) DO UPDATE
  SET data_overwrite = EXCLUDED.data_overwrite;
