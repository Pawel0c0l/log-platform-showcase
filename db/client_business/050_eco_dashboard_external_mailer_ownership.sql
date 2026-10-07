-- 050_eco_dashboard_external_mailer_ownership.sql
-- Workflow A — Driver Eco Dashboard V1: EXTERNAL SEND-ACCOUNTING OWNERSHIP.
--
-- WHY A SECOND MIGRATION AND NOT AN EDIT OF 049.
--
-- `049_eco_dashboard_delivery_operation.sql` is APPLIED. Every enabled client
-- business database recorded it on 2026-08-19 ~19:55 CEST, in the physical form
-- that file had at commit a5069cd — 43 columns, 32 constraints, no
-- `external_mailer`, no EXTERNAL_MAILER_HANDOFF state and the pre-review guard
-- body. Applied migrations are immutable in this repository
-- (`AGENTS.md` §4, `CONVENTIONS.md` §12), and the runner is right to be:
-- `scripts/apply_client_business_migrations.py` keys on the FILENAME, so an
-- edited 049 is a file that will never execute again anywhere it already ran.
-- The reviewed contract therefore has to arrive as a forward migration, or it
-- does not arrive at all.
--
-- WHAT THE REVIEWED CONTRACT ADDS, AND WHY EACH PART IS PHYSICAL.
--
-- The Driver Eco Dashboard link is delivered by the EXISTING Eco Driving
-- weekly/monthly notification jobs, not by this ledger's own provider
-- lifecycle. `external_mailer` records WHICH lifecycle owns a delivery's send
-- accounting, and the whole point is that the answer is decided by the INSERT
-- that creates the row and by nothing afterwards: a row created without an
-- owner and annotated later has a window in which the provider lifecycle is
-- entitled to adopt it, and adopting it means a second, dashboard-specific
-- message to a driver `eco_*_email_send_log` already accounts for.
--
-- So ownership is bound at creation (host side), the guard trigger refuses
-- every later change in BOTH directions, and the provider lifecycle is made
-- physically unreachable for an owned row rather than merely discouraged.
--
-- SAFE WITH ROWS, NOT ONLY WITH AN EMPTY TABLE.
--
--   * `external_mailer` is added NULLable with no default, so every existing
--     row means exactly what it meant before: "this ledger's own provider
--     lifecycle owns the send".
--   * The three replaced CHECKs differ from their 049 form ONLY by admitting
--     `EXTERNAL_MAILER_HANDOFF`. `chk_..._state` is widened; the other two are
--     narrowed for that one state alone — and no pre-050 row can be in it,
--     because the 049 `chk_..._state` made it unrepresentable. No existing row
--     changes classification.
--   * The three new CHECKs are all `external_mailer IS NULL OR ...`, which
--     every pre-existing row satisfies vacuously.
--   * Each `ADD CONSTRAINT` is VALIDATED, so PostgreSQL verifies the existing
--     rows itself. A row that somehow contradicted the reviewed contract aborts
--     the migration; nothing is rewritten, coerced or dropped anywhere in this
--     file. Fail-closed, and no `NOT VALID` escape.
--
-- ATOMICITY AND THE DROP/ADD WINDOW.
--
-- The migration runner executes this file inside ONE transaction
-- (`autocommit=False`, one commit per file), and onboarding does the same. The
-- first `ALTER TABLE` takes ACCESS EXCLUSIVE on the relation and holds it to
-- commit, so the interval in which a replaced CHECK has been dropped and not
-- yet re-added is not observable by any other session, and a failure at any
-- statement leaves the 049 contract exactly as it was. `lock_timeout` makes
-- that lock fail fast rather than queue behind a long reader.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- ---------------------------------------------------------------------------
-- PRECONDITION. 050 is a DELTA, and says so rather than failing obscurely.
-- ---------------------------------------------------------------------------
DO $eco_dashboard_050_precondition$
BEGIN
  IF to_regclass('public.eco_dashboard_delivery_operation') IS NULL THEN
    RAISE EXCEPTION
      'eco_dashboard_delivery_operation does not exist: migration 050 upgrades '
      'the ledger migration 049 creates and cannot install it'
      USING ERRCODE = 'undefined_table',
            HINT = 'ECO_DASHBOARD_050_REQUIRES_049';
  END IF;
END $eco_dashboard_050_precondition$;

-- ---------------------------------------------------------------------------
-- 1. OWNERSHIP COLUMN
-- ---------------------------------------------------------------------------
--
-- WHO OWNS THIS DELIVERY'S SEND ACCOUNTING. Bound at INSERT and never
-- afterwards (the guard trigger below refuses every change, including
-- NULL -> value), because ownership decided later is ownership that does not
-- exist during the window that matters: a crash between "row created" and
-- "ownership annotated" would leave a row the provider lifecycle is entitled
-- to adopt and turn into a second, dashboard-specific message to a driver the
-- Eco jobs are already mailing.
--
--   NULL      this ledger's own provider lifecycle owns the send.
--   non-NULL  an EXTERNAL mailing lifecycle owns it, and the value names
--             which one (an existing Eco weekly/monthly notification job).
--             `eco_*_email_send_log` is then the sole authority for whether
--             a driver was mailed.
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD COLUMN IF NOT EXISTS external_mailer TEXT;

-- ---------------------------------------------------------------------------
-- 2. THE THREE 049 STATE RULES THAT MUST ADMIT EXTERNAL_MAILER_HANDOFF
-- ---------------------------------------------------------------------------
--
-- PostgreSQL cannot alter a CHECK expression in place, so each is dropped and
-- recreated with its 049 text plus the new state. Nothing else in any of the
-- three changes; `DROP ... IF EXISTS` keeps the file deterministic rather than
-- order-dependent on drift.

-- The state vocabulary. EXTERNAL_MAILER_HANDOFF: the capability was handed to
-- an EXTERNAL mailing lifecycle (the existing Eco Driving weekly/monthly jobs
-- and their eco_*_email_send_log), which owns the send accounting from there
-- on. Terminal for this ledger's automation and provider-unbound forever: it
-- asserts nothing about whether a message was sent, only that this side has
-- nothing left to decide.
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
      'OPERATOR_REQUIRED'
    ));

-- A state that may construct or reconcile a message must hold the bearer.
-- EXTERNAL_MAILER_HANDOFF joins that set for the same kind of reason
-- PROVIDER_AMBIGUOUS is in it: its contract is to answer "what is this driver's
-- link for this period?" identically on every rerun of the external mailer, and
-- a row that has forgotten the bearer can only answer that by rotating the
-- capability.
--
-- RETENTION IS BOUNDED BY THE GRANT, NOT BY THE STATE. This constraint says a
-- row IN that state holds a bearer; it does not say a dead bearer is kept
-- because the state is terminal. A capability past `capability_expires_at` can
-- no longer answer the question the state exists to answer, so the delivery
-- leaves for BEARER_RECOVERY_REQUIRED — which the bearer_absent constraint
-- requires to hold no bearer at all — and rotates through the explicit recovery
-- operation. Expired bearer material is therefore destroyed in the same
-- statement that gives up on it. The transition graph itself is host-side
-- (delivery_contract.LEGAL_TRANSITIONS), deliberately not encoded here.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_bearer_present;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_bearer_present
    CHECK (
      state NOT IN ('CAPABILITY_PERSISTED', 'EXTERNAL_MAILER_HANDOFF',
                    'DELIVERY_INTENT_RECORDED',
                    'PROVIDER_SUBMISSION_PENDING', 'PROVIDER_ACCEPTED',
                    'PROVIDER_AMBIGUOUS')
      OR (capability_id IS NOT NULL AND capability_secret IS NOT NULL
          AND capability_digest IS NOT NULL AND bearer_persisted_at IS NOT NULL
          AND bearer_generation >= 1)
    );

-- EXTERNAL_MAILER_HANDOFF is provider-unbound PERMANENTLY rather than merely
-- not-yet-bound: that branch of the lifecycle never contacts a provider at all,
-- so a row carrying a submission identity there would describe a delivery this
-- ledger both handed away and claims to be sending itself.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_provider_unbound;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_provider_unbound
    CHECK (
      state NOT IN ('PREPARED', 'BEARER_RECOVERY_REQUIRED', 'CAPABILITY_PERSISTED',
                    'EXTERNAL_MAILER_HANDOFF')
      OR (provider_name IS NULL
          AND provider_idempotency_key IS NULL
          AND provider_backend_id IS NULL
          AND provider_message_fingerprint IS NULL
          AND provider_bound_capability_id IS NULL
          AND provider_bound_bearer_generation IS NULL)
    );

-- ---------------------------------------------------------------------------
-- 3. EXTERNAL OWNERSHIP IS A PHYSICAL REFUSAL, NOT A CONVENTION
-- ---------------------------------------------------------------------------
--
-- A row an external mailer owns can never carry a provider submission identity,
-- in ANY state. Combined with `chk_..._provider_key_present` — which makes
-- every provider-driven state require the whole binding — this makes the
-- provider lifecycle's states unreachable for an externally-owned delivery at
-- the level of the database, so a defect in the host code cannot produce a
-- second message under a delivery the Eco send log already accounts for.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_external_mailer;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_external_mailer
    CHECK (external_mailer IS NULL OR btrim(external_mailer) <> '');

ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_external_mailer_unbound;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_external_mailer_unbound
    CHECK (
      external_mailer IS NULL
      OR (provider_name IS NULL
          AND provider_idempotency_key IS NULL
          AND provider_backend_id IS NULL
          AND provider_message_fingerprint IS NULL
          AND provider_bound_capability_id IS NULL
          AND provider_bound_bearer_generation IS NULL
          AND provider_message_id IS NULL
          AND provider_submitted_at IS NULL
          AND provider_accepted_at IS NULL
          AND remote_delivered_at IS NULL
          AND provider_attempts = 0)
    );

-- ...AND THE HANDOFF STATE CANNOT BE REACHED WITHOUT IT. The state is the
-- consequence of ownership, never its source.
ALTER TABLE public.eco_dashboard_delivery_operation
  DROP CONSTRAINT IF EXISTS chk_eco_dashboard_delivery_operation_handoff_owned;
ALTER TABLE public.eco_dashboard_delivery_operation
  ADD CONSTRAINT chk_eco_dashboard_delivery_operation_handoff_owned
    CHECK (state <> 'EXTERNAL_MAILER_HANDOFF' OR external_mailer IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 4. THE ROW GUARD, REPLACED WITH THE REVIEWED BODY
-- ---------------------------------------------------------------------------
--
-- Identical to 049's guard except for the ownership-immutability block. It is
-- CREATE OR REPLACE, which preserves the function OID, so the trigger 049
-- created stays bound to it and no trigger is dropped or recreated here — the
-- window in which the relation would carry no guard at all never exists.
--
-- OWNERSHIP IS PART OF THE DELIVERY IDENTITY, AND IT IS IMMUTABLE IN BOTH
-- DIRECTIONS. Refusing only `value -> other value` would still allow the late
-- binding `external_mailer` exists to eliminate, and refusing only
-- `value -> NULL` would allow a provider-owned row to be adopted by an external
-- mailer after the fact. Whatever the INSERT decided stands.
CREATE OR REPLACE FUNCTION public.eco_dashboard_delivery_operation_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $eco_dashboard_guard$
DECLARE
  diagnostics TEXT;
BEGIN
  diagnostics := COALESCE(NEW.failure_code, '')
    || E'\n' || COALESCE(NEW.failure_detail, '')
    || E'\n' || COALESCE(NEW.failure_phase, '')
    || E'\n' || COALESCE(NEW.metadata_json::text, '');

  IF NEW.capability_secret IS NOT NULL
     AND position(NEW.capability_secret IN diagnostics) > 0 THEN
    RAISE EXCEPTION
      'eco_dashboard_delivery_operation: a raw capability bearer may not be '
      'written into failure_code, failure_phase, failure_detail or metadata_json'
      USING ERRCODE = 'check_violation',
            HINT = 'ECO_DASHBOARD_BEARER_IN_DIAGNOSTICS';
  END IF;

  IF TG_OP = 'UPDATE' AND OLD.capability_secret IS NOT NULL
     AND position(OLD.capability_secret IN diagnostics) > 0 THEN
    -- THE COPY-AND-CLEAR CASE. The bearer being cleared in the same statement
    -- is precisely why the OLD row has to be consulted.
    RAISE EXCEPTION
      'eco_dashboard_delivery_operation: the raw capability bearer this row '
      'held may not be copied into a diagnostic field'
      USING ERRCODE = 'check_violation',
            HINT = 'ECO_DASHBOARD_BEARER_IN_DIAGNOSTICS';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    IF OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.subject_ref IS DISTINCT FROM NEW.subject_ref
       OR OLD.payload_digest IS DISTINCT FROM NEW.payload_digest
       OR OLD.recipient_identity IS DISTINCT FROM NEW.recipient_identity
       OR OLD.recipient_email IS DISTINCT FROM NEW.recipient_email
       OR OLD.client_id IS DISTINCT FROM NEW.client_id
       OR OLD.identity_key IS DISTINCT FROM NEW.identity_key THEN
      RAISE EXCEPTION
        'eco_dashboard_delivery_operation: the delivery identity is immutable'
        USING ERRCODE = 'check_violation',
              HINT = 'ECO_DASHBOARD_DELIVERY_IDENTITY_IMMUTABLE';
    END IF;

    -- OWNERSHIP IS PART OF THAT IDENTITY, AND IT IS IMMUTABLE IN BOTH
    -- DIRECTIONS. Refusing only `value -> other value` would still allow the
    -- late binding this column exists to eliminate, and refusing only
    -- `value -> NULL` would allow a provider-owned row to be adopted by an
    -- external mailer after the fact. Whatever the INSERT decided stands.
    IF OLD.external_mailer IS DISTINCT FROM NEW.external_mailer THEN
      RAISE EXCEPTION
        'eco_dashboard_delivery_operation: delivery send-accounting ownership '
        'is bound at creation and is immutable'
        USING ERRCODE = 'check_violation',
              HINT = 'ECO_DASHBOARD_DELIVERY_OWNERSHIP_IMMUTABLE';
    END IF;

    IF (OLD.provider_idempotency_key IS NOT NULL
        AND NEW.provider_idempotency_key IS DISTINCT FROM OLD.provider_idempotency_key)
       OR (OLD.provider_name IS NOT NULL
           AND NEW.provider_name IS DISTINCT FROM OLD.provider_name)
       OR (OLD.provider_backend_id IS NOT NULL
           AND NEW.provider_backend_id IS DISTINCT FROM OLD.provider_backend_id)
       OR (OLD.provider_message_fingerprint IS NOT NULL
           AND NEW.provider_message_fingerprint IS DISTINCT FROM OLD.provider_message_fingerprint)
       OR (OLD.provider_bound_capability_id IS NOT NULL
           AND NEW.provider_bound_capability_id IS DISTINCT FROM OLD.provider_bound_capability_id)
       OR (OLD.provider_bound_bearer_generation IS NOT NULL
           AND NEW.provider_bound_bearer_generation IS DISTINCT FROM OLD.provider_bound_bearer_generation) THEN
      RAISE EXCEPTION
        'eco_dashboard_delivery_operation: the provider submission identity is '
        'immutable once bound'
        USING ERRCODE = 'check_violation',
              HINT = 'ECO_DASHBOARD_PROVIDER_BINDING_IMMUTABLE';
    END IF;
  END IF;

  RETURN NEW;
END;
$eco_dashboard_guard$;

COMMENT ON FUNCTION public.eco_dashboard_delivery_operation_guard() IS
  'Row guard for eco_dashboard_delivery_operation: refuses a raw bearer (OLD or NEW) in generic diagnostic fields, and refuses any change to the delivery identity, to external send-accounting ownership, or to a bound provider submission identity.';

COMMENT ON COLUMN public.eco_dashboard_delivery_operation.external_mailer IS
  'Names the EXTERNAL mailing lifecycle that owns this delivery''s send accounting (an existing Eco weekly/monthly notification job). Bound at INSERT, immutable afterwards. NULL means this ledger''s own provider lifecycle owns the send.';

-- ---------------------------------------------------------------------------
-- RUNTIME PRIVILEGES — deliberately unchanged.
-- ---------------------------------------------------------------------------
--
-- 049 granted SELECT, INSERT and UPDATE on the TABLE, not on a column list, and
-- a table-level grant covers columns added later. `external_mailer` is
-- therefore already readable and writable by the runtime role on every database
-- that ran 049, and re-running the grant block here would only risk widening
-- privileges on a database whose grants an operator has since narrowed.
