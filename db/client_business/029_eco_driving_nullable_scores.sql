-- 029_eco_driving_nullable_scores.sql
-- Workflow A — allow Eco Driving score/point columns to be NULL.
--
-- Zero-distance periods have NULL per-100km rates. The Python scoring helper
-- intentionally returns NULL points and NULL total score for incomplete rates,
-- so persisted stats must not force fake zero scores.

ALTER TABLE IF EXISTS public.eco_driver_weekly_stats
  ALTER COLUMN overrev_points DROP NOT NULL,
  ALTER COLUMN harsh_braking_points DROP NOT NULL,
  ALTER COLUMN harsh_acceleration_points DROP NOT NULL,
  ALTER COLUMN harsh_turning_points DROP NOT NULL,
  ALTER COLUMN idle_points DROP NOT NULL,
  ALTER COLUMN speeding_140_160_points DROP NOT NULL,
  ALTER COLUMN speeding_160_170_points DROP NOT NULL,
  ALTER COLUMN speeding_170_plus_points DROP NOT NULL,
  ALTER COLUMN eco_driving_score_total DROP NOT NULL;

ALTER TABLE IF EXISTS public.eco_driver_monthly_stats
  ALTER COLUMN overrev_points DROP NOT NULL,
  ALTER COLUMN harsh_braking_points DROP NOT NULL,
  ALTER COLUMN harsh_acceleration_points DROP NOT NULL,
  ALTER COLUMN harsh_turning_points DROP NOT NULL,
  ALTER COLUMN idle_points DROP NOT NULL,
  ALTER COLUMN speeding_140_160_points DROP NOT NULL,
  ALTER COLUMN speeding_160_170_points DROP NOT NULL,
  ALTER COLUMN speeding_170_plus_points DROP NOT NULL,
  ALTER COLUMN eco_driving_score_total DROP NOT NULL;

