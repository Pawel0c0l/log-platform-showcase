-- ALPHA00001 Workflow B postprocess enrichment target.
--
-- This migration is intentionally nullable and safe for all client business
-- databases because scripts/apply_client_business_migrations.py applies every
-- db/client_business/*.sql file to every enabled client DB. The enrichment job
-- itself is hard-limited to client_code=ALPHA00001.

ALTER TABLE IF EXISTS public.client_trips
    ADD COLUMN IF NOT EXISTS "Dysponent_ID" TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_client_trips_dysponent_id_present
    ON public.client_trips ("Dysponent_ID")
    WHERE "Dysponent_ID" IS NOT NULL AND btrim("Dysponent_ID") <> '';
