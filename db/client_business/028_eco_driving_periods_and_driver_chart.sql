-- 028_eco_driving_periods_and_driver_chart.sql
-- Workflow A — adjust Eco Driving schema for month-bounded ranking periods,
-- private-trip exclusion audit, and driver identity/ranking configuration.
--
-- Physical names follow the repository's snake_case table convention. A quoted
-- compatibility view named public."Eco_Drivers_ID_Chart" is provided for the
-- business-facing name.

CREATE TABLE IF NOT EXISTS public.eco_drivers_id_chart (
  client_id UUID NOT NULL,
  driver_id TEXT NOT NULL,
  driver_name TEXT NULL,
  email TEXT NULL,
  ranking_included BOOLEAN NOT NULL DEFAULT true,
  is_active BOOLEAN NOT NULL DEFAULT true,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, driver_id)
);

CREATE INDEX IF NOT EXISTS idx_eco_drivers_id_chart_ranking_included
  ON public.eco_drivers_id_chart (ranking_included);

CREATE INDEX IF NOT EXISTS idx_eco_drivers_id_chart_is_active
  ON public.eco_drivers_id_chart (is_active);

CREATE INDEX IF NOT EXISTS idx_eco_drivers_id_chart_email
  ON public.eco_drivers_id_chart (email)
  WHERE email IS NOT NULL AND btrim(email) <> '';

CREATE OR REPLACE VIEW public."Eco_Drivers_ID_Chart" AS
SELECT
  client_id,
  driver_id,
  driver_name,
  email,
  ranking_included,
  is_active,
  metadata_json,
  created_at,
  updated_at
FROM public.eco_drivers_id_chart;

ALTER TABLE IF EXISTS public.eco_trip_assignments
  ADD COLUMN IF NOT EXISTS driver_tag_description TEXT NULL,
  ADD COLUMN IF NOT EXISTS is_private_trip BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS exclusion_reason TEXT NULL,
  ADD COLUMN IF NOT EXISTS aggregation_included BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE IF EXISTS public.eco_trip_assignments
  DROP CONSTRAINT IF EXISTS chk_eco_trip_assignments_exclusion_reason,
  ADD CONSTRAINT chk_eco_trip_assignments_exclusion_reason
    CHECK (exclusion_reason IS NULL OR exclusion_reason IN ('PRIVATE_DRIVER_TAG'));

ALTER TABLE IF EXISTS public.eco_trip_assignments
  DROP CONSTRAINT IF EXISTS chk_eco_trip_assignments_private_trip_exclusion,
  ADD CONSTRAINT chk_eco_trip_assignments_private_trip_exclusion
    CHECK (
      is_private_trip IS NOT TRUE
      OR (aggregation_included IS FALSE AND exclusion_reason = 'PRIVATE_DRIVER_TAG')
    );

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_aggregation_included
  ON public.eco_trip_assignments (aggregation_included);

CREATE INDEX IF NOT EXISTS idx_eco_trip_assignments_private_trip
  ON public.eco_trip_assignments (is_private_trip)
  WHERE is_private_trip IS TRUE;

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  ADD COLUMN IF NOT EXISTS period_start_date DATE NULL,
  ADD COLUMN IF NOT EXISTS period_end_date DATE NULL,
  ADD COLUMN IF NOT EXISTS month_start_date DATE NULL,
  ADD COLUMN IF NOT EXISTS period_sequence_in_month INTEGER NULL,
  ADD COLUMN IF NOT EXISTS period_label TEXT NULL,
  ADD COLUMN IF NOT EXISTS is_partial_period BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS ranking_included BOOLEAN NULL,
  ADD COLUMN IF NOT EXISTS ranking_group TEXT NOT NULL DEFAULT 'UNKNOWN_DRIVER',
  ADD COLUMN IF NOT EXISTS ranking_position INTEGER NULL,
  ADD COLUMN IF NOT EXISTS ranking_total_participants INTEGER NULL;

UPDATE public.eco_driver_weekly_stats
SET
  period_start_date = COALESCE(period_start_date, week_start_date),
  period_end_date = COALESCE(
    period_end_date,
    LEAST(
      week_end_date,
      (date_trunc('month', week_start_date::timestamp)::date + INTERVAL '1 month')::date
    )
  ),
  month_start_date = COALESCE(month_start_date, date_trunc('month', week_start_date::timestamp)::date),
  period_sequence_in_month = COALESCE(
    period_sequence_in_month,
    GREATEST(1, ((week_start_date - date_trunc('month', week_start_date::timestamp)::date) / 7) + 1)
  ),
  period_label = COALESCE(
    period_label,
    to_char(date_trunc('month', week_start_date::timestamp), 'YYYY-MM')
      || '-W'
      || GREATEST(1, ((week_start_date - date_trunc('month', week_start_date::timestamp)::date) / 7) + 1)::text
  ),
  is_partial_period = COALESCE(is_partial_period, false),
  ranking_group = COALESCE(ranking_group, 'UNKNOWN_DRIVER')
WHERE period_start_date IS NULL
   OR period_end_date IS NULL
   OR month_start_date IS NULL
   OR period_sequence_in_month IS NULL
   OR period_label IS NULL
   OR ranking_group IS NULL;

UPDATE public.eco_driver_weekly_stats
SET is_partial_period = (
  period_start_date <> date_trunc('week', period_start_date::timestamp)::date
  OR period_end_date <> period_start_date + 7
)
WHERE period_start_date IS NOT NULL
  AND period_end_date IS NOT NULL;

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  ALTER COLUMN period_start_date SET NOT NULL,
  ALTER COLUMN period_end_date SET NOT NULL,
  ALTER COLUMN month_start_date SET NOT NULL,
  ALTER COLUMN period_sequence_in_month SET NOT NULL,
  ALTER COLUMN period_label SET NOT NULL,
  ALTER COLUMN ranking_group SET NOT NULL;

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_weekly_stats_week;

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS eco_driver_weekly_stats_pkey;

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  ADD CONSTRAINT eco_driver_weekly_stats_pkey
    PRIMARY KEY (client_id, assigned_id, period_start_date, period_end_date);

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS uq_eco_driver_weekly_stats_period,
  ADD CONSTRAINT uq_eco_driver_weekly_stats_period
    UNIQUE (client_id, assigned_id, period_start_date, period_end_date);

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_weekly_stats_period,
  ADD CONSTRAINT chk_eco_driver_weekly_stats_period
    CHECK (
      period_end_date > period_start_date
      AND month_start_date = date_trunc('month', month_start_date::timestamp)::date
      AND period_start_date >= month_start_date
      AND period_start_date < (month_start_date + INTERVAL '1 month')::date
      AND period_end_date <= (month_start_date + INTERVAL '1 month')::date
      AND period_sequence_in_month >= 1
      AND btrim(period_label) <> ''
    );

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_weekly_stats_ranking_group,
  ADD CONSTRAINT chk_eco_driver_weekly_stats_ranking_group
    CHECK (ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER'));

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_weekly_stats_ranking_positions,
  ADD CONSTRAINT chk_eco_driver_weekly_stats_ranking_positions
    CHECK (
      (ranking_position IS NULL OR ranking_position > 0)
      AND (ranking_total_participants IS NULL OR ranking_total_participants >= 0)
    );

CREATE INDEX IF NOT EXISTS idx_eco_driver_weekly_stats_period_start
  ON public.eco_driver_weekly_stats (period_start_date);

CREATE INDEX IF NOT EXISTS idx_eco_driver_weekly_stats_month_start
  ON public.eco_driver_weekly_stats (month_start_date);

CREATE INDEX IF NOT EXISTS idx_eco_driver_weekly_stats_ranking_group
  ON public.eco_driver_weekly_stats (ranking_group);

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  ADD COLUMN IF NOT EXISTS ranking_included BOOLEAN NULL,
  ADD COLUMN IF NOT EXISTS ranking_group TEXT NOT NULL DEFAULT 'UNKNOWN_DRIVER',
  ADD COLUMN IF NOT EXISTS ranking_position INTEGER NULL,
  ADD COLUMN IF NOT EXISTS ranking_total_participants INTEGER NULL;

UPDATE public.eco_driver_monthly_stats
SET ranking_group = COALESCE(ranking_group, 'UNKNOWN_DRIVER')
WHERE ranking_group IS NULL;

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  ALTER COLUMN ranking_group SET NOT NULL;

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  DROP CONSTRAINT IF EXISTS eco_driver_monthly_stats_pkey;

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  ADD CONSTRAINT eco_driver_monthly_stats_pkey
    PRIMARY KEY (client_id, assigned_id, month_start_date);

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  DROP CONSTRAINT IF EXISTS uq_eco_driver_monthly_stats_month,
  ADD CONSTRAINT uq_eco_driver_monthly_stats_month
    UNIQUE (client_id, assigned_id, month_start_date);

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_monthly_stats_ranking_group,
  ADD CONSTRAINT chk_eco_driver_monthly_stats_ranking_group
    CHECK (ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER'));

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  DROP CONSTRAINT IF EXISTS chk_eco_driver_monthly_stats_ranking_positions,
  ADD CONSTRAINT chk_eco_driver_monthly_stats_ranking_positions
    CHECK (
      (ranking_position IS NULL OR ranking_position > 0)
      AND (ranking_total_participants IS NULL OR ranking_total_participants >= 0)
    );

CREATE INDEX IF NOT EXISTS idx_eco_driver_monthly_stats_ranking_group
  ON public.eco_driver_monthly_stats (ranking_group);

COMMENT ON TABLE public.eco_drivers_id_chart IS
  'Eco Driving driver identity and ranking configuration per client and aggregation driver_id.';

COMMENT ON VIEW public."Eco_Drivers_ID_Chart" IS
  'Business-facing compatibility view for public.eco_drivers_id_chart.';

COMMENT ON COLUMN public.eco_trip_assignments.driver_tag_description IS
  'Source client_trips.driver_tag_description copied for private-trip exclusion audit; values containing pryw case-insensitively are excluded by the postprocess job.';

COMMENT ON COLUMN public.eco_driver_weekly_stats.period_start_date IS
  'Month-bounded weekly ranking period start date, inclusive, in Europe/Warsaw business time.';

COMMENT ON COLUMN public.eco_driver_weekly_stats.period_end_date IS
  'Month-bounded weekly ranking period end date, exclusive, in Europe/Warsaw business time.';
