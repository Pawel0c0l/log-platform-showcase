-- 019_workflow_b_report_type_registry.sql
-- Workflow B — report type registry/read model.
--
-- This is an operator/control-plane table that documents the Stage 2 Python
-- registry currently used for report detection and cleaning. It is not used by
-- jobs.reports.stage2.job_stage2 at runtime; the Python registry remains the
-- source of truth for Stage 2 execution.
--
-- Idempotent: seed rows use ON CONFLICT DO UPDATE so re-applying this SQL is
-- safe and keeps the read model aligned with the current Python classes.

CREATE SCHEMA IF NOT EXISTS workflow_b_control;

COMMENT ON SCHEMA workflow_b_control IS
  'Workflow B backup/legacy control-plane read models and operator inspection tables.';

CREATE TABLE IF NOT EXISTS workflow_b_control.report_type_registry (
  report_type TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  cleaner_module TEXT NOT NULL,
  cleaner_function TEXT NOT NULL DEFAULT 'clean',
  detection_module TEXT,
  detection_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
  required_columns JSONB NOT NULL DEFAULT '[]'::jsonb,
  optional_columns JSONB NOT NULL DEFAULT '[]'::jsonb,
  min_detection_score NUMERIC(5,2),
  priority INTEGER NOT NULL DEFAULT 100,
  implementation_status TEXT NOT NULL DEFAULT 'implemented',
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_report_type_not_empty;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_report_type_not_empty
  CHECK (btrim(report_type) <> '');

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_cleaner_module_not_empty;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_cleaner_module_not_empty
  CHECK (btrim(cleaner_module) <> '');

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_cleaner_function_not_empty;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_cleaner_function_not_empty
  CHECK (btrim(cleaner_function) <> '');

ALTER TABLE workflow_b_control.report_type_registry
  DROP CONSTRAINT IF EXISTS ck_report_type_registry_implementation_status;
ALTER TABLE workflow_b_control.report_type_registry
  ADD CONSTRAINT ck_report_type_registry_implementation_status
  CHECK (implementation_status IN ('implemented', 'partial', 'todo', 'deprecated'));

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

CREATE INDEX IF NOT EXISTS idx_report_type_registry_enabled
  ON workflow_b_control.report_type_registry (enabled);

CREATE INDEX IF NOT EXISTS idx_report_type_registry_implementation_status
  ON workflow_b_control.report_type_registry (implementation_status);

CREATE INDEX IF NOT EXISTS idx_report_type_registry_cleaner_module
  ON workflow_b_control.report_type_registry (cleaner_module);

CREATE OR REPLACE FUNCTION workflow_b_control.set_report_type_registry_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_report_type_registry_updated_at
  ON workflow_b_control.report_type_registry;

CREATE TRIGGER trg_report_type_registry_updated_at
  BEFORE UPDATE ON workflow_b_control.report_type_registry
  FOR EACH ROW
  EXECUTE FUNCTION workflow_b_control.set_report_type_registry_updated_at();

COMMENT ON TABLE workflow_b_control.report_type_registry IS
  'Read model of currently registered Workflow B Stage 2 report types. Stage 2 runtime still uses jobs.reports.stage2.registry.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.report_type IS
  'Machine-readable Stage 2 report type identifier, matching jobs.reports.stage2.* TYPE values.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.enabled IS
  'Whether the report type is currently considered supported/active for operator inspection.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.cleaner_module IS
  'Python module containing the Stage 2 report class/cleaner.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.cleaner_function IS
  'Class method or callable used by Stage 2 for cleaning when detection succeeds.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.detection_module IS
  'Python class method or callable that contributes detection score; the global detector still selects the highest score.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.detection_rules IS
  'Best-effort JSON summary of detection score terms and cleaner header/table checks derived from Python code.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.required_columns IS
  'Stage 2 validation REQUIRED_COLUMNS from the Python report class.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.optional_columns IS
  'Stage 2 validation OPTIONAL_COLUMNS from the Python report class.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.min_detection_score IS
  'Current global Stage 2 DETECT_THRESHOLD when no report-specific threshold exists.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.priority IS
  'Operator ordering placeholder for future conflict resolution; current Stage 2 runtime ignores this column.';
COMMENT ON COLUMN workflow_b_control.report_type_registry.implementation_status IS
  'implemented, partial, todo, or deprecated; EcoDriving rows are todo because clean() raises NotImplementedError.';

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
  notes
)
VALUES
  (
    'd104_1',
    '104 ogólny raport podróży - podsumowanie',
    true,
    'jobs.reports.stage2.types.d104_1',
    'D1041Report.clean',
    'jobs.reports.stage2.types.d104_1.D1041Report.detect',
    '{"class":"D1041Report","multi_table":false,"scan_rows":40,"score_terms":[{"weight":0.45,"check":"text contains 104 ogólny raport podróży - podsumowanie and not 104.1"},{"weight":0.30,"check":"text contains kierowca, nr, and rodzaj"},{"weight":0.25,"check":"text contains moce przyśpieszenia"}],"cleaning":{"header_must_have":["Kierowca","Rodzaj","Dystans"],"header_scan_rows":80,"data_start_offset":2,"alarm_row_before_header_must_have":["Przekraczanie prędkości","Ostre Hamowania"],"max_dynamic_alarm_labels":5,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Czas jazdy","Czas postoju","Czas-Koniec","Czas-Start","Dystans","Kierowca","Nr rejestracyjny","Rodzaj podróży"]'::jsonb,
    '["Geostrefa koniec","Geostrefa start","Lokalizacja koniec","Lokalizacja start","Moce przyśpieszenia","Nadmierny postój","Ostre Hamowania","Ostre Skręty","Przekraczanie prędkości"]'::jsonb,
    0.70,
    10,
    'implemented',
    'Cleaner dynamically appends up to five alarm labels found before the header; required/optional columns are the Stage 2 validation contract.'
  ),
  (
    'n104_1',
    '104.1 ogólny raport podróży - podsumowanie',
    true,
    'jobs.reports.stage2.types.n104_1',
    'N1041Report.clean',
    'jobs.reports.stage2.types.n104_1.N1041Report.detect',
    '{"class":"N1041Report","multi_table":false,"scan_rows":40,"score_terms":[{"weight":0.50,"check":"text contains 104.1 ogólny raport podróży - podsumowanie"},{"weight":0.30,"check":"text contains opis pojazdu and rodzaj"},{"weight":0.20,"check":"text contains silne przyśpieszenia"}],"cleaning":{"header_must_have":["Opis pojazdu","Rodzaj","Dystans"],"header_scan_rows":80,"data_start_offset":2,"alarm_row_before_header_must_have":["Przekraczanie prędkości","Ostre Hamowania"],"max_dynamic_alarm_labels":5,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Czas jazdy","Czas postoju","Czas-Koniec","Czas-Start","Dystans","Nr rejestracyjny","Opis pojazdu","Rodzaj podróży"]'::jsonb,
    '["Geostrefa koniec","Geostrefa start","Lokalizacja koniec","Lokalizacja start","Nadmierny postój","Ostre Hamowania","Ostre Skręty","Przekraczanie prędkości","Silne Przyśpieszenia"]'::jsonb,
    0.70,
    20,
    'implemented',
    'Cleaner dynamically appends up to five alarm labels found before the header; required/optional columns are the Stage 2 validation contract.'
  ),
  (
    'report_112',
    '112 raport stanu licznika',
    true,
    'jobs.reports.stage2.types.report_112',
    'Report112.clean',
    'jobs.reports.stage2.types.report_112.Report112.detect',
    '{"class":"Report112","multi_table":false,"scan_rows":25,"score_terms":[{"weight":0.80,"check":"text contains 112 raport stanu licznika"},{"weight":0.40,"check":"text contains licznik początek and licznik koniec"}],"cleaning":{"header_must_have":["Nr rejestracyjny","Licznik początek","Licznik koniec"],"header_scan_rows":80,"data_start_offset":1,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Dystans","Licznik koniec","Licznik początek","Nr rejestracyjny"]'::jsonb,
    '["Opis"]'::jsonb,
    0.70,
    30,
    'implemented',
    'Detection score can reach 1.0 because score terms are summed and capped by the report class.'
  ),
  (
    'report_602',
    '602 raport zużycia paliwa',
    true,
    'jobs.reports.stage2.types.report_602',
    'Report602.clean',
    'jobs.reports.stage2.types.report_602.Report602.detect',
    '{"class":"Report602","multi_table":false,"scan_rows":30,"score_terms":[{"weight":0.70,"check":"text contains 602 raport zużycia paliwa"},{"weight":0.30,"check":"text contains paliwo zużyte"}],"cleaning":{"header_must_have":["Nr rejestracyjny","Paliwo zużyte"],"header_scan_rows":80,"data_start_offset":1,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Nr rejestracyjny","Paliwo zużyte"]'::jsonb,
    '["Grupa","Kierowca","Opis pojazdu"]'::jsonb,
    0.70,
    40,
    'implemented',
    NULL
  ),
  (
    'report_602_ev',
    '602 raport zużycia energii',
    true,
    'jobs.reports.stage2.types.report_602_ev',
    'Report602EV.clean',
    'jobs.reports.stage2.types.report_602_ev.Report602EV.detect',
    '{"class":"Report602EV","multi_table":false,"scan_rows":35,"score_terms":[{"weight":0.70,"check":"text contains 602 raport zużycia energii"},{"weight":0.30,"check":"text contains zużyta energia"}],"cleaning":{"header_must_have":["Nr rejestracyjny","Zużyta energia","Dystans"],"header_scan_rows":80,"data_start_offset":2,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Dystans","Nr rejestracyjny","Zużyta energia"]'::jsonb,
    '["Marka i model","Pojemność baterii","Średnie zuzycie energii"]'::jsonb,
    0.70,
    50,
    'implemented',
    NULL
  ),
  (
    'd104_7',
    'D104.7',
    true,
    'jobs.reports.stage2.types.d104_7',
    'D1047Report.clean',
    'jobs.reports.stage2.types.d104_7.D1047Report.detect',
    '{"class":"D1047Report","multi_table":false,"scan_rows":30,"score_terms":[{"weight":0.70,"check":"text contains d104.7"},{"weight":0.30,"check":"text contains data godzina startu"}],"cleaning":{"header_must_have":["Numer rejestracyjny","Data godzina startu","Data godzina zakończenia"],"header_scan_rows":80,"data_start_offset":1,"stop_when_first_cell_starts_with":"razem"}}'::jsonb,
    '["Data godzina startu","Data godzina zakończenia","Dystans","Numer rejestracyjny"]'::jsonb,
    '["Czas jazdy","Kierowca","Lokalizacja koniec","Lokalizacja start","Opis pojazdu"]'::jsonb,
    0.70,
    60,
    'implemented',
    'Display name is limited to the literal report marker discoverable in the detector.'
  ),
  (
    'd105_2',
    'D105.2',
    true,
    'jobs.reports.stage2.types.d105_2',
    'D1052Report.clean',
    'jobs.reports.stage2.types.d105_2.D1052Report.detect',
    '{"class":"D1052Report","multi_table":false,"scan_rows":1,"score_terms":[{"weight":0.60,"check":"first row contains nr rejestracyjny and kierowca id"},{"weight":0.40,"check":"first row contains data rozpoczęcia and data zakończenia"}],"cleaning":{"header_source":"first_row","data_start_offset":1}}'::jsonb,
    '["Data rozpoczęcia","Data zakończenia","Nr Rejestracyjny"]'::jsonb,
    '["Dysponent ID","Dysponent imię i nazwisko","Flota","Kierowca ID","Kierowca imię i nazwisko","Marka","Model"]'::jsonb,
    0.70,
    70,
    'implemented',
    'Cleaner uses the first CSV row as output columns; required/optional columns are the Stage 2 validation contract.'
  ),
  (
    'eco_driving_driver',
    'Raport EcoDriving - kierowcy',
    true,
    'jobs.reports.stage2.types.eco_driving_driver',
    'EcoDrivingDriver.clean',
    'jobs.reports.stage2.types.eco_driving_driver.EcoDrivingDriver.detect',
    '{"class":"EcoDrivingDriver","multi_table":true,"scan_rows_per_table":20,"score_terms":[{"weight":0.50,"check":"flattened table text contains raport ecodriving"},{"weight":0.20,"check":"flattened table text contains kierowc"},{"weight":0.30,"check":"at least two tables are present"}],"cleaning":{"status":"not_implemented","raises":"NotImplementedError"}}'::jsonb,
    '["Kierowca","Ocena"]'::jsonb,
    '[]'::jsonb,
    0.70,
    80,
    'todo',
    'Detection exists, but clean() raises NotImplementedError; Stage 2 marks matching files PENDING_REVIEW with cleaning_not_implemented.'
  ),
  (
    'eco_driving_vehicle',
    'Raport EcoDriving - pojazdy',
    true,
    'jobs.reports.stage2.types.eco_driving_vehicle',
    'EcoDrivingVehicle.clean',
    'jobs.reports.stage2.types.eco_driving_vehicle.EcoDrivingVehicle.detect',
    '{"class":"EcoDrivingVehicle","multi_table":true,"scan_rows_per_table":20,"score_terms":[{"weight":0.50,"check":"flattened table text contains raport ecodriving"},{"weight":0.20,"check":"flattened table text contains nr rejestr or pojazd"},{"weight":0.30,"check":"at least two tables are present"}],"cleaning":{"status":"not_implemented","raises":"NotImplementedError"}}'::jsonb,
    '["Nr rejestracyjny","Ocena"]'::jsonb,
    '[]'::jsonb,
    0.70,
    90,
    'todo',
    'Detection exists, but clean() raises NotImplementedError; Stage 2 marks matching files PENDING_REVIEW with cleaning_not_implemented.'
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
      notes = EXCLUDED.notes;
