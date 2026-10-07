-- 011_extend_client_trips.sql
-- Adds location (start/end) and fuel consumption columns to client_trips.

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS start_location TEXT,
  ADD COLUMN IF NOT EXISTS start_latitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS start_longitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS end_location TEXT,
  ADD COLUMN IF NOT EXISTS end_latitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS end_longitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS fuel_consumed_liters DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS avg_fuel_l_per_100km DOUBLE PRECISION;
