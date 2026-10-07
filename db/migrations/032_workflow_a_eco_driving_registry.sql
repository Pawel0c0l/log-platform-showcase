-- 032_workflow_a_eco_driving_registry.sql
-- Workflow A — register Eco Driving aggregation with the DB-driven dispatcher.
--
-- The dispatcher remains the only scheduler mechanism. Three logical dataset
-- rows point at the same job module so each cadence can have its own
-- client_dataset_schedule row under the existing UNIQUE(client_id,dataset_name)
-- model.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module, description)
VALUES
  ('eco_driving_weekly_snapshot',
   'jobs.ecodriving.job_eco_driving_aggregate',
   'Eco Driving cumulative month-to-date weekly snapshot aggregation.'),
  ('eco_driving_month_end_weekly_snapshot',
   'jobs.ecodriving.job_eco_driving_aggregate',
   'Eco Driving final cumulative weekly snapshot for the previous month.'),
  ('eco_driving_monthly_aggregation',
   'jobs.ecodriving.job_eco_driving_aggregate',
   'Eco Driving independent full calendar-month aggregation.')
ON CONFLICT (dataset_name) DO UPDATE
  SET job_module = EXCLUDED.job_module,
      description = EXCLUDED.description,
      updated_at = now();

INSERT INTO workflow_a_control.table_registry
  (table_name, dataset_name, schema_name, retention_key_column, description)
VALUES
  ('eco_trip_assignments',
   'eco_driving_weekly_snapshot', 'public', 'trip_start_ts',
   'Eco Driving source trip assignment audit rows.'),
  ('eco_driver_weekly_stats',
   'eco_driving_weekly_snapshot', 'public', 'period_start_date',
   'Eco Driving cumulative month-to-date weekly snapshot stats.'),
  ('eco_driver_monthly_stats',
   'eco_driving_monthly_aggregation', 'public', 'month_start_date',
   'Eco Driving independent full calendar-month stats.')
ON CONFLICT (table_name) DO UPDATE
  SET dataset_name = EXCLUDED.dataset_name,
      schema_name = EXCLUDED.schema_name,
      retention_key_column = EXCLUDED.retention_key_column,
      description = EXCLUDED.description,
      updated_at = now();

-- Existing clients get disabled rows so operators can enable the schedules by
-- updating configuration instead of hand-inserting dataset names. New clients
-- receive equivalent rows from scripts/onboard_workflow_a_client.py.
INSERT INTO workflow_a_control.client_dataset_schedule (
  client_id, client_code, dataset_name, enabled,
  frequency, day_of_week, day_of_month, run_time, timezone,
  lookback_days, overwrite_existing
)
SELECT
  ca.client_id,
  ca.client_code,
  ds.dataset_name,
  false AS enabled,
  ds.frequency,
  ds.day_of_week,
  ds.day_of_month,
  ds.run_time::time,
  'Europe/Warsaw' AS timezone,
  0 AS lookback_days,
  true AS overwrite_existing
FROM workflow_a_control.client_account ca
CROSS JOIN (
  VALUES
    ('eco_driving_weekly_snapshot', 'weekly', 0::smallint, NULL::smallint, '03:00'),
    ('eco_driving_month_end_weekly_snapshot', 'monthly', NULL::smallint, 1::smallint, '03:30'),
    ('eco_driving_monthly_aggregation', 'monthly', NULL::smallint, 1::smallint, '04:00')
) AS ds(dataset_name, frequency, day_of_week, day_of_month, run_time)
ON CONFLICT (client_id, dataset_name) DO NOTHING;

INSERT INTO workflow_a_control.client_table_retention (
  client_id, client_code, table_name, enabled, retention_days
)
SELECT
  ca.client_id,
  ca.client_code,
  t.table_name,
  false AS enabled,
  365 AS retention_days
FROM workflow_a_control.client_account ca
CROSS JOIN (
  VALUES
    ('eco_trip_assignments'),
    ('eco_driver_weekly_stats'),
    ('eco_driver_monthly_stats')
) AS t(table_name)
ON CONFLICT (client_id, table_name) DO NOTHING;
