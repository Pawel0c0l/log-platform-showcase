-- 009_workflow_a_client_business.sql
-- Workflow A v1 client business tables.
-- Intended to be applied to each client's separate business database.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---- client_trips ----
-- Provider trip facts + computed per-trip speeding bucket counts.
CREATE TABLE IF NOT EXISTS public.client_trips (
  client_id UUID NOT NULL,
  provider_trip_id INTEGER NOT NULL,

  -- Defensive business identity / joins
  registration TEXT NOT NULL,

  -- Driver / vehicle / terminal
  driver_id UUID NULL,
  driver_name TEXT NULL,
  driver_surname TEXT NULL,
  vehicle_id INTEGER NULL,
  chassis_number TEXT NULL,
  terminal_id INTEGER NULL,
  terminal_serial TEXT NULL,

  -- Trip window (from provider)
  start_timestamp TIMESTAMPTZ NULL,
  end_timestamp TIMESTAMPTZ NULL,
  trip_duration_seconds INTEGER NULL,
  trip_distance_meters INTEGER NULL,

  -- Geofence names (v1: derived from trips; reference sync deferred to v1.1)
  start_geofence_name TEXT NULL,
  end_geofence_name TEXT NULL,

  -- Harsh events / idle aggregates
  harsh_acceleration_events INTEGER NULL,
  harsh_braking_events INTEGER NULL,
  harsh_turning_events INTEGER NULL,
  idle_events INTEGER NULL,
  idle_time_seconds INTEGER NULL,

  -- Speeding buckets (computed; thresholds finalized after proof-of-data)
  speeding_bucket_140_150_events INTEGER NOT NULL DEFAULT 0,
  speeding_bucket_150_160_events INTEGER NOT NULL DEFAULT 0,
  speeding_bucket_160_170_events INTEGER NOT NULL DEFAULT 0,
  speeding_bucket_gt_170_events INTEGER NOT NULL DEFAULT 0,

  -- Bucket recomputation lineage (audit/re-runs/debugging)
  speeding_buckets_computed_at TIMESTAMPTZ NULL,
  speeding_buckets_source_window_start_ts TIMESTAMPTZ NULL,
  speeding_buckets_source_window_end_ts TIMESTAMPTZ NULL,

  -- Ingestion metadata
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sync_run_id UUID NULL,

  PRIMARY KEY (client_id, provider_trip_id)
);

CREATE INDEX IF NOT EXISTS idx_client_trips_registration_window
  ON public.client_trips (registration, start_timestamp, end_timestamp);

-- ---- client_speeding_notifications ----
-- Provider notification events needed to compute per-trip speeding buckets.
CREATE TABLE IF NOT EXISTS public.client_speeding_notifications (
  client_id UUID NOT NULL,
  provider_notification_id UUID NOT NULL,

  registration TEXT NOT NULL,
  vehicle_id INTEGER NULL,
  event_ts TIMESTAMPTZ NOT NULL,

  -- Raw provider speed (nullable because provider notifications.speed can be NULL)
  speed_raw INTEGER NULL,

  -- Trigger metadata used to select “speeding incidents” (TEXT tokens; see control-plane config semantics)
  trigger_description TEXT NULL,
  geofence_id UUID NULL,
  notification_msg TEXT NULL,

  -- Optional linkage (planned for later; v1 can compute buckets without persisting)
  assigned_trip_id INTEGER NULL,

  -- Ingestion metadata
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sync_run_id UUID NULL,

  PRIMARY KEY (client_id, provider_notification_id)
);

CREATE INDEX IF NOT EXISTS idx_client_speeding_notifications_registration_event_ts
  ON public.client_speeding_notifications (registration, event_ts);

CREATE INDEX IF NOT EXISTS idx_client_speeding_notifications_trigger_description
  ON public.client_speeding_notifications (trigger_description);

