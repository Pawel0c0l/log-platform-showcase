-- 037_eco_driving_score_from_rounded_per_100km.sql
-- Recalculate Eco Driving scoring from rounded whole-number per-100km rates.
-- This is an idempotent data backfill for rows produced before aggregation
-- used the same rounded coefficients for display and scoring.

WITH source_rows AS (
  SELECT
    ctid AS row_id,
    ROUND(overrev_events_per_100km) AS overrev_rate,
    ROUND(harsh_braking_events_per_100km) AS harsh_braking_rate,
    ROUND(harsh_acceleration_events_per_100km) AS harsh_acceleration_rate,
    ROUND(harsh_turning_events_per_100km) AS harsh_turning_rate,
    ROUND(idle_events_per_100km) AS idle_rate,
    ROUND(speeding_140_160_events_per_100km) AS speeding_140_160_rate,
    ROUND(speeding_160_170_events_per_100km) AS speeding_160_170_rate,
    ROUND(speeding_170_plus_events_per_100km) AS speeding_170_plus_rate
  FROM public.eco_driver_weekly_stats
  WHERE COALESCE(total_distance_meters, 0) > 0
    AND overrev_events_per_100km IS NOT NULL
    AND harsh_braking_events_per_100km IS NOT NULL
    AND harsh_acceleration_events_per_100km IS NOT NULL
    AND harsh_turning_events_per_100km IS NOT NULL
    AND idle_events_per_100km IS NOT NULL
    AND speeding_140_160_events_per_100km IS NOT NULL
    AND speeding_160_170_events_per_100km IS NOT NULL
    AND speeding_170_plus_events_per_100km IS NOT NULL
), metric_points AS (
  SELECT
    *,
    CASE
      WHEN overrev_rate <= 0 THEN 15
      WHEN overrev_rate <= 2 THEN 11
      WHEN overrev_rate <= 5 THEN 7
      WHEN overrev_rate <= 9 THEN 4
      WHEN overrev_rate <= 13 THEN 0
      WHEN overrev_rate <= 20 THEN -7
      ELSE -15
    END AS overrev_points_new,
    CASE
      WHEN harsh_braking_rate <= 0 THEN 10
      WHEN harsh_braking_rate <= 1 THEN 8
      WHEN harsh_braking_rate <= 4 THEN 4
      WHEN harsh_braking_rate <= 6 THEN 2
      WHEN harsh_braking_rate <= 8 THEN 0
      WHEN harsh_braking_rate <= 10 THEN -5
      ELSE -10
    END AS harsh_braking_points_new,
    CASE
      WHEN harsh_acceleration_rate <= 0 THEN 10
      WHEN harsh_acceleration_rate <= 1 THEN 5
      WHEN harsh_acceleration_rate <= 2 THEN 0
      WHEN harsh_acceleration_rate <= 4 THEN -5
      ELSE -10
    END AS harsh_acceleration_points_new,
    CASE
      WHEN harsh_turning_rate <= 4 THEN 10
      WHEN harsh_turning_rate <= 7 THEN 7
      WHEN harsh_turning_rate <= 12 THEN 4
      WHEN harsh_turning_rate <= 16 THEN 0
      WHEN harsh_turning_rate <= 23 THEN -4
      WHEN harsh_turning_rate <= 30 THEN -8
      ELSE -10
    END AS harsh_turning_points_new,
    CASE
      WHEN idle_rate <= 0 THEN 10
      WHEN idle_rate <= 2 THEN 7
      WHEN idle_rate <= 4 THEN 4
      WHEN idle_rate <= 5 THEN 0
      WHEN idle_rate <= 6 THEN -5
      WHEN idle_rate <= 8 THEN -7
      ELSE -10
    END AS idle_points_new,
    CASE
      WHEN speeding_140_160_rate <= 0 THEN 15
      WHEN speeding_140_160_rate <= 2 THEN 10
      WHEN speeding_140_160_rate <= 4 THEN 5
      WHEN speeding_140_160_rate <= 6 THEN 0
      WHEN speeding_140_160_rate <= 8 THEN -5
      WHEN speeding_140_160_rate <= 10 THEN -10
      ELSE -15
    END AS speeding_140_160_points_new,
    CASE
      WHEN speeding_160_170_rate <= 0 THEN 15
      WHEN speeding_160_170_rate <= 1 THEN 10
      WHEN speeding_160_170_rate <= 2 THEN 5
      WHEN speeding_160_170_rate <= 3 THEN 0
      WHEN speeding_160_170_rate <= 4 THEN -5
      WHEN speeding_160_170_rate <= 5 THEN -10
      ELSE -15
    END AS speeding_160_170_points_new,
    CASE
      WHEN speeding_170_plus_rate <= 0 THEN 15
      WHEN speeding_170_plus_rate <= 1 THEN 0
      WHEN speeding_170_plus_rate <= 2 THEN -7
      ELSE -15
    END AS speeding_170_plus_points_new
  FROM source_rows
), scored AS (
  SELECT
    *,
    LEAST(overrev_points_new - 15, 0) AS overrev_subtract_new,
    LEAST(harsh_braking_points_new - 10, 0) AS harsh_braking_subtract_new,
    LEAST(harsh_acceleration_points_new - 10, 0) AS harsh_acceleration_subtract_new,
    LEAST(harsh_turning_points_new - 10, 0) AS harsh_turning_subtract_new,
    LEAST(idle_points_new - 10, 0) AS idle_subtract_new,
    LEAST(speeding_140_160_points_new - 15, 0) AS speeding_140_160_subtract_new,
    LEAST(speeding_160_170_points_new - 15, 0) AS speeding_160_170_subtract_new,
    LEAST(speeding_170_plus_points_new - 15, 0) AS speeding_170_plus_subtract_new
  FROM metric_points
), validated AS (
  SELECT
    scored.*,
    (
      100
      + overrev_subtract_new
      + harsh_braking_subtract_new
      + harsh_acceleration_subtract_new
      + harsh_turning_subtract_new
      + idle_subtract_new
      + speeding_140_160_subtract_new
      + speeding_160_170_subtract_new
      + speeding_170_plus_subtract_new
    ) AS score_total_new,
    validation_labels.top_validations
  FROM scored
  CROSS JOIN LATERAL (
    SELECT ARRAY(
      SELECT label
      FROM (
        VALUES
          (scored.overrev_subtract_new, 1, 'Nadmierne obroty'),
          (scored.harsh_braking_subtract_new, 2, 'Gwałtowne hamowania'),
          (scored.harsh_acceleration_subtract_new, 3, 'Gwałtowne przyspieszenia'),
          (scored.harsh_turning_subtract_new, 4, 'Ostre skręty'),
          (scored.idle_subtract_new, 5, 'Nadmierny postój'),
          (scored.speeding_140_160_subtract_new, 6, 'Przekroczenia prędkości między 140-160'),
          (scored.speeding_160_170_subtract_new, 7, 'Przekroczenia prędkości między 160-170'),
          (scored.speeding_170_plus_subtract_new, 8, 'Przekroczenia prędkości powyżej 170')
      ) AS losses(subtract_value, metric_order, label)
      WHERE subtract_value < 0
      ORDER BY subtract_value ASC, metric_order ASC
      LIMIT 2
    ) AS top_validations
  ) AS validation_labels
)
UPDATE public.eco_driver_weekly_stats AS target
SET
  overrev_events_per_100km = validated.overrev_rate,
  harsh_braking_events_per_100km = validated.harsh_braking_rate,
  harsh_acceleration_events_per_100km = validated.harsh_acceleration_rate,
  harsh_turning_events_per_100km = validated.harsh_turning_rate,
  idle_events_per_100km = validated.idle_rate,
  speeding_140_160_events_per_100km = validated.speeding_140_160_rate,
  speeding_160_170_events_per_100km = validated.speeding_160_170_rate,
  speeding_170_plus_events_per_100km = validated.speeding_170_plus_rate,
  overrev_points = validated.overrev_points_new,
  harsh_braking_points = validated.harsh_braking_points_new,
  harsh_acceleration_points = validated.harsh_acceleration_points_new,
  harsh_turning_points = validated.harsh_turning_points_new,
  idle_points = validated.idle_points_new,
  speeding_140_160_points = validated.speeding_140_160_points_new,
  speeding_160_170_points = validated.speeding_160_170_points_new,
  speeding_170_plus_points = validated.speeding_170_plus_points_new,
  overrev_maxpoints_subtract = validated.overrev_subtract_new,
  harsh_braking_maxpoints_subtract = validated.harsh_braking_subtract_new,
  harsh_acceleration_maxpoints_subtract = validated.harsh_acceleration_subtract_new,
  harsh_turning_maxpoints_subtract = validated.harsh_turning_subtract_new,
  idle_maxpoints_subtract = validated.idle_subtract_new,
  speeding_140_160_maxpoints_subtract = validated.speeding_140_160_subtract_new,
  speeding_160_170_maxpoints_subtract = validated.speeding_160_170_subtract_new,
  speeding_170_plus_maxpoints_subtract = validated.speeding_170_plus_subtract_new,
  eco_driving_score_total = validated.score_total_new,
  top_1_validation = validated.top_validations[1],
  top_2_validation = validated.top_validations[2],
  ecodriving_rating_type = CASE
    WHEN validated.score_total_new >= 85 THEN 'bezpieczny'
    WHEN validated.score_total_new >= 40 THEN 'akceptowalny'
    ELSE 'niebezpieczny'
  END,
  updated_at = NOW()
FROM validated
WHERE target.ctid = validated.row_id;

WITH source_rows AS (
  SELECT
    ctid AS row_id,
    ROUND(overrev_events_per_100km) AS overrev_rate,
    ROUND(harsh_braking_events_per_100km) AS harsh_braking_rate,
    ROUND(harsh_acceleration_events_per_100km) AS harsh_acceleration_rate,
    ROUND(harsh_turning_events_per_100km) AS harsh_turning_rate,
    ROUND(idle_events_per_100km) AS idle_rate,
    ROUND(speeding_140_160_events_per_100km) AS speeding_140_160_rate,
    ROUND(speeding_160_170_events_per_100km) AS speeding_160_170_rate,
    ROUND(speeding_170_plus_events_per_100km) AS speeding_170_plus_rate
  FROM public.eco_driver_monthly_stats
  WHERE COALESCE(total_distance_meters, 0) > 0
    AND overrev_events_per_100km IS NOT NULL
    AND harsh_braking_events_per_100km IS NOT NULL
    AND harsh_acceleration_events_per_100km IS NOT NULL
    AND harsh_turning_events_per_100km IS NOT NULL
    AND idle_events_per_100km IS NOT NULL
    AND speeding_140_160_events_per_100km IS NOT NULL
    AND speeding_160_170_events_per_100km IS NOT NULL
    AND speeding_170_plus_events_per_100km IS NOT NULL
), metric_points AS (
  SELECT
    *,
    CASE
      WHEN overrev_rate <= 0 THEN 15
      WHEN overrev_rate <= 2 THEN 11
      WHEN overrev_rate <= 5 THEN 7
      WHEN overrev_rate <= 9 THEN 4
      WHEN overrev_rate <= 13 THEN 0
      WHEN overrev_rate <= 20 THEN -7
      ELSE -15
    END AS overrev_points_new,
    CASE
      WHEN harsh_braking_rate <= 0 THEN 10
      WHEN harsh_braking_rate <= 1 THEN 8
      WHEN harsh_braking_rate <= 4 THEN 4
      WHEN harsh_braking_rate <= 6 THEN 2
      WHEN harsh_braking_rate <= 8 THEN 0
      WHEN harsh_braking_rate <= 10 THEN -5
      ELSE -10
    END AS harsh_braking_points_new,
    CASE
      WHEN harsh_acceleration_rate <= 0 THEN 10
      WHEN harsh_acceleration_rate <= 1 THEN 5
      WHEN harsh_acceleration_rate <= 2 THEN 0
      WHEN harsh_acceleration_rate <= 4 THEN -5
      ELSE -10
    END AS harsh_acceleration_points_new,
    CASE
      WHEN harsh_turning_rate <= 4 THEN 10
      WHEN harsh_turning_rate <= 7 THEN 7
      WHEN harsh_turning_rate <= 12 THEN 4
      WHEN harsh_turning_rate <= 16 THEN 0
      WHEN harsh_turning_rate <= 23 THEN -4
      WHEN harsh_turning_rate <= 30 THEN -8
      ELSE -10
    END AS harsh_turning_points_new,
    CASE
      WHEN idle_rate <= 0 THEN 10
      WHEN idle_rate <= 2 THEN 7
      WHEN idle_rate <= 4 THEN 4
      WHEN idle_rate <= 5 THEN 0
      WHEN idle_rate <= 6 THEN -5
      WHEN idle_rate <= 8 THEN -7
      ELSE -10
    END AS idle_points_new,
    CASE
      WHEN speeding_140_160_rate <= 0 THEN 15
      WHEN speeding_140_160_rate <= 2 THEN 10
      WHEN speeding_140_160_rate <= 4 THEN 5
      WHEN speeding_140_160_rate <= 6 THEN 0
      WHEN speeding_140_160_rate <= 8 THEN -5
      WHEN speeding_140_160_rate <= 10 THEN -10
      ELSE -15
    END AS speeding_140_160_points_new,
    CASE
      WHEN speeding_160_170_rate <= 0 THEN 15
      WHEN speeding_160_170_rate <= 1 THEN 10
      WHEN speeding_160_170_rate <= 2 THEN 5
      WHEN speeding_160_170_rate <= 3 THEN 0
      WHEN speeding_160_170_rate <= 4 THEN -5
      WHEN speeding_160_170_rate <= 5 THEN -10
      ELSE -15
    END AS speeding_160_170_points_new,
    CASE
      WHEN speeding_170_plus_rate <= 0 THEN 15
      WHEN speeding_170_plus_rate <= 1 THEN 0
      WHEN speeding_170_plus_rate <= 2 THEN -7
      ELSE -15
    END AS speeding_170_plus_points_new
  FROM source_rows
), scored AS (
  SELECT
    *,
    LEAST(overrev_points_new - 15, 0) AS overrev_subtract_new,
    LEAST(harsh_braking_points_new - 10, 0) AS harsh_braking_subtract_new,
    LEAST(harsh_acceleration_points_new - 10, 0) AS harsh_acceleration_subtract_new,
    LEAST(harsh_turning_points_new - 10, 0) AS harsh_turning_subtract_new,
    LEAST(idle_points_new - 10, 0) AS idle_subtract_new,
    LEAST(speeding_140_160_points_new - 15, 0) AS speeding_140_160_subtract_new,
    LEAST(speeding_160_170_points_new - 15, 0) AS speeding_160_170_subtract_new,
    LEAST(speeding_170_plus_points_new - 15, 0) AS speeding_170_plus_subtract_new
  FROM metric_points
), validated AS (
  SELECT
    scored.*,
    (
      100
      + overrev_subtract_new
      + harsh_braking_subtract_new
      + harsh_acceleration_subtract_new
      + harsh_turning_subtract_new
      + idle_subtract_new
      + speeding_140_160_subtract_new
      + speeding_160_170_subtract_new
      + speeding_170_plus_subtract_new
    ) AS score_total_new,
    validation_labels.top_validations
  FROM scored
  CROSS JOIN LATERAL (
    SELECT ARRAY(
      SELECT label
      FROM (
        VALUES
          (scored.overrev_subtract_new, 1, 'Nadmierne obroty'),
          (scored.harsh_braking_subtract_new, 2, 'Gwałtowne hamowania'),
          (scored.harsh_acceleration_subtract_new, 3, 'Gwałtowne przyspieszenia'),
          (scored.harsh_turning_subtract_new, 4, 'Ostre skręty'),
          (scored.idle_subtract_new, 5, 'Nadmierny postój'),
          (scored.speeding_140_160_subtract_new, 6, 'Przekroczenia prędkości między 140-160'),
          (scored.speeding_160_170_subtract_new, 7, 'Przekroczenia prędkości między 160-170'),
          (scored.speeding_170_plus_subtract_new, 8, 'Przekroczenia prędkości powyżej 170')
      ) AS losses(subtract_value, metric_order, label)
      WHERE subtract_value < 0
      ORDER BY subtract_value ASC, metric_order ASC
      LIMIT 2
    ) AS top_validations
  ) AS validation_labels
)
UPDATE public.eco_driver_monthly_stats AS target
SET
  overrev_events_per_100km = validated.overrev_rate,
  harsh_braking_events_per_100km = validated.harsh_braking_rate,
  harsh_acceleration_events_per_100km = validated.harsh_acceleration_rate,
  harsh_turning_events_per_100km = validated.harsh_turning_rate,
  idle_events_per_100km = validated.idle_rate,
  speeding_140_160_events_per_100km = validated.speeding_140_160_rate,
  speeding_160_170_events_per_100km = validated.speeding_160_170_rate,
  speeding_170_plus_events_per_100km = validated.speeding_170_plus_rate,
  overrev_points = validated.overrev_points_new,
  harsh_braking_points = validated.harsh_braking_points_new,
  harsh_acceleration_points = validated.harsh_acceleration_points_new,
  harsh_turning_points = validated.harsh_turning_points_new,
  idle_points = validated.idle_points_new,
  speeding_140_160_points = validated.speeding_140_160_points_new,
  speeding_160_170_points = validated.speeding_160_170_points_new,
  speeding_170_plus_points = validated.speeding_170_plus_points_new,
  overrev_maxpoints_subtract = validated.overrev_subtract_new,
  harsh_braking_maxpoints_subtract = validated.harsh_braking_subtract_new,
  harsh_acceleration_maxpoints_subtract = validated.harsh_acceleration_subtract_new,
  harsh_turning_maxpoints_subtract = validated.harsh_turning_subtract_new,
  idle_maxpoints_subtract = validated.idle_subtract_new,
  speeding_140_160_maxpoints_subtract = validated.speeding_140_160_subtract_new,
  speeding_160_170_maxpoints_subtract = validated.speeding_160_170_subtract_new,
  speeding_170_plus_maxpoints_subtract = validated.speeding_170_plus_subtract_new,
  eco_driving_score_total = validated.score_total_new,
  top_1_validation = validated.top_validations[1],
  top_2_validation = validated.top_validations[2],
  ecodriving_rating_type = CASE
    WHEN validated.score_total_new >= 85 THEN 'bezpieczny'
    WHEN validated.score_total_new >= 40 THEN 'akceptowalny'
    ELSE 'niebezpieczny'
  END,
  updated_at = NOW()
FROM validated
WHERE target.ctid = validated.row_id;

WITH denominators AS (
  SELECT
    client_id,
    period_start_date,
    period_end_date,
    count(*) AS denominator
  FROM public.eco_driver_weekly_stats
  WHERE ranking_included IS TRUE
    AND qualification_status = 'QUALIFIED'
    AND ecodriving_rating_type IS NOT NULL
  GROUP BY client_id, period_start_date, period_end_date
), numerators AS (
  SELECT
    client_id,
    period_start_date,
    period_end_date,
    ecodriving_rating_type,
    count(*) AS numerator
  FROM public.eco_driver_weekly_stats
  WHERE ranking_included IS TRUE
    AND qualification_status = 'QUALIFIED'
    AND ecodriving_rating_type IS NOT NULL
  GROUP BY client_id, period_start_date, period_end_date, ecodriving_rating_type
), shares AS (
  SELECT
    target.ctid AS row_id,
    CASE
      WHEN target.ranking_included IS TRUE
       AND target.qualification_status = 'QUALIFIED'
       AND target.ecodriving_rating_type IS NOT NULL
       AND denominators.denominator > 0
      THEN ROUND((numerators.numerator::numeric * 100) / denominators.denominator::numeric, 2)
      ELSE NULL
    END AS share_percent
  FROM public.eco_driver_weekly_stats AS target
  LEFT JOIN denominators
    ON denominators.client_id = target.client_id
   AND denominators.period_start_date = target.period_start_date
   AND denominators.period_end_date = target.period_end_date
  LEFT JOIN numerators
    ON numerators.client_id = target.client_id
   AND numerators.period_start_date = target.period_start_date
   AND numerators.period_end_date = target.period_end_date
   AND numerators.ecodriving_rating_type = target.ecodriving_rating_type
)
UPDATE public.eco_driver_weekly_stats AS target
SET
  ecodriving_rating_type_share_percent = shares.share_percent,
  updated_at = NOW()
FROM shares
WHERE target.ctid = shares.row_id;

WITH denominators AS (
  SELECT
    client_id,
    month_start_date,
    count(*) AS denominator
  FROM public.eco_driver_monthly_stats
  WHERE ranking_included IS TRUE
    AND qualification_status = 'QUALIFIED'
    AND ecodriving_rating_type IS NOT NULL
  GROUP BY client_id, month_start_date
), numerators AS (
  SELECT
    client_id,
    month_start_date,
    ecodriving_rating_type,
    count(*) AS numerator
  FROM public.eco_driver_monthly_stats
  WHERE ranking_included IS TRUE
    AND qualification_status = 'QUALIFIED'
    AND ecodriving_rating_type IS NOT NULL
  GROUP BY client_id, month_start_date, ecodriving_rating_type
), shares AS (
  SELECT
    target.ctid AS row_id,
    CASE
      WHEN target.ranking_included IS TRUE
       AND target.qualification_status = 'QUALIFIED'
       AND target.ecodriving_rating_type IS NOT NULL
       AND denominators.denominator > 0
      THEN ROUND((numerators.numerator::numeric * 100) / denominators.denominator::numeric, 2)
      ELSE NULL
    END AS share_percent
  FROM public.eco_driver_monthly_stats AS target
  LEFT JOIN denominators
    ON denominators.client_id = target.client_id
   AND denominators.month_start_date = target.month_start_date
  LEFT JOIN numerators
    ON numerators.client_id = target.client_id
   AND numerators.month_start_date = target.month_start_date
   AND numerators.ecodriving_rating_type = target.ecodriving_rating_type
)
UPDATE public.eco_driver_monthly_stats AS target
SET
  ecodriving_rating_type_share_percent = shares.share_percent,
  updated_at = NOW()
FROM shares
WHERE target.ctid = shares.row_id;
