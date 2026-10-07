-- 003_ingest_raw_file_persisted_duplicate_of.sql
-- Add persisted + duplicate_of_id and enforce semantics for DUPLICATE_CONTENT.
-- Must be safe to re-run.

-- 1) Columns
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='ingest' AND table_name='raw_file' AND column_name='persisted'
  ) THEN
    ALTER TABLE ingest.raw_file
      ADD COLUMN persisted boolean NOT NULL DEFAULT true;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='ingest' AND table_name='raw_file' AND column_name='duplicate_of_id'
  ) THEN
    ALTER TABLE ingest.raw_file
      ADD COLUMN duplicate_of_id uuid NULL;
  END IF;
END$$;

-- 2) Foreign key: add ONLY if there is no FK on duplicate_of_id yet (any name)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE c.contype='f'
      AND n.nspname='ingest'
      AND t.relname='raw_file'
      AND pg_get_constraintdef(c.oid) LIKE '%FOREIGN KEY (duplicate_of_id)%'
  ) THEN
    ALTER TABLE ingest.raw_file
      ADD CONSTRAINT fk_raw_file_duplicate_of_id
      FOREIGN KEY (duplicate_of_id) REFERENCES ingest.raw_file(id);
  END IF;
END$$;

-- 3) Backfill persisted (safe to re-run)
UPDATE ingest.raw_file
SET persisted = (raw_path IS NOT NULL);

-- 4) Constraints (add only if missing)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname='ingest'
      AND t.relname='raw_file'
      AND c.contype='c'
      AND c.conname='ck_raw_file_paths_vs_persisted'
  ) THEN
    ALTER TABLE ingest.raw_file
      ADD CONSTRAINT ck_raw_file_paths_vs_persisted
      CHECK (
        (persisted = true  AND raw_path IS NOT NULL)
     OR (persisted = false AND raw_path IS NULL AND normalized_csv_path IS NULL)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname='ingest'
      AND t.relname='raw_file'
      AND c.contype='c'
      AND c.conname='ck_dup_has_duplicate_of'
  ) THEN
    ALTER TABLE ingest.raw_file
      ADD CONSTRAINT ck_dup_has_duplicate_of
      CHECK (
        status <> 'DUPLICATE_CONTENT'
     OR duplicate_of_id IS NOT NULL
      );
  END IF;
END$$;

-- 5) Index (add only if missing)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname='ingest'
      AND tablename='raw_file'
      AND indexname='idx_raw_file_duplicate_of_id'
  ) THEN
    CREATE INDEX idx_raw_file_duplicate_of_id
      ON ingest.raw_file (duplicate_of_id);
  END IF;
END$$;
