-- 016_workflow_a_disable_declared_v2_registry.sql
-- Workflow A — keep active registry rows limited to implemented jobs.
--
-- Migration 015 declared V2 dataset/table rows before the corresponding job
-- modules existed. That is unsafe now that the dispatcher and retention worker
-- use the Python registry as their runtime allowlist: registering missing
-- modules in Python would make the dispatcher treat them as runnable.
--
-- This migration removes the declared-only V2 rows from the active platform
-- registry. The client-business V2 staging DDL remains in
-- db/client_business/017_v2_staging_tables.sql, but no schedule or retention
-- policy should point at it until real V2 jobs are implemented.
--
-- It also re-seeds the implemented V1 descriptions so SQL and
-- jobs/api/telematics/registry.py stay byte-for-byte aligned.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

-- Drop any accidental per-client V2 policies/schedules first so the registry
-- deletes below are not blocked by foreign keys. These rows cannot have been
-- produced by repo jobs because the V2 job modules do not exist.
DELETE FROM workflow_a_control.client_table_retention
 WHERE table_name IN (
   'source_trips',
   'source_notifications',
   'source_fuel_observations'
 );

DELETE FROM workflow_a_control.client_schedule_run_history
 WHERE schedule_id IN (
   SELECT schedule_id
     FROM workflow_a_control.client_dataset_schedule
    WHERE dataset_name IN (
      'trips_ingest',
      'notifications_ingest',
      'fuel_ingest',
      'trips_enrichment'
    )
 );

DELETE FROM workflow_a_control.client_dataset_schedule
 WHERE dataset_name IN (
   'trips_ingest',
   'notifications_ingest',
   'fuel_ingest',
   'trips_enrichment'
 );

DELETE FROM workflow_a_control.table_registry
 WHERE table_name IN (
   'source_trips',
   'source_notifications',
   'source_fuel_observations'
 );

DELETE FROM workflow_a_control.dataset_registry
 WHERE dataset_name IN (
   'trips_ingest',
   'notifications_ingest',
   'fuel_ingest',
   'trips_enrichment'
 );

INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module, description)
VALUES
  ('trips_sync',
   'jobs.api.telematics.sync_trips_and_speeding',
   'Telematics provider sync: trips + fleet-wide raw vehicle-event speeding and HIGH_RPM/OVERREV counts, plus odometer and location enrichment.'),
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
   'Legacy provider notification events retained for historical data.'),
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
