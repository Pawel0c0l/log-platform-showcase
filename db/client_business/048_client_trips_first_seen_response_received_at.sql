-- 048_client_trips_first_seen_response_received_at.sql
-- M-LAG — EXPAND. The durable first-observation instant, beside the identity it
-- belongs to.
--
-- Specification of record:
--   docs/21_telematics_delivery_lag_trace.md §2 (the durable fact), §3 (the
--     metric), §4 (NULL, negative and PENDING semantics), §5 (the retention
--     boundary), §11 (the expand-contract rollout and its compatibility matrix)
--   db/client_business/047_client_trips_first_seen_request_id.sql (the identity
--     half of the same first-seen event, whose semantics this column copies)
--
-- ============================================================================
-- THIS IS THE EXPAND HALF OF AN EXPAND-CONTRACT ROLLOUT. READ THIS FIRST.
-- ============================================================================
--
-- Migration 047 already shipped, and the CURRENTLY DEPLOYED M4-era writer
-- already populates `first_seen_request_id`. So at the moment this migration is
-- applied, live rows legitimately look like:
--
--     first_seen_request_id IS NOT NULL
--     first_seen_response_received_at_utc IS NULL       <- the column is new
--
-- and the deployed writer keeps producing exactly that shape until the M-LAG
-- release is activated. That state is called PROVENANCE_TIMESTAMP_PENDING and it
-- is a legitimate transitional state, not corruption.
--
-- Consequently this migration MUST NOT enforce the strict bidirectional pairing
--
--     first_seen_request_id IS NULL <=> first_seen_response_received_at_utc IS NULL
--
-- Doing so would:
--   * fail to validate against existing post-M4 rows; and
--   * reject every insert from the deployed writer, breaking normal ingestion;
--     and
--   * make rollback from the M-LAG release to the M4 release unsafe, because the
--     old writer could no longer write at all.
--
-- Strict pairing is the CONTRACT half, and it is deliberately NOT a file in this
-- directory — see the CONTRACT note at the bottom.
--
-- ============================================================================
-- WHAT THE EXPAND CONSTRAINT DOES ENFORCE, AND WHY ONLY THAT DIRECTION
-- ============================================================================
--
--     CHECK (first_seen_response_received_at_utc IS NULL
--            OR first_seen_request_id IS NOT NULL)
--
-- One direction only: an instant may not exist without an identity. That is the
-- half that carries real integrity during EXPAND — a timestamp with no request
-- to attribute it to is unusable evidence and could only arrive from a defective
-- writer. The other direction is exactly the transitional state above, so
-- forbidding it now is what would break the rollout.
--
-- It rejects nothing that exists (the column is new, so every existing row has
-- the instant NULL) and nothing the deployed writer can produce (that writer
-- does not know the column exists and can never set it).
--
-- It is added `NOT VALID`, and that is a deliberate choice rather than
-- hesitancy. In PostgreSQL a `NOT VALID` CHECK **is still enforced on every
-- INSERT and UPDATE**; `NOT VALID` only skips the one-off verification scan of
-- pre-existing rows. Since every pre-existing row satisfies it by construction,
-- that scan would be a guaranteed no-op — and, for the reason in the next
-- section, a very expensive one. Validation is therefore left to the CONTRACT
-- step, which needs a scan anyway.
--
-- ============================================================================
-- LOCK SAFETY — WHY THERE IS NOTHING ELSE IN THIS FILE
-- ============================================================================
--
-- `scripts/apply_client_business_migrations.py` connects with
-- `autocommit=False`, executes the WHOLE file in a single `cur.execute(...)`,
-- and commits once. Every statement here therefore shares ONE transaction.
--
-- That has a consequence which is easy to miss: `ALTER TABLE ... ADD COLUMN`
-- takes an ACCESS EXCLUSIVE lock, and because the transaction does not end until
-- the file does, that lock is held until the last statement completes. Any table
-- scan or index build placed after it therefore runs while the whole table is
-- locked against readers and writers.
--
-- So this file contains only catalog-only work:
--
--   * `ADD COLUMN ... NULL` with no DEFAULT — metadata only in PostgreSQL 11+,
--     no table rewrite, no scan;
--   * `ADD CONSTRAINT ... NOT VALID` — metadata only, no scan.
--
-- Both are effectively instantaneous, so the ACCESS EXCLUSIVE window is a
-- catalog update rather than an outage.
--
-- Deliberately NOT here:
--
--   * `VALIDATE CONSTRAINT` — a full scan under the inherited ACCESS EXCLUSIVE
--     lock. Deferred to the CONTRACT tool, which owns its own transactions and
--     can take the correct, weaker SHARE UPDATE EXCLUSIVE lock for it.
--   * The aggregation-read index. It is an OPTIMIZATION, not a correctness
--     requirement: the delivery-lag recompute reads a trailing window of days
--     bounded by the deepest enabled lookback, which the existing
--     `end_timestamp` access paths already serve, and it runs at operator
--     cadence rather than in the ingestion path. Building it here would mean a
--     full non-concurrent index build under ACCESS EXCLUSIVE.
--     `CREATE INDEX CONCURRENTLY` cannot rescue that — PostgreSQL forbids it
--     inside a transaction block, which is what this runner always provides. If
--     measurement later shows the recompute needs it, it belongs in its own
--     operator step using CONCURRENTLY outside a transaction, not here.
--
-- ============================================================================
-- SEMANTICS OF THE COLUMN — identical to 047's, for the same reasons
-- ============================================================================
--
--   * Set on INSERT ONLY, and NEVER in `ON CONFLICT DO UPDATE SET`. PostgreSQL
--     keeps the original value on an overlapping re-upsert, so a DAILY,
--     WEEKLY_RECONCILIATION or MONTHLY_RECONCILIATION rediscovery of the same
--     trip cannot restate when it was first observed.
--   * Written from the same in-memory page record as `first_seen_request_id`, so
--     the new writer cannot produce one without the other.
--   * NULL has TWO distinct meanings during EXPAND, and they must not be
--     conflated (docs/21 §4):
--       - `request_id IS NULL` too  -> NO_PROVENANCE. Genuinely never captured:
--         pre-M4 rows, the insert-only backfill, `strict_meta` runs.
--       - `request_id IS NOT NULL` -> PROVENANCE_TIMESTAMP_PENDING. The
--         observation happened and is recoverable from platform evidence; the
--         instant simply has not been copied here yet.
--     After the CONTRACT step only the first meaning remains possible.
--   * NEVER imputed. Not from `synced_at` (which is LAST-touched), not from the
--     trip timestamps, not from any job or projection time. The only permitted
--     source is the exact matching
--     `workflow_a_control.provider_request_log.response_received_at_utc`, copied
--     by `ops/enrich_telematics_first_seen_timestamps.py`. This migration is
--     consequently pure DDL: it contains no UPDATE, and one must not be added.
--   * `TIMESTAMPTZ`, an absolute instant; the session timezone cannot change
--     what is stored.
--
-- ============================================================================
-- ROLLBACK AND THE LATER CONTRACT
-- ============================================================================
--
-- ROLLBACK of this migration: deliberately asymmetric, exactly as 047 is.
-- Dropping the column would destroy provenance that cannot be reconstructed, so
-- the supported rollback is to stop writing it. The EXPAND schema is forward-
-- and backward-compatible: the M4 release and the M-LAG release both run against
-- it, which is the whole point.
--
-- CONTRACT: strict bidirectional pairing is closed by
-- `ops/close_telematics_first_seen_pair_contract.py`, NOT by a file here.
-- `scripts/apply_client_business_migrations.py` applies every pending file in
-- this directory automatically, so a `049_..._contract.sql` could fire before
-- the rollout conditions existed — and a gated file that refused would abort the
-- client's remaining migrations. The closure therefore lives in a reviewed,
-- dry-run-first operator tool that verifies its own preconditions, owns its own
-- transactions, and records a synthetic ledger entry when it succeeds. Closing
-- the contract intentionally ends M4-writer rollback compatibility; see
-- docs/21 §11.

ALTER TABLE IF EXISTS public.client_trips
    ADD COLUMN IF NOT EXISTS first_seen_response_received_at_utc TIMESTAMPTZ NULL;

DO $$
BEGIN
  IF to_regclass('public.client_trips') IS NULL THEN
    RAISE NOTICE 'public.client_trips absent; nothing to constrain';
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'ck_client_trips_first_seen_instant_needs_request'
       AND conrelid = 'public.client_trips'::regclass
  ) THEN
    -- NOT VALID: enforced on every new INSERT/UPDATE, no scan of existing rows.
    ALTER TABLE public.client_trips
      ADD CONSTRAINT ck_client_trips_first_seen_instant_needs_request
      CHECK (
        first_seen_response_received_at_utc IS NULL
        OR first_seen_request_id IS NOT NULL
      )
      NOT VALID;
  END IF;
END;
$$;

COMMENT ON COLUMN public.client_trips.first_seen_response_received_at_utc IS
  'M-LAG (EXPAND): the instant the provider response that FIRST returned this trip '
  'was received. Set on INSERT only, never in ON CONFLICT DO UPDATE SET, and always '
  'written together with first_seen_request_id from the same page record. Survives '
  'the 180-day prune of workflow_a_control.provider_request_log, which is why it is '
  'copied here at all. NULL with a NULL request id means provenance was never '
  'captured; NULL with a NON-NULL request id is the transitional '
  'PROVENANCE_TIMESTAMP_PENDING state, enrichable ONLY from the exact matching '
  'provider_request_log row and never imputed. Strict bidirectional pairing is a '
  'later CONTRACT invariant, not enforced by this migration. '
  'observed_delivery_lag_seconds = this - end_timestamp, derived and never stored.';
