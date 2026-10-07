-- Allow portal Database Explorer catalog columns to store real PostgreSQL
-- physical column names that require quoted identifiers.
--
-- SQL safety remains application-owned: schema/table references stay simple
-- identifiers, selected columns are validated against the discovered physical
-- column allowlist, and query builders double-quote catalog-approved names.

DO $$
DECLARE
  constraint_name TEXT;
BEGIN
  SELECT con.conname
  INTO constraint_name
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
  WHERE nsp.nspname = 'public'
    AND rel.relname = 'portal_database_dataset_columns'
    AND con.contype = 'c'
    AND pg_get_constraintdef(con.oid) LIKE '%column_name%'
    AND pg_get_constraintdef(con.oid) LIKE '%~%'
  LIMIT 1;

  IF constraint_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.portal_database_dataset_columns DROP CONSTRAINT %I', constraint_name);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
    WHERE nsp.nspname = 'public'
      AND rel.relname = 'portal_database_dataset_columns'
      AND con.conname = 'portal_database_dataset_columns_column_name_not_blank'
  ) THEN
    ALTER TABLE public.portal_database_dataset_columns
      ADD CONSTRAINT portal_database_dataset_columns_column_name_not_blank
      CHECK (btrim(column_name) <> '') NOT VALID;
  END IF;
END $$;

ALTER TABLE public.portal_database_dataset_columns
  VALIDATE CONSTRAINT portal_database_dataset_columns_column_name_not_blank;

DO $$
DECLARE
  constraint_name TEXT;
BEGIN
  SELECT con.conname
  INTO constraint_name
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
  WHERE nsp.nspname = 'public'
    AND rel.relname = 'portal_database_datasets'
    AND con.contype = 'c'
    AND pg_get_constraintdef(con.oid) LIKE '%default_date_column%'
    AND pg_get_constraintdef(con.oid) LIKE '%~%'
  LIMIT 1;

  IF constraint_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.portal_database_datasets DROP CONSTRAINT %I', constraint_name);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
    WHERE nsp.nspname = 'public'
      AND rel.relname = 'portal_database_datasets'
      AND con.conname = 'portal_database_datasets_default_date_column_not_blank'
  ) THEN
    ALTER TABLE public.portal_database_datasets
      ADD CONSTRAINT portal_database_datasets_default_date_column_not_blank
      CHECK (default_date_column IS NULL OR btrim(default_date_column) <> '') NOT VALID;
  END IF;
END $$;

ALTER TABLE public.portal_database_datasets
  VALIDATE CONSTRAINT portal_database_datasets_default_date_column_not_blank;
