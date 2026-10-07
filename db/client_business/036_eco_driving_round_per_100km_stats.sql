-- 036_eco_driving_round_per_100km_stats.sql
-- Round persisted Eco Driving per-100km stats to whole-number values.
-- Column types intentionally remain NUMERIC to avoid a risky type rewrite on
-- existing client databases; this migration only normalizes stored values.

UPDATE public.eco_driver_weekly_stats
SET
  overrev_events_per_100km = ROUND(overrev_events_per_100km),
  harsh_braking_events_per_100km = ROUND(harsh_braking_events_per_100km),
  harsh_acceleration_events_per_100km = ROUND(harsh_acceleration_events_per_100km),
  harsh_turning_events_per_100km = ROUND(harsh_turning_events_per_100km),
  idle_events_per_100km = ROUND(idle_events_per_100km),
  speeding_140_160_events_per_100km = ROUND(speeding_140_160_events_per_100km),
  speeding_160_170_events_per_100km = ROUND(speeding_160_170_events_per_100km),
  speeding_170_plus_events_per_100km = ROUND(speeding_170_plus_events_per_100km)
WHERE overrev_events_per_100km IS NOT NULL
  OR harsh_braking_events_per_100km IS NOT NULL
  OR harsh_acceleration_events_per_100km IS NOT NULL
  OR harsh_turning_events_per_100km IS NOT NULL
  OR idle_events_per_100km IS NOT NULL
  OR speeding_140_160_events_per_100km IS NOT NULL
  OR speeding_160_170_events_per_100km IS NOT NULL
  OR speeding_170_plus_events_per_100km IS NOT NULL;

UPDATE public.eco_driver_monthly_stats
SET
  overrev_events_per_100km = ROUND(overrev_events_per_100km),
  harsh_braking_events_per_100km = ROUND(harsh_braking_events_per_100km),
  harsh_acceleration_events_per_100km = ROUND(harsh_acceleration_events_per_100km),
  harsh_turning_events_per_100km = ROUND(harsh_turning_events_per_100km),
  idle_events_per_100km = ROUND(idle_events_per_100km),
  speeding_140_160_events_per_100km = ROUND(speeding_140_160_events_per_100km),
  speeding_160_170_events_per_100km = ROUND(speeding_160_170_events_per_100km),
  speeding_170_plus_events_per_100km = ROUND(speeding_170_plus_events_per_100km)
WHERE overrev_events_per_100km IS NOT NULL
  OR harsh_braking_events_per_100km IS NOT NULL
  OR harsh_acceleration_events_per_100km IS NOT NULL
  OR harsh_turning_events_per_100km IS NOT NULL
  OR idle_events_per_100km IS NOT NULL
  OR speeding_140_160_events_per_100km IS NOT NULL
  OR speeding_160_170_events_per_100km IS NOT NULL
  OR speeding_170_plus_events_per_100km IS NOT NULL;
