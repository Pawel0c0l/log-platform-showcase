-- 011_workflow_a_dataset_registry.sql
-- Workflow A — central dataset and table registry.
--
-- Two purposes:
--   1) Logical reference for the dispatcher (which job runs which dataset).
--   2) Allowlist of (schema, table, retention_key_column) used by the retention
--      worker. The Python module `jobs.api.telematics.registry` is the source of
--      truth; this DDL seeds the same set of rows so the database can be
--      queried directly (e.g. by retention SQL or by ops dashboards).
--
-- Both tables are platform-global (not per-client). Per-client decisions live
-- in `client_dataset_schedule` (012) and `client_table_retention` (013).
--
-- Idempotent: the seed uses ON CONFLICT DO UPDATE so re-applying the migration
-- via ops/db_migrate.sh is safe if the rows already exist.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

-- ---- dataset_registry ------------------------------------------------------
-- Logical datasets (e.g. trips_sync). Scheduling and overwrite semantics live
-- one level down, in client_dataset_schedule, since they are per-client.

CREATE TABLE IF NOT EXISTS workflow_a_control.dataset_registry (
  dataset_name TEXT PRIMARY KEY,
  job_module TEXT NOT NULL,
  description TEXT NOT NULL,
  -- updated_at lets us detect drift between the Python registry and DB seed.
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cheap basic shape check — full identifier is enforced in Python before use.
ALTER TABLE workflow_a_control.dataset_registry
  DROP CONSTRAINT IF EXISTS ck_dataset_registry_name_shape;
ALTER TABLE workflow_a_control.dataset_registry
  ADD CONSTRAINT ck_dataset_registry_name_shape
  CHECK (dataset_name ~ '^[a-z][a-z0-9_]*$');

ALTER TABLE workflow_a_control.dataset_registry
  DROP CONSTRAINT IF EXISTS ck_dataset_registry_job_module_shape;
ALTER TABLE workflow_a_control.dataset_registry
  ADD CONSTRAINT ck_dataset_registry_job_module_shape
  CHECK (job_module ~ '^[a-zA-Z_][a-zA-Z0-9_.]*$');


-- ---- table_registry -------------------------------------------------------
-- Maps a dataset to one or more tables in client business DBs and pins the
-- column the retention worker is allowed to compare cutoff_ts against.

CREATE TABLE IF NOT EXISTS workflow_a_control.table_registry (
  table_name TEXT PRIMARY KEY,
  dataset_name TEXT NOT NULL REFERENCES workflow_a_control.dataset_registry (dataset_name)
                              ON UPDATE CASCADE ON DELETE RESTRICT,
  schema_name TEXT NOT NULL DEFAULT 'public',
  retention_key_column TEXT NOT NULL,
  description TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE workflow_a_control.table_registry
  DROP CONSTRAINT IF EXISTS ck_table_registry_table_name_shape;
ALTER TABLE workflow_a_control.table_registry
  ADD CONSTRAINT ck_table_registry_table_name_shape
  CHECK (table_name ~ '^[a-z][a-z0-9_]*$');

ALTER TABLE workflow_a_control.table_registry
  DROP CONSTRAINT IF EXISTS ck_table_registry_schema_name_shape;
ALTER TABLE workflow_a_control.table_registry
  ADD CONSTRAINT ck_table_registry_schema_name_shape
  CHECK (schema_name ~ '^[a-z_][a-z0-9_]*$');

ALTER TABLE workflow_a_control.table_registry
  DROP CONSTRAINT IF EXISTS ck_table_registry_retention_key_column_shape;
ALTER TABLE workflow_a_control.table_registry
  ADD CONSTRAINT ck_table_registry_retention_key_column_shape
  CHECK (retention_key_column ~ '^[a-z_][a-z0-9_]*$');

CREATE INDEX IF NOT EXISTS idx_table_registry_dataset
  ON workflow_a_control.table_registry (dataset_name);


-- ---- Seed (kept in sync with jobs/api/telematics/registry.py) ---------------

INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module, description)
VALUES
  ('trips_sync',
   'jobs.api.telematics.sync_trips_and_speeding',
   'Telematics provider sync: trips + speeding notifications; computes speeding buckets, HIGH_RPM/OVERREV, odometer, and location enrichment.'),
  ('fuel_daily_aggregation',
   'jobs.api.telematics.aggregate_trip_fuel_daily',
   'Daily aggregation of client_trips into per-vehicle and per-vehicle+driver daily fuel rollups.')
ON CONFLICT (dataset_name) DO UPDATE
  SET job_module = EXCLUDED.job_module,
      description = EXCLUDED.description,
      updated_at = now();


INSERT INTO workflow_a_control.table_registry
  (table_name, dataset_name, schema_name, retention_key_column, description)
VALUES
  ('client_trips',
   'trips_sync', 'public', 'start_timestamp',
   'Provider trip facts + per-trip speeding bucket counts.'),
  ('client_speeding_notifications',
   'trips_sync', 'public', 'event_ts',
   'Provider notification events feeding speeding-bucket computation.'),
  ('client_vehicle_daily_fuel',
   'fuel_daily_aggregation', 'public', 'day',
   'Per-vehicle, per-day fuel and distance aggregates.'),
  ('client_vehicle_driver_daily_fuel',
   'fuel_daily_aggregation', 'public', 'day',
   'Per-vehicle+driver, per-day fuel and distance aggregates.')
ON CONFLICT (table_name) DO UPDATE
  SET dataset_name = EXCLUDED.dataset_name,
      schema_name = EXCLUDED.schema_name,
      retention_key_column = EXCLUDED.retention_key_column,
      description = EXCLUDED.description,
      updated_at = now();
