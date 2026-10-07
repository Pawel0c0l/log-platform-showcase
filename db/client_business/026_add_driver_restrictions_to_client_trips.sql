-- 026_add_driver_restrictions_to_client_trips.sql
-- Workflow A — add driver license restrictions enrichment output.
--
-- PostgreSQL appends new columns physically. The required user-facing order is
-- handled by logical SELECT/INSERT/export order in the job, not by rebuilding
-- public.client_trips only to change physical ordinal_position.

ALTER TABLE IF EXISTS public.client_trips
    ADD COLUMN IF NOT EXISTS "Driver_Restrictions" TEXT NULL;
