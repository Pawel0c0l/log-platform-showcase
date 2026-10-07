-- 013_client_vehicle_driver_daily_fuel.sql
-- Daily fuel aggregation per vehicle + driver.

CREATE TABLE IF NOT EXISTS public.client_vehicle_driver_daily_fuel (
  client_id UUID NOT NULL,
  client_code TEXT,
  day DATE NOT NULL,
  vehicle_id TEXT NOT NULL,
  registration TEXT,
  driver_id TEXT NOT NULL,
  driver_name TEXT,
  driver_surname TEXT,

  distance_m BIGINT,
  distance_km DOUBLE PRECISION,
  fuel_consumed_liters DOUBLE PRECISION,
  avg_fuel_l_per_100km DOUBLE PRECISION,
  trip_count INTEGER,

  first_trip_start_ts TIMESTAMPTZ,
  last_trip_end_ts TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (client_id, vehicle_id, driver_id, day)
);

CREATE INDEX IF NOT EXISTS idx_client_veh_drv_daily_fuel_day
  ON public.client_vehicle_driver_daily_fuel (day);

CREATE INDEX IF NOT EXISTS idx_client_veh_drv_daily_fuel_registration
  ON public.client_vehicle_driver_daily_fuel (registration, day);
