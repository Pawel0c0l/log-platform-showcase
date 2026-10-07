-- 021_workflow_b_report_207_registry.sql
-- Add Telematics report 207 to the Workflow B control-plane/read-model registry.
--
-- Stage 2 runtime remains Python-registry driven. This row is for operator
-- visibility and future DB-driven detection metadata only.

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
  'report_207',
  '207 Raport przekroczeń limitów prędkości drogowej',
  true,
  'jobs.reports.stage2.types.report_207',
  'Report207.clean',
  'jobs.reports.stage2.types.report_207.Report207.detect',
  '{
    "schema_version": 1,
    "text_scope": {"table_scope": "first_table", "mode": "all_rows"},
    "required_anywhere_strings": ["207 Raport przekroczeń limitów prędkości drogowej"],
    "required_header_labels": [
      "Data i czas",
      "Nr rejestracyjny",
      "Prędkość",
      "Ograniczenie prędkości drogowej",
      "Lokalizacja"
    ],
    "optional_header_labels": [],
    "forbidden_anywhere_strings": [],
    "filename_hints": [],
    "score_weights": {
      "required_header_labels": 0.70,
      "required_anywhere_strings": 0.30,
      "filename_hints": 0.00
    },
    "term_groups": [
      {
        "name": "required_header_labels",
        "scope": "row_cells",
        "op": "contains_all_exact_normalized_cells",
        "values": [
          "Data i czas",
          "Nr rejestracyjny",
          "Prędkość",
          "Ograniczenie prędkości drogowej",
          "Lokalizacja"
        ],
        "weight": 0.70
      },
      {
        "name": "report_title",
        "scope": "anywhere_text",
        "op": "contains_normalized",
        "values": ["207 Raport przekroczeń limitów prędkości drogowej"],
        "weight": 0.30
      }
    ]
  }'::jsonb,
  '[
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "Ograniczenie prędkości drogowej",
    "Lokalizacja"
  ]'::jsonb,
  '[]'::jsonb,
  0.70,
  35,
  'implemented',
  'Stage 2 runtime still uses Python detection in jobs.reports.stage2.registry/detector. This DB row is control-plane/operator metadata for visibility and future DB-driven detection only; Stage 3 loading is not implemented.',
  1,
  '{
    "Data i czas": "datetime",
    "Nr rejestracyjny": "string",
    "Prędkość": "numeric",
    "Ograniczenie prędkości drogowej": "numeric",
    "Lokalizacja": "string"
  }'::jsonb,
  false,
  'jobs.reports.stage2.types.report_207:Report207.clean'
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
