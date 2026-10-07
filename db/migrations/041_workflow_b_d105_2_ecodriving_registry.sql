-- 041_workflow_b_d105_2_ecodriving_registry.sql
-- Add the special D105.2 EcoDriving/event metrics report variant to the
-- Workflow B report-type read model.
--
-- Stage 2 runtime remains Python-registry driven. This row gives operators
-- visibility and configures existing Stage 2 client_code/record_id finalization.

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
  'report_d105_2_ecodriving',
  'D105.2 EcoDriving trip metrics report',
  true,
  'jobs.reports.stage2.types.d105_2_ecodriving',
  'D1052EcoDrivingReport.clean',
  'jobs.reports.stage2.types.d105_2_ecodriving.D1052EcoDrivingReport.detect',
  '{
    "schema_version": 1,
    "text_scope": {"table_scope": "first_table", "mode": "header_only"},
    "required_anywhere_strings": [],
    "required_header_labels": [
      "Nr Rejestracyjny",
      "Czas rozpoczęcia",
      "Czas zakończenia",
      "przekroczenia obr/min",
      "> 140kmh",
      "> 160kmh",
      "> 170kmh"
    ],
    "optional_header_labels": [],
    "forbidden_anywhere_strings": [],
    "filename_hints": [],
    "score_weights": {
      "required_header_labels": 1.00,
      "required_anywhere_strings": 0.00,
      "filename_hints": 0.00
    },
    "term_groups": [
      {
        "name": "required_header_labels",
        "scope": "row_cells",
        "op": "contains_all_exact_normalized_cells",
        "values": [
          "Nr Rejestracyjny",
          "Czas rozpoczęcia",
          "Czas zakończenia",
          "przekroczenia obr/min",
          "> 140kmh",
          "> 160kmh",
          "> 170kmh"
        ],
        "weight": 1.00
      }
    ]
  }'::jsonb,
  '[
    "Nr Rejestracyjny",
    "Czas rozpoczęcia",
    "Czas zakończenia",
    "przekroczenia obr/min",
    "> 140kmh",
    "> 160kmh",
    "> 170kmh"
  ]'::jsonb,
  '[]'::jsonb,
  0.70,
  36,
  'implemented',
  'Special D105.2 report variant with EcoDriving/event metrics. Detection is structural only and must not depend on filename or report title. Stage 3 dynamic loader target table is telematics_reports.report_d105_2_ecodriving; postprocess job jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration writes client_trips metrics only when trip_metrics_population_source=d105_2_ecodriving_migration.',
  1,
  '{
    "Nr Rejestracyjny": "string",
    "Czas rozpoczęcia": "datetime",
    "Czas zakończenia": "datetime",
    "przekroczenia obr/min": "numeric",
    "> 140kmh": "numeric",
    "> 160kmh": "numeric",
    "> 170kmh": "numeric"
  }'::jsonb,
  false,
  'jobs.reports.stage2.types.d105_2_ecodriving:D1052EcoDrivingReport.clean',
  'Nr Rejestracyjny',
  'Nr Rejestracyjny,Czas rozpoczęcia,Czas zakończenia,przekroczenia obr/min,> 140kmh,> 160kmh,> 170kmh'
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
      cleaner_entrypoint = EXCLUDED.cleaner_entrypoint,
      id_sync_column_name = EXCLUDED.id_sync_column_name,
      record_id_ingredients = EXCLUDED.record_id_ingredients;
