-- 047_client_trips_first_seen_request_id.sql
-- M4 — immutable first-seen provenance for a trip row.
--
-- Specification of record:
--   docs/20_telematics_ingestion_permanent_repair_plan.md §4.1 gap 2, §4.3 (1),
--     §4.3a D2, §21.4, §21.6
--
-- WHAT PROBLEM THIS SOLVES.
--   Nothing today records which request first returned a given
--   `provider_trip_id`. `synced_at` and `sync_run_id` are LAST-TOUCHED, not
--   first-seen: the scheduled path upserts with `overwrite_existing = true`, so
--   every overlapping re-request rewrites them. An audit built on `synced_at`
--   would therefore be wrong, and it gets worse under a cadence that
--   deliberately re-requests the same trips more than one way.
--
-- SEMANTICS — all four are load-bearing.
--   * Set on INSERT ONLY. The job's upsert places this column in the INSERT
--     list and NEVER in `ON CONFLICT DO UPDATE SET`, the same protection
--     `Dysponent_ID` already relies on. PostgreSQL therefore keeps the original
--     value on an overlapping re-upsert, and a repeated observation cannot
--     create a second first-seen event.
--   * NO FOREIGN KEY, and this is not an omission. The referenced
--     `workflow_a_control.provider_request_log.request_id` lives in the PLATFORM
--     database `logdb`; this table lives in the per-client BUSINESS database.
--     PostgreSQL has no cross-database foreign key, so referential integrity
--     here is a convention the writer upholds, not a constraint the database
--     enforces. Stated plainly rather than implying a guarantee that does not
--     exist.
--   * NULL means exactly one thing: first-seen provenance was NEVER CAPTURED
--     for this row. Not "unknown", not "pending", not a sentinel.
--   * NO BACKFILL, EVER. Every pre-M4 row is NULL and stays NULL. First-seen
--     provenance cannot be reconstructed after the fact, and deriving it from
--     `synced_at` would fabricate exactly the evidence M4 exists to make
--     trustworthy. Derived metrics must EXCLUDE NULL rows, never impute them.
--     This migration is consequently pure DDL: it contains no UPDATE, and one
--     must not be added later.
--
-- ROLLBACK.
--   Deliberately asymmetric with the platform side. The platform table drops
--   cleanly because nothing reads it until the coverage gate is enabled; THIS
--   COLUMN SHOULD BE LEFT IN PLACE on rollback. It is additive and nullable, so
--   it costs a rolled-back release nothing, while dropping it would destroy
--   provenance for every row captured in the meantime — and that provenance is
--   precisely the thing that cannot be recreated.

ALTER TABLE IF EXISTS public.client_trips
    ADD COLUMN IF NOT EXISTS first_seen_request_id UUID NULL;

COMMENT ON COLUMN public.client_trips.first_seen_request_id IS
  'M4: identity of the provider request that FIRST returned this trip. Set on INSERT '
  'only, never in ON CONFLICT DO UPDATE SET. Unenforced cross-database reference to '
  'workflow_a_control.provider_request_log.request_id in the platform database. '
  'NULL means first-seen provenance was never captured; it is never backfilled and '
  'never imputed from synced_at, which is last-touched.';
