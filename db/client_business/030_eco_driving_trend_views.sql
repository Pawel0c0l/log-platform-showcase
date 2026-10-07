-- 030_eco_driving_trend_views.sql
-- Workflow A — trend/progress query views for Eco Driving stats.
--
-- Weekly stats are cumulative month-to-date snapshots. Deltas in the weekly
-- trend view compare one cumulative snapshot with the previous cumulative
-- snapshot for the same client/assigned_id, not isolated week-only slices.
-- Do not sum weekly trend rows to calculate monthly totals.

CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view AS
WITH trend_base AS (
  SELECT
    s.client_id,
    s.assigned_id,
    c.driver_name,
    c.email,
    s.ranking_included,
    s.ranking_group,
    s.month_start_date,
    s.period_start_date,
    s.period_end_date,
    s.period_sequence_in_month,
    s.period_label,
    s.is_partial_period,
    s.trips_count,
    s.total_distance_meters,
    s.total_kilometers,
    s.eco_driving_score_total,
    LAG(s.eco_driving_score_total) OVER weekly_trend_window AS previous_snapshot_score,
    AVG(s.eco_driving_score_total) OVER weekly_4_snapshot_window AS rolling_4_snapshot_avg_score,
    AVG(s.eco_driving_score_total) OVER weekly_8_snapshot_window AS rolling_8_snapshot_avg_score,
    COUNT(*) OVER weekly_to_date_window AS snapshots_observed,
    MAX(s.eco_driving_score_total) OVER weekly_to_date_window AS best_score_to_date,
    MIN(s.eco_driving_score_total) OVER weekly_to_date_window AS worst_score_to_date,
    LAG(s.total_kilometers) OVER weekly_trend_window AS previous_snapshot_kilometers,
    s.ranking_position,
    LAG(s.ranking_position) OVER weekly_trend_window AS previous_ranking_position,
    s.ranking_total_participants,
    s.qualification_status,
    s.calculation_status
  FROM public.eco_driver_weekly_stats s
  LEFT JOIN public.eco_drivers_id_chart c
    ON c.client_id = s.client_id
   AND c.driver_id = s.assigned_id
  WINDOW
    weekly_trend_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date, s.period_end_date
    ),
    weekly_to_date_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date, s.period_end_date
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ),
    weekly_4_snapshot_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date, s.period_end_date
      ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
    ),
    weekly_8_snapshot_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date, s.period_end_date
      ROWS BETWEEN 7 PRECEDING AND CURRENT ROW
    )
)
SELECT
  client_id,
  assigned_id,
  driver_name,
  email,
  ranking_included,
  ranking_group,
  month_start_date,
  period_start_date,
  period_end_date,
  period_sequence_in_month,
  period_label,
  is_partial_period,
  trips_count,
  total_distance_meters,
  total_kilometers,
  eco_driving_score_total,
  previous_snapshot_score,
  CASE
    WHEN eco_driving_score_total IS NULL OR previous_snapshot_score IS NULL THEN NULL
    ELSE eco_driving_score_total - previous_snapshot_score
  END AS score_delta_abs,
  CASE
    WHEN eco_driving_score_total IS NULL
      OR previous_snapshot_score IS NULL
      OR previous_snapshot_score = 0 THEN NULL
    ELSE ((eco_driving_score_total - previous_snapshot_score) / abs(previous_snapshot_score)) * 100
  END AS score_delta_pct,
  rolling_4_snapshot_avg_score,
  rolling_8_snapshot_avg_score,
  snapshots_observed,
  best_score_to_date,
  worst_score_to_date,
  previous_snapshot_kilometers,
  CASE
    WHEN total_kilometers IS NULL OR previous_snapshot_kilometers IS NULL THEN NULL
    ELSE total_kilometers - previous_snapshot_kilometers
  END AS kilometers_delta_abs,
  ranking_position,
  previous_ranking_position,
  CASE
    WHEN ranking_position IS NULL OR previous_ranking_position IS NULL THEN NULL
    ELSE previous_ranking_position - ranking_position
  END AS ranking_position_delta,
  ranking_total_participants,
  qualification_status,
  calculation_status
FROM trend_base;

COMMENT ON VIEW public.eco_driver_weekly_trends_view IS
  'Eco Driving weekly progress over cumulative month-to-date snapshots. Previous/delta fields compare each snapshot with the previous chronological snapshot for the same client and assigned_id; weekly rows must not be summed into monthly totals.';

CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view AS
WITH trend_base AS (
  SELECT
    s.client_id,
    s.assigned_id,
    c.driver_name,
    c.email,
    s.ranking_included,
    s.ranking_group,
    s.month_start_date,
    s.month_end_date,
    s.trips_count,
    s.total_distance_meters,
    s.total_kilometers,
    s.eco_driving_score_total,
    LAG(s.eco_driving_score_total) OVER monthly_trend_window AS previous_month_score,
    AVG(s.eco_driving_score_total) OVER monthly_3_month_window AS rolling_3_month_avg_score,
    AVG(s.eco_driving_score_total) OVER monthly_6_month_window AS rolling_6_month_avg_score,
    COUNT(*) OVER monthly_to_date_window AS months_observed,
    MAX(s.eco_driving_score_total) OVER monthly_to_date_window AS best_month_score_to_date,
    MIN(s.eco_driving_score_total) OVER monthly_to_date_window AS worst_month_score_to_date,
    LAG(s.total_kilometers) OVER monthly_trend_window AS previous_month_kilometers,
    s.ranking_position,
    LAG(s.ranking_position) OVER monthly_trend_window AS previous_ranking_position,
    s.ranking_total_participants,
    s.qualification_status,
    s.calculation_status
  FROM public.eco_driver_monthly_stats s
  LEFT JOIN public.eco_drivers_id_chart c
    ON c.client_id = s.client_id
   AND c.driver_id = s.assigned_id
  WINDOW
    monthly_trend_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date
    ),
    monthly_to_date_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ),
    monthly_3_month_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date
      ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ),
    monthly_6_month_window AS (
      PARTITION BY s.client_id, s.assigned_id
      ORDER BY s.month_start_date
      ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
    )
)
SELECT
  client_id,
  assigned_id,
  driver_name,
  email,
  ranking_included,
  ranking_group,
  month_start_date,
  month_end_date,
  trips_count,
  total_distance_meters,
  total_kilometers,
  eco_driving_score_total,
  previous_month_score,
  CASE
    WHEN eco_driving_score_total IS NULL OR previous_month_score IS NULL THEN NULL
    ELSE eco_driving_score_total - previous_month_score
  END AS score_delta_abs,
  CASE
    WHEN eco_driving_score_total IS NULL
      OR previous_month_score IS NULL
      OR previous_month_score = 0 THEN NULL
    ELSE ((eco_driving_score_total - previous_month_score) / abs(previous_month_score)) * 100
  END AS score_delta_pct,
  rolling_3_month_avg_score,
  rolling_6_month_avg_score,
  months_observed,
  best_month_score_to_date,
  worst_month_score_to_date,
  previous_month_kilometers,
  CASE
    WHEN total_kilometers IS NULL OR previous_month_kilometers IS NULL THEN NULL
    ELSE total_kilometers - previous_month_kilometers
  END AS kilometers_delta_abs,
  ranking_position,
  previous_ranking_position,
  CASE
    WHEN ranking_position IS NULL OR previous_ranking_position IS NULL THEN NULL
    ELSE previous_ranking_position - ranking_position
  END AS ranking_position_delta,
  ranking_total_participants,
  qualification_status,
  calculation_status
FROM trend_base;

COMMENT ON VIEW public.eco_driver_monthly_trends_view IS
  'Eco Driving month-over-month trends over independently calculated monthly stats rows. Previous/delta fields compare each month with the previous chronological month for the same client and assigned_id.';
