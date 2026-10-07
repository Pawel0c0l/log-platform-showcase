-- 020_workflow_b_report_registry_detection_contract.sql
-- Workflow B — Phase 1 contract for future DB-driven Stage 2 detection.
--
-- This migration only enriches workflow_b_control.report_type_registry as a
-- machine-readable control-plane/read-model table. It does not change
-- jobs.reports.stage2 runtime behavior; the Python registry and detector remain
-- the default source of truth.

ALTER TABLE workflow_b_control.report_type_registry
  ADD COLUMN IF NOT EXISTS detection_rules_schema_version INTEGER;

UPDATE workflow_b_control.report_type_registry
   SET detection_rules_schema_version = 1
 WHERE detection_rules_schema_version IS NULL;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN detection_rules_schema_version SET DEFAULT 1;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN detection_rules_schema_version SET NOT NULL;

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_detection_rules_schema_version;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_detection_rules_schema_version
  CHECK (detection_rules_schema_version >= 1);

ALTER TABLE workflow_b_control.report_type_registry
  ADD COLUMN IF NOT EXISTS column_types JSONB;

UPDATE workflow_b_control.report_type_registry
   SET column_types = '{}'::jsonb
 WHERE column_types IS NULL;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN column_types SET DEFAULT '{}'::jsonb;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN column_types SET NOT NULL;

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_column_types_object;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_column_types_object
  CHECK (jsonb_typeof(column_types) = 'object');

ALTER TABLE workflow_b_control.report_type_registry
  ADD COLUMN IF NOT EXISTS multi_table BOOLEAN;

UPDATE workflow_b_control.report_type_registry
   SET multi_table = false
 WHERE multi_table IS NULL;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN multi_table SET DEFAULT false;

ALTER TABLE workflow_b_control.report_type_registry
  ALTER COLUMN multi_table SET NOT NULL;

ALTER TABLE workflow_b_control.report_type_registry
  ADD COLUMN IF NOT EXISTS cleaner_entrypoint TEXT;

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_cleaner_entrypoint_shape;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_cleaner_entrypoint_shape
  CHECK (
    cleaner_entrypoint IS NULL
    OR cleaner_entrypoint ~ '^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*$'
  );

-- Reassert JSON shape constraints that are part of the Phase 1 contract.
ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_detection_rules_object;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_detection_rules_object
  CHECK (jsonb_typeof(detection_rules) = 'object');

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_required_columns_array;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_required_columns_array
  CHECK (jsonb_typeof(required_columns) = 'array');

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_optional_columns_array;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_optional_columns_array
  CHECK (jsonb_typeof(optional_columns) = 'array');

COMMENT ON COLUMN workflow_b_control.report_type_registry.detection_rules_schema_version IS
  'Version of the machine-readable detection_rules contract. Version 1 is seeded from current Stage 2 Python detectors.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.column_types IS
  'Machine-readable output column type hints derived from Stage 2 Python COLUMN_TYPES; values use string/date/datetime/integer/numeric/boolean.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.multi_table IS
  'Whether the report type currently expects Stage 2 split_into_tables multi-table input.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.cleaner_entrypoint IS
  'Canonical future dynamic cleaner entrypoint in module:Class.method format; cleaner_module and cleaner_function are retained for compatibility.';

WITH contract_rows (
  report_type,
  detection_rules_schema_version,
  column_types,
  multi_table,
  cleaner_entrypoint,
  detection_rules,
  notes
) AS (
VALUES
  (
    'd104_1',
    1,
    '{"Czas-Start":"date","Czas-Koniec":"date","Dystans":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.d104_1:D1041Report.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 40},
      "required_anywhere_strings": ["104 ogólny raport podróży - podsumowanie"],
      "required_header_labels": ["Kierowca", "Rodzaj", "Dystans"],
      "optional_header_labels": ["Moce przyśpieszenia"],
      "forbidden_anywhere_strings": ["104.1"],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.45,
        "required_header_labels": 0.30,
        "optional_header_labels": 0.25,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_title", "scope": "anywhere_text", "op": "contains_all", "values": ["104 ogólny raport podróży - podsumowanie"], "forbidden_values": ["104.1"], "weight": 0.45},
        {"name": "driver_trip_labels", "scope": "anywhere_text", "op": "contains_all", "values": ["kierowca", "nr", "rodzaj"], "weight": 0.30},
        {"name": "acceleration_label", "scope": "anywhere_text", "op": "contains_all", "values": ["moce przyśpieszenia"], "weight": 0.25}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from D1041Report.detect and clean header lookup. filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'n104_1',
    1,
    '{"Czas-Start":"date","Czas-Koniec":"date","Dystans":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.n104_1:N1041Report.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 40},
      "required_anywhere_strings": ["104.1 ogólny raport podróży - podsumowanie"],
      "required_header_labels": ["Opis pojazdu", "Rodzaj", "Dystans"],
      "optional_header_labels": ["Silne Przyśpieszenia"],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.50,
        "required_header_labels": 0.30,
        "optional_header_labels": 0.20,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_title", "scope": "anywhere_text", "op": "contains_all", "values": ["104.1 ogólny raport podróży - podsumowanie"], "weight": 0.50},
        {"name": "vehicle_trip_labels", "scope": "anywhere_text", "op": "contains_all", "values": ["opis pojazdu", "rodzaj"], "weight": 0.30},
        {"name": "acceleration_label", "scope": "anywhere_text", "op": "contains_all", "values": ["silne przyśpieszenia"], "weight": 0.20}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from N1041Report.detect and clean header lookup. filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'report_112',
    1,
    '{"Licznik początek":"numeric","Licznik koniec":"numeric","Dystans":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.report_112:Report112.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 25},
      "required_anywhere_strings": ["112 raport stanu licznika"],
      "required_header_labels": ["Nr rejestracyjny", "Licznik początek", "Licznik koniec"],
      "optional_header_labels": [],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.80,
        "required_header_labels": 0.40,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_title", "scope": "anywhere_text", "op": "contains_all", "values": ["112 raport stanu licznika"], "weight": 0.80},
        {"name": "odometer_labels", "scope": "anywhere_text", "op": "contains_all", "values": ["licznik początek", "licznik koniec"], "weight": 0.40}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from Report112.detect and clean header lookup. Score terms intentionally sum above 1.0 because the Python detector clamps the final score.'
  ),
  (
    'report_602',
    1,
    '{"Paliwo zużyte":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.report_602:Report602.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 30},
      "required_anywhere_strings": ["602 raport zużycia paliwa"],
      "required_header_labels": ["Nr rejestracyjny", "Paliwo zużyte"],
      "optional_header_labels": [],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.70,
        "required_header_labels": 0.30,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_title", "scope": "anywhere_text", "op": "contains_all", "values": ["602 raport zużycia paliwa"], "weight": 0.70},
        {"name": "fuel_label", "scope": "anywhere_text", "op": "contains_all", "values": ["paliwo zużyte"], "weight": 0.30}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from Report602.detect and clean header lookup. filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'report_602_ev',
    1,
    '{"Zużyta energia":"numeric","Dystans":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.report_602_ev:Report602EV.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 35},
      "required_anywhere_strings": ["602 raport zużycia energii"],
      "required_header_labels": ["Nr rejestracyjny", "Zużyta energia", "Dystans"],
      "optional_header_labels": [],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.70,
        "required_header_labels": 0.30,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_title", "scope": "anywhere_text", "op": "contains_all", "values": ["602 raport zużycia energii"], "weight": 0.70},
        {"name": "energy_label", "scope": "anywhere_text", "op": "contains_all", "values": ["zużyta energia"], "weight": 0.30}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from Report602EV.detect and clean header lookup. filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'd104_7',
    1,
    '{"Data godzina startu":"date","Data godzina zakończenia":"date","Dystans":"numeric"}'::jsonb,
    false,
    'jobs.reports.stage2.types.d104_7:D1047Report.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_rows", "rows": 30},
      "required_anywhere_strings": ["d104.7"],
      "required_header_labels": ["Numer rejestracyjny", "Data godzina startu", "Data godzina zakończenia"],
      "optional_header_labels": [],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.70,
        "required_header_labels": 0.30,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "report_marker", "scope": "anywhere_text", "op": "contains_all", "values": ["d104.7"], "weight": 0.70},
        {"name": "start_time_label", "scope": "anywhere_text", "op": "contains_all", "values": ["data godzina startu"], "weight": 0.30}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from D1047Report.detect and clean header lookup. filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'd105_2',
    1,
    '{"Data rozpoczęcia":"date","Data zakończenia":"date"}'::jsonb,
    false,
    'jobs.reports.stage2.types.d105_2:D1052Report.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "first_table", "mode": "first_row", "rows": 1},
      "required_anywhere_strings": [],
      "required_header_labels": ["Nr Rejestracyjny", "Data rozpoczęcia", "Data zakończenia"],
      "optional_header_labels": ["Kierowca ID"],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.0,
        "required_header_labels": 1.0,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0
      },
      "term_groups": [
        {"name": "vehicle_driver_labels", "scope": "first_row_text", "op": "contains_all", "values": ["nr rejestracyjny", "kierowca id"], "weight": 0.60},
        {"name": "date_labels", "scope": "first_row_text", "op": "contains_all", "values": ["data rozpoczęcia", "data zakończenia"], "weight": 0.40}
      ]
    }'::jsonb,
    'Phase 1 detection contract seeded from D1052Report.detect. Detection uses first-row text; filename_hints is empty because the Python detector does not inspect filenames.'
  ),
  (
    'eco_driving_driver',
    1,
    '{}'::jsonb,
    true,
    'jobs.reports.stage2.types.eco_driving_driver:EcoDrivingDriver.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "all_tables", "mode": "first_rows_per_table", "rows": 20},
      "required_anywhere_strings": ["raport ecodriving"],
      "required_header_labels": ["Kierowca"],
      "optional_header_labels": [],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.50,
        "required_header_labels": 0.20,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0,
        "min_table_count": 0.30
      },
      "term_groups": [
        {"name": "report_title", "scope": "all_tables_text", "op": "contains_all", "values": ["raport ecodriving"], "weight": 0.50},
        {"name": "driver_label_stem", "scope": "all_tables_text", "op": "contains_all", "values": ["kierowc"], "weight": 0.20},
        {"name": "multi_table_shape", "scope": "tables", "op": "table_count_at_least", "value": 2, "weight": 0.30}
      ]
    }'::jsonb,
    'Detection exists, but clean() raises NotImplementedError; Stage 2 marks matching files PENDING_REVIEW with cleaning_not_implemented. Phase 1 contract keeps filename_hints empty because the Python detector does not inspect filenames.'
  ),
  (
    'eco_driving_vehicle',
    1,
    '{}'::jsonb,
    true,
    'jobs.reports.stage2.types.eco_driving_vehicle:EcoDrivingVehicle.clean',
    '{
      "schema_version": 1,
      "text_scope": {"table_scope": "all_tables", "mode": "first_rows_per_table", "rows": 20},
      "required_anywhere_strings": ["raport ecodriving"],
      "required_header_labels": ["Nr rejestracyjny"],
      "optional_header_labels": ["pojazd"],
      "forbidden_anywhere_strings": [],
      "filename_hints": [],
      "score_weights": {
        "required_anywhere_strings": 0.50,
        "required_header_labels": 0.20,
        "optional_header_labels": 0.0,
        "filename_hints": 0.0,
        "min_table_count": 0.30
      },
      "term_groups": [
        {"name": "report_title", "scope": "all_tables_text", "op": "contains_all", "values": ["raport ecodriving"], "weight": 0.50},
        {"name": "vehicle_label", "scope": "all_tables_text", "op": "contains_any", "values": ["nr rejestr", "pojazd"], "weight": 0.20},
        {"name": "multi_table_shape", "scope": "tables", "op": "table_count_at_least", "value": 2, "weight": 0.30}
      ]
    }'::jsonb,
    'Detection exists, but clean() raises NotImplementedError; Stage 2 marks matching files PENDING_REVIEW with cleaning_not_implemented. Phase 1 contract keeps filename_hints empty because the Python detector does not inspect filenames.'
  )
)
UPDATE workflow_b_control.report_type_registry AS registry
   SET detection_rules_schema_version = contract_rows.detection_rules_schema_version,
       column_types = contract_rows.column_types,
       multi_table = contract_rows.multi_table,
       cleaner_entrypoint = contract_rows.cleaner_entrypoint,
       detection_rules = contract_rows.detection_rules,
       notes = contract_rows.notes
  FROM contract_rows
 WHERE registry.report_type = contract_rows.report_type;
