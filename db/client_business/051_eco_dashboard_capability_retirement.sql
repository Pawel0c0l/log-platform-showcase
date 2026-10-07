-- 051_eco_dashboard_capability_retirement.sql
-- Workflow A — Driver Eco Dashboard V1: EXPIRED-CAPABILITY RETIREMENT.
--
-- WHY A THIRD MIGRATION.
--
-- `049` and `050` are APPLIED, and applied migrations are immutable here
-- (`AGENTS.md` §4, `CONVENTIONS.md` §12); `scripts/apply_client_business_migrations.py`
-- keys on the FILENAME, so an edited 049/050 is a file that will never execute
-- again anywhere it already ran. The reviewed contract arrives forward, or not
-- at all.
--
-- WHAT IT CHANGES, AND THE PRODUCT DECISION BEHIND IT.
--
-- Capability lifetime is now PERIOD-SCOPED — a weekly grant lives 10 days, a
-- monthly grant 60 (`delivery/driver_eco_dashboard/worker/lib/capability_ttl.js`)
-- — and publishing a newer period never revokes an older still-valid link.
-- Both of those make expiry an ordinary, frequent event rather than a
-- once-in-45-days curiosity, and the 049/050 state model had no way to say what
-- an expired delivery IS:
--
--   * a delivery in `EXTERNAL_MAILER_HANDOFF` retains the RAW bearer, on
--     purpose, so a rerun of the same period hands over the identical link.
--     050 already documented that the retention is bounded by the grant's own
--     lifetime — but the only thing that enforced the bound was a rerun. A
--     historical period nobody ever re-mails therefore kept live secret
--     material for as long as the row existed;
--   * every state except `FINALIZED` counts as open operational work
--     (`idx_..._open`), so an expired historical delivery stayed permanently
--     in the "actionable" surface with nothing anybody could actually do
--     about it.
--
-- `CAPABILITY_RETIRED` is the missing statement: *mailing ownership completed;
-- the capability expired; no operator action is required; the audit metadata is
-- retained*. It is deliberately NOT `FINALIZED` — that state asserts a
-- completed provider delivery this ledger may never have made, and the whole
-- point of `EXTERNAL_MAILER_HANDOFF` is that `eco_*_email_send_log`, not this
-- table, is authoritative for whether a driver was mailed.
--
-- WHAT IT IS NOT. It is not a retention rule for the historical report. The R2
-- snapshot object outlives its bearer and is not deleted by anything here;
-- capability expiry and report retention are separate concerns with separate
-- owners.
--
-- SAFE WITH ROWS, NOT ONLY WITH AN EMPTY TABLE.
--
--   * no column is added, dropped or rewritten, and no row is updated;
--   * `chk_..._state` is WIDENED by one value. No existing row changes
--     classification, and no pre-051 row can be in the new state because the
--     050 CHECK made it unrepresentable;
--   * `chk_..._bearer_absent` is NARROWED for that one new state alone. Its
--     049/050 text is otherwise reproduced exactly, so every existing row
--     satisfies it identically;
--   * the new `chk_..._retired_audit` is `state <> 'CAPABILITY_RETIRED' OR …`,
--     which every pre-existing row satisfies vacuously;
--   * every `ADD CONSTRAINT` is VALIDATED — PostgreSQL verifies the existing
--     rows itself and aborts the migration rather than coercing anything. No
--     `NOT VALID` escape;
--   * `idx_..._open` is replaced by an index with the same name and one more
--     excluded state, inside the same transaction.
--
-- ATOMICITY. The runner executes this file in ONE transaction, and onboarding
-- does the same. The first `ALTER TABLE` takes ACCESS EXCLUSIVE and holds it to
-- commit, so no other session can observe a dropped-and-not-yet-re-added CHECK
-- or the moment the open-operations index does not exist, and a failure at any
-- statement leaves the 050 contract exactly as it was.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- ---------------------------------------------------------------------------
-- PRECONDITION. 051 is a DELTA on 050, and says so rather than failing
-- obscurely halfway through.
-- ---------------------------------------------------------------------------
DO $eco_dashboard_051_precondition$
BEGIN
  IF to_regclass('public.eco_dashboard_delivery_operation') IS NULL THEN
    RAISE EXCEPTION
      'eco_dashboard_delivery_operation does not exist: migration 051 upgrades '
      'the ledger migration 049 creates and migration 050 extends'
      USING ERRCODE = 'undefined_table',
            HINT = 'ECO_DASHBOARD_051_REQUIRES_049_050';
  END IF;
  IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'eco_dashboard_delivery_operation'
           AND column_name = 'external_mailer') THEN
    RAISE EXCEPTION
      'eco_dashboard_delivery_operation.external_mailer is missing: migration '
      '050 has not been applied to this database'
      USING ERRCODE = 'undefined_column',
            HINT = 'ECO_DASHBOARD_051_REQUIRES_049_050';
  END IF;
END $eco_dashboard_051_precondition$;

-- ---------------------------------------------------------------------------
-- 1. THE STATE VOCABULARY GAINS ONE VALUE
-- ---------------------------------------------------------------------------
--
-- CAPABILITY_RETIRED: the capability's validity window has passed, its raw
-- bearer no longer exists here, and this ledger requires no further action. It
-- asserts NOTHING about whether a message was sent, and NOTHING about the
-- historical snapshot. The host transition graph
-- (`delivery_contract.LEGAL_TRANSITIONS`) decides which states may reach it and
-- is deliberately not encoded in the database, exactly as 050 left it.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_state;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_state
    CHECK (state IN (
      'PREPARED',
      'BEARER_RECOVERY_REQUIRED',
      'CAPABILITY_PERSISTED',
      'EXTERNAL_MAILER_HANDOFF',
      'DELIVERY_INTENT_RECORDED',
      'PROVIDER_SUBMISSION_PENDING',
      'PROVIDER_ACCEPTED',
      'PROVIDER_AMBIGUOUS',
      'PROVIDER_REJECTED',
      'REMOTE_DELIVERED',
      'FINALIZED',
      'CAPABILITY_RETIRED',
      'OPERATOR_REQUIRED'
    ));

-- ---------------------------------------------------------------------------
-- 2. A RETIRED ROW MAY NOT HOLD A BEARER — PHYSICALLY
-- ---------------------------------------------------------------------------
--
-- This is the load-bearing half of the whole migration. The destruction of the
-- expired bearer is performed by one statement in
-- `DeliveryLedger.retire_expired_capabilities`, but a rule enforced only by the
-- application is a rule one defect away from silently not holding. With this
-- constraint, a row that reached `CAPABILITY_RETIRED` while still carrying
-- secret material is not merely a bug — it is unrepresentable.
--
-- The rest of the expression is the 049/050 text, unchanged.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_bearer_absent;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_bearer_absent
    CHECK (state NOT IN ('PREPARED', 'BEARER_RECOVERY_REQUIRED', 'FINALIZED',
                         'CAPABILITY_RETIRED')
           OR capability_secret IS NULL);

-- ---------------------------------------------------------------------------
-- 3. ...AND IT MUST STILL BE ABLE TO ANSWER *WHICH* GRANT EXPIRED
-- ---------------------------------------------------------------------------
--
-- Retirement minimises secrets; it does not erase history. The non-secret audit
-- identity is exactly what makes an expired link explainable months later —
-- which grant this delivery held, which generation it was, and when it stopped
-- working — so a retired row that had forgotten all of it would be a row that
-- destroyed the evidence along with the secret. `capability_digest` is a
-- one-way digest of a 256-bit CSPRNG value and is not a route back to the
-- bearer.
--
-- Every state that can legally retire (`CAPABILITY_PERSISTED`,
-- `EXTERNAL_MAILER_HANDOFF`, `DELIVERY_INTENT_RECORDED`, `PROVIDER_REJECTED`)
-- already required all four facts, directly or through the state it came from,
-- so this constraint refuses a defect rather than adding a new obligation.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_retired_audit;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_retired_audit
    CHECK (
      state <> 'CAPABILITY_RETIRED'
      OR (capability_id IS NOT NULL
          AND capability_digest IS NOT NULL
          AND capability_expires_at IS NOT NULL
          AND bearer_generation >= 1)
    );

-- NOTE ON THE CONSTRAINTS THAT ARE DELIBERATELY *NOT* TOUCHED.
--
--   chk_..._bearer_present     `CAPABILITY_RETIRED` is absent from its state
--                              list, so a retired row is not required to hold
--                              a bearer. Correct: it must not.
--   chk_..._provider_unbound   also absent, and that is deliberate rather than
--                              an oversight: a row retired from
--                              `DELIVERY_INTENT_RECORDED` or
--                              `PROVIDER_REJECTED` KEEPS its immutable
--                              submission identity, which is audit evidence
--                              the guard trigger already refuses to change.
--   chk_..._provider_key_present  likewise absent, so retirement neither
--                              requires nor forbids a binding — whichever the
--                              source state had, survives.
--   chk_..._operator_flag      unchanged. Retirement sets
--                              `operator_action_required = FALSE`, which the
--                              existing expression already permits for any
--                              state outside the three that demand it.
--   chk_..._finalized          unchanged, and it is what keeps the two terminal
--                              conditions distinct: retirement never sets
--                              `finalized_at`, so `CAPABILITY_RETIRED` can
--                              never masquerade as a completed delivery.
--   the row guard trigger      unchanged. Retirement writes no identity field,
--                              no ownership field and no provider field, and
--                              the bearer it clears is never copied anywhere.

-- ---------------------------------------------------------------------------
-- 4. THE OPERATIONAL SURFACE STOPS COUNTING EXPIRED HISTORY AS WORK
-- ---------------------------------------------------------------------------
--
-- 049's `idx_..._open` backs `DeliveryLedger.open_operations()`, whose question
-- is "what still needs something to happen?". A retired delivery does not, so
-- it leaves the index for the same reason `FINALIZED` was never in it. Same
-- name, same shape, one more excluded value — `open_operations()` and this
-- predicate are kept in step by `delivery_contract.CLOSED_STATES`.
--
-- Note what stays: a delivery an external mailer owns whose capability is
-- STILL LIVE remains visible, because a rerun may legitimately ask for that
-- link again. Only the dead ones leave.
DROP INDEX IF EXISTS public.idx_eco_dashboard_delivery_operation_open;
CREATE INDEX IF NOT EXISTS idx_eco_dashboard_delivery_operation_open
  ON public.eco_dashboard_delivery_operation (state, updated_at)
  WHERE state NOT IN ('FINALIZED', 'CAPABILITY_RETIRED');

-- The sweep's own access path. It selects the retirable states ordered by
-- capability expiry and takes a bounded batch, so a partial index on exactly
-- that predicate keeps the housekeeping a small indexed range scan on a ledger
-- of any size instead of a sequential scan of every delivery ever made.
--
-- The state list mirrors `delivery_contract.RETIRABLE_ON_EXPIRY_STATES`. A
-- partial index is an optimisation, never the rule: the statement carries its
-- own full predicate, so an index that fell behind the contract would cost
-- speed and could never widen what the sweep is allowed to touch.
CREATE INDEX IF NOT EXISTS idx_eco_dashboard_delivery_operation_expiring
  ON public.eco_dashboard_delivery_operation (capability_expires_at)
  WHERE state IN ('CAPABILITY_PERSISTED', 'EXTERNAL_MAILER_HANDOFF',
                  'DELIVERY_INTENT_RECORDED', 'PROVIDER_REJECTED')
    AND capability_expires_at IS NOT NULL;

COMMENT ON TABLE public.eco_dashboard_delivery_operation IS
  'Driver Eco Dashboard V1 host-side publication/e-mail delivery ledger. One row per logical delivery. capability_secret is ephemeral raw bearer material, cleared at finalisation and destroyed once the grant expires (state CAPABILITY_RETIRED).';

-- ---------------------------------------------------------------------------
-- RUNTIME PRIVILEGES — deliberately unchanged.
-- ---------------------------------------------------------------------------
--
-- 049 granted SELECT, INSERT and UPDATE on the TABLE. Retirement is an UPDATE
-- of existing rows and needs nothing more; DELETE is still not granted and
-- still not wanted, because nothing in this lifecycle removes a row. Re-running
-- the grant block here would only risk widening privileges on a database whose
-- grants an operator has since narrowed.
