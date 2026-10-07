-- 027_eco_driving_schema.sql
-- Workflow A — Eco Driving assignment audit and weekly/monthly stats support.
--
-- Source table: public.client_trips.
-- Source trip identity: (client_id, provider_trip_id), matching the current
-- public.client_trips primary key. The Eco Driving postprocess should use
-- Europe/Warsaw business weeks via the shared Python timezone utility.

CREATE TABLE IF NOT EXISTS public.eco_trip_assignments (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  provider_trip_id INTEGER NOT NULL,
  record_id UUID NULL,

  assigned_id TEXT NULL,
  assignment_source TEXT NOT NULL,
  driver_restrictions_raw TEXT NULL,
  dysponent_id_raw TEXT NULL,

  trip_start_ts TIMESTAMPTZ NOT NULL,
  trip_end_ts TIMESTAMPTZ NULL,
  business_week_start_date DATE NOT NULL,
  business_week_end_date DATE NOT NULL,

  trip_distance_meters BIGINT NULL,
  overrev_events_count BIGINT NOT NULL DEFAULT 0,
  harsh_braking_events BIGINT NOT NULL DEFAULT 0,
  harsh_acceleration_events BIGINT NOT NULL DEFAULT 0,
  harsh_turning_events BIGINT NOT NULL DEFAULT 0,
  idle_events BIGINT NOT NULL DEFAULT 0,
  speeding_140_160_count BIGINT NOT NULL DEFAULT 0,
  speeding_160_170_count BIGINT NOT NULL DEFAULT 0,
  speeding_170_plus_count BIGINT NOT NULL DEFAULT 0,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (client_id, provider_trip_id),
  CONSTRAINT chk_eco_trip_assignments_assignment_source
    CHECK (assignment_source IN ('DRIVER_RESTRICTIONS', 'DYSPONENT_ID', 'SKIPPED_NO_ID')),
  CONSTRAINT chk_eco_trip_assignments_assigned_id
    CHECK (
      (assignment_source = 'SKIPPED_NO_ID' AND assigned_id IS NULL)
      OR
      (assignment_source IN ('DRIVER_RESTRICTIONS', 'DYSPONENT_ID')
        AND assigned_id IS NOT NULL
        AND btrim(assigned_id) <> '')
    ),
  CONSTRAINT chk_eco_trip_assignments_business_week
    CHECK (
      business_week_end_date = business_week_start_date + 7
      AND EXTRACT(ISODOW FROM business_week_start_date) = 1
      AND EXTRACT(ISODOW FROM business_week_end_date) = 1
    ),
  CONSTRAINT chk_eco_trip_assignments_non_negative_counts
    CHECK (
      COALESCE(trip_distance_meters, 0) >= 0
      AND overrev_events_count >= 0
      AND harsh_braking_events >= 0
      AND harsh_acceleration_events >= 0
      AND harsh_turning_events >= 0
      AND idle_events >= 0
      AND speeding_140_160_count >= 0
      AND speeding_160_170_count >= 0
      AND speeding_170_plus_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_assigned_id
  ON public.eco_trip_assignments (assigned_id)
  WHERE assigned_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_trip_start_ts
  ON public.eco_trip_assignments (trip_start_ts);

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_assignment_source
  ON public.eco_trip_assignments (assignment_source);

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_business_week_start
  ON public.eco_trip_assignments (business_week_start_date);

CREATE TABLE IF NOT EXISTS public.eco_driver_weekly_stats (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  assigned_id TEXT NOT NULL,
  week_start_date DATE NOT NULL,
  week_end_date DATE NOT NULL,

  trips_count INTEGER NOT NULL DEFAULT 0,
  source_trips_count INTEGER NOT NULL DEFAULT 0,
  skipped_trips_count INTEGER NOT NULL DEFAULT 0,
  total_distance_meters BIGINT NOT NULL DEFAULT 0,
  total_kilometers NUMERIC(14, 3) NOT NULL DEFAULT 0,

  overrev_events_count BIGINT NOT NULL DEFAULT 0,
  harsh_braking_events BIGINT NOT NULL DEFAULT 0,
  harsh_acceleration_events BIGINT NOT NULL DEFAULT 0,
  harsh_turning_events BIGINT NOT NULL DEFAULT 0,
  idle_events BIGINT NOT NULL DEFAULT 0,
  speeding_140_160_count BIGINT NOT NULL DEFAULT 0,
  speeding_160_170_count BIGINT NOT NULL DEFAULT 0,
  speeding_170_plus_count BIGINT NOT NULL DEFAULT 0,

  overrev_events_per_100km NUMERIC(14, 4) NULL,
  harsh_braking_events_per_100km NUMERIC(14, 4) NULL,
  harsh_acceleration_events_per_100km NUMERIC(14, 4) NULL,
  harsh_turning_events_per_100km NUMERIC(14, 4) NULL,
  idle_events_per_100km NUMERIC(14, 4) NULL,
  speeding_140_160_events_per_100km NUMERIC(14, 4) NULL,
  speeding_160_170_events_per_100km NUMERIC(14, 4) NULL,
  speeding_170_plus_events_per_100km NUMERIC(14, 4) NULL,

  overrev_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_braking_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_acceleration_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_turning_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  idle_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_140_160_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_160_170_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_170_plus_points NUMERIC(12, 2) NOT NULL DEFAULT 0,

  eco_driving_score_total NUMERIC(12, 2) NOT NULL DEFAULT 0,
  qualification_status TEXT NOT NULL,
  calculation_status TEXT NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (assigned_id, week_start_date),
  CONSTRAINT chk_eco_driver_weekly_stats_week
    CHECK (
      week_end_date = week_start_date + 7
      AND EXTRACT(ISODOW FROM week_start_date) = 1
      AND EXTRACT(ISODOW FROM week_end_date) = 1
    ),
  CONSTRAINT chk_eco_driver_weekly_stats_qualification_status
    CHECK (qualification_status IN ('QUALIFIED', 'LOW_DISTANCE', 'NO_DISTANCE')),
  CONSTRAINT chk_eco_driver_weekly_stats_calculation_status
    CHECK (calculation_status IN ('OK', 'NO_ASSIGNED_ID', 'NO_DISTANCE', 'ERROR')),
  CONSTRAINT chk_eco_driver_weekly_stats_non_negative_counts
    CHECK (
      trips_count >= 0
      AND source_trips_count >= 0
      AND skipped_trips_count >= 0
      AND total_distance_meters >= 0
      AND total_kilometers >= 0
      AND overrev_events_count >= 0
      AND harsh_braking_events >= 0
      AND harsh_acceleration_events >= 0
      AND harsh_turning_events >= 0
      AND idle_events >= 0
      AND speeding_140_160_count >= 0
      AND speeding_160_170_count >= 0
      AND speeding_170_plus_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_eco_driver_weekly_stats_assigned_id
  ON public.eco_driver_weekly_stats (assigned_id);

CREATE INDEX IF NOT EXISTS idx_eco_driver_weekly_stats_week_start
  ON public.eco_driver_weekly_stats (week_start_date);

CREATE TABLE IF NOT EXISTS public.eco_driver_monthly_stats (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  assigned_id TEXT NOT NULL,
  month_start_date DATE NOT NULL,
  month_end_date DATE NOT NULL,

  trips_count INTEGER NOT NULL DEFAULT 0,
  source_trips_count INTEGER NOT NULL DEFAULT 0,
  skipped_trips_count INTEGER NOT NULL DEFAULT 0,
  total_distance_meters BIGINT NOT NULL DEFAULT 0,
  total_kilometers NUMERIC(14, 3) NOT NULL DEFAULT 0,

  overrev_events_count BIGINT NOT NULL DEFAULT 0,
  harsh_braking_events BIGINT NOT NULL DEFAULT 0,
  harsh_acceleration_events BIGINT NOT NULL DEFAULT 0,
  harsh_turning_events BIGINT NOT NULL DEFAULT 0,
  idle_events BIGINT NOT NULL DEFAULT 0,
  speeding_140_160_count BIGINT NOT NULL DEFAULT 0,
  speeding_160_170_count BIGINT NOT NULL DEFAULT 0,
  speeding_170_plus_count BIGINT NOT NULL DEFAULT 0,

  overrev_events_per_100km NUMERIC(14, 4) NULL,
  harsh_braking_events_per_100km NUMERIC(14, 4) NULL,
  harsh_acceleration_events_per_100km NUMERIC(14, 4) NULL,
  harsh_turning_events_per_100km NUMERIC(14, 4) NULL,
  idle_events_per_100km NUMERIC(14, 4) NULL,
  speeding_140_160_events_per_100km NUMERIC(14, 4) NULL,
  speeding_160_170_events_per_100km NUMERIC(14, 4) NULL,
  speeding_170_plus_events_per_100km NUMERIC(14, 4) NULL,

  overrev_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_braking_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_acceleration_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  harsh_turning_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  idle_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_140_160_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_160_170_points NUMERIC(12, 2) NOT NULL DEFAULT 0,
  speeding_170_plus_points NUMERIC(12, 2) NOT NULL DEFAULT 0,

  eco_driving_score_total NUMERIC(12, 2) NOT NULL DEFAULT 0,
  qualification_status TEXT NOT NULL,
  calculation_status TEXT NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (assigned_id, month_start_date),
  CONSTRAINT chk_eco_driver_monthly_stats_month
    CHECK (
      month_start_date = date_trunc('month', month_start_date::timestamp)::date
      AND month_end_date = (month_start_date + INTERVAL '1 month')::date
    ),
  CONSTRAINT chk_eco_driver_monthly_stats_qualification_status
    CHECK (qualification_status IN ('QUALIFIED', 'LOW_DISTANCE', 'NO_DISTANCE')),
  CONSTRAINT chk_eco_driver_monthly_stats_calculation_status
    CHECK (calculation_status IN ('OK', 'NO_ASSIGNED_ID', 'NO_DISTANCE', 'ERROR')),
  CONSTRAINT chk_eco_driver_monthly_stats_non_negative_counts
    CHECK (
      trips_count >= 0
      AND source_trips_count >= 0
      AND skipped_trips_count >= 0
      AND total_distance_meters >= 0
      AND total_kilometers >= 0
      AND overrev_events_count >= 0
      AND harsh_braking_events >= 0
      AND harsh_acceleration_events >= 0
      AND harsh_turning_events >= 0
      AND idle_events >= 0
      AND speeding_140_160_count >= 0
      AND speeding_160_170_count >= 0
      AND speeding_170_plus_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_eco_driver_monthly_stats_assigned_id
  ON public.eco_driver_monthly_stats (assigned_id);

CREATE INDEX IF NOT EXISTS idx_eco_driver_monthly_stats_month_start
  ON public.eco_driver_monthly_stats (month_start_date);

COMMENT ON TABLE public.eco_trip_assignments IS
  'One row per public.client_trips row for Eco Driving assignment audit, including skipped trips with missing assignment IDs.';

COMMENT ON TABLE public.eco_driver_weekly_stats IS
  'Eco Driving weekly stats per assigned_id and Europe/Warsaw business week. Week end is exclusive.';

COMMENT ON TABLE public.eco_driver_monthly_stats IS
  'Eco Driving monthly stats per assigned_id and business month. Materialized as a table to match existing client-business aggregate table patterns.';
