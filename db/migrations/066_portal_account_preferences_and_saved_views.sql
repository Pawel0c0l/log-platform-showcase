-- Portal server-side account preferences and saved Database Explorer views
-- (approved stage S13: `D-007`, `D-011`, `SH-8`, `DB-30`).
--
-- Three account-owned entities, and nothing else:
--
--   1. `portal_user_preferences` — the per-account theme override. `D-011`
--      resolves the theme as "follow prefers-color-scheme by default; a
--      three-way override (AUTO / light / dark) persisted server-side per
--      account", and `SH-8` requires that choice to survive a logout, another
--      browser and another device.
--   2. `portal_database_saved_views` — named, per-account, per-dataset
--      snapshots of canonical Database Explorer view state (`D-007`).
--   3. `portal_database_column_sets` — named, per-account, per-dataset
--      snapshots of the reusable column-layout state alone (`DB-30`).
--
-- WHY A PREFERENCES TABLE RATHER THAN A COLUMN ON `artifact_users`.
--   A column on `artifact_users` would be smaller, but `artifact_users` is read
--   by the session path on every authenticated request. Application code that
--   named a not-yet-migrated column there would fail *login*, not one feature.
--   A separate relation makes the migration-absent case detectable by the same
--   probe convention migration 043 established (`_database_export_schema_available`),
--   and its absence degrades exactly one control instead of the portal.
--   Row absence is the AUTO default, so no backfill exists and every existing
--   account is already valid the moment this applies.
--
-- WHY NOT ONE GENERIC "saved object" TABLE.
--   A saved view and a column set are different contracts — one is broad view
--   state, the other is column layout only — and collapsing them would put the
--   distinction in a discriminator column that application code would have to
--   re-derive on every read. Two narrow relations keep each payload's validation
--   rule attached to its own relation.
--
-- OWNERSHIP AND SCOPE.
--   Every saved object is owned by exactly one account and scoped to exactly one
--   dataset. There is deliberately no group ownership, no sharing column, no
--   ACL, no visibility flag and no version history: S13 defines saved objects as
--   private to their owner, and a column that could later mean "shared" would be
--   an authorization surface nobody has approved.
--
-- DELETION SEMANTICS.
--   `ON DELETE CASCADE` on both foreign keys. A deleted account and a deleted
--   dataset both make the object meaningless, and keeping an orphan whose owner
--   or dataset no longer resolves would leave rows that no authorization query
--   can ever scope. This is not the same as a *revoked grant*: a revoked grant
--   deletes nothing — the object simply stops resolving for that account, which
--   is enforced in the application's dataset-access join, not here.
--
-- PAYLOAD.
--   JSONB, validated and canonicalized at the application boundary before it is
--   written and re-parsed through the current canonical parsers on every apply.
--   The database enforces only what it can enforce cheaply and immutably: the
--   payload is an object, and it is bounded. No SQL, no URL and no expression is
--   ever stored.
--
-- Additive, idempotent and forward-only, per the repository migration contract.
-- Rollout order: apply this migration first, then deploy the application. Code
-- running against a database without it degrades to the pre-S13 behaviour
-- (browser-local theme, no saved views, no column sets) rather than failing.
--
-- NOT APPLIED TO PRODUCTION BY THIS CHANGE.

-- ---------------------------------------------------------------------------
-- 1. Per-account theme preference (`D-011`, `SH-8`)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_user_preferences (
  user_id UUID PRIMARY KEY REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  theme TEXT NOT NULL DEFAULT 'auto',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT portal_user_preferences_theme_check
    CHECK (theme IN ('auto', 'light', 'dark'))
);

COMMENT ON TABLE portal_user_preferences IS
  'Per-account portal preferences. An absent row means every preference is at its default; for theme that default is AUTO (follow prefers-color-scheme).';
COMMENT ON COLUMN portal_user_preferences.theme IS
  'Approved three-way theme override (D-011): auto | light | dark. No other theme exists.';

-- ---------------------------------------------------------------------------
-- 2. Named saved Database Explorer views (`D-007`)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_database_saved_views (
  saved_view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
  view_name TEXT NOT NULL,
  view_state_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT portal_database_saved_views_name_check
    CHECK (view_name <> '' AND char_length(view_name) <= 80),
  CONSTRAINT portal_database_saved_views_state_object_check
    CHECK (jsonb_typeof(view_state_json) = 'object'),
  CONSTRAINT portal_database_saved_views_state_bound_check
    CHECK (char_length(view_state_json::text) <= 8192),
  -- Names are unique per owner per dataset, and exactly as the user typed them
  -- after trimming. Case-insensitive uniqueness is deliberately NOT imposed:
  -- the approved copy defines no such rule, dataset and column names in this
  -- product are case-sensitive data, and silently refusing `Trasy` because
  -- `trasy` exists would be a surprise the design does not ask for.
  CONSTRAINT portal_database_saved_views_owner_dataset_name_key
    UNIQUE (owner_user_id, dataset_id, view_name)
);

-- Listing is always "this account's views for this dataset, by name". The
-- unique constraint above already indexes (owner, dataset, name); this index
-- serves the catalogue, which lists one account's views across every dataset it
-- can currently open.
CREATE INDEX IF NOT EXISTS idx_portal_database_saved_views_owner
  ON portal_database_saved_views (owner_user_id, dataset_id, view_name ASC);

COMMENT ON TABLE portal_database_saved_views IS
  'Named, account-owned, dataset-scoped snapshots of canonical Database Explorer view state (D-007). Private to the owner; never shared, never an authorization grant.';
COMMENT ON COLUMN portal_database_saved_views.view_state_json IS
  'Canonical view state written by the server: filters, search, sort, page size and column layout. Never SQL, never a URL, never a row reference.';

-- ---------------------------------------------------------------------------
-- 3. Named column sets (`DB-30`)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_database_column_sets (
  column_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES artifact_users(user_id) ON DELETE CASCADE,
  dataset_id UUID NOT NULL REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE,
  set_name TEXT NOT NULL,
  layout_state_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT portal_database_column_sets_name_check
    CHECK (set_name <> '' AND char_length(set_name) <= 80),
  CONSTRAINT portal_database_column_sets_state_object_check
    CHECK (jsonb_typeof(layout_state_json) = 'object'),
  CONSTRAINT portal_database_column_sets_state_bound_check
    CHECK (char_length(layout_state_json::text) <= 8192),
  CONSTRAINT portal_database_column_sets_owner_dataset_name_key
    UNIQUE (owner_user_id, dataset_id, set_name)
);

CREATE INDEX IF NOT EXISTS idx_portal_database_column_sets_owner
  ON portal_database_column_sets (owner_user_id, dataset_id, set_name ASC);

COMMENT ON TABLE portal_database_column_sets IS
  'Named, account-owned, dataset-scoped column layouts (DB-30): visible columns, order, widths and pins only. Never filters, search, sort, page or density.';
COMMENT ON COLUMN portal_database_column_sets.layout_state_json IS
  'Canonical S5 layout state written by the server: cols, colorder, colw, colpin. Re-intersected with the approved column universe on every apply.';
