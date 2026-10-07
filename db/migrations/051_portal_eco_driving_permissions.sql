-- 051_portal_eco_driving_permissions.sql
-- Eco Driving Explorer portal permissions (Stage 2, read-only API).
--
-- Additive-only, idempotent migration that extends the existing additive portal
-- access model (migrations 033 + 037) with three Eco Driving capabilities on
-- both the direct (portal_user_clients) and group (portal_group_clients) grant
-- tables. Effective per-client access remains the additive union of direct OR
-- active-group flags, exactly like can_view_database / can_view_reports.
--
-- IMPORTANT (fail-closed defaults):
--   * All three columns are NOT NULL DEFAULT FALSE.
--   * Existing rows therefore receive FALSE (no Eco access is granted implicitly).
--   * There is NO backfill from can_view_database / can_view_reports.
--   * No user or group is enabled automatically.
--   * Existing Database Explorer / Reports permissions are untouched.
--   * Administrators do NOT bypass these client grants (this mirrors the
--     existing portal convention where artifact_users.is_admin only unlocks the
--     /admin surface and still requires direct/group client grants for data).
--
-- Rollback: repository migrations are forward-only. To revert, add a NEW
-- migration that runs `ALTER TABLE ... DROP COLUMN IF EXISTS ...` for the three
-- columns on both tables. Do not edit or delete this file once applied.
--
-- Permission meaning:
--   can_view_eco_ranking       -> list providers, periods, ranking entries, and
--                                 open the persisted ranking-entry summary.
--                                 Does NOT grant trip-level records.
--   can_view_eco_trip_details  -> list contributing trips, view safe technical
--                                 trip scoring inputs, and run reconciliation.
--                                 Requires can_view_eco_ranking to be useful.
--   can_view_eco_trip_routes   -> RESERVED for a later stage (location / address
--                                 / map fields). Grants NO additional data in
--                                 this stage because route columns are not yet
--                                 exposed by the API. Added now so the RBAC
--                                 contract is stable before route data lands.

ALTER TABLE portal_user_clients
  ADD COLUMN IF NOT EXISTS can_view_eco_ranking BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS can_view_eco_trip_details BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS can_view_eco_trip_routes BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE portal_group_clients
  ADD COLUMN IF NOT EXISTS can_view_eco_ranking BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS can_view_eco_trip_details BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS can_view_eco_trip_routes BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN portal_user_clients.can_view_eco_ranking IS
  'Eco Driving Explorer: list providers/periods/ranking entries and open the persisted ranking-entry summary. No trip-level data.';
COMMENT ON COLUMN portal_user_clients.can_view_eco_trip_details IS
  'Eco Driving Explorer: list contributing trips, view safe trip scoring inputs, and run reconciliation. Requires can_view_eco_ranking.';
COMMENT ON COLUMN portal_user_clients.can_view_eco_trip_routes IS
  'Eco Driving Explorer: reserved for future location/address/map fields. Grants no data in the current stage.';

COMMENT ON COLUMN portal_group_clients.can_view_eco_ranking IS
  'Eco Driving Explorer (group grant): list providers/periods/ranking entries and open the persisted ranking-entry summary. No trip-level data.';
COMMENT ON COLUMN portal_group_clients.can_view_eco_trip_details IS
  'Eco Driving Explorer (group grant): list contributing trips, view safe trip scoring inputs, and run reconciliation. Requires can_view_eco_ranking.';
COMMENT ON COLUMN portal_group_clients.can_view_eco_trip_routes IS
  'Eco Driving Explorer (group grant): reserved for future location/address/map fields. Grants no data in the current stage.';
