-- 014_add_record_id_and_synced_at.sql
-- Workflow A — additive client-business DDL.
--
-- Phase 1 of the safe record_id rollout:
--   * Add `record_id UUID` as NULLABLE on every Workflow A table.
--   * Add `synced_at TIMESTAMPTZ` and `sync_run_id UUID` on the daily fuel
--     aggregation tables (which until now only carry `updated_at`).
--
-- This file is INTENTIONALLY additive only:
--   - No NOT NULL constraints are added here. Existing rows are nullable
--     until `scripts/backfill_record_id.py` has populated them.
--   - No UNIQUE INDEX is added here. The unique index is added later via
--     CREATE UNIQUE INDEX CONCURRENTLY (also from `backfill_record_id.py`).
--   - The existing `updated_at` columns on daily fuel tables are KEPT for
--     backward compatibility with anything that already reads them.
--
-- Apply via: `python scripts/apply_client_business_migrations.py --apply`.
-- Postcondition for any existing client DB:
--   * client_trips, client_speeding_notifications, client_vehicle_daily_fuel,
--     client_vehicle_driver_daily_fuel — each has a nullable `record_id` UUID.
--   * client_vehicle_daily_fuel, client_vehicle_driver_daily_fuel — each
--     additionally has nullable `synced_at` and `sync_run_id`.

-- ---- record_id ------------------------------------------------------------
ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS record_id UUID NULL;

ALTER TABLE IF EXISTS public.client_speeding_notifications
  ADD COLUMN IF NOT EXISTS record_id UUID NULL;

ALTER TABLE IF EXISTS public.client_vehicle_daily_fuel
  ADD COLUMN IF NOT EXISTS record_id UUID NULL;

ALTER TABLE IF EXISTS public.client_vehicle_driver_daily_fuel
  ADD COLUMN IF NOT EXISTS record_id UUID NULL;


-- ---- synced_at + sync_run_id on daily fuel tables -------------------------
-- Keeps `updated_at` (NOT NULL DEFAULT now()) untouched on these tables.
-- New columns are nullable so existing rows can stay until the next sync.

ALTER TABLE IF EXISTS public.client_vehicle_daily_fuel
  ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sync_run_id UUID NULL;

ALTER TABLE IF EXISTS public.client_vehicle_driver_daily_fuel
  ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sync_run_id UUID NULL;
