-- 062_workflow_a_multi_cadence_schedule_identity.sql
-- M5 — multi-cadence schedule identity and shared per-dataset coverage.
--
-- Specification of record:
--   docs/20_telematics_ingestion_permanent_repair_plan.md
--     §3.2 B1 (one schedule row per (client, dataset) blocks a second cadence)
--     §3.2 B2 (coverage keyed by schedule_id splits one dataset's single truth)
--     §4.3a D4 (M4 before M5; the only debt M5 owes M4 is the read path)
--     §14 milestone M5, §15 verification row M5
--   docs/15_telematics_coverage_mutation_contract.md §4, §4.1.1, §5, §7
--     (the claim-time carrier and the normative CAS shapes this re-keys)
--   db/migrations/012 (schedule), 057 (coverage), 058 (manual recovery)
--
-- ============================================================================
-- WHAT M5 IS, AND WHAT IT DELIBERATELY IS NOT
-- ============================================================================
--
-- M5 is STRUCTURAL ENABLEMENT. It creates no schedule, changes no cadence, no
-- lookback, no overlap, no recovery horizon, no provider behaviour and no
-- coverage value. After this migration the fleet still runs exactly the
-- schedules it ran before, with exactly the same watermarks, because every
-- (client, dataset) still has exactly one schedule row and one coverage row.
--
-- What changes is what the schema PERMITS: a second and third cadence over one
-- dataset become representable, and the coverage watermark becomes a fact about
-- the DATASET rather than about one schedule.
--
-- ============================================================================
-- `run_type` IS A ROLE, NOT A CADENCE
-- ============================================================================
--
-- `frequency` already answers "how often does this fire". `run_type` answers a
-- different question: "what part does this schedule play for its dataset".
--
--   DAILY                    the base ingestion schedule — the one that carries
--                            forward coverage. AT MOST ONE per (client,
--                            dataset); exactly one wherever coverage exists.
--                            See BASE-SCHEDULE CARDINALITY below, which states
--                            the domain precisely. The name comes from the trips
--                            cadence that motivated the discriminator; it does
--                            NOT constrain `frequency`, and it must not, because
--                            migrations 032 and 046 already seed base schedules
--                            whose frequency is 'weekly'.
--   WEEKLY_RECONCILIATION    an additive weekly reconciliation pass (M6).
--   MONTHLY_RECONCILIATION   an additive monthly reconciliation pass (M7).
--
-- Every schedule row that exists when this migration runs is, by construction,
-- the only row for its (client, dataset) — `uq_client_dataset_schedule` has
-- enforced that since migration 012. Every one of them is therefore the base
-- schedule, and `DAILY` is a DETERMINISTIC classification of existing rows, not
-- an inference about their history. The migration still refuses rather than
-- assumes: guard 2 below proves the one-row property from the catalog and the
-- data before a single row is labelled.
--
-- The reconciliation values ARE cadence-constrained, because a reconciliation
-- pass whose frequency contradicts its role is a misconfiguration that should
-- never reach the dispatcher. `DAILY` is left unconstrained on purpose.
--
-- ============================================================================
-- COVERAGE: SHARED OWNERSHIP WITHOUT DESTROYING ROLLBACK
-- ============================================================================
--
-- The M5 requirement is "exactly one coverage row per (client_id,
-- dataset_name)". That is ADDED here as a UNIQUE constraint.
--
-- `pk_client_dataset_coverage PRIMARY KEY (schedule_id)` is deliberately
-- RETAINED. This is the reviewed rollback-compatibility decision, and it is not
-- a compromise:
--
--   * it does not contradict the new model. UNIQUE (client_id, dataset_name) is
--     strictly stronger than the old key over the same rows; the conjunction of
--     the two is exactly the stronger one. There is no state the new model
--     permits that the retained key forbids, because a coverage row names
--     exactly one owning schedule and no two rows can name the same one;
--   * `client_dataset_coverage.schedule_id` is NEVER mutated by any runtime
--     writer. The only two UPDATE statements in the repository that touch this
--     relation set `covered_through_ts`/`covered_through_source`/`updated_at`
--     and `bootstrap_status`/`last_gap_detected_ts`/`updated_at` respectively.
--     `schedule_id` is written once, by the bootstrap writer, and never again —
--     so it is stable, immutable provenance, not a moving pointer;
--   * because it is immutable and its row cannot be deleted while it anchors
--     coverage (see OWNER COHERENCE below), a pre-M5 release's
--     `WHERE schedule_id = …` CAS still addresses exactly one row for as long as
--     the physical shape this migration installs is in force.
--
-- THE EXACT ROLLBACK BOUNDARY, stated rather than implied. It is bounded by the
-- schema, not unconditional — a later migration that changes coverage identity
-- again would supersede every row of this table, and no claim here extends past
-- that:
--
--   after this migration, while only base schedules exist (i.e. through all of
--     M5) — a pre-M5 release operates completely unchanged. Every enabled
--     schedule owns the coverage row whose schedule_id equals its own;
--   after a future M6/M7 cadence row exists — a pre-M5 release still operates
--     correctly for the BASE schedule, and REFUSES FAIL-CLOSED for a
--     reconciliation schedule, because no coverage row carries that schedule's
--     id and `evaluate_coverage_gate` returns TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
--     for a missing row. That is a refusal, never a wrong watermark. Owning
--     that boundary is M6's job, not this migration's.
--
-- ============================================================================
-- OWNER COHERENCE, AND THE BASE/PROVENANCE LIFECYCLE
-- ============================================================================
--
-- Independent review found two holes that the owner re-key opens and that the
-- first candidate left to application discipline. Both are closed structurally
-- here, because "the writer upholds it" is not an integrity guarantee.
--
-- HOLE 1 — provenance could contradict the owner. Migration 057's FK proves only
-- that `coverage.schedule_id` references SOME schedule. It does not prove that
-- schedule belongs to the same `(client_id, dataset_name)` the coverage row is
-- now addressed by. Before M5 the gate's firing-schedule equality check masked
-- this; M5 correctly removed that check, which exposed it.
--
-- The fix is a COMPOSITE foreign key over `(schedule_id, client_id,
-- dataset_name)`. This invents no duplicated business state: coverage already
-- stores all three columns, and the FK target added below is a strict superset
-- of the schedule primary key, so it constrains the parent not at all and simply
-- makes the pairing referenceable. The same treatment is applied to
-- `client_dataset_recovery_run`, whose owner-keyed uniqueness indexes are only
-- trustworthy if its own three columns describe one schedule.
--
-- HOLE 2 — lifecycle. Migration 057's FK carried `ON DELETE CASCADE`, which was
-- right when coverage was schedule-owned: deleting the schedule deleted its
-- private watermark. After M5 the watermark is SHARED, so the same cascade would
-- let one schedule's deletion destroy a dataset's coverage while other cadences
-- keep running against nothing. The composite FK is therefore `ON DELETE
-- RESTRICT`: a schedule that anchors coverage cannot be deleted at all. Removing
-- one becomes an explicit, reviewed operation that must deal with the watermark
-- first, which is the correct shape for a fact this expensive to reconstruct.
--
-- `ON UPDATE RESTRICT` is deliberate for the same reason in the other direction:
-- cascading a re-owned schedule into the coverage row would silently re-anchor a
-- watermark, which is exactly the "re-anchoring" this section exists to prevent.
--
-- BASE-SCHEDULE CARDINALITY — stated precisely, because the first candidate
-- overclaimed it. `uq_client_dataset_schedule_base` enforces AT MOST ONE base
-- schedule per `(client_id, dataset_name)`. "Exactly one" is NOT true in the
-- global domain and never was: an enabled client may legitimately own no
-- schedule at all for a dataset, which the release gate's own fleet fixture
-- exercises deliberately. The enforceable invariant is scoped to the domain
-- where it is load-bearing:
--
--   for every dataset that BEARS COVERAGE there is exactly one schedule
--   anchoring it, that schedule belongs to the same owner, and it cannot be
--   deleted or re-owned while it does.
--
-- That is what the composite FK plus `uq_client_dataset_coverage_dataset` plus
-- `ON DELETE/UPDATE RESTRICT` enforce together, and it is the property M6
-- actually needs. Whether the anchoring schedule carries `run_type = 'DAILY'`
-- is enforced one layer up, at the single authorized coverage INSERT surface
-- (`ops/bootstrap_telematics_*_coverage.py`, which resolve the base role
-- explicitly) and re-checked fail-closed by the dispatcher when it loads
-- coverage. A partial unique index cannot be a foreign-key target in
-- PostgreSQL, so pushing that last predicate into the FK would require a
-- redundant `run_type` copy on the coverage row — duplicated business state
-- invented to make a constraint convenient, which is the wrong trade.
--
-- ============================================================================
-- RECOVERY EXCLUSIVITY FOLLOWS THE WATERMARK
-- ============================================================================
--
-- Migration 058 keyed "at most one non-terminal recovery" on `schedule_id`
-- because, at the time, one schedule WAS one watermark. M5 decouples those two
-- concepts, so a schedule-keyed exclusivity index would silently stop meaning
-- what it was written to mean: two operator recoveries on two cadences of one
-- dataset could be simultaneously active over ONE shared watermark.
--
-- The coverage row lock plus the full-fingerprint CAS would keep that SAFE — the
-- loser gets TRIPS_COVERAGE_ADVANCE_CONFLICT and overwrites nothing — but 058's
-- index exists to make concurrent recovery IMPOSSIBLE rather than merely
-- survivable, and that intent must survive the decoupling.
--
-- Both recovery uniqueness indexes are therefore re-keyed from `schedule_id` to
-- `dataset_name`, alongside the `client_id` they already carry. Today this is a
-- ZERO-BEHAVIOUR-CHANGE re-key — with one schedule per (client, dataset) the two
-- key sets are identical — and after M6 it preserves the original guarantee.
--
-- ============================================================================
-- FAILURE POLICY
-- ============================================================================
--
-- This migration is one explicit transaction. Every guard runs BEFORE any DDL,
-- and every guard RAISES rather than repairs. It never merges a coverage row,
-- never picks a winner between two rows, never rewrites a timestamp, never
-- deletes anything and never normalizes contradictory state. A contradiction is
-- an operator-visible stop, because a control plane that quietly reshapes
-- itself around bad data is exactly what M3 and M4 were built to prevent.
--
-- Re-running the migration after a successful apply is a no-op: every step is
-- guarded by IF NOT EXISTS / IF EXISTS or by an idempotent catalog predicate.

BEGIN;


-- ---------------------------------------------------------------------------
-- 0) Preflight — the parent objects must already have the shape this binds to.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
  IF to_regclass('workflow_a_control.client_dataset_schedule') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_schedule is absent; migrations 012 and 014 must be applied before 062';
  END IF;

  IF to_regclass('workflow_a_control.client_dataset_coverage') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_coverage is absent; migration 057 must be applied before 062';
  END IF;

  IF to_regclass('workflow_a_control.client_dataset_recovery_run') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_recovery_run is absent; migration 058 must be applied before 062';
  END IF;

  -- The coverage primary key is the rollback-compatibility anchor documented in
  -- the header. If something has already removed or re-keyed it, the assumption
  -- this migration reasons from is void and it must not proceed.
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint c
     WHERE c.conrelid = 'workflow_a_control.client_dataset_coverage'::regclass
       AND c.conname  = 'pk_client_dataset_coverage'
       AND c.contype  = 'p'
  ) THEN
    RAISE EXCEPTION
      'pk_client_dataset_coverage is absent or is not a primary key; the M5 rollback-compatibility contract cannot be established';
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 1) Guard — the schedule uniqueness this migration replaces must be the exact
--    constraint migration 012 installed, over exactly (client_id, dataset_name).
--
--    This is not ceremony. The whole justification for labelling every existing
--    row `DAILY` is that this constraint has made "one row per (client,
--    dataset)" true for the entire life of the table. If the constraint is
--    missing, or covers different columns, that justification is gone and the
--    labelling would become an inference.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  observed_columns TEXT;
BEGIN
  -- Already re-keyed by a previous successful apply of this migration: nothing
  -- to prove, the idempotent branch below handles it.
  IF EXISTS (
    SELECT 1
      FROM pg_attribute a
     WHERE a.attrelid = 'workflow_a_control.client_dataset_schedule'::regclass
       AND a.attname  = 'run_type'
       AND a.attnum   > 0
       AND NOT a.attisdropped
  ) THEN
    RETURN;
  END IF;

  SELECT string_agg(a.attname, ',' ORDER BY k.ord)
    INTO observed_columns
    FROM pg_constraint c
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid
     AND a.attnum   = k.attnum
   WHERE c.conrelid = 'workflow_a_control.client_dataset_schedule'::regclass
     AND c.conname  = 'uq_client_dataset_schedule'
     AND c.contype  = 'u';

  IF observed_columns IS NULL THEN
    RAISE EXCEPTION
      'uq_client_dataset_schedule is absent; 062 will not label existing schedule rows DAILY without the constraint that proves there is exactly one row per (client_id, dataset_name)';
  END IF;

  IF observed_columns IS DISTINCT FROM 'client_id,dataset_name' THEN
    RAISE EXCEPTION
      'uq_client_dataset_schedule covers (%), expected (client_id,dataset_name); the M5 re-key preconditions do not hold',
      observed_columns;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 2) Guard — prove the one-row property from the DATA as well as the catalog.
--
--    A constraint can be dropped and recreated NOT VALID, a table can be
--    restored from a dump with constraints deferred, and a ledger can disagree
--    with reality. The physical check is cheap and is the only one that is
--    authoritative about the rows actually present.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  offenders TEXT;
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_attribute a
     WHERE a.attrelid = 'workflow_a_control.client_dataset_schedule'::regclass
       AND a.attname  = 'run_type'
       AND a.attnum   > 0
       AND NOT a.attisdropped
  ) THEN
    RETURN;
  END IF;

  SELECT string_agg(format('%s/%s x%s', d.client_id, d.dataset_name, d.n), '; ')
    INTO offenders
    FROM (
      SELECT client_id, dataset_name, count(*) AS n
        FROM workflow_a_control.client_dataset_schedule
       GROUP BY client_id, dataset_name
      HAVING count(*) > 1
       ORDER BY 1, 2
       LIMIT 20
    ) AS d;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'client_dataset_schedule already holds more than one row for at least one (client_id, dataset_name): %. 062 cannot classify them as base schedules; resolve this by review before migrating',
      offenders;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 3) Guard — coverage must already satisfy the target invariant.
--
--    The migration NEVER merges, picks a winner, or rewrites a watermark. If two
--    coverage rows exist for one dataset, that is a genuine split truth and the
--    only correct machine response is to stop and hand it to a human.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  offenders TEXT;
BEGIN
  SELECT string_agg(format('%s/%s x%s', d.client_id, d.dataset_name, d.n), '; ')
    INTO offenders
    FROM (
      SELECT client_id, dataset_name, count(*) AS n
        FROM workflow_a_control.client_dataset_coverage
       GROUP BY client_id, dataset_name
      HAVING count(*) > 1
       ORDER BY 1, 2
       LIMIT 20
    ) AS d;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'client_dataset_coverage already holds more than one watermark for at least one (client_id, dataset_name): %. M5 makes one dataset one watermark; 062 will not merge, choose or rewrite coverage rows',
      offenders;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 4) Guard — the recovery re-key must not be violated by existing rows.
--
--    Re-keying the two uniqueness indexes is only safe if the current data
--    already satisfies the stricter keys. It does, whenever step 2 held, but
--    prove it rather than derive it: `client_dataset_recovery_run` is historical
--    evidence and may contain rows for schedules that no longer exist.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  active_offenders   TEXT;
  approval_offenders TEXT;
BEGIN
  SELECT string_agg(format('%s/%s x%s', d.client_id, d.dataset_name, d.n), '; ')
    INTO active_offenders
    FROM (
      SELECT client_id, dataset_name, count(*) AS n
        FROM workflow_a_control.client_dataset_recovery_run
       WHERE status IN ('PLANNED', 'RUNNING')
       GROUP BY client_id, dataset_name
      HAVING count(*) > 1
       ORDER BY 1, 2
       LIMIT 20
    ) AS d;

  IF active_offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'more than one non-terminal recovery already exists for at least one (client_id, dataset_name): %. Resolve the concurrent recoveries before M5 re-keys recovery exclusivity to the shared watermark',
      active_offenders;
  END IF;

  SELECT string_agg(
           format('%s/%s/%s..%s/%s x%s',
                  d.client_id, d.dataset_name, d.window_start_ts,
                  d.window_end_ts, d.approval_ref, d.n),
           '; ')
    INTO approval_offenders
    FROM (
      SELECT client_id, dataset_name, window_start_ts, window_end_ts,
             approval_ref, count(*) AS n
        FROM workflow_a_control.client_dataset_recovery_run
       GROUP BY client_id, dataset_name, window_start_ts, window_end_ts,
                approval_ref
      HAVING count(*) > 1
       ORDER BY 1, 2, 3, 4, 5
       LIMIT 20
    ) AS d;

  IF approval_offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'one approved recovery window already has more than one execution record once schedule_id is removed from the key: %. 062 will not delete or reclassify recovery evidence',
      approval_offenders;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 4a) Guard — coverage provenance must already agree with the coverage owner.
--
--     The composite FK installed in step 7 would reject these rows anyway, but
--     it would do so with a bare integrity error naming a constraint. A named
--     refusal that lists the offending rows is what an operator can act on, and
--     it keeps the "guards run before DDL" property that makes a refusal total.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  offenders TEXT;
BEGIN
  SELECT string_agg(
           format('coverage(%s/%s) -> schedule %s owned by (%s/%s)',
                  d.client_id, d.dataset_name, d.schedule_id,
                  d.owner_client_id, d.owner_dataset_name),
           '; ')
    INTO offenders
    FROM (
      SELECT c.client_id, c.dataset_name, c.schedule_id,
             s.client_id    AS owner_client_id,
             s.dataset_name AS owner_dataset_name
        FROM workflow_a_control.client_dataset_coverage AS c
        JOIN workflow_a_control.client_dataset_schedule AS s
          ON s.schedule_id = c.schedule_id
       WHERE s.client_id IS DISTINCT FROM c.client_id
          OR s.dataset_name IS DISTINCT FROM c.dataset_name
       ORDER BY 1, 2
       LIMIT 20
    ) AS d;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'a coverage row names a provenance schedule belonging to a different owner: %. M5 addresses coverage by (client_id, dataset_name); 062 will not reassign, repair or choose a provenance schedule',
      offenders;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 4b) Guard — recovery provenance must already agree with the recovery owner.
--
--     Owner-keyed recovery exclusivity is only as trustworthy as the identity it
--     is keyed on. A historical row whose `schedule_id` belongs to a different
--     client or dataset would make the re-keyed indexes enforce exclusivity on
--     the wrong logical owner, so it is refused rather than migrated.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  offenders TEXT;
BEGIN
  SELECT string_agg(
           format('recovery %s claims (%s/%s) but schedule %s is (%s/%s)',
                  d.recovery_run_id, d.client_id, d.dataset_name, d.schedule_id,
                  d.owner_client_id, d.owner_dataset_name),
           '; ')
    INTO offenders
    FROM (
      SELECT r.recovery_run_id, r.client_id, r.dataset_name, r.schedule_id,
             s.client_id    AS owner_client_id,
             s.dataset_name AS owner_dataset_name
        FROM workflow_a_control.client_dataset_recovery_run AS r
        JOIN workflow_a_control.client_dataset_schedule AS s
          ON s.schedule_id = r.schedule_id
       WHERE s.client_id IS DISTINCT FROM r.client_id
          OR s.dataset_name IS DISTINCT FROM r.dataset_name
       ORDER BY 1
       LIMIT 20
    ) AS d;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'a recovery row names a schedule belonging to a different owner: %. Owner-keyed recovery exclusivity cannot be installed over identities that disagree, and 062 never rewrites recovery evidence',
      offenders;
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 5) Schedule — the `run_type` discriminator.
--
--    Added nullable, backfilled for the rows step 2 proved are base schedules,
--    then made NOT NULL. The DEFAULT is set only AFTER the backfill so that the
--    backfill statement is what assigns every existing row, visibly, rather than
--    a column default silently doing it.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD COLUMN IF NOT EXISTS run_type TEXT;

UPDATE workflow_a_control.client_dataset_schedule
   SET run_type = 'DAILY'
 WHERE run_type IS NULL;

ALTER TABLE workflow_a_control.client_dataset_schedule
  ALTER COLUMN run_type SET DEFAULT 'DAILY';

ALTER TABLE workflow_a_control.client_dataset_schedule
  ALTER COLUMN run_type SET NOT NULL;

-- Closed vocabulary. A direct SQL writer cannot invent a fourth role.
ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS ck_client_dataset_schedule_run_type;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT ck_client_dataset_schedule_run_type
  CHECK (run_type IN ('DAILY', 'WEEKLY_RECONCILIATION', 'MONTHLY_RECONCILIATION'));

-- Role/cadence coherence. A reconciliation pass whose frequency contradicts its
-- role is a misconfiguration; the base role is deliberately unconstrained,
-- because base schedules legitimately fire daily, weekly (032/046) or monthly.
ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS ck_client_dataset_schedule_run_type_cadence;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT ck_client_dataset_schedule_run_type_cadence
  CHECK (
    run_type = 'DAILY'
    OR (run_type = 'WEEKLY_RECONCILIATION'  AND frequency = 'weekly')
    OR (run_type = 'MONTHLY_RECONCILIATION' AND frequency = 'monthly')
  );


-- ---------------------------------------------------------------------------
-- 6) Schedule — re-key uniqueness to (client_id, dataset_name, run_type).
--
--    The constraint KEEPS ITS NAME. Operator runbooks, review notes and the
--    repository's own tests refer to `uq_client_dataset_schedule` by name; a
--    rename would leave every one of those references silently pointing at
--    nothing, which is a worse outcome than a name whose column list changed
--    once, visibly, in a reviewed migration.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_schedule
  DROP CONSTRAINT IF EXISTS uq_client_dataset_schedule;
ALTER TABLE workflow_a_control.client_dataset_schedule
  ADD CONSTRAINT uq_client_dataset_schedule
  UNIQUE (client_id, dataset_name, run_type);

-- AT MOST ONE base schedule per (client, dataset). That is precisely what this
-- index proves and no more: a partial unique index cannot require a row to
-- exist, and requiring one globally would be wrong anyway — an enabled client
-- may legitimately own no schedule at all for a dataset.
--
-- The stronger system invariant is assembled from three objects, not this one:
-- this index (at most one base), `uq_client_dataset_coverage_dataset` (at most
-- one watermark) and the composite provenance FK with `RESTRICT` (the anchor
-- exists, belongs to the same owner, and cannot be deleted or re-owned while
-- coverage references it). Together they give: EVERY COVERAGE-BEARING DATASET
-- HAS EXACTLY ONE ANCHORING SCHEDULE OF THE SAME OWNER. That the anchor carries
-- the base role specifically is required by the supported runtime and operator
-- paths and re-checked fail-closed when the dispatcher loads coverage; see
-- OWNER COHERENCE in the header for why it is not expressible as a foreign key.
DROP INDEX IF EXISTS workflow_a_control.uq_client_dataset_schedule_base;
CREATE UNIQUE INDEX uq_client_dataset_schedule_base
  ON workflow_a_control.client_dataset_schedule (client_id, dataset_name)
  WHERE run_type = 'DAILY';

CREATE INDEX IF NOT EXISTS idx_client_dataset_schedule_run_type
  ON workflow_a_control.client_dataset_schedule (dataset_name, run_type);


-- ---------------------------------------------------------------------------
-- 7) Coverage — one watermark per (client, dataset).
--
--    ADDED alongside the retained primary key, not instead of it. See the
--    header: the retained `pk_client_dataset_coverage (schedule_id)` is the
--    rollback-compatibility anchor and the immutable provenance key, and the two
--    constraints are consistent by construction.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS uq_client_dataset_coverage_dataset;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT uq_client_dataset_coverage_dataset
  UNIQUE (client_id, dataset_name);


-- ---------------------------------------------------------------------------
-- 7a) The composite-FK target.
--
--     `schedule_id` is already the primary key, so this UNIQUE is a strict
--     superset of it and constrains the schedule table not at all. Its only job
--     is to make the triple `(schedule_id, client_id, dataset_name)` a
--     referenceable key, which is what lets a child row prove — structurally,
--     not by convention — that its provenance schedule and its stored owner are
--     the same schedule.
-- ---------------------------------------------------------------------------

--     Created only when absent, deliberately NOT drop-and-recreate: the two
--     composite foreign keys below depend on this key, so a replay that dropped
--     it first would fail with `DependentObjectsStillExist` and take the whole
--     migration down. Adding `CASCADE` to force it would silently drop those
--     foreign keys — the exact integrity this migration installs — so the
--     idempotent form is the only correct one. Whether an existing key has the
--     right shape is the release gate's job, not a repair this migration may do.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conrelid = 'workflow_a_control.client_dataset_schedule'::regclass
       AND conname  = 'uq_client_dataset_schedule_owner_identity'
  ) THEN
    ALTER TABLE workflow_a_control.client_dataset_schedule
      ADD CONSTRAINT uq_client_dataset_schedule_owner_identity
      UNIQUE (schedule_id, client_id, dataset_name);
  END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 7b) Coverage provenance coherence and the base/provenance lifecycle.
--
--     Replaces 057's single-column `ON DELETE CASCADE` FK. Two changes, both
--     load-bearing after the owner re-key (see OWNER COHERENCE in the header):
--
--       * COMPOSITE — the referenced schedule must be the same owner;
--       * RESTRICT — a schedule that anchors a shared watermark cannot be
--         deleted or re-owned. The cascade was correct for a private watermark
--         and is destructive for a shared one.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_coverage
  DROP CONSTRAINT IF EXISTS fk_client_dataset_coverage_schedule;
ALTER TABLE workflow_a_control.client_dataset_coverage
  ADD CONSTRAINT fk_client_dataset_coverage_schedule
  FOREIGN KEY (schedule_id, client_id, dataset_name)
  REFERENCES workflow_a_control.client_dataset_schedule
             (schedule_id, client_id, dataset_name)
  ON DELETE RESTRICT
  ON UPDATE RESTRICT;


-- ---------------------------------------------------------------------------
-- 7c) Recovery provenance coherence.
--
--     058's FK was already `ON DELETE RESTRICT`, which stays correct; only the
--     coherence half is new. Without it the owner-keyed exclusivity indexes
--     installed in step 8 could serialize the wrong logical owner.
-- ---------------------------------------------------------------------------

ALTER TABLE workflow_a_control.client_dataset_recovery_run
  DROP CONSTRAINT IF EXISTS fk_client_dataset_recovery_run_schedule;
ALTER TABLE workflow_a_control.client_dataset_recovery_run
  ADD CONSTRAINT fk_client_dataset_recovery_run_schedule
  FOREIGN KEY (schedule_id, client_id, dataset_name)
  REFERENCES workflow_a_control.client_dataset_schedule
             (schedule_id, client_id, dataset_name)
  ON DELETE RESTRICT
  ON UPDATE RESTRICT;


-- ---------------------------------------------------------------------------
-- 8) Recovery — exclusivity and approval idempotency follow the watermark.
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS workflow_a_control.uq_client_dataset_recovery_run_active;
CREATE UNIQUE INDEX uq_client_dataset_recovery_run_active
  ON workflow_a_control.client_dataset_recovery_run (client_id, dataset_name)
  WHERE status IN ('PLANNED', 'RUNNING');

DROP INDEX IF EXISTS workflow_a_control.uq_client_dataset_recovery_run_approved_window;
CREATE UNIQUE INDEX uq_client_dataset_recovery_run_approved_window
  ON workflow_a_control.client_dataset_recovery_run
     (client_id, dataset_name, window_start_ts, window_end_ts, approval_ref);


-- ---------------------------------------------------------------------------
-- 9) Documented semantics. These comments are the durable answer to "what does
--    this column mean now", and are read by operators long after the migration.
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN workflow_a_control.client_dataset_schedule.run_type IS
  'M5 role discriminator: DAILY is the single base ingestion schedule for the '
  '(client, dataset); WEEKLY_RECONCILIATION (M6) and MONTHLY_RECONCILIATION (M7) '
  'are additive reconciliation passes. Orthogonal to `frequency`, which says how '
  'often the row fires — a base schedule may legitimately be weekly or monthly.';

COMMENT ON COLUMN workflow_a_control.client_dataset_coverage.schedule_id IS
  'M5: IMMUTABLE PROVENANCE, not the coverage identity. It names the schedule '
  'that seeded this watermark and is written exactly once, by the bootstrap '
  'writer. No advancement, gap transition or recovery ever rewrites it, so it '
  'does not track "last advancer" and must not be read as such. The coverage '
  'row is owned by (client_id, dataset_name); every cadence over that dataset '
  'shares this one row and one compare-and-swap. The column is retained as a '
  'unique key so a pre-M5 release addressing coverage by schedule_id still '
  'resolves exactly one row after this migration.';

COMMENT ON CONSTRAINT fk_client_dataset_coverage_schedule
  ON workflow_a_control.client_dataset_coverage IS
  'M5: composite provenance FK. Proves the anchoring schedule belongs to the '
  'SAME (client_id, dataset_name) the coverage row is addressed by — 057''s '
  'single-column FK proved only that some schedule existed. RESTRICT on both '
  'DELETE and UPDATE: the watermark is shared after M5, so deleting or re-owning '
  'its anchor would destroy or silently re-anchor a dataset''s coverage. 057''s '
  'ON DELETE CASCADE was correct only while the watermark was private.';

COMMENT ON CONSTRAINT fk_client_dataset_recovery_run_schedule
  ON workflow_a_control.client_dataset_recovery_run IS
  'M5: composite provenance FK. The owner-keyed recovery uniqueness indexes are '
  'only trustworthy if a recovery row''s schedule_id, client_id and dataset_name '
  'describe one schedule; this makes that structural rather than conventional.';

COMMENT ON INDEX workflow_a_control.uq_client_dataset_schedule_base IS
  'M5: AT MOST ONE base schedule per (client_id, dataset_name). Not "exactly '
  'one": an enabled client may legitimately own no schedule for a dataset. The '
  '"exactly one" invariant holds in the coverage-bearing domain, where it is '
  'enforced by uq_client_dataset_coverage_dataset plus the composite provenance '
  'FK with ON DELETE/UPDATE RESTRICT.';

COMMENT ON INDEX workflow_a_control.uq_client_dataset_recovery_run_active IS
  'M5: at most one non-terminal recovery per (client_id, dataset_name) — i.e. '
  'per shared coverage watermark. Re-keyed from schedule_id by migration 062, '
  'because M5 decouples schedule identity from watermark ownership and the '
  'original intent was watermark exclusivity.';

COMMIT;
