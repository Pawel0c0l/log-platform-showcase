-- 058_telematics_trips_manual_recovery.sql
-- Workflow A — durable identity and evidence for reviewed manual Telematics
-- `trips_sync` compatibility recovery (delivery-plan C11).
--
-- Specification of record:
--   docs/13_telematics_trips_stabilization_windows.md §5.4, §12 (the revised
--     manual/recovery rules: an ordinary ad hoc job still never touches A/W;
--     exactly two reviewed surfaces may advance W)
--   docs/14_telematics_trips_compatibility_implementation_plan.md §1.1, §3/C11,
--     §12 (recovery ordering and gates)
--   docs/15_telematics_coverage_mutation_contract.md §4, §5, §7 (the shared
--     claim-time CAS this recovery reuses)
--   db/migrations/057_workflow_a_trips_coverage_state.sql (the coverage
--     contract this migration extends but never rewrites)
--
-- WHY A SEPARATE TABLE AND NOT A SCHEDULE-HISTORY ROW.
--   A manual recovery is not a scheduled fire. Reusing
--   `client_schedule_run_history` would require either mutating a terminal row
--   or inventing a synthetic `scheduled_fire_ts`, and both destroy the meaning
--   of the `UNIQUE (schedule_id, scheduled_fire_ts)` evidence key. This table
--   therefore carries its own identity, and the recovery tooling is forbidden
--   from creating, editing, deleting, retrying or reclassifying any
--   `client_schedule_run_history` row.
--
-- INERTNESS CONTRACT for this migration:
--   * it creates one table and one additive constraint replacement; it inserts
--     no row, so no recovery exists until an operator executes one;
--   * it creates no trigger, function or procedure, and in particular nothing
--     that mutates `client_dataset_coverage` automatically;
--   * it does not modify migration 057, any coverage row, any history row, any
--     schedule or any client configuration;
--   * it enables no client and changes no pagination mode.

CREATE SCHEMA IF NOT EXISTS workflow_a_control;


-- ---------------------------------------------------------------------------
-- 0) Preflight — the parents this migration binds to must already exist.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF to_regclass('workflow_a_control.client_account') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_account is absent; migration 008 must be applied before 058';
  END IF;

  IF to_regclass('workflow_a_control.client_dataset_schedule') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_schedule is absent; migration 012 must be applied before 058';
  END IF;

  IF to_regclass('workflow_a_control.client_dataset_coverage') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_coverage is absent; migration 057 must be applied before 058';
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 1) Extend the `covered_through_source` vocabulary with `manual_recovery`.
--
--    Migration 057 accepted 'bootstrap', 'scheduled_run' and 'operator'. A
--    watermark moved by the reviewed manual recovery must stay distinguishable
--    from both a scheduled advancement and a raw operator edit: docs/15 §4.3
--    uses an operator-set 'operator' provenance as the normative example of a
--    race that must cause a CAS conflict, so reusing that value for C11 would
--    erase the very signal the contract depends on.
--
--    This only widens the accepted set. Every existing row keeps its value and
--    no row is rewritten.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS ck_client_dataset_coverage_covered_through_source;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT ck_client_dataset_coverage_covered_through_source
  CHECK (covered_through_source IN
    ('bootstrap', 'scheduled_run', 'operator', 'manual_recovery'));


-- ---------------------------------------------------------------------------
-- 2) client_dataset_recovery_run — one row per reviewed manual recovery.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_recovery_run (
  recovery_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Identity of the exact target. `client_code` is denormalized operator
  -- convenience only; `client_id` and `schedule_id` are canonical
  -- (CONVENTIONS.md §3).
  client_id    UUID NOT NULL,
  client_code  TEXT NULL,
  schedule_id  UUID NOT NULL,
  dataset_name TEXT NOT NULL,

  -- The explicit operator-approved UTC interval. Never derived, never shifted
  -- and never widened by any job (docs/13 §12).
  window_start_ts TIMESTAMPTZ NOT NULL,
  window_end_ts   TIMESTAMPTZ NOT NULL,

  -- The watermark this recovery was authorized against. It is the CAS anchor:
  -- if W moved between planning and finalization, the recovery must fail
  -- closed rather than overwrite the newer value.
  expected_old_covered_through_ts TIMESTAMPTZ NOT NULL,

  status TEXT NOT NULL DEFAULT 'PLANNED',

  -- Bounded, sanitized operator justification. Never a payload, never a
  -- credential, never trip data.
  reason          TEXT NOT NULL,
  approval_ref    TEXT NOT NULL,
  repository_head TEXT NOT NULL,

  -- The compatibility configuration in force when the recovery was claimed.
  -- Not reconstructable afterwards, because all four values are mutable.
  pagination_mode             TEXT    NOT NULL,
  stabilization_delay_seconds INTEGER NOT NULL,
  overlap_seconds             INTEGER NOT NULL,
  max_recovery_span_seconds   INTEGER NOT NULL,

  -- The immutable claim-time coverage snapshot and its canonical fingerprint
  -- (`telematics-coverage-fingerprint/1`). The snapshot is the authoritative CAS
  -- carrier; the fingerprint is evidence about it, never an authorization.
  initial_coverage_snapshot    JSONB NOT NULL,
  initial_coverage_fingerprint TEXT  NOT NULL,

  -- Outcome evidence, all nullable until the recovery reaches a terminal state.
  platform_run_id            UUID  NULL,
  provider_summary           JSONB NULL,
  job_summary                JSONB NULL,
  finalizer_result           JSONB NULL,
  final_coverage_fingerprint TEXT  NULL,
  error_classification       TEXT  NULL,
  error_summary              TEXT  NULL,

  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at  TIMESTAMPTZ NULL,
  finished_at TIMESTAMPTZ NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2a) Heal a pre-existing, partially created table (018/056/057 pattern).
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD COLUMN IF NOT EXISTS client_id UUID,
  ADD COLUMN IF NOT EXISTS client_code TEXT,
  ADD COLUMN IF NOT EXISTS schedule_id UUID,
  ADD COLUMN IF NOT EXISTS dataset_name TEXT,
  ADD COLUMN IF NOT EXISTS window_start_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS window_end_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS expected_old_covered_through_ts TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status TEXT,
  ADD COLUMN IF NOT EXISTS reason TEXT,
  ADD COLUMN IF NOT EXISTS approval_ref TEXT,
  ADD COLUMN IF NOT EXISTS repository_head TEXT,
  ADD COLUMN IF NOT EXISTS pagination_mode TEXT,
  ADD COLUMN IF NOT EXISTS stabilization_delay_seconds INTEGER,
  ADD COLUMN IF NOT EXISTS overlap_seconds INTEGER,
  ADD COLUMN IF NOT EXISTS max_recovery_span_seconds INTEGER,
  ADD COLUMN IF NOT EXISTS initial_coverage_snapshot JSONB,
  ADD COLUMN IF NOT EXISTS initial_coverage_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS platform_run_id UUID,
  ADD COLUMN IF NOT EXISTS provider_summary JSONB,
  ADD COLUMN IF NOT EXISTS job_summary JSONB,
  ADD COLUMN IF NOT EXISTS finalizer_result JSONB,
  ADD COLUMN IF NOT EXISTS final_coverage_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS error_classification TEXT,
  ADD COLUMN IF NOT EXISTS error_summary TEXT,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;


-- 2b) Lifecycle binding. Deliberately ON DELETE RESTRICT, unlike the coverage
--     row's CASCADE: a coverage claim is state that a re-created schedule must
--     re-earn, while a recovery run is *operational evidence of an executed
--     production action*. Evidence is never erased as a side effect of deleting
--     a schedule or a client; an operator who genuinely needs the parent gone
--     must deal with the evidence explicitly and separately.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS fk_client_dataset_recovery_run_client;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT fk_client_dataset_recovery_run_client
  FOREIGN KEY (client_id)
  REFERENCES workflow_a_control.client_account (client_id)
  ON DELETE RESTRICT;

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS fk_client_dataset_recovery_run_schedule;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT fk_client_dataset_recovery_run_schedule
  FOREIGN KEY (schedule_id)
  REFERENCES workflow_a_control.client_dataset_schedule (schedule_id)
  ON DELETE RESTRICT;


-- 2c) Scope. Coverage semantics exist only for compatibility `trips_sync`.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_dataset;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_dataset
  CHECK (dataset_name = 'trips_sync');

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_mode;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_mode
  CHECK (pagination_mode = 'data_invariants_v1');


-- 2d) Interval sanity. The recovery must request a real forward interval and
--     must be anchored exactly at the watermark it was authorized against, so
--     that a success can never claim history the run did not fetch.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_window_order;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_window_order
  CHECK (window_end_ts >= window_start_ts);

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_window_anchor;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_window_anchor
  CHECK (window_start_ts = expected_old_covered_through_ts);


-- 2e) Status vocabulary and terminal-timestamp consistency.
--     PLANNED  — claimed, business work not started;
--     RUNNING  — business work in flight;
--     SUCCESS  — business work committed and W advanced atomically;
--     FAILED   — provider/business/orchestration failure; coverage untouched;
--     FINALIZATION_CONFLICT — business work succeeded but the coverage CAS was
--                             refused; coverage untouched, operator review owed.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_status;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_status
  CHECK (status IN
    ('PLANNED', 'RUNNING', 'SUCCESS', 'FAILED', 'FINALIZATION_CONFLICT'));

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_terminal_times;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_terminal_times
  CHECK (
    (
      status IN ('SUCCESS', 'FAILED', 'FINALIZATION_CONFLICT')
      AND started_at IS NOT NULL
      AND finished_at IS NOT NULL
      AND finished_at >= started_at
    )
    OR (
      status IN ('PLANNED', 'RUNNING')
      AND finished_at IS NULL
    )
  );

-- A terminal non-success must say why; a success must not carry an error.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_error_consistency;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_error_consistency
  CHECK (
    (
      status IN ('FAILED', 'FINALIZATION_CONFLICT')
      AND error_classification IS NOT NULL
      AND btrim(error_classification) <> ''
    )
    OR (
      status NOT IN ('FAILED', 'FINALIZATION_CONFLICT')
      AND error_classification IS NULL
    )
  );

-- Only a SUCCESS may carry a finalizer result and a post-write fingerprint.
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_success_evidence;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_success_evidence
  CHECK (
    status = 'SUCCESS'
    OR (finalizer_result IS NULL AND final_coverage_fingerprint IS NULL)
  );


-- 2f) Bounded, sanitized text. The database can enforce shape and length; it
--     can never verify meaning, so the tooling's redaction rules remain
--     required (docs/06_security.md).
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_bounded_text;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_bounded_text
  CHECK (
    btrim(reason) <> '' AND length(reason) <= 500
    AND btrim(approval_ref) <> '' AND length(approval_ref) <= 200
    AND (error_classification IS NULL OR length(error_classification) <= 120)
    AND (error_summary IS NULL OR length(error_summary) <= 4000)
    AND (client_code IS NULL OR length(client_code) <= 64)
  );

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_repository_head;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_repository_head
  CHECK (repository_head ~ '^[0-9a-f]{40}$');

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_fingerprints;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_fingerprints
  CHECK (
    initial_coverage_fingerprint ~ '^[0-9a-f]{64}$'
    AND (
      final_coverage_fingerprint IS NULL
      OR final_coverage_fingerprint ~ '^[0-9a-f]{64}$'
    )
  );

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS ck_client_dataset_recovery_run_config_bounds;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT ck_client_dataset_recovery_run_config_bounds
  CHECK (
    stabilization_delay_seconds >= 0
    AND overlap_seconds >= 0
    AND max_recovery_span_seconds > 0
    AND max_recovery_span_seconds <= 2678400
  );


-- 2g) NOT NULL for the columns the healing block above added as nullable.
DO $$
DECLARE
  offending BIGINT;
BEGIN
  SELECT count(*)
    INTO offending
    FROM workflow_a_control.client_dataset_recovery_run
   WHERE client_id IS NULL
      OR schedule_id IS NULL
      OR dataset_name IS NULL
      OR window_start_ts IS NULL
      OR window_end_ts IS NULL
      OR expected_old_covered_through_ts IS NULL
      OR status IS NULL
      OR reason IS NULL
      OR approval_ref IS NULL
      OR repository_head IS NULL
      OR pagination_mode IS NULL
      OR initial_coverage_snapshot IS NULL
      OR initial_coverage_fingerprint IS NULL
      OR created_at IS NULL
      OR updated_at IS NULL;

  IF offending > 0 THEN
    RAISE EXCEPTION
      '% pre-existing workflow_a_control.client_dataset_recovery_run row(s) hold NULL in a required column; refusing to normalize recovery evidence',
      offending;
  END IF;
END;
$$;

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ALTER COLUMN status SET DEFAULT 'PLANNED',
  ALTER COLUMN created_at SET DEFAULT now(),
  ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ALTER COLUMN client_id SET NOT NULL,
  ALTER COLUMN schedule_id SET NOT NULL,
  ALTER COLUMN dataset_name SET NOT NULL,
  ALTER COLUMN window_start_ts SET NOT NULL,
  ALTER COLUMN window_end_ts SET NOT NULL,
  ALTER COLUMN expected_old_covered_through_ts SET NOT NULL,
  ALTER COLUMN status SET NOT NULL,
  ALTER COLUMN reason SET NOT NULL,
  ALTER COLUMN approval_ref SET NOT NULL,
  ALTER COLUMN repository_head SET NOT NULL,
  ALTER COLUMN pagination_mode SET NOT NULL,
  ALTER COLUMN stabilization_delay_seconds SET NOT NULL,
  ALTER COLUMN overlap_seconds SET NOT NULL,
  ALTER COLUMN max_recovery_span_seconds SET NOT NULL,
  ALTER COLUMN initial_coverage_snapshot SET NOT NULL,
  ALTER COLUMN initial_coverage_fingerprint SET NOT NULL,
  ALTER COLUMN created_at SET NOT NULL,
  ALTER COLUMN updated_at SET NOT NULL;


-- ---------------------------------------------------------------------------
-- 3) Uniqueness and access paths.
-- ---------------------------------------------------------------------------

-- 3a) One approved recovery is executed exactly once. A second attempt at the
--     same client × schedule × interval × approval is rejected by the database,
--     not only by tooling discipline. Re-running a genuinely new recovery over
--     the same interval requires a new approval reference, which is precisely
--     the reviewed decision that should not be implicit.
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_dataset_recovery_run_approved_window
  ON workflow_a_control.client_dataset_recovery_run
     (client_id, schedule_id, window_start_ts, window_end_ts, approval_ref);

-- 3b) At most one non-terminal recovery per schedule, ever. This is what makes
--     "reject a concurrent recovery" a database invariant instead of a race
--     between two preflights.
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_dataset_recovery_run_active
  ON workflow_a_control.client_dataset_recovery_run (schedule_id)
  WHERE status IN ('PLANNED', 'RUNNING');

-- 3c) Operator triage paths.
CREATE INDEX IF NOT EXISTS idx_client_dataset_recovery_run_schedule_created
  ON workflow_a_control.client_dataset_recovery_run (schedule_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_client_dataset_recovery_run_attention
  ON workflow_a_control.client_dataset_recovery_run (status)
  WHERE status <> 'SUCCESS';


COMMENT ON TABLE workflow_a_control.client_dataset_recovery_run IS
  'Durable identity and non-personal evidence for one reviewed manual Telematics trips compatibility recovery (C11). Never a scheduled fire; never mutates client_schedule_run_history.';
COMMENT ON COLUMN workflow_a_control.client_dataset_recovery_run.expected_old_covered_through_ts IS
  'The covered_through_ts this recovery was authorized against; the compare-and-swap anchor for the shared C6/C11 coverage finalizer.';
COMMENT ON COLUMN workflow_a_control.client_dataset_recovery_run.initial_coverage_snapshot IS
  'Immutable claim-time coverage snapshot (docs/15 §4). Authoritative CAS carrier; never re-read at mutation time.';
