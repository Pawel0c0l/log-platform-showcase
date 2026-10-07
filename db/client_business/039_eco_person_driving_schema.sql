-- 039_eco_person_driving_schema.sql
-- Workflow A - isolated Eco Driving Person schema for driver_name to real-person aggregation.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION public.eco_person_normalize_driver_name(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT NULLIF(lower(regexp_replace(btrim(value), '\s+', ' ', 'g')), '')
$$;

CREATE TABLE IF NOT EXISTS public.eco_person_people (
  client_id UUID NOT NULL,
  person_id UUID NOT NULL DEFAULT gen_random_uuid(),
  person_name TEXT NOT NULL,
  email TEXT NULL,
  ranking_included BOOLEAN NOT NULL DEFAULT true,
  is_active BOOLEAN NOT NULL DEFAULT true,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, person_id),
  CONSTRAINT chk_eco_person_people_person_name CHECK (btrim(person_name) <> ''),
  CONSTRAINT chk_eco_person_people_email CHECK (email IS NULL OR btrim(email) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_person_people_identity_email
  ON public.eco_person_people (
    client_id,
    public.eco_person_normalize_driver_name(person_name),
    public.eco_person_normalize_driver_name(email)
  )
  WHERE email IS NOT NULL AND btrim(email) <> '';

CREATE INDEX IF NOT EXISTS idx_eco_person_people_email
  ON public.eco_person_people (email)
  WHERE email IS NOT NULL AND btrim(email) <> '';

CREATE INDEX IF NOT EXISTS idx_eco_person_people_active
  ON public.eco_person_people (client_id, is_active);

CREATE TABLE IF NOT EXISTS public.eco_person_driver_mappings (
  mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  person_id UUID NOT NULL,
  driver_name TEXT NOT NULL,
  normalized_driver_name TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_eco_person_driver_mappings_person
    FOREIGN KEY (client_id, person_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT chk_eco_person_driver_mappings_driver_name CHECK (btrim(driver_name) <> ''),
  CONSTRAINT chk_eco_person_driver_mappings_normalized CHECK (btrim(normalized_driver_name) <> ''),
  CONSTRAINT uq_eco_person_driver_mappings_person_alias
    UNIQUE (client_id, person_id, normalized_driver_name)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_person_driver_mappings_client_person_mapping
  ON public.eco_person_driver_mappings (client_id, person_id, mapping_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_person_driver_mappings_active_alias
  ON public.eco_person_driver_mappings (client_id, normalized_driver_name)
  WHERE is_active IS TRUE;

CREATE INDEX IF NOT EXISTS idx_eco_person_driver_mappings_person
  ON public.eco_person_driver_mappings (client_id, person_id);

CREATE INDEX IF NOT EXISTS idx_eco_person_driver_mappings_lookup
  ON public.eco_person_driver_mappings (client_id, normalized_driver_name, is_active);

CREATE OR REPLACE FUNCTION public.eco_person_driver_mappings_normalize_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.normalized_driver_name := public.eco_person_normalize_driver_name(NEW.driver_name);
  IF NEW.normalized_driver_name IS NULL THEN
    RAISE EXCEPTION 'eco_person_driver_mappings.driver_name must not be empty';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_eco_person_driver_mappings_normalize ON public.eco_person_driver_mappings;
CREATE TRIGGER trg_eco_person_driver_mappings_normalize
BEFORE INSERT OR UPDATE
ON public.eco_person_driver_mappings
FOR EACH ROW
EXECUTE FUNCTION public.eco_person_driver_mappings_normalize_trigger();

CREATE OR REPLACE VIEW public.eco_person_driver_mappings_view AS
SELECT
  p.client_id,
  p.person_id,
  p.person_name,
  p.email,
  m.mapping_id,
  m.driver_name,
  m.normalized_driver_name,
  p.ranking_included,
  p.is_active AS person_is_active,
  m.is_active AS mapping_is_active,
  (p.is_active AND m.is_active) AS is_active,
  m.created_at,
  m.updated_at
FROM public.eco_person_driver_mappings m
JOIN public.eco_person_people p
  ON p.client_id = m.client_id
 AND p.person_id = m.person_id;

CREATE OR REPLACE VIEW public.eco_person_people_email_view AS
SELECT
  client_id,
  person_id,
  person_id AS driver_id,
  person_name,
  person_name AS driver_name,
  email,
  ranking_included,
  is_active,
  metadata_json,
  created_at,
  updated_at
FROM public.eco_person_people;

CREATE TABLE IF NOT EXISTS public.eco_person_trip_assignments (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  provider_trip_id INTEGER NOT NULL,
  record_id UUID NULL,
  assigned_id UUID NULL,
  assignment_source TEXT NOT NULL,
  mapping_id UUID NULL,
  driver_name_raw TEXT NULL,
  normalized_driver_name TEXT NULL,
  driver_tag_description TEXT NULL,
  trip_mode TEXT NULL,
  is_private_trip BOOLEAN NOT NULL DEFAULT false,
  exclusion_reason TEXT NULL,
  aggregation_included BOOLEAN NOT NULL DEFAULT false,
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
  CONSTRAINT chk_eco_person_trip_assignments_assignment_source
    CHECK (assignment_source IN ('DRIVER_NAME_MAP', 'UNMAPPED_DRIVER_NAME', 'SKIPPED_NO_DRIVER_NAME')),
  CONSTRAINT chk_eco_person_trip_assignments_assigned
    CHECK (
      (
        assignment_source = 'DRIVER_NAME_MAP'
        AND assigned_id IS NOT NULL
        AND mapping_id IS NOT NULL
        AND normalized_driver_name IS NOT NULL
        AND aggregation_included IS TRUE
        AND exclusion_reason IS NULL
      )
      OR (
        assignment_source = 'UNMAPPED_DRIVER_NAME'
        AND assigned_id IS NULL
        AND mapping_id IS NULL
        AND normalized_driver_name IS NOT NULL
        AND aggregation_included IS FALSE
      )
      OR (
        assignment_source = 'SKIPPED_NO_DRIVER_NAME'
        AND assigned_id IS NULL
        AND mapping_id IS NULL
        AND normalized_driver_name IS NULL
        AND aggregation_included IS FALSE
      )
    ),
  CONSTRAINT chk_eco_person_trip_assignments_exclusion_reason
    CHECK (exclusion_reason IS NULL OR exclusion_reason IN ('UNMAPPED_DRIVER_NAME', 'SKIPPED_NO_DRIVER_NAME')),
  CONSTRAINT chk_eco_person_trip_assignments_business_week
    CHECK (
      business_week_end_date = business_week_start_date + 7
      AND EXTRACT(ISODOW FROM business_week_start_date) = 1
      AND EXTRACT(ISODOW FROM business_week_end_date) = 1
    ),
  CONSTRAINT fk_eco_person_trip_assignments_person
    FOREIGN KEY (client_id, assigned_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_eco_person_trip_assignments_mapping
    FOREIGN KEY (client_id, assigned_id, mapping_id)
    REFERENCES public.eco_person_driver_mappings (client_id, person_id, mapping_id)
    ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_eco_person_trip_assignments_assigned_id
  ON public.eco_person_trip_assignments (client_id, assigned_id)
  WHERE assigned_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eco_person_trip_assignments_trip_start_ts
  ON public.eco_person_trip_assignments (trip_start_ts);
CREATE INDEX IF NOT EXISTS idx_eco_person_trip_assignments_assignment_source
  ON public.eco_person_trip_assignments (client_id, assignment_source);
CREATE INDEX IF NOT EXISTS idx_eco_person_trip_assignments_normalized_driver_name
  ON public.eco_person_trip_assignments (client_id, normalized_driver_name);

CREATE TABLE IF NOT EXISTS public.eco_person_weekly_stats (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  assigned_id UUID NOT NULL,
  week_start_date DATE NOT NULL,
  week_end_date DATE NOT NULL,
  period_start_date DATE NOT NULL,
  period_end_date DATE NOT NULL,
  month_start_date DATE NOT NULL,
  period_sequence_in_month INTEGER NOT NULL,
  period_label TEXT NOT NULL,
  is_partial_period BOOLEAN NOT NULL DEFAULT false,
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

  overrev_points NUMERIC(12, 2) NULL,
  harsh_braking_points NUMERIC(12, 2) NULL,
  harsh_acceleration_points NUMERIC(12, 2) NULL,
  harsh_turning_points NUMERIC(12, 2) NULL,
  idle_points NUMERIC(12, 2) NULL,
  speeding_140_160_points NUMERIC(12, 2) NULL,
  speeding_160_170_points NUMERIC(12, 2) NULL,
  speeding_170_plus_points NUMERIC(12, 2) NULL,

  overrev_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_braking_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_acceleration_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_turning_maxpoints_subtract NUMERIC(12, 2) NULL,
  idle_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_140_160_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_160_170_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_170_plus_maxpoints_subtract NUMERIC(12, 2) NULL,

  top_1_validation TEXT NULL,
  top_2_validation TEXT NULL,
  ecodriving_rating_type TEXT NULL,
  ecodriving_rating_type_share_percent NUMERIC(7, 2) NULL,
  eco_driving_score_total NUMERIC(12, 2) NULL,
  qualification_status TEXT NOT NULL,
  calculation_status TEXT NOT NULL,
  ranking_included BOOLEAN NULL,
  ranking_group TEXT NOT NULL DEFAULT 'UNKNOWN_DRIVER',
  ranking_position INTEGER NULL,
  ranking_total_participants INTEGER NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, assigned_id, period_start_date, period_end_date),
  CONSTRAINT chk_eco_person_weekly_stats_period
    CHECK (
      period_end_date > period_start_date
      AND month_start_date = date_trunc('month', month_start_date::timestamp)::date
      AND period_start_date >= month_start_date
      AND period_start_date < (month_start_date + INTERVAL '1 month')::date
      AND period_end_date <= (month_start_date + INTERVAL '1 month')::date
      AND period_sequence_in_month >= 1
      AND btrim(period_label) <> ''
    ),
  CONSTRAINT chk_eco_person_weekly_stats_qualification_status
    CHECK (qualification_status IN ('QUALIFIED', 'LOW_DISTANCE', 'NO_DISTANCE')),
  CONSTRAINT chk_eco_person_weekly_stats_calculation_status
    CHECK (calculation_status IN ('OK', 'NO_ASSIGNED_ID', 'NO_DISTANCE', 'ERROR')),
  CONSTRAINT chk_eco_person_weekly_stats_ranking_group
    CHECK (ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER')),
  CONSTRAINT chk_eco_person_weekly_stats_ranking_positions
    CHECK (
      (ranking_position IS NULL OR ranking_position > 0)
      AND (ranking_total_participants IS NULL OR ranking_total_participants >= 0)
    ),
  CONSTRAINT chk_eco_person_weekly_stats_non_negative_counts
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
    ),
  CONSTRAINT fk_eco_person_weekly_stats_person
    FOREIGN KEY (client_id, assigned_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_stats_period_start
  ON public.eco_person_weekly_stats (client_id, period_start_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_stats_month_start
  ON public.eco_person_weekly_stats (client_id, month_start_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_stats_ranking_group
  ON public.eco_person_weekly_stats (client_id, ranking_group);

CREATE TABLE IF NOT EXISTS public.eco_person_monthly_stats (
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  assigned_id UUID NOT NULL,
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

  overrev_points NUMERIC(12, 2) NULL,
  harsh_braking_points NUMERIC(12, 2) NULL,
  harsh_acceleration_points NUMERIC(12, 2) NULL,
  harsh_turning_points NUMERIC(12, 2) NULL,
  idle_points NUMERIC(12, 2) NULL,
  speeding_140_160_points NUMERIC(12, 2) NULL,
  speeding_160_170_points NUMERIC(12, 2) NULL,
  speeding_170_plus_points NUMERIC(12, 2) NULL,

  overrev_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_braking_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_acceleration_maxpoints_subtract NUMERIC(12, 2) NULL,
  harsh_turning_maxpoints_subtract NUMERIC(12, 2) NULL,
  idle_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_140_160_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_160_170_maxpoints_subtract NUMERIC(12, 2) NULL,
  speeding_170_plus_maxpoints_subtract NUMERIC(12, 2) NULL,

  top_1_validation TEXT NULL,
  top_2_validation TEXT NULL,
  ecodriving_rating_type TEXT NULL,
  ecodriving_rating_type_share_percent NUMERIC(7, 2) NULL,
  eco_driving_score_total NUMERIC(12, 2) NULL,
  qualification_status TEXT NOT NULL,
  calculation_status TEXT NOT NULL,
  ranking_included BOOLEAN NULL,
  ranking_group TEXT NOT NULL DEFAULT 'UNKNOWN_DRIVER',
  ranking_position INTEGER NULL,
  ranking_total_participants INTEGER NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, assigned_id, month_start_date),
  CONSTRAINT chk_eco_person_monthly_stats_month
    CHECK (
      month_start_date = date_trunc('month', month_start_date::timestamp)::date
      AND month_end_date = (month_start_date + INTERVAL '1 month')::date
    ),
  CONSTRAINT chk_eco_person_monthly_stats_qualification_status
    CHECK (qualification_status IN ('QUALIFIED', 'LOW_DISTANCE', 'NO_DISTANCE')),
  CONSTRAINT chk_eco_person_monthly_stats_calculation_status
    CHECK (calculation_status IN ('OK', 'NO_ASSIGNED_ID', 'NO_DISTANCE', 'ERROR')),
  CONSTRAINT chk_eco_person_monthly_stats_ranking_group
    CHECK (ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER')),
  CONSTRAINT chk_eco_person_monthly_stats_ranking_positions
    CHECK (
      (ranking_position IS NULL OR ranking_position > 0)
      AND (ranking_total_participants IS NULL OR ranking_total_participants >= 0)
    ),
  CONSTRAINT chk_eco_person_monthly_stats_non_negative_counts
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
    ),
  CONSTRAINT fk_eco_person_monthly_stats_person
    FOREIGN KEY (client_id, assigned_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT uq_eco_person_monthly_stats_period
    UNIQUE (client_id, assigned_id, month_start_date, month_end_date)
);

CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_stats_month_start
  ON public.eco_person_monthly_stats (client_id, month_start_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_stats_ranking_group
  ON public.eco_person_monthly_stats (client_id, ranking_group);

CREATE OR REPLACE VIEW public.eco_person_weekly_trends_view AS
SELECT s.*, p.person_name, p.email
FROM public.eco_person_weekly_stats s
LEFT JOIN public.eco_person_people p
  ON p.client_id = s.client_id AND p.person_id = s.assigned_id;

CREATE OR REPLACE VIEW public.eco_person_monthly_trends_view AS
SELECT s.*, p.person_name, p.email
FROM public.eco_person_monthly_stats s
LEFT JOIN public.eco_person_people p
  ON p.client_id = s.client_id AND p.person_id = s.assigned_id;

CREATE TABLE IF NOT EXISTS public.eco_person_weekly_email_send_log (
  send_log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  run_id TEXT NULL,
  assigned_id UUID NOT NULL,
  recipient_email TEXT NOT NULL,
  original_recipient_email TEXT NULL,
  ranking_type TEXT NULL,
  report_type TEXT NOT NULL DEFAULT 'weekly',
  send_scope TEXT NOT NULL DEFAULT 'normal',
  idempotency_key TEXT NULL,
  parent_send_log_id UUID NULL,
  template_type TEXT NOT NULL,
  template_filename TEXT NOT NULL,
  qualification_status TEXT NULL,
  ranking_included BOOLEAN NULL,
  template_variant TEXT NULL,
  period_start_date DATE NOT NULL,
  period_end_date DATE NOT NULL,
  ecodriving_rating_type TEXT NOT NULL,
  email_subject TEXT NOT NULL,
  status TEXT NOT NULL,
  smtp_message_id TEXT NULL,
  provider_response TEXT NULL,
  error_message TEXT NULL,
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT chk_eco_person_weekly_email_send_log_report_type CHECK (report_type = 'weekly'),
  CONSTRAINT chk_eco_person_weekly_email_send_log_send_scope CHECK (send_scope IN ('normal','forced','test','dry_run','skipped')),
  CONSTRAINT chk_eco_person_weekly_email_send_log_idempotency_key CHECK (
    (send_scope = 'normal' AND idempotency_key IS NOT NULL AND btrim(idempotency_key) <> '')
    OR (send_scope <> 'normal')
  ),
  CONSTRAINT chk_eco_person_weekly_email_send_log_period CHECK (period_end_date > period_start_date),
  CONSTRAINT chk_eco_person_weekly_email_send_log_status CHECK (status IN ('pending','skipped','skipped_already_sent','skipped_existing_reservation','skipped_missing_email','skipped_unknown_rating_type','dry_run_rendered','sent','failed')),
  CONSTRAINT chk_eco_person_weekly_email_send_log_sent_at CHECK ((status = 'sent' AND sent_at IS NOT NULL) OR (status <> 'sent' AND sent_at IS NULL)),
  CONSTRAINT chk_eco_person_weekly_email_send_log_template_variant CHECK (template_variant IS NULL OR template_variant IN ('ranked','norank','low_distance')),
  CONSTRAINT fk_eco_person_weekly_email_send_log_person
    FOREIGN KEY (client_id, assigned_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_eco_person_weekly_email_send_log_stats
    FOREIGN KEY (client_id, assigned_id, period_start_date, period_end_date)
    REFERENCES public.eco_person_weekly_stats (client_id, assigned_id, period_start_date, period_end_date)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_eco_person_weekly_email_send_log_parent
    FOREIGN KEY (parent_send_log_id)
    REFERENCES public.eco_person_weekly_email_send_log (send_log_id)
    ON UPDATE CASCADE ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_person_weekly_email_send_log_normal_idempotency
  ON public.eco_person_weekly_email_send_log (idempotency_key)
  WHERE send_scope = 'normal' AND status IN ('pending','sent');

CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_send_log_client_period
  ON public.eco_person_weekly_email_send_log (client_id, period_start_date, period_end_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_send_log_assigned_period
  ON public.eco_person_weekly_email_send_log (client_id, assigned_id, period_start_date, period_end_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_send_log_idempotency
  ON public.eco_person_weekly_email_send_log (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eco_person_weekly_email_send_log_status
  ON public.eco_person_weekly_email_send_log (status, attempted_at);

CREATE TABLE IF NOT EXISTS public.eco_person_monthly_email_send_log (
  send_log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  run_id TEXT NULL,
  assigned_id UUID NOT NULL,
  recipient_email TEXT NOT NULL,
  original_recipient_email TEXT NULL,
  ranking_type TEXT NULL,
  report_type TEXT NOT NULL DEFAULT 'monthly',
  send_scope TEXT NOT NULL DEFAULT 'normal',
  idempotency_key TEXT NULL,
  parent_send_log_id UUID NULL,
  template_type TEXT NOT NULL,
  template_filename TEXT NOT NULL,
  qualification_status TEXT NULL,
  ranking_included BOOLEAN NULL,
  template_variant TEXT NULL,
  period_start_date DATE NOT NULL,
  period_end_date DATE NOT NULL,
  ecodriving_rating_type TEXT NOT NULL,
  email_subject TEXT NOT NULL,
  status TEXT NOT NULL,
  smtp_message_id TEXT NULL,
  provider_response TEXT NULL,
  error_message TEXT NULL,
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT chk_eco_person_monthly_email_send_log_report_type CHECK (report_type = 'monthly'),
  CONSTRAINT chk_eco_person_monthly_email_send_log_send_scope CHECK (send_scope IN ('normal','forced','test','dry_run','skipped')),
  CONSTRAINT chk_eco_person_monthly_email_send_log_idempotency_key CHECK (
    (send_scope = 'normal' AND idempotency_key IS NOT NULL AND btrim(idempotency_key) <> '')
    OR (send_scope <> 'normal')
  ),
  CONSTRAINT chk_eco_person_monthly_email_send_log_period CHECK (period_end_date > period_start_date),
  CONSTRAINT chk_eco_person_monthly_email_send_log_status CHECK (status IN ('pending','skipped','skipped_already_sent','skipped_existing_reservation','skipped_missing_email','skipped_unknown_rating_type','dry_run_rendered','sent','failed')),
  CONSTRAINT chk_eco_person_monthly_email_send_log_sent_at CHECK ((status = 'sent' AND sent_at IS NOT NULL) OR (status <> 'sent' AND sent_at IS NULL)),
  CONSTRAINT chk_eco_person_monthly_email_send_log_template_variant CHECK (template_variant IS NULL OR template_variant IN ('ranked','norank','low_distance')),
  CONSTRAINT fk_eco_person_monthly_email_send_log_person
    FOREIGN KEY (client_id, assigned_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_eco_person_monthly_email_send_log_stats
    FOREIGN KEY (client_id, assigned_id, period_start_date, period_end_date)
    REFERENCES public.eco_person_monthly_stats (client_id, assigned_id, month_start_date, month_end_date)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  CONSTRAINT fk_eco_person_monthly_email_send_log_parent
    FOREIGN KEY (parent_send_log_id)
    REFERENCES public.eco_person_monthly_email_send_log (send_log_id)
    ON UPDATE CASCADE ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_person_monthly_email_send_log_normal_idempotency
  ON public.eco_person_monthly_email_send_log (idempotency_key)
  WHERE send_scope = 'normal' AND status IN ('pending','sent');

CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_send_log_client_period
  ON public.eco_person_monthly_email_send_log (client_id, period_start_date, period_end_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_send_log_assigned_period
  ON public.eco_person_monthly_email_send_log (client_id, assigned_id, period_start_date, period_end_date);
CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_send_log_idempotency
  ON public.eco_person_monthly_email_send_log (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eco_person_monthly_email_send_log_status
  ON public.eco_person_monthly_email_send_log (status, attempted_at);

DO $$
DECLARE
    grant_row record;
    target_table text;
BEGIN
    FOR target_table IN
      SELECT unnest(ARRAY[
        'eco_person_people',
        'eco_person_driver_mappings',
        'eco_person_trip_assignments',
        'eco_person_weekly_stats',
        'eco_person_monthly_stats',
        'eco_person_weekly_email_send_log',
        'eco_person_monthly_email_send_log'
      ])
    LOOP
      FOR grant_row IN
          SELECT grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
          FROM (
              SELECT DISTINCT grantee, privilege_type
              FROM information_schema.role_table_grants
              WHERE table_schema = 'public'
                AND table_name = 'client_trips'
                AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
          ) AS grants
          GROUP BY grantee
      LOOP
          EXECUTE format('GRANT %s ON TABLE public.%I TO %I', grant_row.privileges, target_table, grant_row.grantee);
      END LOOP;
    END LOOP;
END $$;

COMMENT ON TABLE public.eco_person_people IS 'Real-person identity table for isolated Eco Driving Person aggregation.';
COMMENT ON TABLE public.eco_person_driver_mappings IS 'Driver-name alias mappings for Eco Driving Person aggregation. Active normalized aliases are unique per client.';
COMMENT ON TABLE public.eco_person_trip_assignments IS 'Per-trip assignment diagnostics for Eco Driving Person, including unmapped and missing driver names.';
COMMENT ON VIEW public.eco_person_driver_mappings_view IS 'Import/admin view exposing one person plus one driver_name alias per row.';
