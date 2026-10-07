-- 064_public_runs_historical_reconciliation.sql
-- Platform core — durable provenance for the retrospective reconciliation of an
-- abandoned historical `public.runs` row.
--
-- Specification of record:
--   docs/03_api_spec.md §"Runs" (the live one-way terminalization contract this
--     migration deliberately does NOT touch)
--   docs/07_operations.md §5.8 (the operator procedure this table serves)
--   db/migrations/058_telematics_trips_manual_recovery.sql (the evidence-table
--     pattern this migration follows: separate identity, explicit approval,
--     CAS anchor, fail-closed constraints, no seeded rows)
--
-- WHY THIS EXISTS.
--   A run whose process stopped before terminalization stays `RUNNING` forever.
--   Nothing reaps `public.runs` — the Workflow A dispatcher's stale sweep owns
--   `workflow_a_control.client_schedule_run_history` only, and the execution
--   watchdog is expectation-driven, so an ad hoc manual run is never its
--   subject. The only existing writer, `PATCH /runs/{run_id}`, always stamps
--   `ended_at = now()`. Using it months later would record a fabricated
--   multi-week execution as historical fact.
--
--   This table makes the missing distinction durable and queryable:
--
--       a run with no row here  -> finalized naturally by the job itself
--       a run with a row here   -> terminalized retrospectively by an operator
--
--   Without it "reconciled" would live only in ephemeral logs, and a future
--   reader of `public.runs` could not tell an observed outcome from an
--   administrative one.
--
-- WHY A SEPARATE TABLE AND NOT COLUMNS ON `public.runs`.
--   `public.runs` is the hot lifecycle record every job writes on every
--   execution, and its shape is part of the `run(client, run_id, params)`
--   contract (AGENTS.md §4). Reconciliation is rare, administrative and
--   append-only. Widening the lifecycle table with six columns that are NULL
--   for 7000+ rows would put administrative metadata on the critical path of
--   normal finalization and invite exactly the silent-rewrite class P1-I closed.
--
-- WHY `FAILED` AND `CANCELED` ONLY, AND NEVER `SUCCESS`.
--   Reconciliation asserts that a run did not reach its own terminalization.
--   Recording `SUCCESS` retrospectively would fabricate a business outcome that
--   nobody observed, which is the precise failure mode P1-I exists to prevent.
--   `FAILED` and `CANCELED` are both admissible and mean different things
--   ("the work did not complete" vs "an operator abandoned it deliberately");
--   the database refuses to choose between them, so the tooling must make the
--   operator say which one, per run.
--
-- INERTNESS CONTRACT for this migration:
--   * it creates one table in the existing `ops_control` schema and inserts no
--     row, so no reconciliation exists until an operator executes one;
--   * it creates no trigger, function or procedure, and in particular nothing
--     that mutates `public.runs` automatically;
--   * it modifies no existing table, constraint, column or row;
--   * it does not terminalize, reap, age or reclassify any run.

CREATE SCHEMA IF NOT EXISTS ops_control;


-- ---------------------------------------------------------------------------
-- 0) Preflight — the parent this migration binds to must already exist with a
--    key the foreign key can reference.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF to_regclass('public.runs') IS NULL THEN
    RAISE EXCEPTION
      'public.runs is absent; the platform core schema must be applied before 064';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.runs'::regclass
       AND contype IN ('p', 'u')
       AND conkey = ARRAY[
             (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'public.runs'::regclass AND attname = 'run_id')
           ]::smallint[]
  ) THEN
    RAISE EXCEPTION
      'public.runs.run_id carries no primary/unique key; 064 cannot bind reconciliation evidence to it';
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 1) ops_control.run_reconciliation — one row per retrospectively reconciled
--    run. The primary key IS the run_id, so "reconcile a run twice" is a
--    database invariant rather than tooling discipline.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ops_control.run_reconciliation (
  -- Identity of the exact target. One reconciliation per run, ever.
  run_id UUID PRIMARY KEY,

  -- Immutable snapshot of what the row looked like when it was claimed. Kept
  -- because `public.runs.started_at` is the CAS anchor for the timestamp
  -- invariants below, and an auditor must be able to re-derive them without
  -- trusting that the parent row was never touched afterwards.
  run_source    TEXT        NOT NULL,
  run_started_at TIMESTAMPTZ NOT NULL,

  -- The terminal status the operator explicitly chose. Never defaulted, never
  -- inferred from age or from the absence of evidence.
  reconciled_status TEXT NOT NULL,

  -- The defensible historical end of execution, supplied by the operator. This
  -- is what lands in `public.runs.ended_at`; the reconciliation clock below is
  -- deliberately a different column so the two can never be confused.
  historical_ended_at TIMESTAMPTZ NOT NULL,

  -- When the administrative action itself happened.
  reconciled_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Who and why. Bounded, sanitized operator text: never a payload, never a
  -- credential, never business data.
  actor           TEXT NOT NULL,
  reason          TEXT NOT NULL,
  approval_ref    TEXT NOT NULL,
  repository_head TEXT NOT NULL,

  -- Optional pointer at the investigation that justified the disposition.
  evidence_ref TEXT NULL
);

-- 1a) Heal a pre-existing, partially created table (018/056/057/058 pattern).
ALTER TABLE ops_control.run_reconciliation
  ADD COLUMN IF NOT EXISTS run_source TEXT,
  ADD COLUMN IF NOT EXISTS run_started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reconciled_status TEXT,
  ADD COLUMN IF NOT EXISTS historical_ended_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS actor TEXT,
  ADD COLUMN IF NOT EXISTS reason TEXT,
  ADD COLUMN IF NOT EXISTS approval_ref TEXT,
  ADD COLUMN IF NOT EXISTS repository_head TEXT,
  ADD COLUMN IF NOT EXISTS evidence_ref TEXT;


-- 1b) Lifecycle binding. ON DELETE RESTRICT, like 058: a reconciliation record
--     is evidence of an executed production action. Evidence is never erased as
--     a side effect of deleting the run it describes.
ALTER TABLE ops_control.run_reconciliation
  DROP CONSTRAINT IF EXISTS fk_run_reconciliation_run;
ALTER TABLE ops_control.run_reconciliation
  ADD CONSTRAINT fk_run_reconciliation_run
  FOREIGN KEY (run_id)
  REFERENCES public.runs (run_id)
  ON DELETE RESTRICT;


-- 1c) Terminal vocabulary. `SUCCESS` is absent on purpose (see header), and so
--     is `RUNNING`: reconciliation only ever moves a row *out* of non-terminal
--     state.
ALTER TABLE ops_control.run_reconciliation
  DROP CONSTRAINT IF EXISTS ck_run_reconciliation_status;
ALTER TABLE ops_control.run_reconciliation
  ADD CONSTRAINT ck_run_reconciliation_status
  CHECK (reconciled_status IN ('FAILED', 'CANCELED'));


-- 1d) Timestamp truthfulness. These are the invariants that make the stored
--     `ended_at` defensible rather than convenient:
--
--       started_at <= historical_ended_at   the run cannot end before it began;
--       historical_ended_at <= reconciled_at  the administrative act cannot
--                                             precede the execution it records,
--                                             which is also what forbids a
--                                             future `ended_at`.
--
--     Together they bound the supplied timestamp inside the only interval that
--     can possibly be true, and they do it in the database, so a future second
--     writer cannot quietly relax them.
ALTER TABLE ops_control.run_reconciliation
  DROP CONSTRAINT IF EXISTS ck_run_reconciliation_time_order;
ALTER TABLE ops_control.run_reconciliation
  ADD CONSTRAINT ck_run_reconciliation_time_order
  CHECK (
    historical_ended_at >= run_started_at
    AND historical_ended_at <= reconciled_at
  );


-- 1e) Bounded, sanitized text. The database enforces shape and length; it can
--     never verify meaning, so the tooling's redaction rules remain required
--     (docs/06_security.md).
ALTER TABLE ops_control.run_reconciliation
  DROP CONSTRAINT IF EXISTS ck_run_reconciliation_bounded_text;
ALTER TABLE ops_control.run_reconciliation
  ADD CONSTRAINT ck_run_reconciliation_bounded_text
  CHECK (
    btrim(actor) <> '' AND length(actor) <= 200
    AND btrim(reason) <> '' AND length(reason) <= 500
    AND btrim(approval_ref) <> '' AND length(approval_ref) <= 200
    AND btrim(run_source) <> '' AND length(run_source) <= 200
    AND (evidence_ref IS NULL OR (btrim(evidence_ref) <> '' AND length(evidence_ref) <= 500))
  );

ALTER TABLE ops_control.run_reconciliation
  DROP CONSTRAINT IF EXISTS ck_run_reconciliation_repository_head;
ALTER TABLE ops_control.run_reconciliation
  ADD CONSTRAINT ck_run_reconciliation_repository_head
  CHECK (repository_head ~ '^[0-9a-f]{40}$');


-- 1f) NOT NULL for the columns the healing block above added as nullable.
DO $$
DECLARE
  offending BIGINT;
BEGIN
  SELECT count(*)
    INTO offending
    FROM ops_control.run_reconciliation
   WHERE run_source IS NULL
      OR run_started_at IS NULL
      OR reconciled_status IS NULL
      OR historical_ended_at IS NULL
      OR reconciled_at IS NULL
      OR actor IS NULL
      OR reason IS NULL
      OR approval_ref IS NULL
      OR repository_head IS NULL;

  IF offending > 0 THEN
    RAISE EXCEPTION
      '% pre-existing ops_control.run_reconciliation row(s) hold NULL in a required column; refusing to normalize reconciliation evidence',
      offending;
  END IF;
END;
$$;

ALTER TABLE ops_control.run_reconciliation
  ALTER COLUMN reconciled_at SET DEFAULT now();

ALTER TABLE ops_control.run_reconciliation
  ALTER COLUMN run_source SET NOT NULL,
  ALTER COLUMN run_started_at SET NOT NULL,
  ALTER COLUMN reconciled_status SET NOT NULL,
  ALTER COLUMN historical_ended_at SET NOT NULL,
  ALTER COLUMN reconciled_at SET NOT NULL,
  ALTER COLUMN actor SET NOT NULL,
  ALTER COLUMN reason SET NOT NULL,
  ALTER COLUMN approval_ref SET NOT NULL,
  ALTER COLUMN repository_head SET NOT NULL;


-- ---------------------------------------------------------------------------
-- 2) Operator triage path.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_run_reconciliation_reconciled_at
  ON ops_control.run_reconciliation (reconciled_at DESC);


COMMENT ON TABLE ops_control.run_reconciliation IS
  'One row per retrospectively reconciled public.runs row. Presence of a row is the only durable signal that a terminal status was set administratively rather than observed by the job. Never written by a job; only by ops/reconcile_historical_run.py under explicit authorization.';
COMMENT ON COLUMN ops_control.run_reconciliation.historical_ended_at IS
  'The defensible historical end of execution supplied by the operator and written to public.runs.ended_at. Never the reconciliation clock — that is reconciled_at.';
COMMENT ON COLUMN ops_control.run_reconciliation.reconciled_status IS
  'Explicit operator choice, FAILED or CANCELED. SUCCESS is structurally excluded: reconciliation records that a run did not finalize itself, and must never fabricate an unobserved business outcome.';
