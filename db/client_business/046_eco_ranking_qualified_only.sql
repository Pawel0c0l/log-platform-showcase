-- 046_eco_ranking_qualified_only.sql
-- Ranking eligibility becomes conditional on qualification.
--
-- New contract: only `qualification_status = 'QUALIFIED'` rows belong to a
-- ranking population. `LOW_DISTANCE` and `NO_DISTANCE` rows are outside every
-- ranking group, so `ranking_group` must be able to hold NULL, meaning
-- "not part of any ranking population" (distinct from `UNKNOWN_DRIVER`, which
-- stays reserved for QUALIFIED rows with no chart mapping).
--
-- This migration changes structure only — it relaxes `ranking_group` to allow
-- NULL and adds two constraints encoding the new contract. It intentionally
-- performs NO data backfill: historical rows keep the ranking values that were
-- correct under the previous contract until an operator re-runs the ordinary
-- aggregation jobs with `recalculate=true` for the periods that matter. That
-- recalculation is irreversible; see the WARNING at the end of this file.
--
-- Every table-specific statement is guarded because client schemas are
-- heterogeneous: driver tables exist only for ALPHA00001, person tables only
-- for BRAVO00016.
--
-- DEPLOYMENT ORDER: apply this file to every client business database BEFORE
-- deploying the aggregation job change. The new job writes NULL ranking groups,
-- so against an un-migrated database the whole aggregation transaction fails
-- (fail-closed, no partial rows) until the migration lands. The reverse order is
-- safe: the old job always supplies an explicit non-NULL value.
--
-- `SET LOCAL` below is effective under scripts/apply_client_business_migrations.py,
-- which runs each file inside a transaction. scripts/onboard_workflow_a_client.py
-- connects with autocommit, so these two statements are ignored there with a
-- warning; that path only ever targets a fresh, empty database. Do not add
-- explicit BEGIN/COMMIT here — it would break the migration runner.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
DECLARE
  t text;
  c text;
  already_validated boolean;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'eco_driver_weekly_stats',
    'eco_driver_monthly_stats',
    'eco_person_weekly_stats',
    'eco_person_monthly_stats'
  ] LOOP
    IF to_regclass('public.' || t) IS NULL THEN
      RAISE NOTICE 'ECO_RANKING_TABLE_NOT_APPLICABLE: table=%', t;
      CONTINUE;
    END IF;

    -- NULL now carries meaning; the column default is dropped so an insert that
    -- omits ranking_group can never silently claim UNKNOWN_DRIVER membership.
    EXECUTE format('ALTER TABLE public.%I ALTER COLUMN ranking_group DROP NOT NULL', t);
    EXECUTE format('ALTER TABLE public.%I ALTER COLUMN ranking_group DROP DEFAULT', t);

    c := 'chk_' || t || '_ranking_group';
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, c);
    EXECUTE format(
      'ALTER TABLE public.%I ADD CONSTRAINT %I CHECK ('
      || 'ranking_group IS NULL '
      || 'OR ranking_group IN (''INCLUDED'', ''EXCLUDED'', ''UNKNOWN_DRIVER''))',
      t, c
    );

    -- A row outside every ranking group can never carry ranking coordinates.
    -- Every pre-existing row satisfies this (ranking_group was NOT NULL until
    -- now), so it is added VALID and costs only one scan.
    c := 'chk_' || t || '_ranking_group_position_coherence';
    EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, c);
    EXECUTE format(
      'ALTER TABLE public.%I ADD CONSTRAINT %I CHECK ('
      || 'ranking_group IS NOT NULL '
      || 'OR (ranking_position IS NULL AND ranking_total_participants IS NULL))',
      t, c
    );

    -- The actual new business contract, stated as a biconditional: a row belongs
    -- to a ranking population if and only if it is QUALIFIED. Both directions
    -- matter — a non-qualified row must never hold a group, and a QUALIFIED row
    -- always gets one (at least UNKNOWN_DRIVER). `qualification_status` is
    -- NOT NULL, so both sides are always a defined boolean.
    -- This one genuinely needs NOT VALID, because rows written under the
    -- previous contract DO violate it (a LOW_DISTANCE row could sit in
    -- INCLUDED), and this migration must not rewrite history.
    c := 'chk_' || t || '_ranking_requires_qualified';
    SELECT convalidated INTO already_validated
      FROM pg_constraint
     WHERE conname = c
       AND conrelid = to_regclass('public.' || t);
    IF already_validated THEN
      -- Never silently downgrade a constraint an operator already validated.
      RAISE NOTICE 'ECO_RANKING_CONSTRAINT_ALREADY_VALIDATED: table=% constraint=%', t, c;
    ELSE
      EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I', t, c);
      EXECUTE format(
        'ALTER TABLE public.%I ADD CONSTRAINT %I CHECK ('
        || '(qualification_status = ''QUALIFIED'') = (ranking_group IS NOT NULL))'
        || ' NOT VALID',
        t, c
      );
    END IF;

    RAISE NOTICE 'ECO_RANKING_QUALIFIED_ONLY_APPLIED: table=%', t;
  END LOOP;
END
$$;

-- `chk_<table>_ranking_requires_qualified` is NOT VALID because historical rows
-- computed under the previous contract violate it. New and updated rows are
-- checked immediately, so no writer can put a non-qualified row back into a
-- ranking group. Once the relevant periods have been recalculated through the
-- ordinary aggregation job, run
--   ALTER TABLE public.<table> VALIDATE CONSTRAINT chk_<table>_ranking_requires_qualified;
-- Validation succeeding is then meaningful evidence that no unqualified row is
-- ranked anywhere in that table. Re-running this migration will not undo a
-- completed validation.
--
-- NOTE — VALIDATE is table-wide, not per-period. It keeps failing until EVERY
-- legacy period in that table has been recalculated, so do not expect it to
-- succeed after cleaning up a single period.
--
-- NOTE — a NOT VALID CHECK is still enforced on any UPDATE of an existing row,
-- even one that does not touch the constrained columns. Migrations 028, 036 and
-- 037 are the only non-aggregation writers that UPDATE these tables; re-running
-- 036/037 manually against a database that has 046 applied and legacy violating
-- rows still present would abort. That is fail-closed, but not obvious.
--
-- WARNING — recalculation is irreversible and rewrites already-communicated
-- ranking history. See docs/07_operations.md before authorizing it.
