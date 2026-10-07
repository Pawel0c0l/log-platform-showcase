-- 057_workflow_a_trips_coverage_state.sql
-- Workflow A — inert bounded Telematics trips coverage state (delivery-plan C3).
--
-- Creates `workflow_a_control.client_dataset_coverage` and five nullable
-- claim-time evidence columns on `workflow_a_control.client_schedule_run_history`,
-- exactly as accepted in
--   docs/13_telematics_trips_stabilization_windows.md §5.2, §5.5, §14.1
--   docs/14_telematics_trips_compatibility_implementation_plan.md §4.1–§4.6, §6.1, §6.2
--
-- Semantics of the coverage row (normative, docs/13 §5.2):
--   Operationally verified coverage exists ONLY for the closed interval
--   [coverage_start_ts, covered_through_ts]. A row without a lower bound is
--   NOT a weaker coverage claim — it is no claim at all. `covered_through_ts`
--   is the monotone upper bound used by future scheduled advancement;
--   `coverage_start_ts` is never written by ordinary scheduled advancement.
--
-- INERTNESS CONTRACT for this migration:
--   * No coverage row is created here. A schedule with no row and a schedule
--     with an UNINITIALIZED row are both refused by the future runtime gate
--     (docs/13 §5.2.1), so the absent-row state is the safe direction.
--   * Nothing is inferred from schedule history, client data or timestamps.
--   * No client is enabled for compatibility mode; no `client_account` column,
--     no schedule row and no existing history row is modified.
--   * No trigger, no function and no procedure is created.
--   * No Python runtime reads or writes these objects. Runtime use requires
--     delivery-plan commits C4–C6 and remains separately gated.
--
-- Partial-state policy: because no legitimate coverage table exists in any
-- environment yet, every incompatible pre-existing shape or content is a
-- visible stop. This migration never deletes, rewrites or normalizes a
-- pre-existing coverage row, and never rewrites pre-existing history evidence.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;


-- ---------------------------------------------------------------------------
-- 0) Preflight — the authoritative parent tables must already have the shape
--    this migration binds to (migrations 008, 012, 014, 017).
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  actual_type TEXT;
  schedule_attnum SMALLINT;
BEGIN
  IF to_regclass('workflow_a_control.client_dataset_schedule') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_schedule is absent; migrations 012 and 014 must be applied before 057';
  END IF;

  IF to_regclass('workflow_a_control.client_schedule_run_history') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_schedule_run_history is absent; migrations 008 and 014 must be applied before 057';
  END IF;

  SELECT format_type(a.atttypid, a.atttypmod), a.attnum
    INTO actual_type, schedule_attnum
    FROM pg_attribute a
   WHERE a.attrelid = 'workflow_a_control.client_dataset_schedule'::regclass
     AND a.attname = 'schedule_id'
     AND a.attnum > 0
     AND NOT a.attisdropped;

  IF actual_type IS DISTINCT FROM 'uuid' THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_schedule.schedule_id has incompatible type %, expected uuid',
      coalesce(actual_type, '<missing>');
  END IF;

  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint c
     WHERE c.conrelid = 'workflow_a_control.client_dataset_schedule'::regclass
       AND c.contype IN ('p', 'u')
       AND c.conkey = ARRAY[schedule_attnum]::smallint[]
  ) THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_schedule.schedule_id is not uniquely keyed; the coverage foreign key cannot be installed';
  END IF;
END;
$$;

-- Existing history-evidence columns must already match the complete physical
-- contract before this migration creates any C3 object. `ops/db_migrate.sh`
-- runs migration statements with psql autocommit, so this preflight must stay
-- ahead of the coverage-table DDL: an incompatible default must not leave a
-- partially installed C3 schema behind. The catalog presence bit is
-- authoritative; the rendered expression is diagnostic only and is never
-- normalized or compared semantically.
DO $$
DECLARE
  spec RECORD;
  actual_type TEXT;
  actual_notnull BOOLEAN;
  actual_has_default BOOLEAN;
  observed_default TEXT;
BEGIN
  FOR spec IN
    SELECT *
      FROM (VALUES
        ('nominal_window_start_ts',     'timestamp with time zone'),
        ('nominal_window_end_ts',       'timestamp with time zone'),
        ('stabilization_delay_seconds', 'integer'),
        ('overlap_seconds',             'integer'),
        ('trips_pagination_mode',       'text')
      ) AS t(column_name, expected_type)
  LOOP
    SELECT format_type(a.atttypid, a.atttypmod),
           a.attnotnull,
           a.atthasdef,
           pg_get_expr(d.adbin, d.adrelid)
      INTO actual_type, actual_notnull, actual_has_default, observed_default
      FROM pg_attribute a
      LEFT JOIN pg_attrdef d
        ON d.adrelid = a.attrelid
       AND d.adnum = a.attnum
     WHERE a.attrelid = 'workflow_a_control.client_schedule_run_history'::regclass
       AND a.attname = spec.column_name
       AND a.attnum > 0
       AND NOT a.attisdropped;

    -- A missing column is added later with the canonical nullable/defaultless
    -- definition. Every column that already exists is validated here.
    IF NOT FOUND THEN
      CONTINUE;
    END IF;

    IF actual_type IS DISTINCT FROM spec.expected_type THEN
      RAISE EXCEPTION
        'workflow_a_control.client_schedule_run_history.% has incompatible type %, expected %',
        spec.column_name, actual_type, spec.expected_type;
    END IF;

    IF actual_notnull THEN
      RAISE EXCEPTION
        'workflow_a_control.client_schedule_run_history.% is NOT NULL; claim-time evidence must stay nullable',
        spec.column_name;
    END IF;

    IF actual_has_default THEN
      RAISE EXCEPTION
        'workflow_a_control.client_schedule_run_history.% has incompatible default %, expected no default for claim-time evidence',
        spec.column_name, coalesce(observed_default, '<catalog default present>');
    END IF;
  END LOOP;
END;
$$;


-- ---------------------------------------------------------------------------
-- 1) client_dataset_coverage — bounded, per-schedule coverage claim
-- ---------------------------------------------------------------------------

-- 1a) Refuse to repair an incompatible, already-populated coverage table.
DO $$
DECLARE
  missing_columns TEXT;
  existing_rows BIGINT;
BEGIN
  IF to_regclass('workflow_a_control.client_dataset_coverage') IS NULL THEN
    RETURN;
  END IF;

  SELECT string_agg(expected.column_name, ', ' ORDER BY expected.column_name)
    INTO missing_columns
    FROM unnest(ARRAY[
      'schedule_id', 'client_id', 'client_code', 'dataset_name',
      'coverage_start_ts', 'covered_through_ts',
      'bootstrap_status', 'bootstrap_evidence_ref', 'seeded_at', 'seeded_by',
      'covered_through_source', 'last_gap_detected_ts', 'updated_at'
    ]) AS expected(column_name)
   WHERE NOT EXISTS (
     SELECT 1
       FROM pg_attribute a
      WHERE a.attrelid = 'workflow_a_control.client_dataset_coverage'::regclass
        AND a.attname = expected.column_name
        AND a.attnum > 0
        AND NOT a.attisdropped
   );

  IF missing_columns IS NULL THEN
    RETURN;
  END IF;

  EXECUTE 'SELECT count(*) FROM workflow_a_control.client_dataset_coverage'
     INTO existing_rows;

  IF existing_rows > 0 THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_coverage already holds % row(s) and is missing column(s) %; refusing to repair coverage state automatically',
      existing_rows, missing_columns;
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_coverage (
  -- Identity and lifecycle anchor: exactly one coverage row per schedule.
  schedule_id            UUID        NOT NULL,

  -- Denormalized operator identity. `client_id` is the canonical join key and
  -- `client_code` the operator-friendly code; never substituted for one
  -- another (CONVENTIONS.md §3).
  client_id              UUID        NOT NULL,
  client_code            TEXT        NULL,
  dataset_name           TEXT        NOT NULL,

  -- A: earliest instant of the explicitly verified closed interval.
  coverage_start_ts      TIMESTAMPTZ NULL,
  -- W: latest instant of the explicitly verified closed interval.
  covered_through_ts     TIMESTAMPTZ NULL,

  -- Gates whether the pair above may be used at all. The default creates no
  -- coverage claim.
  bootstrap_status       TEXT        NOT NULL DEFAULT 'UNINITIALIZED',
  bootstrap_evidence_ref TEXT        NULL,
  seeded_at              TIMESTAMPTZ NULL,
  seeded_by              TEXT        NULL,

  -- What last moved W.
  covered_through_source TEXT        NOT NULL DEFAULT 'bootstrap',
  last_gap_detected_ts   TIMESTAMPTZ NULL,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 1b) Heal a pre-existing, empty, partially created table (018/056 pattern).
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD COLUMN IF NOT EXISTS schedule_id UUID,
  ADD COLUMN IF NOT EXISTS client_id UUID,
  ADD COLUMN IF NOT EXISTS client_code TEXT,
  ADD COLUMN IF NOT EXISTS dataset_name TEXT,
  ADD COLUMN IF NOT EXISTS coverage_start_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS covered_through_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS bootstrap_status TEXT,
  ADD COLUMN IF NOT EXISTS bootstrap_evidence_ref TEXT,
  ADD COLUMN IF NOT EXISTS seeded_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS seeded_by TEXT,
  ADD COLUMN IF NOT EXISTS covered_through_source TEXT,
  ADD COLUMN IF NOT EXISTS last_gap_detected_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- 1c) Column types are a hard contract; an incompatible one is a visible stop.
DO $$
DECLARE
  spec RECORD;
  actual_type TEXT;
BEGIN
  FOR spec IN
    SELECT *
      FROM (VALUES
        ('schedule_id',            'uuid'),
        ('client_id',              'uuid'),
        ('client_code',            'text'),
        ('dataset_name',           'text'),
        ('coverage_start_ts',      'timestamp with time zone'),
        ('covered_through_ts',     'timestamp with time zone'),
        ('bootstrap_status',       'text'),
        ('bootstrap_evidence_ref', 'text'),
        ('seeded_at',              'timestamp with time zone'),
        ('seeded_by',              'text'),
        ('covered_through_source', 'text'),
        ('last_gap_detected_ts',   'timestamp with time zone'),
        ('updated_at',             'timestamp with time zone')
      ) AS t(column_name, expected_type)
  LOOP
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO actual_type
      FROM pg_attribute a
     WHERE a.attrelid = 'workflow_a_control.client_dataset_coverage'::regclass
       AND a.attname = spec.column_name
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF actual_type IS DISTINCT FROM spec.expected_type THEN
      RAISE EXCEPTION
        'workflow_a_control.client_dataset_coverage.% has incompatible type %, expected %',
        spec.column_name, coalesce(actual_type, '<missing>'), spec.expected_type;
    END IF;
  END LOOP;
END;
$$;

-- 1d) A pre-existing row holding NULL in a required column is incompatible
--     state, not something to normalize.
DO $$
DECLARE
  offending BIGINT;
BEGIN
  SELECT count(*)
    INTO offending
    FROM workflow_a_control.client_dataset_coverage
   WHERE schedule_id IS NULL
      OR client_id IS NULL
      OR dataset_name IS NULL
      OR bootstrap_status IS NULL
      OR covered_through_source IS NULL
      OR updated_at IS NULL;

  IF offending > 0 THEN
    RAISE EXCEPTION
      '% pre-existing workflow_a_control.client_dataset_coverage row(s) hold NULL in a required column; refusing to normalize coverage state',
      offending;
  END IF;
END;
$$;

ALTER TABLE workflow_a_control.client_dataset_coverage
  ALTER COLUMN bootstrap_status SET DEFAULT 'UNINITIALIZED',
  ALTER COLUMN covered_through_source SET DEFAULT 'bootstrap',
  ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE workflow_a_control.client_dataset_coverage
  ALTER COLUMN schedule_id SET NOT NULL,
  ALTER COLUMN client_id SET NOT NULL,
  ALTER COLUMN dataset_name SET NOT NULL,
  ALTER COLUMN bootstrap_status SET NOT NULL,
  ALTER COLUMN covered_through_source SET NOT NULL,
  ALTER COLUMN updated_at SET NOT NULL;

-- 1e) One coverage row per schedule. Duplicate pre-existing rows make this
--     fail visibly rather than being silently collapsed.
DO $$
DECLARE
  pk_name TEXT;
  pk_columns TEXT;
BEGIN
  SELECT c.conname,
         (SELECT string_agg(a.attname, ',' ORDER BY k.ord)
            FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = k.attnum)
    INTO pk_name, pk_columns
    FROM pg_constraint c
   WHERE c.conrelid = 'workflow_a_control.client_dataset_coverage'::regclass
     AND c.contype = 'p';

  IF pk_name IS NULL THEN
    ALTER TABLE workflow_a_control.client_dataset_coverage
      ADD CONSTRAINT pk_client_dataset_coverage PRIMARY KEY (schedule_id);
  ELSIF pk_columns IS DISTINCT FROM 'schedule_id' THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_coverage primary key % covers (%), expected (schedule_id)',
      pk_name, pk_columns;
  END IF;
END;
$$;

-- 1f) Lifecycle binding. ON DELETE CASCADE is required by the accepted design
--     (docs/13 §5.5, docs/14 §6.2, §6.6) and matches the existing convention
--     for schedule-scoped control rows (`fk_run_history_schedule`, 014). A
--     re-created schedule is a new coverage claim and must be bootstrapped
--     again rather than inheriting an unrelated proof; disabling a schedule
--     (`enabled = false`) deletes nothing and preserves the row.
ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS fk_client_dataset_coverage_schedule;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT fk_client_dataset_coverage_schedule
  FOREIGN KEY (schedule_id)
  REFERENCES workflow_a_control.client_dataset_schedule (schedule_id)
  ON DELETE CASCADE;

-- 1g) Status vocabulary (docs/13 §5.2).
ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS ck_client_dataset_coverage_bootstrap_status;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT ck_client_dataset_coverage_bootstrap_status
  CHECK (bootstrap_status IN
    ('UNINITIALIZED', 'READY', 'GAP_DETECTED', 'RESEED_REQUIRED'));

-- 1h) Provenance vocabulary for the upper bound (docs/14 §6.1, §6.2).
ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS ck_client_dataset_coverage_covered_through_source;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT ck_client_dataset_coverage_covered_through_source
  CHECK (covered_through_source IN ('bootstrap', 'scheduled_run', 'operator'));

-- 1i) Bounds ordering whenever both bounds are present. Deliberately does NOT
--     force NULL bounds for a non-READY status: docs/13 §5.2 states A/W "may
--     be NULL" while UNINITIALIZED, and §6.3 permits an operator to prepare a
--     row before evidence review, so the transitional representation is
--     accepted. GAP_DETECTED and RESEED_REQUIRED keep their last verified
--     bounded interval by the same rule.
ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS ck_client_dataset_coverage_bounds_order;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT ck_client_dataset_coverage_bounds_order
  CHECK (
    coverage_start_ts IS NULL
    OR covered_through_ts IS NULL
    OR coverage_start_ts <= covered_through_ts
  );

-- 1j) READY completeness. This is what makes the fail-closed rule of
--     docs/13 §5.2.1 enforceable in SQL and not only in Python: a READY row
--     without both bounds and without a present, non-empty evidence reference
--     and operator identity cannot exist. The database can verify that an
--     evidence reference is present, never that it is meaningful — the runtime
--     check of C5 remains required.
ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS ck_client_dataset_coverage_ready_complete;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT ck_client_dataset_coverage_ready_complete
  CHECK (
    bootstrap_status <> 'READY'
    OR (
      coverage_start_ts IS NOT NULL
      AND covered_through_ts IS NOT NULL
      AND coverage_start_ts <= covered_through_ts
      AND bootstrap_evidence_ref IS NOT NULL
      AND btrim(bootstrap_evidence_ref) <> ''
      AND seeded_at IS NOT NULL
      AND seeded_by IS NOT NULL
      AND btrim(seeded_by) <> ''
    )
  );

-- 1k) Operator-triage access path only. The runtime reads one row by primary
--     key, so this index must not be justified by runtime need (docs/14 §4.5).
CREATE INDEX IF NOT EXISTS idx_client_dataset_coverage_attention
  ON workflow_a_control.client_dataset_coverage (bootstrap_status)
  WHERE bootstrap_status <> 'READY';


-- ---------------------------------------------------------------------------
-- 2) client_schedule_run_history — nullable claim-time evidence columns
--
--    `window_start_ts` / `window_end_ts` keep their existing meaning: the
--    window the job was actually asked to fetch. The nominal window and the
--    configuration in force are not derivable after the fact because
--    `lookback_days`, D, O and the mode are all mutable, so they are recorded
--    separately (docs/13 §14.1).
--
--    All five columns are nullable with no default. NULL is the correct and
--    permanent value for every historical row and for every strict-mode row;
--    nothing here backfills, infers or invents evidence, and no existing row
--    is updated. `uq_run_history_schedule_fire (schedule_id,
--    scheduled_fire_ts)` is untouched, terminal rows stay immutable, and
--    `_finalize_run` semantics are unchanged.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD COLUMN IF NOT EXISTS nominal_window_start_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS nominal_window_end_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stabilization_delay_seconds INTEGER,
  ADD COLUMN IF NOT EXISTS overlap_seconds INTEGER,
  ADD COLUMN IF NOT EXISTS trips_pagination_mode TEXT;

-- Nullable-value CHECKs. Every one is vacuously satisfied by NULL, so no
-- historical row is affected; invalid pre-existing non-NULL evidence makes the
-- constraint installation fail visibly instead of being rewritten.
ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS ck_run_history_nominal_window_order;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT ck_run_history_nominal_window_order
  CHECK (
    nominal_window_start_ts IS NULL
    OR nominal_window_end_ts IS NULL
    OR nominal_window_start_ts <= nominal_window_end_ts
  );

ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS ck_run_history_stabilization_delay_seconds;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT ck_run_history_stabilization_delay_seconds
  CHECK (stabilization_delay_seconds IS NULL OR stabilization_delay_seconds >= 0);

ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS ck_run_history_overlap_seconds;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT ck_run_history_overlap_seconds
  CHECK (overlap_seconds IS NULL OR overlap_seconds >= 0);

ALTER TABLE workflow_a_control.client_schedule_run_history
  DROP CONSTRAINT IF EXISTS ck_run_history_trips_pagination_mode;
ALTER TABLE workflow_a_control.client_schedule_run_history
  ADD CONSTRAINT ck_run_history_trips_pagination_mode
  CHECK (
    trips_pagination_mode IS NULL
    OR trips_pagination_mode IN ('strict_meta', 'data_invariants_v1')
  );
