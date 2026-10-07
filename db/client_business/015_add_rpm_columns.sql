-- 015_add_rpm_columns.sql
-- Workflow A — additive client-business DDL.
--
-- Adds two RPM-event count columns to public.client_trips. Both are
-- nullable INTEGERs because:
--   * existing rows pre-dating this migration have no source data and
--     must remain untouched (back-compat: no breaking changes),
--   * a future trip whose notifications cannot be matched (e.g. missing
--     vehicle_id on the provider side) should remain NULL rather than
--     be defaulted to 0, so we can distinguish "no data" from "0 events".
--
-- Counts are sourced from the existing /alerts/notifications feed
-- already fetched by jobs.api.telematics.sync_trips_and_speeding — NOT from
-- /trips. They are computed in Python during the trips upsert pass and
-- written as part of the same INSERT statement. See docs/05_jobs.md.
--
-- Constraints:
--   * record_id stays NOT a primary key (still added by 014_*).
--   * The existing PK on client_trips (client_id, provider_trip_id) is
--     left unchanged.
--
-- Apply via: `python scripts/apply_client_business_migrations.py --apply`.

ALTER TABLE IF EXISTS public.client_trips
  ADD COLUMN IF NOT EXISTS high_rpm_events_count INTEGER NULL,
  ADD COLUMN IF NOT EXISTS overrev_events_count INTEGER NULL;
