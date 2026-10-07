-- 046_workflow_a_eco_person_registry.sql
-- Workflow A - register isolated Eco Driving Person datasets with dispatcher metadata.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;

INSERT INTO workflow_a_control.dataset_registry (dataset_name, job_module, description)
VALUES
  ('eco_person_driving_weekly_snapshot',
   'jobs.ecodriving_person.job_eco_driving_person_aggregate',
   'Eco Driving Person cumulative month-to-date weekly snapshot aggregation.'),
  ('eco_person_driving_month_end_weekly_snapshot',
   'jobs.ecodriving_person.job_eco_driving_person_aggregate',
   'Eco Driving Person final cumulative weekly snapshot for the previous month.'),
  ('eco_person_driving_monthly_aggregation',
   'jobs.ecodriving_person.job_eco_driving_person_aggregate',
   'Eco Driving Person independent full calendar-month aggregation.'),
  ('eco_person_driving_weekly_email_notifications',
   'jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications',
   'Eco Driving Person weekly real-person email notifications.'),
  ('eco_person_driving_monthly_email_notifications',
   'jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications',
   'Eco Driving Person monthly real-person email notifications.')
ON CONFLICT (dataset_name) DO UPDATE
  SET job_module = EXCLUDED.job_module,
      description = EXCLUDED.description,
      updated_at = now();

INSERT INTO workflow_a_control.table_registry
  (table_name, dataset_name, schema_name, retention_key_column, description)
VALUES
  ('eco_person_people',
   'eco_person_driving_weekly_snapshot', 'public', 'updated_at',
   'Eco Driving Person real-person identity rows.'),
  ('eco_person_driver_mappings',
   'eco_person_driving_weekly_snapshot', 'public', 'updated_at',
   'Eco Driving Person driver-name alias mappings.'),
  ('eco_person_trip_assignments',
   'eco_person_driving_weekly_snapshot', 'public', 'trip_start_ts',
   'Eco Driving Person source trip assignment audit rows.'),
  ('eco_person_weekly_stats',
   'eco_person_driving_weekly_snapshot', 'public', 'period_start_date',
   'Eco Driving Person cumulative month-to-date weekly snapshot stats.'),
  ('eco_person_monthly_stats',
   'eco_person_driving_monthly_aggregation', 'public', 'month_start_date',
   'Eco Driving Person independent full calendar-month stats.'),
  ('eco_person_weekly_email_send_log',
   'eco_person_driving_weekly_email_notifications', 'public', 'attempted_at',
   'Eco Driving Person weekly email send/audit log.'),
  ('eco_person_monthly_email_send_log',
   'eco_person_driving_monthly_email_notifications', 'public', 'attempted_at',
   'Eco Driving Person monthly email send/audit log.')
ON CONFLICT (table_name) DO UPDATE
  SET dataset_name = EXCLUDED.dataset_name,
      schema_name = EXCLUDED.schema_name,
      retention_key_column = EXCLUDED.retention_key_column,
      description = EXCLUDED.description,
      updated_at = now();

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
    ('eco_person_driving_weekly_snapshot', 'weekly', 0::smallint, NULL::smallint, '05:00'),
    ('eco_person_driving_month_end_weekly_snapshot', 'monthly', NULL::smallint, 1::smallint, '05:30'),
    ('eco_person_driving_monthly_aggregation', 'monthly', NULL::smallint, 1::smallint, '06:00'),
    ('eco_person_driving_weekly_email_notifications', 'weekly', 0::smallint, NULL::smallint, '07:00'),
    ('eco_person_driving_monthly_email_notifications', 'monthly', NULL::smallint, 1::smallint, '07:30')
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
    ('eco_person_people'),
    ('eco_person_driver_mappings'),
    ('eco_person_trip_assignments'),
    ('eco_person_weekly_stats'),
    ('eco_person_monthly_stats'),
    ('eco_person_weekly_email_send_log'),
    ('eco_person_monthly_email_send_log')
) AS t(table_name)
ON CONFLICT (client_id, table_name) DO NOTHING;
