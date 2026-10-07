-- Database Explorer background exports: user cancellation (approved stage S8).
--
-- `Anuluj` on a queued or generating export needs a terminal status the worker's
-- existing fences already refuse to leave. Every worker transition is
-- conditional on `status = 'queued'` (claim) or `status = 'running'` (lease
-- refresh, failure, publication, stale recovery), so a job moved to `cancelled`
-- can no longer be claimed, cannot be published READY by a stale worker, and
-- cannot be overwritten by a late failure. No worker fencing change is required
-- for that property; this migration only widens the status vocabulary.
--
-- Additive and backward-safe:
--   * no existing row changes status;
--   * an old API and an old worker never write 'cancelled', so applying this
--     before the application deploy is safe;
--   * a new API against an old worker is also safe, because every worker
--     transition is already conditional on a status it does know.
--
-- Rollout order: apply this migration first, then deploy the API, then the
-- worker. See docs/31_database_explorer_export_panel_and_background_states.md.

DO $$
DECLARE
  constraint_name TEXT;
BEGIN
  -- The 043 constraint was declared inline and therefore carries a generated
  -- name. Resolve it from the catalog instead of assuming one, so this runs on
  -- any environment where the generated name differs.
  FOR constraint_name IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
    WHERE rel.relname = 'database_export_jobs'
      AND nsp.nspname = current_schema()
      AND con.contype = 'c'
      AND pg_get_constraintdef(con.oid) LIKE '%status%'
      AND pg_get_constraintdef(con.oid) LIKE '%queued%'
  LOOP
    EXECUTE format('ALTER TABLE database_export_jobs DROP CONSTRAINT %I', constraint_name);
  END LOOP;
EXCEPTION WHEN undefined_table THEN NULL;
END $$;

DO $$
BEGIN
  ALTER TABLE database_export_jobs
    ADD CONSTRAINT database_export_jobs_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'expired', 'cancelled'));
EXCEPTION
  WHEN undefined_table THEN NULL;
  WHEN duplicate_object THEN NULL;
END $$;
