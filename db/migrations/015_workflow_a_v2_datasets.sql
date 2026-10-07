-- 015_workflow_a_v2_datasets.sql
-- Workflow A V2 — Phase 1 dataset/table registry seed (additive,
-- idempotent).
--
-- This migration only seeds rows in the existing
-- `workflow_a_control.dataset_registry` and
-- `workflow_a_control.table_registry` tables (created by 011_*). It
-- does NOT alter the registry schema, does NOT create
-- `client_dataset_schedule` rows, and does NOT change any V1 row.
--
-- The Python module `jobs.api.telematics.registry` is the canonical
-- catalog and is updated alongside this file; the registry-sync
-- manual test (`ops/tests_manual/test_workflow_a_registry_sync.py`)
-- merges 011_* + 015_* before comparing to Python.
--
-- IMPORTANT — Phase 1 framing:
--   * The job_module strings below (`jobs.api.telematics.ingest_trips`,
--     `jobs.api.telematics.ingest_notifications`,
--     `jobs.api.telematics.ingest_fuel`, `jobs.api.telematics.enrich_trips`)
--     are **declared identifiers**. The corresponding Python modules
--     are NOT in the repo yet (Phase 2/3). The dispatcher cross-checks
--     `(dataset_name, job_module)` against `registry.DATASETS` before
--     launching, so an operator who flips an enabled=true row for one
--     of these datasets *prematurely* would only see a dispatcher-side
--     ERROR + skip, never an actual job invocation.
--   * The V1 dataset `trips_sync` (-> sync_trips_and_speeding) keeps
--     producing `client_trips`. Nothing here disables, deprecates, or
--     reroutes it.

-- ---- dataset_registry ----

INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module, description)
VALUES
  ('trips_ingest',
   'jobs.api.telematics.ingest_trips',
   'V2 Phase 1 (declared, job not yet implemented): raw /trips ingest into source_trips. Producer of staging only; never writes client_trips.'),
  ('notifications_ingest',
   'jobs.api.telematics.ingest_notifications',
   'V2 Phase 1 (declared, job not yet implemented): raw /alerts/notifications ingest into source_notifications.'),
  ('fuel_ingest',
   'jobs.api.telematics.ingest_fuel',
   'V2 Phase 1 (declared, job not yet implemented): per-(registration, window) /fuel/consumed ingest into source_fuel_observations.'),
  ('trips_enrichment',
   'jobs.api.telematics.enrich_trips',
   'V2 Phase 1 (declared, job not yet implemented): pure-DB enrichment of client_trips from source_trips + source_notifications + source_fuel_observations.')
ON CONFLICT (dataset_name) DO UPDATE
  SET job_module = EXCLUDED.job_module,
      description = EXCLUDED.description,
      updated_at = now();


-- ---- table_registry ----

INSERT INTO workflow_a_control.table_registry
  (table_name, dataset_name, schema_name, retention_key_column, description)
VALUES
  ('source_trips',
   'trips_ingest', 'public', 'start_timestamp',
   'V2 staging: raw /trips payloads with typed columns + JSONB raw_payload.'),
  ('source_notifications',
   'notifications_ingest', 'public', 'event_ts',
   'V2 staging: raw /alerts/notifications with canonical type + JSONB raw_payload.'),
  ('source_fuel_observations',
   'fuel_ingest', 'public', 'window_end_ts',
   'V2 staging: per-(registration_norm, window) /fuel/consumed result, including negative responses.')
ON CONFLICT (table_name) DO UPDATE
  SET dataset_name = EXCLUDED.dataset_name,
      schema_name = EXCLUDED.schema_name,
      retention_key_column = EXCLUDED.retention_key_column,
      description = EXCLUDED.description,
      updated_at = now();
