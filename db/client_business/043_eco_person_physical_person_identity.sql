-- 043_eco_person_physical_person_identity.sql
-- BRAVO00016 Eco Driving Person: textual source aliases grouped by physical person.
--
-- This migration intentionally does not convert obsolete UUID identities. It is
-- fail-closed unless all isolated Eco Person configuration/runtime tables are empty.

DO $$
DECLARE
  table_name TEXT;
  row_count BIGINT;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'eco_person_people',
    'eco_person_driver_mappings',
    'eco_person_trip_assignments',
    'eco_person_weekly_stats',
    'eco_person_monthly_stats',
    'eco_person_weekly_email_send_log',
    'eco_person_monthly_email_send_log'
  ]
  LOOP
    EXECUTE format('SELECT count(*) FROM public.%I', table_name) INTO row_count;
    IF row_count <> 0 THEN
      RAISE EXCEPTION '043 requires empty public.% (found % rows)', table_name, row_count;
    END IF;
  END LOOP;
END $$;

DROP VIEW IF EXISTS public.eco_person_weekly_trends_view;
DROP VIEW IF EXISTS public.eco_person_monthly_trends_view;
DROP VIEW IF EXISTS public.eco_person_people_email_view;
DROP VIEW IF EXISTS public.eco_person_driver_mappings_view;

CREATE OR REPLACE FUNCTION public.eco_person_normalize_source_identity(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  SELECT NULLIF(
    regexp_replace(normalize(lower(value), NFC), '[^[:alpha:][:digit:]]', '', 'g'),
    ''
  )
$$;

CREATE OR REPLACE FUNCTION public.eco_person_canonical_person_name(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  SELECT NULLIF(
    regexp_replace(normalize(btrim(value), NFC), '[[:space:]]+', ' ', 'g'),
    ''
  )
$$;

CREATE OR REPLACE FUNCTION public.eco_person_person_name_group_key(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  SELECT lower(public.eco_person_canonical_person_name(value))
$$;

-- Keep the old function name only as a compatibility alias. Runtime matching
-- uses eco_person_normalize_source_identity and eco_person_people directly.
CREATE OR REPLACE FUNCTION public.eco_person_normalize_driver_name(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  SELECT public.eco_person_normalize_source_identity(value)
$$;

ALTER TABLE public.eco_person_trip_assignments
  DROP CONSTRAINT IF EXISTS fk_eco_person_trip_assignments_mapping,
  DROP CONSTRAINT IF EXISTS fk_eco_person_trip_assignments_person,
  DROP CONSTRAINT IF EXISTS chk_eco_person_trip_assignments_assigned,
  DROP CONSTRAINT IF EXISTS chk_eco_person_trip_assignments_assignment_source,
  DROP CONSTRAINT IF EXISTS chk_eco_person_trip_assignments_exclusion_reason;

ALTER TABLE public.eco_person_weekly_email_send_log
  DROP CONSTRAINT IF EXISTS fk_eco_person_weekly_email_send_log_person,
  DROP CONSTRAINT IF EXISTS fk_eco_person_weekly_email_send_log_stats;

ALTER TABLE public.eco_person_monthly_email_send_log
  DROP CONSTRAINT IF EXISTS fk_eco_person_monthly_email_send_log_person,
  DROP CONSTRAINT IF EXISTS fk_eco_person_monthly_email_send_log_stats;

ALTER TABLE public.eco_person_weekly_stats
  DROP CONSTRAINT IF EXISTS fk_eco_person_weekly_stats_person,
  DROP CONSTRAINT IF EXISTS eco_person_weekly_stats_pkey;

ALTER TABLE public.eco_person_monthly_stats
  DROP CONSTRAINT IF EXISTS fk_eco_person_monthly_stats_person,
  DROP CONSTRAINT IF EXISTS uq_eco_person_monthly_stats_period,
  DROP CONSTRAINT IF EXISTS eco_person_monthly_stats_pkey;

ALTER TABLE public.eco_person_weekly_email_send_log
  DROP CONSTRAINT IF EXISTS fk_eco_person_weekly_email_send_log_person,
  DROP CONSTRAINT IF EXISTS fk_eco_person_weekly_email_send_log_stats;

ALTER TABLE public.eco_person_monthly_email_send_log
  DROP CONSTRAINT IF EXISTS fk_eco_person_monthly_email_send_log_person,
  DROP CONSTRAINT IF EXISTS fk_eco_person_monthly_email_send_log_stats;

DROP INDEX IF EXISTS public.uq_eco_person_people_identity_email;
DROP INDEX IF EXISTS public.idx_eco_person_trip_assignments_assigned_id;
DROP INDEX IF EXISTS public.idx_eco_person_trip_assignments_normalized_driver_name;
DROP INDEX IF EXISTS public.idx_eco_person_weekly_email_send_log_assigned_period;
DROP INDEX IF EXISTS public.idx_eco_person_monthly_email_send_log_assigned_period;
DROP INDEX IF EXISTS public.uq_eco_person_weekly_email_send_log_normal_idempotency;
DROP INDEX IF EXISTS public.uq_eco_person_monthly_email_send_log_normal_idempotency;

ALTER TABLE public.eco_person_driver_mappings
  DROP CONSTRAINT IF EXISTS fk_eco_person_driver_mappings_person,
  DROP CONSTRAINT IF EXISTS uq_eco_person_driver_mappings_person_alias;

ALTER TABLE public.eco_person_people
  DROP CONSTRAINT IF EXISTS eco_person_people_pkey,
  ALTER COLUMN person_id DROP DEFAULT,
  ALTER COLUMN person_id TYPE TEXT USING person_id::text,
  ADD COLUMN person_id_match_key TEXT NOT NULL,
  ADD COLUMN person_name_group_key TEXT NOT NULL,
  ADD CONSTRAINT eco_person_people_pkey PRIMARY KEY (client_id, person_id),
  ADD CONSTRAINT uq_eco_person_people_source_match_key
    UNIQUE (client_id, person_id_match_key),
  ADD CONSTRAINT uq_eco_person_people_source_lineage
    UNIQUE (client_id, person_id_match_key, person_id),
  ADD CONSTRAINT chk_eco_person_people_person_id CHECK (btrim(person_id) <> ''),
  ADD CONSTRAINT chk_eco_person_people_match_key CHECK (
    person_id_match_key = public.eco_person_normalize_source_identity(person_id)
  ),
  ADD CONSTRAINT chk_eco_person_people_person_name_group_key CHECK (
    person_name_group_key = public.eco_person_person_name_group_key(person_name)
  );

ALTER TABLE public.eco_person_driver_mappings
  ALTER COLUMN person_id TYPE TEXT USING person_id::text,
  ADD CONSTRAINT fk_eco_person_driver_mappings_person
    FOREIGN KEY (client_id, person_id)
    REFERENCES public.eco_person_people (client_id, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  ADD CONSTRAINT uq_eco_person_driver_mappings_person_alias
    UNIQUE (client_id, person_id, normalized_driver_name);

CREATE OR REPLACE FUNCTION public.eco_person_people_identity_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.person_id := normalize(btrim(NEW.person_id), NFC);
  NEW.person_id_match_key := public.eco_person_normalize_source_identity(NEW.person_id);
  NEW.person_name := public.eco_person_canonical_person_name(NEW.person_name);
  NEW.person_name_group_key := public.eco_person_person_name_group_key(NEW.person_name);
  IF NEW.person_id_match_key IS NULL THEN
    RAISE EXCEPTION 'eco_person_people.person_id has an empty normalized identity';
  END IF;
  IF NEW.person_name_group_key IS NULL THEN
    RAISE EXCEPTION 'eco_person_people.person_name has an empty physical-person key';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_eco_person_people_identity ON public.eco_person_people;
CREATE TRIGGER trg_eco_person_people_identity
BEFORE INSERT OR UPDATE ON public.eco_person_people
FOR EACH ROW EXECUTE FUNCTION public.eco_person_people_identity_trigger();

CREATE OR REPLACE FUNCTION public.eco_person_people_group_consistency_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.eco_person_people p
    WHERE p.client_id = NEW.client_id
      AND p.person_name_group_key = NEW.person_name_group_key
    GROUP BY p.client_id, p.person_name_group_key
    HAVING count(DISTINCT lower(btrim(p.email))) FILTER (WHERE p.email IS NOT NULL) > 1
       OR (count(*) FILTER (WHERE p.email IS NULL) > 0
           AND count(*) FILTER (WHERE p.email IS NOT NULL) > 0)
       OR count(DISTINCT p.ranking_included) > 1
       OR count(DISTINCT p.is_active) > 1
  ) THEN
    RAISE EXCEPTION 'inconsistent physical-person contact/configuration group: %',
      NEW.person_name_group_key;
  END IF;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_eco_person_people_group_consistency ON public.eco_person_people;
CREATE CONSTRAINT TRIGGER trg_eco_person_people_group_consistency
AFTER INSERT OR UPDATE ON public.eco_person_people
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.eco_person_people_group_consistency_trigger();

ALTER TABLE public.eco_person_trip_assignments
  DROP COLUMN assigned_id,
  DROP COLUMN mapping_id,
  DROP COLUMN normalized_driver_name,
  ADD COLUMN source_person_id TEXT NULL,
  ADD COLUMN source_person_id_match_key TEXT NULL,
  ADD COLUMN person_name TEXT NULL,
  ADD COLUMN person_name_group_key TEXT NULL,
  ADD CONSTRAINT chk_eco_person_trip_assignments_assignment_source CHECK (
    assignment_source IN (
      'PERSON_ID_MATCH',
      'UNMAPPED_DRIVER_NAME',
      'SKIPPED_NO_DRIVER_NAME',
      'INVALID_AMBIGUOUS_MAPPING'
    )
  ),
  ADD CONSTRAINT chk_eco_person_trip_assignments_identity_outcome CHECK (
    (
      assignment_source = 'PERSON_ID_MATCH'
      AND source_person_id IS NOT NULL
      AND source_person_id_match_key IS NOT NULL
      AND person_name IS NOT NULL
      AND person_name_group_key IS NOT NULL
      AND aggregation_included IS TRUE
      AND exclusion_reason IS NULL
    )
    OR (
      assignment_source <> 'PERSON_ID_MATCH'
      AND source_person_id IS NULL
      AND person_name_group_key IS NULL
      AND aggregation_included IS FALSE
    )
  ),
  ADD CONSTRAINT chk_eco_person_trip_assignments_exclusion_reason CHECK (
    exclusion_reason IS NULL
    OR exclusion_reason IN (
      'UNMAPPED_DRIVER_NAME',
      'SKIPPED_NO_DRIVER_NAME',
      'INVALID_AMBIGUOUS_MAPPING'
    )
  ),
  ADD CONSTRAINT fk_eco_person_trip_assignments_source_person
    FOREIGN KEY (client_id, source_person_id_match_key, source_person_id)
    REFERENCES public.eco_person_people (client_id, person_id_match_key, person_id)
    ON UPDATE CASCADE ON DELETE RESTRICT;

CREATE INDEX idx_eco_person_trip_assignments_person_group
  ON public.eco_person_trip_assignments (client_id, person_name_group_key)
  WHERE person_name_group_key IS NOT NULL;
CREATE INDEX idx_eco_person_trip_assignments_source_match
  ON public.eco_person_trip_assignments (client_id, source_person_id_match_key);

ALTER TABLE public.eco_person_weekly_stats
  DROP COLUMN assigned_id,
  ADD COLUMN person_name_group_key TEXT NOT NULL,
  ADD COLUMN person_name TEXT NOT NULL,
  ADD CONSTRAINT eco_person_weekly_stats_pkey
    PRIMARY KEY (client_id, person_name_group_key, period_start_date, period_end_date);

ALTER TABLE public.eco_person_monthly_stats
  DROP COLUMN assigned_id,
  ADD COLUMN person_name_group_key TEXT NOT NULL,
  ADD COLUMN person_name TEXT NOT NULL,
  ADD CONSTRAINT eco_person_monthly_stats_pkey
    PRIMARY KEY (client_id, person_name_group_key, month_start_date),
  ADD CONSTRAINT uq_eco_person_monthly_stats_period
    UNIQUE (client_id, person_name_group_key, month_start_date, month_end_date);

ALTER TABLE public.eco_person_weekly_email_send_log
  DROP COLUMN assigned_id,
  ADD COLUMN person_name_group_key TEXT NOT NULL,
  ADD COLUMN person_name TEXT NOT NULL,
  ADD CONSTRAINT fk_eco_person_weekly_email_send_log_stats
    FOREIGN KEY (client_id, person_name_group_key, period_start_date, period_end_date)
    REFERENCES public.eco_person_weekly_stats
      (client_id, person_name_group_key, period_start_date, period_end_date)
    ON UPDATE CASCADE ON DELETE RESTRICT;

ALTER TABLE public.eco_person_monthly_email_send_log
  DROP COLUMN assigned_id,
  ADD COLUMN person_name_group_key TEXT NOT NULL,
  ADD COLUMN person_name TEXT NOT NULL,
  ADD CONSTRAINT fk_eco_person_monthly_email_send_log_stats
    FOREIGN KEY (client_id, person_name_group_key, period_start_date, period_end_date)
    REFERENCES public.eco_person_monthly_stats
      (client_id, person_name_group_key, month_start_date, month_end_date)
    ON UPDATE CASCADE ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_eco_person_weekly_email_send_log_normal_identity
  ON public.eco_person_weekly_email_send_log (
    client_id, person_name_group_key, period_start_date, period_end_date, template_type
  )
  WHERE send_scope = 'normal' AND status IN ('pending', 'sent');
CREATE UNIQUE INDEX uq_eco_person_weekly_email_send_log_normal_idempotency
  ON public.eco_person_weekly_email_send_log (idempotency_key)
  WHERE send_scope = 'normal' AND status IN ('pending', 'sent');

CREATE UNIQUE INDEX uq_eco_person_monthly_email_send_log_normal_identity
  ON public.eco_person_monthly_email_send_log (
    client_id, person_name_group_key, period_start_date, period_end_date, template_type
  )
  WHERE send_scope = 'normal' AND status IN ('pending', 'sent');
CREATE UNIQUE INDEX uq_eco_person_monthly_email_send_log_normal_idempotency
  ON public.eco_person_monthly_email_send_log (idempotency_key)
  WHERE send_scope = 'normal' AND status IN ('pending', 'sent');

CREATE OR REPLACE VIEW public.eco_person_people_email_view AS
SELECT
  client_id,
  person_name_group_key,
  min(person_name COLLATE "C") AS person_name,
  min(email COLLATE "C") AS email,
  bool_and(ranking_included) AS ranking_included,
  bool_and(is_active) AS is_active,
  count(*)::integer AS source_identity_count
FROM public.eco_person_people
GROUP BY client_id, person_name_group_key;

CREATE OR REPLACE VIEW public.eco_person_driver_mappings_view AS
SELECT
  client_id,
  person_id AS source_person_id,
  person_id_match_key,
  person_name,
  person_name_group_key,
  email,
  ranking_included,
  is_active
FROM public.eco_person_people;

CREATE OR REPLACE VIEW public.eco_person_weekly_trends_view AS
SELECT s.*, p.email, p.is_active, p.source_identity_count
FROM public.eco_person_weekly_stats s
LEFT JOIN public.eco_person_people_email_view p
  ON p.client_id = s.client_id
 AND p.person_name_group_key = s.person_name_group_key;

CREATE OR REPLACE VIEW public.eco_person_monthly_trends_view AS
SELECT s.*, p.email, p.is_active, p.source_identity_count
FROM public.eco_person_monthly_stats s
LEFT JOIN public.eco_person_people_email_view p
  ON p.client_id = s.client_id
 AND p.person_name_group_key = s.person_name_group_key;

COMMENT ON COLUMN public.eco_person_people.person_id IS
  'Textual provider source identity corresponding to client_trips.driver_name; not an application UUID.';
COMMENT ON COLUMN public.eco_person_people.person_name IS
  'Canonical human-readable physical driver name. Multiple source person_id aliases may share one group.';
COMMENT ON TABLE public.eco_person_driver_mappings IS
  'Deprecated compatibility table. Eco Person runtime matching is authoritative from eco_person_people.person_id.';

DO $$
DECLARE
  grant_row RECORD;
BEGIN
  FOR grant_row IN
    SELECT DISTINCT grantee
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public'
      AND table_name = 'client_trips'
      AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
  LOOP
    EXECUTE format(
      'GRANT SELECT ON TABLE public.eco_person_people_email_view, '
      'public.eco_person_driver_mappings_view, '
      'public.eco_person_weekly_trends_view, '
      'public.eco_person_monthly_trends_view TO %I',
      grant_row.grantee
    );
    EXECUTE format(
      'GRANT EXECUTE ON FUNCTION public.eco_person_normalize_source_identity(TEXT), '
      'public.eco_person_canonical_person_name(TEXT), '
      'public.eco_person_person_name_group_key(TEXT), '
      'public.eco_person_people_identity_trigger(), '
      'public.eco_person_people_group_consistency_trigger() TO %I',
      grant_row.grantee
    );
  END LOOP;
END $$;
