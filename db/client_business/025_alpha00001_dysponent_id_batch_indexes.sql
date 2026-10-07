-- Batch helper index for the ALPHA00001 Workflow B Dysponent_ID enrichment.
--
-- The job is ALPHA00001-only, but this migration is safe for all client
-- business DBs because the column is nullable and the index is partial.

CREATE INDEX IF NOT EXISTS idx_client_trips_dysponent_id_pending_window
    ON public.client_trips (start_timestamp, client_id, provider_trip_id)
    WHERE "Dysponent_ID" IS NULL OR btrim("Dysponent_ID") = '';
