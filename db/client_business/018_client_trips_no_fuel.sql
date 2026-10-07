-- 018_client_trips_no_fuel.sql
-- Adds location (start/end) columns to client_trips for new onboarding.
-- Deprecated trip-level fuel columns are intentionally not created here.

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS start_location TEXT,
  ADD COLUMN IF NOT EXISTS start_latitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS start_longitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS end_location TEXT,
  ADD COLUMN IF NOT EXISTS end_latitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS end_longitude DOUBLE PRECISION;
