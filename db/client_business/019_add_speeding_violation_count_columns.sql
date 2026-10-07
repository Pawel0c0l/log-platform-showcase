-- 019_add_speeding_violation_count_columns.sql
-- Workflow A — additive client-business DDL.
--
-- Adds the current per-trip speeding violation count columns to
-- public.client_trips. These replace the older four-column
-- speeding_bucket_140_150_events / speeding_bucket_150_160_events /
-- speeding_bucket_160_170_events / speeding_bucket_gt_170_events write path.
--
-- Counting semantics:
--   * source: fleet-wide GET /vehicles/events raw telemetry
--   * every provider row that passes local filters and trip assignment counts
--     as one violation
--   * no timestamp grouping and no deduplication by registration, timestamp,
--     speed, event_id, or any other provider field
--   * buckets are speed >=140 <160, speed >=160 <170, and speed >=170
--
-- Existing old bucket columns are intentionally retained for backward
-- compatibility with already-provisioned DBs and any external readers. The
-- sync job writes the new columns after this migration is present.
--
-- Apply via:
--   * for new clients   — scripts/onboard_workflow_a_client.py
--   * for existing ones — `python scripts/apply_client_business_migrations.py --apply`

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS speeding_140_160_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS speeding_160_170_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS speeding_170_plus_count INTEGER NOT NULL DEFAULT 0;
