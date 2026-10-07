-- Phase 2C: admin-configured stable row identifier for portal database datasets.
-- Enables the permission-safe user row detail view. This migration is idempotent
-- and only touches the portal catalog metadata; it does NOT alter client business
-- databases or expose any row values.

-- One optional boolean flag per cataloged column. Absence (all false) means the
-- dataset has no row identifier and row detail stays unavailable.
ALTER TABLE portal_database_dataset_columns
  ADD COLUMN IF NOT EXISTS is_row_identifier BOOLEAN NOT NULL DEFAULT false;

-- Enforce at most one row identifier per dataset. The application additionally
-- requires the identifier column to be is_visible=true (validated on write and on
-- read), so row detail can never resolve to a hidden/unpermitted column.
CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_database_dataset_row_identifier
  ON portal_database_dataset_columns (dataset_id)
  WHERE is_row_identifier IS TRUE;
