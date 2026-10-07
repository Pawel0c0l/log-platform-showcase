-- Portal client database mapping for Client Database Explorer.
-- database_name is a PostgreSQL database name only, not a DSN and not credentials.

ALTER TABLE portal_clients
  ADD COLUMN IF NOT EXISTS database_name TEXT;

DO $$
BEGIN
  ALTER TABLE portal_clients
    ADD CONSTRAINT portal_clients_database_name_safe
    CHECK (database_name IS NULL OR database_name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
