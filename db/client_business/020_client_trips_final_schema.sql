-- 020_client_trips_final_schema.sql
-- Workflow A — final ordered client_trips schema.
--
-- Existing clients:
--   Rebuilds public.client_trips into the exact final column order, copies
--   existing data, validates row counts, preserves the primary key, recreates
--   lookup indexes, preserves a record_id unique index when one exists, then
--   swaps the rebuilt table in place. The old table is kept as
--   public.client_trips_legacy_backup_020 for rollback.
--
-- Future clients:
--   Can use this file as the base DDL for client_trips directly; it also
--   creates client_speeding_notifications so onboarding no longer has to
--   create the old client_trips shape first.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.client_trips (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  provider_trip_id INTEGER NOT NULL,
  vehicle_id INTEGER NULL,
  registration TEXT NOT NULL,
  vehicle_name TEXT NULL,
  vehicle_description TEXT NULL,
  chassis_number TEXT NULL,
  driver_name TEXT NULL,
  driver_surname TEXT NULL,
  driver_tag_description TEXT NULL,
  identification_tag_id TEXT NULL,
  start_timestamp TIMESTAMPTZ NULL,
  start_location TEXT NULL,
  start_latitude DOUBLE PRECISION NULL,
  start_longitude DOUBLE PRECISION NULL,
  start_geofence_name TEXT NULL,
  start_odometer_value BIGINT NULL,
  end_timestamp TIMESTAMPTZ NULL,
  end_location TEXT NULL,
  end_latitude DOUBLE PRECISION NULL,
  end_longitude DOUBLE PRECISION NULL,
  end_geofence_name TEXT NULL,
  end_odometer_value BIGINT NULL,
  trip_duration_seconds INTEGER NULL,
  trip_distance_meters INTEGER NULL,
  high_rpm_events_count INTEGER NULL,
  overrev_events_count INTEGER NULL,
  harsh_braking_events INTEGER NULL,
  harsh_acceleration_events INTEGER NULL,
  harsh_turning_events INTEGER NULL,
  idle_events INTEGER NULL,
  idle_time_seconds INTEGER NULL,
  speeding_140_160_count INTEGER NOT NULL DEFAULT 0,
  speeding_160_170_count INTEGER NOT NULL DEFAULT 0,
  speeding_170_plus_count INTEGER NOT NULL DEFAULT 0,
  record_id UUID NULL,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sync_run_id UUID NULL,
  PRIMARY KEY (client_id, provider_trip_id)
);

CREATE INDEX IF NOT EXISTS idx_client_trips_registration_window
  ON public.client_trips (registration, start_timestamp, end_timestamp);

-- Keep the historical notification table available for compatibility and
-- diagnostics. The current sync job no longer writes new notification rows.
CREATE TABLE IF NOT EXISTS public.client_speeding_notifications (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  provider_notification_id UUID NOT NULL,
  registration TEXT NOT NULL,
  vehicle_id INTEGER NULL,
  event_ts TIMESTAMPTZ NOT NULL,
  speed_raw INTEGER NULL,
  trigger_description TEXT NULL,
  geofence_id UUID NULL,
  notification_msg TEXT NULL,
  assigned_trip_id INTEGER NULL,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sync_run_id UUID NULL,
  record_id UUID NULL,
  PRIMARY KEY (client_id, provider_notification_id)
);

ALTER TABLE IF EXISTS public.client_speeding_notifications
  ADD COLUMN IF NOT EXISTS client_code TEXT,
  ADD COLUMN IF NOT EXISTS record_id UUID NULL;

CREATE INDEX IF NOT EXISTS idx_client_speeding_notifications_registration_event_ts
  ON public.client_speeding_notifications (registration, event_ts);

CREATE INDEX IF NOT EXISTS idx_client_speeding_notifications_trigger_description
  ON public.client_speeding_notifications (trigger_description);

DO $$
DECLARE
  expected_cols TEXT[] := ARRAY[
    'client_id',
    'client_code',
    'provider_trip_id',
    'vehicle_id',
    'registration',
    'vehicle_name',
    'vehicle_description',
    'chassis_number',
    'driver_name',
    'driver_surname',
    'driver_tag_description',
    'identification_tag_id',
    'start_timestamp',
    'start_location',
    'start_latitude',
    'start_longitude',
    'start_geofence_name',
    'start_odometer_value',
    'end_timestamp',
    'end_location',
    'end_latitude',
    'end_longitude',
    'end_geofence_name',
    'end_odometer_value',
    'trip_duration_seconds',
    'trip_distance_meters',
    'high_rpm_events_count',
    'overrev_events_count',
    'harsh_braking_events',
    'harsh_acceleration_events',
    'harsh_turning_events',
    'idle_events',
    'idle_time_seconds',
    'speeding_140_160_count',
    'speeding_160_170_count',
    'speeding_170_plus_count',
    'record_id',
    'synced_at',
    'sync_run_id'
  ];
  actual_cols TEXT[];
  record_id_is_not_null BOOLEAN := FALSE;
  has_record_id_unique BOOLEAN := FALSE;
  old_count BIGINT := 0;
  new_count BIGINT := 0;
  grant_rec RECORD;
BEGIN
  SELECT ARRAY_AGG(column_name::TEXT ORDER BY ordinal_position)
    INTO actual_cols
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'client_trips';

  IF actual_cols = expected_cols THEN
    RETURN;
  END IF;

  IF to_regclass('public.client_trips_legacy_backup_020') IS NOT NULL THEN
    RAISE EXCEPTION
      'public.client_trips is not in final order and public.client_trips_legacy_backup_020 already exists; inspect before rerunning 020_client_trips_final_schema.sql';
  END IF;

  SELECT is_nullable = 'NO'
    INTO record_id_is_not_null
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'client_trips'
    AND column_name = 'record_id';

  record_id_is_not_null := COALESCE(record_id_is_not_null, FALSE);

  SELECT EXISTS (
    SELECT 1
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
    JOIN pg_attribute a ON a.attrelid = tbl.oid AND a.attnum = ANY(i.indkey)
    WHERE ns.nspname = 'public'
      AND tbl.relname = 'client_trips'
      AND i.indisunique
      AND NOT i.indisprimary
      AND a.attname = 'record_id'
      AND array_length(i.indkey, 1) = 1
  ) INTO has_record_id_unique;

  DROP TABLE IF EXISTS public.client_trips_rebuilt_020;

  EXECUTE format($create$
    CREATE TABLE public.client_trips_rebuilt_020 (
      client_id UUID NOT NULL,
      client_code TEXT NULL,
      provider_trip_id INTEGER NOT NULL,
      vehicle_id INTEGER NULL,
      registration TEXT NOT NULL,
      vehicle_name TEXT NULL,
      vehicle_description TEXT NULL,
      chassis_number TEXT NULL,
      driver_name TEXT NULL,
      driver_surname TEXT NULL,
      driver_tag_description TEXT NULL,
      identification_tag_id TEXT NULL,
      start_timestamp TIMESTAMPTZ NULL,
      start_location TEXT NULL,
      start_latitude DOUBLE PRECISION NULL,
      start_longitude DOUBLE PRECISION NULL,
      start_geofence_name TEXT NULL,
      start_odometer_value BIGINT NULL,
      end_timestamp TIMESTAMPTZ NULL,
      end_location TEXT NULL,
      end_latitude DOUBLE PRECISION NULL,
      end_longitude DOUBLE PRECISION NULL,
      end_geofence_name TEXT NULL,
      end_odometer_value BIGINT NULL,
      trip_duration_seconds INTEGER NULL,
      trip_distance_meters INTEGER NULL,
      high_rpm_events_count INTEGER NULL,
      overrev_events_count INTEGER NULL,
      harsh_braking_events INTEGER NULL,
      harsh_acceleration_events INTEGER NULL,
      harsh_turning_events INTEGER NULL,
      idle_events INTEGER NULL,
      idle_time_seconds INTEGER NULL,
      speeding_140_160_count INTEGER NOT NULL DEFAULT 0,
      speeding_160_170_count INTEGER NOT NULL DEFAULT 0,
      speeding_170_plus_count INTEGER NOT NULL DEFAULT 0,
      record_id UUID %s,
      synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      sync_run_id UUID NULL,
      PRIMARY KEY (client_id, provider_trip_id)
    )
  $create$, CASE WHEN record_id_is_not_null THEN 'NOT NULL' ELSE 'NULL' END);

  INSERT INTO public.client_trips_rebuilt_020 (
    client_id,
    client_code,
    provider_trip_id,
    vehicle_id,
    registration,
    vehicle_name,
    vehicle_description,
    chassis_number,
    driver_name,
    driver_surname,
    driver_tag_description,
    identification_tag_id,
    start_timestamp,
    start_location,
    start_latitude,
    start_longitude,
    start_geofence_name,
    start_odometer_value,
    end_timestamp,
    end_location,
    end_latitude,
    end_longitude,
    end_geofence_name,
    end_odometer_value,
    trip_duration_seconds,
    trip_distance_meters,
    high_rpm_events_count,
    overrev_events_count,
    harsh_braking_events,
    harsh_acceleration_events,
    harsh_turning_events,
    idle_events,
    idle_time_seconds,
    speeding_140_160_count,
    speeding_160_170_count,
    speeding_170_plus_count,
    record_id,
    synced_at,
    sync_run_id
  )
  SELECT
    (to_jsonb(t)->>'client_id')::uuid,
    to_jsonb(t)->>'client_code',
    (to_jsonb(t)->>'provider_trip_id')::integer,
    (to_jsonb(t)->>'vehicle_id')::integer,
    to_jsonb(t)->>'registration',
    to_jsonb(t)->>'vehicle_name',
    to_jsonb(t)->>'vehicle_description',
    to_jsonb(t)->>'chassis_number',
    to_jsonb(t)->>'driver_name',
    to_jsonb(t)->>'driver_surname',
    to_jsonb(t)->>'driver_tag_description',
    to_jsonb(t)->>'identification_tag_id',
    (to_jsonb(t)->>'start_timestamp')::timestamptz,
    to_jsonb(t)->>'start_location',
    (to_jsonb(t)->>'start_latitude')::double precision,
    (to_jsonb(t)->>'start_longitude')::double precision,
    to_jsonb(t)->>'start_geofence_name',
    (to_jsonb(t)->>'start_odometer_value')::bigint,
    (to_jsonb(t)->>'end_timestamp')::timestamptz,
    to_jsonb(t)->>'end_location',
    (to_jsonb(t)->>'end_latitude')::double precision,
    (to_jsonb(t)->>'end_longitude')::double precision,
    to_jsonb(t)->>'end_geofence_name',
    (to_jsonb(t)->>'end_odometer_value')::bigint,
    (to_jsonb(t)->>'trip_duration_seconds')::integer,
    (to_jsonb(t)->>'trip_distance_meters')::integer,
    (to_jsonb(t)->>'high_rpm_events_count')::integer,
    (to_jsonb(t)->>'overrev_events_count')::integer,
    (to_jsonb(t)->>'harsh_braking_events')::integer,
    (to_jsonb(t)->>'harsh_acceleration_events')::integer,
    (to_jsonb(t)->>'harsh_turning_events')::integer,
    (to_jsonb(t)->>'idle_events')::integer,
    (to_jsonb(t)->>'idle_time_seconds')::integer,
    COALESCE(
      (to_jsonb(t)->>'speeding_140_160_count')::integer,
      COALESCE((to_jsonb(t)->>'speeding_bucket_140_150_events')::integer, 0)
        + COALESCE((to_jsonb(t)->>'speeding_bucket_150_160_events')::integer, 0)
    ),
    COALESCE(
      (to_jsonb(t)->>'speeding_160_170_count')::integer,
      COALESCE((to_jsonb(t)->>'speeding_bucket_160_170_events')::integer, 0)
    ),
    COALESCE(
      (to_jsonb(t)->>'speeding_170_plus_count')::integer,
      COALESCE((to_jsonb(t)->>'speeding_bucket_gt_170_events')::integer, 0)
    ),
    (to_jsonb(t)->>'record_id')::uuid,
    COALESCE((to_jsonb(t)->>'synced_at')::timestamptz, now()),
    (to_jsonb(t)->>'sync_run_id')::uuid
  FROM public.client_trips AS t;

  SELECT COUNT(*) INTO old_count FROM public.client_trips;
  SELECT COUNT(*) INTO new_count FROM public.client_trips_rebuilt_020;
  IF old_count <> new_count THEN
    RAISE EXCEPTION
      'client_trips rebuild row-count mismatch: old %, rebuilt %',
      old_count, new_count;
  END IF;

  CREATE INDEX idx_client_trips_rebuilt_020_registration_window
    ON public.client_trips_rebuilt_020 (registration, start_timestamp, end_timestamp);

  IF has_record_id_unique THEN
    CREATE UNIQUE INDEX uq_client_trips_rebuilt_020_record_id
      ON public.client_trips_rebuilt_020 (record_id);
  END IF;

  FOR grant_rec IN
    SELECT grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public'
      AND table_name = 'client_trips'
    GROUP BY grantee
  LOOP
    EXECUTE format(
      'GRANT %s ON TABLE public.client_trips_rebuilt_020 TO %I',
      grant_rec.privileges,
      grant_rec.grantee
    );
  END LOOP;

  ALTER TABLE public.client_trips RENAME TO client_trips_legacy_backup_020;
  ALTER INDEX IF EXISTS public.idx_client_trips_registration_window
    RENAME TO idx_client_trips_legacy_backup_020_registration_window;
  ALTER INDEX IF EXISTS public.uq_client_trips_record_id
    RENAME TO uq_client_trips_legacy_backup_020_record_id;
  ALTER TABLE public.client_trips_rebuilt_020 RENAME TO client_trips;
  ALTER INDEX public.idx_client_trips_rebuilt_020_registration_window
    RENAME TO idx_client_trips_registration_window;
  IF has_record_id_unique THEN
    ALTER INDEX public.uq_client_trips_rebuilt_020_record_id
      RENAME TO uq_client_trips_record_id;
  END IF;
END $$;
