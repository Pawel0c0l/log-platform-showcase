-- 044_eco_email_fail_closed_idempotency.sql
-- Fail-closed Eco Driving email scopes and template-independent normal identity.
-- Every table-specific statement is guarded because client schemas are heterogeneous.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- ALPHA driver logs gain explicit scopes and reservation/audit columns. All
-- changes roll back if the later conflict gate rejects historical data.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['eco_driving_weekly_email_send_log','eco_driving_monthly_email_send_log'] LOOP
    IF to_regclass('public.' || t) IS NULL THEN
      RAISE NOTICE 'ECO_EMAIL_TABLE_NOT_APPLICABLE: model=driver table=%', t;
      CONTINUE;
    END IF;
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS send_scope text', t);
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS idempotency_key text', t);
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS parent_send_log_id uuid', t);
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS force_resend_reason text', t);
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS force_resend_at timestamptz', t);
    EXECUTE format($q$
      UPDATE public.%I SET send_scope = CASE
        WHEN metadata_json->>'force_resend' = 'true' THEN 'forced'
        WHEN NULLIF(btrim(metadata_json->>'test_recipient_email'),'') IS NOT NULL THEN 'test'
        WHEN status = 'dry_run_rendered' THEN 'render_only'
        WHEN status LIKE 'skipped_%%' OR status = 'failed' THEN 'skipped'
        ELSE 'normal' END WHERE send_scope IS NULL$q$, t);
    EXECUTE format($q$
      UPDATE public.%I
      SET force_resend_reason = COALESCE(NULLIF(btrim(force_resend_reason),''),
            'legacy force_resend metadata; reason unavailable'),
          force_resend_at = COALESCE(force_resend_at,attempted_at)
      WHERE send_scope='forced'$q$, t);
    EXECUTE format('ALTER TABLE public.%I ALTER COLUMN send_scope SET DEFAULT ''normal''', t);
    EXECUTE format('ALTER TABLE public.%I ALTER COLUMN send_scope SET NOT NULL', t);
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, 'chk_'||t||'_send_scope');
    EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (send_scope IN (''normal'',''test'',''forced'',''render_only'',''skipped''))', t, 'chk_'||t||'_send_scope');
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, 'chk_'||t||'_force_reason');
    EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK ((send_scope=''forced'' AND force_resend_reason IS NOT NULL AND btrim(force_resend_reason)<>'''' AND force_resend_at IS NOT NULL) OR (send_scope<>''forced'' AND force_resend_at IS NULL))', t, 'chk_'||t||'_force_reason');
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, 'fk_'||t||'_parent');
    EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (parent_send_log_id) REFERENCES public.%I(send_log_id) ON DELETE SET NULL', t, 'fk_'||t||'_parent', t);
  END LOOP;
END $$;

-- Abort before rewriting stable keys or replacing indexes. Diagnostics omit
-- recipient addresses and rendered content.
DO $$
DECLARE r record; conflict_details jsonb;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('driver','eco_driving_weekly_email_send_log','assigned_id'),
    ('driver','eco_driving_monthly_email_send_log','assigned_id'),
    ('person','eco_person_weekly_email_send_log','person_name_group_key'),
    ('person','eco_person_monthly_email_send_log','person_name_group_key')
  ) AS supported(model_name,table_name,subject_column) LOOP
    IF to_regclass('public.' || r.table_name) IS NULL THEN
      RAISE NOTICE 'ECO_EMAIL_TABLE_NOT_APPLICABLE: model=% table=%', r.model_name, r.table_name;
      CONTINUE;
    END IF;
    EXECUTE format($q$
      SELECT jsonb_agg(to_jsonb(conflict_group) ORDER BY client_id,subject_identity,report_type,period_start_date,period_end_date)
      FROM (
        SELECT client_id::text AS client_id, %1$I::text AS subject_identity,
          report_type, period_start_date, period_end_date, count(*) AS row_count,
          array_agg(DISTINCT status ORDER BY status) AS statuses,
          array_agg(DISTINCT send_scope ORDER BY send_scope) AS scopes,
          array_agg(DISTINCT COALESCE(template_type,'<null>') ORDER BY COALESCE(template_type,'<null>')) AS template_types,
          array_agg(send_log_id::text ORDER BY send_log_id::text) AS row_ids
        FROM public.%2$I
        WHERE send_scope='normal' AND status IN ('pending','sent')
        GROUP BY client_id,%1$I,report_type,period_start_date,period_end_date
        HAVING count(*)>1
      ) conflict_group$q$, r.subject_column, r.table_name) INTO conflict_details;
    IF conflict_details IS NOT NULL THEN
      RAISE EXCEPTION 'ECO_EMAIL_IDEMPOTENCY_CONFLICT: model=% table=% details=%',
        r.model_name, r.table_name, conflict_details;
    END IF;
  END LOOP;
END $$;

-- Stable normal keys exclude template, rating, ranking, and rendered output.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('eco_driving_weekly_email_send_log','assigned_id','eco_driver'),
    ('eco_driving_monthly_email_send_log','assigned_id','eco_driver'),
    ('eco_person_weekly_email_send_log','person_name_group_key','eco_person'),
    ('eco_person_monthly_email_send_log','person_name_group_key','eco_person')
  ) AS supported(table_name,subject_column,key_namespace) LOOP
    IF to_regclass('public.' || r.table_name) IS NULL THEN CONTINUE; END IF;
    EXECUTE format($q$
      UPDATE public.%1$I
      SET idempotency_key=concat_ws('|',%2$L,report_type,client_id::text,%3$I,period_start_date::text,period_end_date::text)
      WHERE send_scope='normal' AND status IN ('pending','sent')
        AND idempotency_key IS DISTINCT FROM concat_ws('|',%2$L,report_type,client_id::text,%3$I,period_start_date::text,period_end_date::text)$q$,
      r.table_name, r.key_namespace, r.subject_column);
  END LOOP;
END $$;

-- Replace indexes only for present tables. Identifiers come from this static list.
DO $$
DECLARE
  r record; identity_index text; idempotency_index text; legacy_index text;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('driver','eco_driving_weekly_email_send_log','assigned_id'),
    ('driver','eco_driving_monthly_email_send_log','assigned_id'),
    ('person','eco_person_weekly_email_send_log','person_name_group_key'),
    ('person','eco_person_monthly_email_send_log','person_name_group_key')
  ) AS supported(model_name,table_name,subject_column) LOOP
    IF to_regclass('public.' || r.table_name) IS NULL THEN CONTINUE; END IF;
    identity_index := 'uq_'||r.table_name||'_normal_identity';
    idempotency_index := 'uq_'||r.table_name||'_normal_idempotency';
    legacy_index := 'uq_'||r.table_name||'_sent_once';
    EXECUTE format('DROP INDEX IF EXISTS public.%I', legacy_index);
    EXECUTE format('DROP INDEX IF EXISTS public.%I', identity_index);
    EXECUTE format('DROP INDEX IF EXISTS public.%I', idempotency_index);
    EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I (client_id,%I,report_type,period_start_date,period_end_date) WHERE send_scope=''normal'' AND status IN (''pending'',''sent'')', identity_index,r.table_name,r.subject_column);
    EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I (idempotency_key) WHERE send_scope=''normal'' AND status IN (''pending'',''sent'')', idempotency_index,r.table_name);
    EXECUTE format('COMMENT ON INDEX public.%I IS %L', identity_index,
      format('One normal pending/sent %s email per client, subject, report type, and half-open reporting period; template/rating do not affect identity.',r.model_name));
  END LOOP;
END $$;
