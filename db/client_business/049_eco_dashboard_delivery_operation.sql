-- 049_eco_dashboard_delivery_operation.sql
-- Workflow A — Driver Eco Dashboard V1 HOST-side publication/delivery ledger.
--
-- WHAT THIS IS
--
-- One durable row per LOGICAL dashboard delivery (one driver, one closed
-- reporting period, one send scope). It is the host's authoritative record of
-- the publish -> capability -> e-mail -> DELIVERED lifecycle, and it is what
-- makes a restart deterministic: every state answers exactly one question,
-- "what is the one safe next action?".
--
-- WHY IT LIVES IN THE CLIENT BUSINESS DATABASE
--
-- It binds a driver identity, a reporting period and a recipient e-mail
-- address, exactly like `eco_driving_*_email_send_log`. Those are per-client
-- business facts and must not migrate into platform state. The Cloudflare/D1
-- authorization ledger holds the complementary half (subject_ref, object key,
-- capability digest) and deliberately holds NO recipient address.
--
-- SECRET HANDLING
--
-- `capability_secret` is the RAW bearer returned exactly once by
-- `POST /api/publish` (or `/api/publish/recover`). It is ephemeral delivery
-- material, not an audit record: it exists only while an unresolved e-mail
-- delivery still needs to construct or reconcile its message, and is cleared
-- on finalisation. `capability_digest` survives cleanup so an operator can
-- still prove which grant was delivered without the value existing anywhere.
--
-- `chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics` is a
-- database-level guarantee that the raw bearer cannot be copied into the
-- generic failure text or the metadata document.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS public.eco_dashboard_delivery_operation (
  delivery_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Remote publication identity. Stable across every retry of this logical
  -- delivery; the Worker's `eco_publication_operation.operation_id`.
  operation_id TEXT NOT NULL,

  -- Logical delivery identity. Duplicate logical sends are impossible because
  -- this tuple is unique.
  client_id UUID NOT NULL,
  identity_key TEXT NOT NULL,
  period_type TEXT NOT NULL,
  period_start_date DATE NOT NULL,
  period_end_date DATE NOT NULL,
  send_scope TEXT NOT NULL DEFAULT 'normal',

  -- Secure-delivery binding. `subject_ref` is opaque and carries no identity;
  -- `payload_digest` is SHA-256 of the EXACT canonical snapshot bytes.
  subject_ref TEXT NOT NULL,
  payload_digest TEXT NOT NULL,

  -- Recipient binding. HOST-ONLY: neither value ever reaches the Worker.
  recipient_identity TEXT NOT NULL,
  recipient_email TEXT NOT NULL,

  state TEXT NOT NULL,

  -- Capability material.
  capability_id TEXT,
  capability_secret TEXT,
  capability_digest TEXT,
  capability_expires_at TIMESTAMPTZ,
  bearer_generation INTEGER NOT NULL DEFAULT 0,
  bearer_persisted_at TIMESTAMPTZ,
  bearer_cleared_at TIMESTAMPTZ,

  -- Provider/message delivery state.
  provider_name TEXT,
  provider_idempotency_key TEXT,

  -- THE IMMUTABLE SUBMISSION IDENTITY. Bound together, before the first
  -- provider call, and never changed afterwards (enforced by the guard trigger
  -- below). An idempotency key only means "the same message" relative to a
  -- backend that has heard of it and to the content it named:
  --   * provider_backend_id       WHICH provider type/account/endpoint scope
  --                               the key was issued against. Derived from
  --                               NON-SECRET scope material only, so rotating
  --                               a credential does not look like a different
  --                               backend and no secret is stored here.
  --   * provider_message_fingerprint  SHA-256 of the exact intended message
  --                               (recipient, subject, Message-ID, both
  --                               bodies). One-way, so it stores no bearer,
  --                               while still making a changed dashboard URL,
  --                               template or bearer generation detectable.
  --   * provider_bound_*          the capability the message was built from.
  --   * provider_name             WHICH adapter issued the key.
  -- All of them, plus `provider_idempotency_key` above, are bound together in
  -- one statement or not at all: see `chk_..._binding_coherent` and
  -- `chk_..._provider_unbound`.
  provider_backend_id TEXT,
  provider_message_fingerprint TEXT,
  provider_bound_capability_id TEXT,
  provider_bound_bearer_generation INTEGER,

  provider_message_id TEXT,
  provider_attempts INTEGER NOT NULL DEFAULT 0,
  provider_submitted_at TIMESTAMPTZ,
  provider_accepted_at TIMESTAMPTZ,

  remote_delivered_at TIMESTAMPTZ,
  finalized_at TIMESTAMPTZ,

  -- Non-secret operational diagnostics.
  failure_phase TEXT,
  failure_code TEXT,
  failure_detail TEXT,
  operator_action_required BOOLEAN NOT NULL DEFAULT FALSE,

  -- Durable ownership. Not an in-memory mutex: a competing invocation loses
  -- the compare-and-set and performs no work at all.
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,

  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_run_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,

  CONSTRAINT uq_eco_dashboard_delivery_operation_operation
    UNIQUE (operation_id),
  CONSTRAINT uq_eco_dashboard_delivery_operation_identity
    UNIQUE (client_id, identity_key, period_type, period_start_date,
            period_end_date, send_scope),
  CONSTRAINT uq_eco_dashboard_delivery_operation_provider_key
    UNIQUE (provider_idempotency_key),

  CONSTRAINT chk_eco_dashboard_delivery_operation_period
    CHECK (period_end_date > period_start_date),
  CONSTRAINT chk_eco_dashboard_delivery_operation_period_type
    CHECK (period_type IN ('weekly', 'monthly')),
  CONSTRAINT chk_eco_dashboard_delivery_operation_send_scope
    CHECK (send_scope IN ('normal', 'test')),
  CONSTRAINT chk_eco_dashboard_delivery_operation_operation_id
    CHECK (operation_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_payload_digest
    CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_capability_id
    CHECK (capability_id IS NULL OR capability_id ~ '^[0-9a-f]{32}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_capability_digest
    CHECK (capability_digest IS NULL OR capability_digest ~ '^[0-9a-f]{64}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_recipient
    CHECK (btrim(recipient_email) <> '' AND btrim(recipient_identity) <> ''),
  CONSTRAINT chk_eco_dashboard_delivery_operation_subject_ref
    CHECK (btrim(subject_ref) <> ''),

  CONSTRAINT chk_eco_dashboard_delivery_operation_state
    CHECK (state IN (
      'PREPARED',
      'BEARER_RECOVERY_REQUIRED',
      'CAPABILITY_PERSISTED',
      'DELIVERY_INTENT_RECORDED',
      'PROVIDER_SUBMISSION_PENDING',
      'PROVIDER_ACCEPTED',
      'PROVIDER_AMBIGUOUS',
      'PROVIDER_REJECTED',
      'REMOTE_DELIVERED',
      'FINALIZED',
      'OPERATOR_REQUIRED'
    )),

  -- A state that may construct or reconcile a message must hold the bearer.
  -- PROVIDER_AMBIGUOUS is in the set deliberately: its documented next action
  -- is a HUMAN reconciliation, and that human has to be able to establish
  -- which link a possibly-sent message carried. A row parked there without the
  -- material its own next action needs is an incomplete state, not a tidy one.
  CONSTRAINT chk_eco_dashboard_delivery_operation_bearer_present
    CHECK (
      state NOT IN ('CAPABILITY_PERSISTED', 'DELIVERY_INTENT_RECORDED',
                    'PROVIDER_SUBMISSION_PENDING', 'PROVIDER_ACCEPTED',
                    'PROVIDER_AMBIGUOUS')
      OR (capability_id IS NOT NULL AND capability_secret IS NOT NULL
          AND capability_digest IS NOT NULL AND bearer_persisted_at IS NOT NULL
          AND bearer_generation >= 1)
    ),
  -- A state that cannot hold a bearer must not hold one.
  CONSTRAINT chk_eco_dashboard_delivery_operation_bearer_absent
    CHECK (state NOT IN ('PREPARED', 'BEARER_RECOVERY_REQUIRED', 'FINALIZED')
           OR capability_secret IS NULL),
  CONSTRAINT chk_eco_dashboard_delivery_operation_bearer_cleared
    CHECK ((capability_secret IS NULL AND bearer_persisted_at IS NOT NULL)
           = (bearer_cleared_at IS NOT NULL)),

  -- Every state that implies provider intent, submission, reconciliation or a
  -- completed send must carry the WHOLE submission identity. A row that says
  -- "a message may exist under key K" without naming the backend K is
  -- meaningful to, or the message K named, does not describe one safe next
  -- action: it describes a guess.
  CONSTRAINT chk_eco_dashboard_delivery_operation_provider_key_present
    CHECK (
      state NOT IN ('DELIVERY_INTENT_RECORDED', 'PROVIDER_SUBMISSION_PENDING',
                    'PROVIDER_ACCEPTED', 'PROVIDER_AMBIGUOUS',
                    'PROVIDER_REJECTED', 'REMOTE_DELIVERED', 'FINALIZED')
      OR (provider_idempotency_key IS NOT NULL AND provider_name IS NOT NULL
          AND provider_backend_id IS NOT NULL
          AND provider_message_fingerprint IS NOT NULL
          AND provider_bound_capability_id IS NOT NULL
          AND provider_bound_bearer_generation IS NOT NULL)
    ),
  CONSTRAINT chk_eco_dashboard_delivery_operation_provider_backend_id
    CHECK (provider_backend_id IS NULL
           OR provider_backend_id ~ '^pbk_[0-9a-f]{40}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_message_fingerprint
    CHECK (provider_message_fingerprint IS NULL
           OR provider_message_fingerprint ~ '^[0-9a-f]{64}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_bound_capability
    CHECK (provider_bound_capability_id IS NULL
           OR provider_bound_capability_id ~ '^[0-9a-f]{32}$'),
  CONSTRAINT chk_eco_dashboard_delivery_operation_bound_generation
    CHECK (provider_bound_bearer_generation IS NULL
           OR provider_bound_bearer_generation >= 1),
  -- THE BINDING IS ONE SEMANTIC UNIT, AND ALL-OR-NONE IS THE WHOLE POINT.
  --
  -- The earlier form anchored coherence on `provider_backend_id`, which left
  -- the two fields written FIRST in a naive implementation — the idempotency
  -- key and the provider name — free to exist on their own. That is not a
  -- harmless partial row: the guard trigger below makes a bound field
  -- immutable the moment it is non-NULL, so a row carrying only a key can
  -- never be bound properly afterwards. It has no valid next action at all,
  -- which is precisely the property every state in this table is supposed to
  -- have. The binding therefore stands or falls as one set.
  CONSTRAINT chk_eco_dashboard_delivery_operation_binding_coherent
    CHECK (
      (provider_idempotency_key IS NULL) = (provider_name IS NULL)
      AND (provider_idempotency_key IS NULL) = (provider_backend_id IS NULL)
      AND (provider_idempotency_key IS NULL) = (provider_message_fingerprint IS NULL)
      AND (provider_idempotency_key IS NULL) = (provider_bound_capability_id IS NULL)
      AND (provider_idempotency_key IS NULL) = (provider_bound_bearer_generation IS NULL)
    ),

  -- ...AND IT MAY NOT EXIST YET BEFORE THE LIFECYCLE REACHES IT. Coherence
  -- alone would still allow a complete binding to appear in a state that has
  -- not decided one, which is the same poisoning by another route: the fields
  -- would be immutable before the step that is supposed to choose them runs.
  -- Every state up to and including CAPABILITY_PERSISTED is therefore
  -- provider-unbound by construction, and DELIVERY_INTENT_RECORDED — the
  -- binding step itself — is the first state that carries any of it.
  CONSTRAINT chk_eco_dashboard_delivery_operation_provider_unbound
    CHECK (
      state NOT IN ('PREPARED', 'BEARER_RECOVERY_REQUIRED', 'CAPABILITY_PERSISTED')
      OR (provider_name IS NULL
          AND provider_idempotency_key IS NULL
          AND provider_backend_id IS NULL
          AND provider_message_fingerprint IS NULL
          AND provider_bound_capability_id IS NULL
          AND provider_bound_bearer_generation IS NULL)
    ),

  -- Lease ownership is a PAIR. A named owner with no expiry can never be
  -- reclaimed; an expiry with no owner fences nobody. Either both are present
  -- or the row is unowned.
  CONSTRAINT chk_eco_dashboard_delivery_operation_lease_pairing
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)
           AND (lease_owner IS NULL OR btrim(lease_owner) <> '')),
  CONSTRAINT chk_eco_dashboard_delivery_operation_accepted
    CHECK (
      state NOT IN ('PROVIDER_ACCEPTED', 'REMOTE_DELIVERED', 'FINALIZED')
      OR (provider_message_id IS NOT NULL AND provider_accepted_at IS NOT NULL)
    ),
  CONSTRAINT chk_eco_dashboard_delivery_operation_remote_delivered
    CHECK (state NOT IN ('REMOTE_DELIVERED', 'FINALIZED')
           OR remote_delivered_at IS NOT NULL),
  CONSTRAINT chk_eco_dashboard_delivery_operation_finalized
    CHECK ((state = 'FINALIZED') = (finalized_at IS NOT NULL)),

  -- The flag is exactly the operational question "does a human have to act?".
  -- It is mandatory for the two states that cannot progress automatically, and
  -- impossible for any state that can.
  CONSTRAINT chk_eco_dashboard_delivery_operation_operator_flag
    CHECK (
      (state NOT IN ('PROVIDER_AMBIGUOUS', 'OPERATOR_REQUIRED')
       OR operator_action_required)
      AND (NOT operator_action_required
           OR state IN ('PROVIDER_AMBIGUOUS', 'OPERATOR_REQUIRED',
                        'PROVIDER_REJECTED'))
    ),

  -- THE raw bearer may never be copied into generic diagnostic text.
  CONSTRAINT chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics
    CHECK (
      capability_secret IS NULL
      OR (position(capability_secret IN COALESCE(failure_detail, '')) = 0
          AND position(capability_secret IN COALESCE(failure_code, '')) = 0
          AND position(capability_secret IN metadata_json::text) = 0)
    ),

  CONSTRAINT chk_eco_dashboard_delivery_operation_counters
    CHECK (attempt_count >= 0 AND provider_attempts >= 0
           AND bearer_generation >= 0),
  CONSTRAINT chk_eco_dashboard_delivery_operation_metadata
    CHECK (jsonb_typeof(metadata_json) = 'object')
);

-- ---------------------------------------------------------------------------
-- ROW GUARD. What a CHECK constraint structurally cannot do.
-- ---------------------------------------------------------------------------
--
-- WHY A TRIGGER AND NOT ONLY A CHECK.
--
-- A CHECK constraint sees the NEW row and nothing else, so it can only compare
-- diagnostics against a bearer the row still holds. That is exactly one UPDATE
-- away from useless:
--
--     UPDATE ... SET failure_detail = capability_secret,
--                    capability_secret = NULL;
--
-- The resulting row contains the bearer in `failure_detail` and no longer
-- contains the source to compare it with, so the CHECK passes and the secret is
-- persisted in a column meant for operator-readable text. Answering that
-- requires the OLD row, which only a trigger has.
--
-- The `chk_..._no_bearer_in_diagnostics` CHECK is kept as well. Two independent
-- controls covering the same invariant from different directions is the same
-- posture the Python-side scrubbing already takes; neither is trusted alone.
--
-- SCOPE, STATED HONESTLY. This is not a universal secret scanner and cannot be
-- one. The guarantee is bounded and checkable: the raw bearer THIS ROW holds or
-- held cannot be written into a generic diagnostic field by any supported write,
-- whether alone, embedded in surrounding text, or nested inside the metadata
-- document.
--
-- The same trigger enforces the other thing a CHECK cannot express: that an
-- identity, once durable, never changes. The provider submission identity is
-- the load-bearing case — an idempotency key whose backend or message could be
-- edited afterwards would be a promise that quietly stops being true.

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
  'Row guard for eco_dashboard_delivery_operation: refuses a raw bearer (OLD or NEW) in generic diagnostic fields, and refuses any change to the delivery identity or to a bound provider submission identity.';

DROP TRIGGER IF EXISTS trg_eco_dashboard_delivery_operation_guard
  ON public.eco_dashboard_delivery_operation;
CREATE TRIGGER trg_eco_dashboard_delivery_operation_guard
  BEFORE INSERT OR UPDATE ON public.eco_dashboard_delivery_operation
  FOR EACH ROW EXECUTE FUNCTION public.eco_dashboard_delivery_operation_guard();

CREATE INDEX IF NOT EXISTS idx_eco_dashboard_delivery_operation_open
  ON public.eco_dashboard_delivery_operation (state, updated_at)
  WHERE state <> 'FINALIZED';

CREATE INDEX IF NOT EXISTS idx_eco_dashboard_delivery_operation_operator
  ON public.eco_dashboard_delivery_operation (updated_at DESC)
  WHERE operator_action_required;

CREATE INDEX IF NOT EXISTS idx_eco_dashboard_delivery_operation_period
  ON public.eco_dashboard_delivery_operation
  (client_id, period_type, period_start_date, period_end_date);

COMMENT ON TABLE public.eco_dashboard_delivery_operation IS
  'Driver Eco Dashboard V1 host-side publication/e-mail delivery ledger. One row per logical delivery. capability_secret is ephemeral raw bearer material, cleared at finalisation.';
COMMENT ON COLUMN public.eco_dashboard_delivery_operation.capability_secret IS
  'RAW capability bearer. Ephemeral delivery material; never logged, never in a URL query string, cleared on finalisation.';
COMMENT ON COLUMN public.eco_dashboard_delivery_operation.recipient_email IS
  'Host-only recipient address. Never sent to the Cloudflare Worker or stored in D1 authorization state.';

-- ---------------------------------------------------------------------------
-- RUNTIME PRIVILEGES
-- ---------------------------------------------------------------------------
--
-- The publisher runs as the per-client runtime role, not as the schema owner,
-- so a table the owner can use and the runtime role cannot is a table the job
-- cannot use at all. Grantees are mirrored from `public.client_trips`, which is
-- the established way this repository names "the DML roles of this client
-- database" without a migration having to know the role name
-- (`db/client_business/033_eco_driving_weekly_email_send_log_grants.sql`).
--
-- THIS BLOCK COVERS THE EXISTING-CLIENT PATH. On a NEW client it is a no-op by
-- construction: `scripts/onboard_workflow_a_client.py` runs `apply_client_ddl`
-- before `apply_grants`, so `client_trips` carries no grants yet when this
-- file executes. That path is covered by naming the table in that script's own
-- grant list, and both paths land on the same privileges.
--
-- MINIMUM PRIVILEGES, AND WHY EACH ONE.
--   SELECT  the lifecycle reads the durable state before every action;
--   INSERT  `ensure_operation()` creates the row before any remote effect;
--   UPDATE  every state transition, lease claim, renewal and release.
-- DELETE is deliberately NOT granted: nothing in the lifecycle removes a row,
-- and the ledger is the record that a delivery happened. TRUNCATE, REFERENCES
-- and ownership are likewise not granted. EXECUTE on the guard function is not
-- granted either — PostgreSQL checks that privilege when the trigger is
-- CREATED, not when it fires, so the runtime role does not need it and must not
-- be able to call the function directly.

DO $eco_dashboard_grants$
DECLARE
    grant_row record;
BEGIN
    FOR grant_row IN
        SELECT DISTINCT grantee
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'client_trips'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
          AND grantee NOT IN ('PUBLIC')
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', grant_row.grantee);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE ON TABLE '
            'public.eco_dashboard_delivery_operation TO %I',
            grant_row.grantee
        );
    END LOOP;
END $eco_dashboard_grants$;
