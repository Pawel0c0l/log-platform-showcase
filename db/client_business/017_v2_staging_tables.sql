-- 017_v2_staging_tables.sql
-- Workflow A V2 — Phase 1 staging schema (additive, idempotent).
--
-- Adds three "source" tables that mirror the provider responses one row
-- per record. They are created **empty** and remain unused until the
-- ingest jobs (Phase 2) and the enrichment job (Phase 3) land. Phase 1
-- only puts the schema in place so Phase 2/3 can be reviewed and rolled
-- out independently.
--
-- Why three tables, why now:
--   * `source_trips` — one row per (client_id, provider_trip_id) carrying
--     the typed provider columns plus the full JSONB raw_payload for
--     audit / re-parsing.
--   * `source_notifications` — one row per (client_id,
--     provider_notification_id) — the dataset that today feeds RPM
--     counts and speeding buckets via `sync_trips_and_speeding`. In V2
--     it is the durable input to enrichment.
--   * `source_fuel_observations` — per-(registration_norm, window) record
--     of every `/fuel/consumed/{registration}` call we ever made,
--     including negative responses (NULL liters, EMPTY, HTTP_ERROR).
--     The `api_status` column tells enrichment "did we ask?".
--
-- Production flow today (V1) is unchanged:
--   * `jobs.api.telematics.sync_trips_and_speeding` is still the only
--     producer of `client_trips` / `client_speeding_notifications`.
--   * No Workflow A job in this repo writes the new tables yet.
--   * No `client_dataset_schedule` row references the V2 datasets.
--
-- Idempotence: every CREATE uses IF NOT EXISTS so this file is safe to
-- replay via `scripts/apply_client_business_migrations.py` and via
-- `scripts/onboard_workflow_a_client.py`.
--
-- Type choices:
--   * `provider_trip_id BIGINT` (vs INTEGER on `client_trips`) — staging
--     is intentionally wider so a future provider ID expansion does not
--     require a re-migration; narrowing back to INTEGER happens in
--     Python at enrichment time when writing to `client_trips`.
--   * `vehicle_id BIGINT` — same rationale.
--   * `driver_id UUID` and terminal fields are raw-source staging fields.
--     The final `client_trips` schema from 020 no longer carries them.
--   * `geofence_id TEXT` — defensively wider than the existing
--     `client_speeding_notifications.geofence_id UUID` so the staging
--     layer never rejects a malformed provider value.
--   * `start_odometer_value` / `end_odometer_value` — `BIGINT` per
--     `016_add_odometer_columns.sql`.
--   * `record_id UUID NOT NULL` — V2 tables start life with the column
--     populated and unique by every writer (no V1-style backfill
--     required).
--
-- No foreign keys to platform DB are added — these tables live in the
-- per-client business DB and the platform DB is a separate Postgres
-- instance. Cross-DB joins are made by the Python jobs.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---- source_trips ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.source_trips (
  record_id                  UUID NOT NULL,
  client_id                  UUID NOT NULL,
  client_code                TEXT NULL,

  provider_trip_id           BIGINT NOT NULL,
  registration               TEXT NULL,
  vehicle_id                 BIGINT NULL,
  driver_id                  UUID NULL,
  driver_name                TEXT NULL,
  driver_surname             TEXT NULL,
  chassis_number             TEXT NULL,
  terminal_id                INTEGER NULL,
  terminal_serial            TEXT NULL,

  start_timestamp            TIMESTAMPTZ NULL,
  end_timestamp              TIMESTAMPTZ NULL,
  trip_duration_seconds      INTEGER NULL,
  trip_distance_meters       INTEGER NULL,

  start_latitude             DOUBLE PRECISION NULL,
  start_longitude            DOUBLE PRECISION NULL,
  end_latitude               DOUBLE PRECISION NULL,
  end_longitude              DOUBLE PRECISION NULL,
  start_geofence_name        TEXT NULL,
  end_geofence_name          TEXT NULL,
  start_location             TEXT NULL,
  end_location               TEXT NULL,

  harsh_acceleration_events  INTEGER NULL,
  harsh_braking_events       INTEGER NULL,
  harsh_turning_events       INTEGER NULL,
  idle_events                INTEGER NULL,
  idle_time_seconds          INTEGER NULL,

  start_odometer_value       BIGINT NULL,
  end_odometer_value         BIGINT NULL,

  raw_payload                JSONB NOT NULL,
  fetched_at                 TIMESTAMPTZ NOT NULL,
  synced_at                  TIMESTAMPTZ NOT NULL,
  sync_run_id                UUID NULL,

  PRIMARY KEY (client_id, provider_trip_id)
);

-- record_id is unique across all staging trip rows. New writers populate
-- it on every INSERT, so we can declare it UNIQUE NOT NULL from day one
-- (no backfill phase needed, unlike client_trips).
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_trips_record_id
  ON public.source_trips (record_id);

-- Enrichment scan: trips for (client, window).
CREATE INDEX IF NOT EXISTS idx_source_trips_client_start_ts
  ON public.source_trips (client_id, start_timestamp);

-- Fuel-join scan: per-vehicle activity inside a window.
CREATE INDEX IF NOT EXISTS idx_source_trips_client_registration_start_ts
  ON public.source_trips (client_id, registration, start_timestamp);


-- ---- source_notifications -------------------------------------------------

CREATE TABLE IF NOT EXISTS public.source_notifications (
  record_id                  UUID NOT NULL,
  client_id                  UUID NOT NULL,
  client_code                TEXT NULL,

  provider_notification_id   UUID NOT NULL,

  -- `type` is the canonical upper-cased token used by enrichment;
  -- `type_raw` is the as-received string for unknown-type debugging.
  type                       TEXT NULL,
  type_raw                   TEXT NULL,

  registration               TEXT NULL,
  -- Stored, indexable result of _normalize_registration() so the RPM
  -- matching join is index-only.
  registration_norm          TEXT NULL,
  vehicle_id                 BIGINT NULL,
  event_ts                   TIMESTAMPTZ NULL,

  speed                      INTEGER NULL,
  trigger_description        TEXT NULL,
  -- TEXT (not UUID) so a malformed provider value does not reject the
  -- whole staging row; canonical projection happens at enrichment time.
  geofence_id                TEXT NULL,
  notification_msg           TEXT NULL,
  status                     TEXT NULL,

  raw_payload                JSONB NOT NULL,
  fetched_at                 TIMESTAMPTZ NOT NULL,
  synced_at                  TIMESTAMPTZ NOT NULL,
  sync_run_id                UUID NULL,

  PRIMARY KEY (client_id, provider_notification_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_notifications_record_id
  ON public.source_notifications (record_id);

-- RPM matching path 1: vehicle_id within window.
CREATE INDEX IF NOT EXISTS idx_source_notifications_client_vehicle_event_ts
  ON public.source_notifications (client_id, vehicle_id, event_ts);

-- RPM matching path 2: registration fallback within window.
CREATE INDEX IF NOT EXISTS idx_source_notifications_client_reg_norm_event_ts
  ON public.source_notifications (client_id, registration_norm, event_ts);

-- Hot path: enrichment scans only the three relevant types. Partial
-- index keeps the structure small even with long retention.
CREATE INDEX IF NOT EXISTS idx_source_notifications_client_event_ts_hot_types
  ON public.source_notifications (client_id, event_ts)
  WHERE type IN ('HIGH_RPM', 'OVERREV', 'SPEEDING');


-- ---- source_fuel_observations ---------------------------------------------

CREATE TABLE IF NOT EXISTS public.source_fuel_observations (
  record_id                  UUID NOT NULL,
  client_id                  UUID NOT NULL,
  client_code                TEXT NULL,

  registration_norm          TEXT NOT NULL,
  window_start_ts            TIMESTAMPTZ NOT NULL,
  window_end_ts              TIMESTAMPTZ NOT NULL,

  fuel_consumed_liters       DOUBLE PRECISION NULL,
  -- Free-form short code recording the call outcome. Phase 2 ingest_fuel
  -- defines the canonical values; suggested set:
  --   'OK'             — non-null liters returned
  --   'EMPTY'          — call succeeded, value is NULL/empty
  --   'SKIP_NO_WINDOW' — trip lacked window/registration; no call made
  --   'SAFETY_ABORT'   — provider safety budget tripped before call
  --   'HTTP_ERROR'     — non-2xx response (retryable)
  api_status                 TEXT NOT NULL,
  http_status                INTEGER NULL,
  raw_payload                JSONB NULL,

  fetched_at                 TIMESTAMPTZ NOT NULL,
  synced_at                  TIMESTAMPTZ NOT NULL,
  sync_run_id                UUID NULL,

  PRIMARY KEY (client_id, registration_norm, window_start_ts, window_end_ts)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_fuel_observations_record_id
  ON public.source_fuel_observations (record_id);

-- Per-vehicle-window lookup used by enrichment to attach fuel to a trip.
CREATE INDEX IF NOT EXISTS idx_source_fuel_observations_client_reg_norm_window_end
  ON public.source_fuel_observations (client_id, registration_norm, window_end_ts);
