-- 071_retention_execution_per_target.sql
--
-- Give the hard-retention ledger the identity it always needed: one current-state
-- row per (policy, scope, SWEPT TARGET).
--
-- WHAT WAS WRONG. Migration 070 keyed `ops_control.retention_execution` on
-- `(policy_id, scope)`. That is correct only where a policy governs exactly one
-- relation. Several govern many — `client_db.v2_staging_tables` sweeps three
-- tables, `client_db.workflow_a_registered_tables` fourteen, and each of those
-- is swept once per client under the SAME scope — so every relation's upsert
-- overwrote the previous one and the surviving row was whichever the loop
-- happened to write last.
--
-- The 2026-08-29 production sweep measured the loss exactly: 162 outcomes, 152
-- of them recordable, 62 rows in the table. An operator reading
-- `client_db.v2_staging_tables / DELTA00001` saw `source_fuel_observations` and
-- could not learn whether `source_trips` had been swept at all, let alone what
-- it deleted. `deleted_count` on a multi-relation policy was not attributable to
-- a relation, which is not sufficient operational evidence for a destructive
-- job.
--
-- WHAT THIS IS NOT. Not an event log. The table stays CURRENT-STATE — one row
-- per independently swept target, rewritten in place — for exactly the reason
-- 070 gives: a history of every sweep of every relation of every client would
-- accumulate without bound and need a retention policy of its own. The row count
-- stays bounded, now by (targets x scopes) instead of (policies x scopes): 152
-- rows after a full sweep of five clients, against 62 today.
--
-- IDENTITY. `target` is the sweep's own `store` string — `schema.table` for a
-- relation, the absolute path for a filesystem root, the store name for a remote
-- store. It is stable across runs, it does not depend on loop order, and it is
-- already what `ops/hard_retention.py` reports; nothing new is invented to
-- produce it.
--
-- EXISTING ROWS. Backfilled from `detail->>'store'`, which every row written by
-- `record_outcomes()` carries and which IS the target that last won the
-- collision. So a legacy row keeps its real counts and becomes the row for the
-- relation it actually described. Its siblings do not appear until the next
-- sweep writes them: NO DELETION HISTORY IS MANUFACTURED for a relation whose
-- outcome was destroyed by the collapse. A row with no recoverable store — none
-- exist in production, and none can be written by the current code — becomes
-- target '-' and is superseded the first time that policy/scope is swept with a
-- real target.
--
-- ADDITIVE AND SAFE ON A POPULATED DATABASE. One nullable column added, filled,
-- then made NOT NULL; the primary key widened; the partial index re-created to
-- match. No row is deleted, no count is altered, and migration 070 is untouched.

BEGIN;

ALTER TABLE ops_control.retention_execution
    ADD COLUMN IF NOT EXISTS target text;

-- The backfill. `detail->>'store'` is written by
-- `ops.hard_retention.record_outcomes()` on every insert, so this recovers the
-- real identity of each surviving row rather than inventing one.
UPDATE ops_control.retention_execution
   SET target = coalesce(nullif(btrim(detail->>'store'), ''), '-')
 WHERE target IS NULL;

ALTER TABLE ops_control.retention_execution
    ALTER COLUMN target SET NOT NULL;

ALTER TABLE ops_control.retention_execution
    DROP CONSTRAINT IF EXISTS ck_retention_execution_target;
ALTER TABLE ops_control.retention_execution
    ADD CONSTRAINT ck_retention_execution_target
        CHECK (length(target) BETWEEN 1 AND 200);

-- Widening the key is the whole migration. Dropping and re-adding is the only
-- way to change a primary key's column list; the table is small and bounded, so
-- the rewrite is trivial.
ALTER TABLE ops_control.retention_execution
    DROP CONSTRAINT IF EXISTS pk_retention_execution;
ALTER TABLE ops_control.retention_execution
    ADD CONSTRAINT pk_retention_execution
        PRIMARY KEY (policy_id, scope, target);

COMMENT ON COLUMN ops_control.retention_execution.target IS
    'The independently swept target this row is about: schema.table for a relation, the absolute path for a filesystem root, the store name for a remote store. Part of the primary key, so two relations under one policy and scope no longer overwrite each other.';
COMMENT ON TABLE ops_control.retention_execution IS
    'Last outcome per (retention policy, execution scope, swept target) for the global hard-retention sweep. Current state, not history: one row per triple, rewritten in place.';

-- The non-compliance index must cover the new key, or the operator question
-- "what is not compliant?" answers at the wrong granularity.
DROP INDEX IF EXISTS ops_control.idx_retention_execution_noncompliant;
CREATE INDEX IF NOT EXISTS idx_retention_execution_noncompliant
    ON ops_control.retention_execution (policy_id, scope, target)
    WHERE oldest_remaining_ts IS NOT NULL;

COMMIT;
