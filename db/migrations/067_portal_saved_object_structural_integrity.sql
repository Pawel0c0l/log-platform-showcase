-- Portal S13 structural integrity and byte-exact payload bounds
-- (correction to migration 066; approved stage S13 review fixes).
--
-- WHY A SECOND MIGRATION RATHER THAN AN EDIT TO 066.
--   `ops/db_migrate.sh` keys `public.schema_migrations` by FILENAME and SKIPs a
--   file it has already applied. Editing 066 would therefore be silently
--   ineffective on every database that already ran it — including the
--   disposable and development instances S13 was built against — while looking
--   correct in the repository. Migration history stays append-only, and the
--   correction is a file of its own, so the FINAL SEQUENCE (066 then 067) is
--   what establishes the authorized structure on a database in ANY prior state.
--
-- WHAT THIS CORRECTS.
--
--   1. STRUCTURAL INTEGRITY. 066 creates its three relations with
--      `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`. That is the
--      repository's additive convention, but it accepts a PRE-EXISTING
--      same-named relation whose structure is wrong or incomplete: the
--      statement is skipped, the migration reports success, and the application
--      activates S13 against a schema that cannot hold its invariants. This
--      migration makes the post-state deterministic. Anything it can complete
--      ADDITIVELY it completes (a missing column, a missing constraint, a
--      missing or differently-defined index); anything it cannot complete
--      without destroying or reinterpreting data — a required column with the
--      wrong type, a missing prerequisite relation — RAISES and the migration
--      FAILS. There is deliberately no path that succeeds while leaving a
--      weaker schema than the one S13 is authorized to rely on.
--
--   2. BYTE-EXACT PAYLOAD BOUND. 066 bounds the payload with
--      `char_length(...) <= 8192`, which counts Unicode CODE POINTS, while both
--      the design and the application called it "8 KiB". A payload of 8192
--      Polish or CJK characters passes a code-point bound and occupies far more
--      than 8 KiB on disk. The authorized contract is **8192 UTF-8 bytes**, so
--      the check becomes `octet_length(...) <= 8192` and the application
--      measures the encoded length of the same document.
--
-- WHAT IT DOES NOT CHANGE.
--   No relation is dropped, no column is dropped, no row is deleted, no data is
--   rewritten. Ownership, the foreign keys, the cascade semantics, the name
--   bounds, the uniqueness rule and the payload's object-shape rule are exactly
--   what 066 authorized. The only semantic change is the payload bound moving
--   from code points to bytes, which is strictly narrower: a document that was
--   valid under the byte bound was always valid under the code-point bound.
--
-- RERUN SEMANTICS.
--   `ops/db_migrate.sh` applies each file at most once. This file is
--   nevertheless written so that a second execution is a no-op on a correct
--   schema and a repair on an incomplete one: every constraint and index it
--   owns is converged to its authorized definition rather than created only
--   when absent. It is not "idempotent" as a goal in itself — the requirement
--   is that neither a re-execution nor a partial prior state may be accepted
--   while the resulting schema is wrong.
--
-- NOT APPLIED TO PRODUCTION BY THIS CHANGE.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Prerequisites. 067 corrects 066; it does not stand in for it.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  missing TEXT;
BEGIN
  FOREACH missing IN ARRAY ARRAY[
    'public.artifact_users',
    'public.portal_database_datasets',
    'public.portal_user_preferences',
    'public.portal_database_saved_views',
    'public.portal_database_column_sets'
  ] LOOP
    IF to_regclass(missing) IS NULL THEN
      RAISE EXCEPTION
        'migration 067 requires relation %; apply 066 (and its prerequisites) first', missing
        USING ERRCODE = 'undefined_table';
    END IF;
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Complete a partially created relation, additively.
--
--    A column that is absent is added with its authorized type, nullability and
--    default. A column that is present with the WRONG type is NOT coerced —
--    section 2 raises on it — because silently casting a stored payload or an
--    owner key is a data reinterpretation no migration is authorized to make.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_user_preferences
  ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'auto',
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE portal_database_saved_views
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE portal_database_column_sets
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ---------------------------------------------------------------------------
-- 2. Column identity and type. Deterministic failure, never a weaker schema.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  expected  TEXT[][] := ARRAY[
    ['portal_user_preferences',      'user_id',           'uuid',        'NO'],
    ['portal_user_preferences',      'theme',             'text',        'NO'],
    ['portal_user_preferences',      'created_at',        'timestamp with time zone', 'NO'],
    ['portal_user_preferences',      'updated_at',        'timestamp with time zone', 'NO'],
    ['portal_database_saved_views',  'saved_view_id',     'uuid',        'NO'],
    ['portal_database_saved_views',  'owner_user_id',     'uuid',        'NO'],
    ['portal_database_saved_views',  'dataset_id',        'uuid',        'NO'],
    ['portal_database_saved_views',  'view_name',         'text',        'NO'],
    ['portal_database_saved_views',  'view_state_json',   'jsonb',       'NO'],
    ['portal_database_saved_views',  'created_at',        'timestamp with time zone', 'NO'],
    ['portal_database_saved_views',  'updated_at',        'timestamp with time zone', 'NO'],
    ['portal_database_column_sets',  'column_set_id',     'uuid',        'NO'],
    ['portal_database_column_sets',  'owner_user_id',     'uuid',        'NO'],
    ['portal_database_column_sets',  'dataset_id',        'uuid',        'NO'],
    ['portal_database_column_sets',  'set_name',          'text',        'NO'],
    ['portal_database_column_sets',  'layout_state_json', 'jsonb',       'NO'],
    ['portal_database_column_sets',  'created_at',        'timestamp with time zone', 'NO'],
    ['portal_database_column_sets',  'updated_at',        'timestamp with time zone', 'NO']
  ];
  i         INT;
  actual    TEXT;
  nullable  TEXT;
BEGIN
  FOR i IN 1 .. array_length(expected, 1) LOOP
    SELECT format_type(a.atttypid, NULL), CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END
      INTO actual, nullable
      FROM pg_attribute a
     WHERE a.attrelid = to_regclass('public.' || expected[i][1])
       AND a.attname  = expected[i][2]
       AND a.attnum > 0
       AND NOT a.attisdropped;
    IF actual IS NULL THEN
      RAISE EXCEPTION
        'S13 schema integrity: %.% is missing and cannot be added additively',
        expected[i][1], expected[i][2]
        USING ERRCODE = 'undefined_column';
    END IF;
    IF actual <> expected[i][3] THEN
      RAISE EXCEPTION
        'S13 schema integrity: %.% is % but S13 requires %; a same-named relation with an incompatible structure is not accepted',
        expected[i][1], expected[i][2], actual, expected[i][3]
        USING ERRCODE = 'datatype_mismatch';
    END IF;
    IF nullable <> expected[i][4] THEN
      -- Nullability IS additively repairable, but only if no row violates it.
      EXECUTE format('ALTER TABLE public.%I ALTER COLUMN %I SET NOT NULL',
                     expected[i][1], expected[i][2]);
    END IF;
  END LOOP;
END
$$;

-- Defaults the application relies on. `SET DEFAULT` is unconditional so a
-- relation created without them converges instead of being accepted.
ALTER TABLE portal_user_preferences     ALTER COLUMN theme         SET DEFAULT 'auto';
ALTER TABLE portal_user_preferences     ALTER COLUMN created_at    SET DEFAULT now();
ALTER TABLE portal_user_preferences     ALTER COLUMN updated_at    SET DEFAULT now();
ALTER TABLE portal_database_saved_views ALTER COLUMN saved_view_id SET DEFAULT gen_random_uuid();
ALTER TABLE portal_database_saved_views ALTER COLUMN created_at    SET DEFAULT now();
ALTER TABLE portal_database_saved_views ALTER COLUMN updated_at    SET DEFAULT now();
ALTER TABLE portal_database_column_sets ALTER COLUMN column_set_id SET DEFAULT gen_random_uuid();
ALTER TABLE portal_database_column_sets ALTER COLUMN created_at    SET DEFAULT now();
ALTER TABLE portal_database_column_sets ALTER COLUMN updated_at    SET DEFAULT now();

-- ---------------------------------------------------------------------------
-- 3. Primary keys and foreign keys, converged to the authorized definition.
--
--    Each constraint is dropped by its authorized NAME and re-added, so a
--    same-named constraint carrying a different rule is corrected rather than
--    accepted. `ADD CONSTRAINT` validates existing rows, so a relation holding
--    data the rule forbids fails the migration deterministically instead of
--    being granted an unenforced constraint.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  pk_name TEXT;
BEGIN
  FOR pk_name IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = to_regclass('public.portal_user_preferences') AND contype = 'p'
  LOOP
    EXECUTE format('ALTER TABLE public.portal_user_preferences DROP CONSTRAINT %I', pk_name);
  END LOOP;
  FOR pk_name IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = to_regclass('public.portal_database_saved_views') AND contype = 'p'
  LOOP
    EXECUTE format('ALTER TABLE public.portal_database_saved_views DROP CONSTRAINT %I', pk_name);
  END LOOP;
  FOR pk_name IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = to_regclass('public.portal_database_column_sets') AND contype = 'p'
  LOOP
    EXECUTE format('ALTER TABLE public.portal_database_column_sets DROP CONSTRAINT %I', pk_name);
  END LOOP;
END
$$;

ALTER TABLE portal_user_preferences
  ADD CONSTRAINT portal_user_preferences_pkey PRIMARY KEY (user_id);
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_pkey PRIMARY KEY (saved_view_id);
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_pkey PRIMARY KEY (column_set_id);

ALTER TABLE portal_user_preferences
  DROP CONSTRAINT IF EXISTS portal_user_preferences_user_id_fkey;
ALTER TABLE portal_user_preferences
  ADD CONSTRAINT portal_user_preferences_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES artifact_users(user_id) ON DELETE CASCADE;

ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_owner_user_id_fkey;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_owner_user_id_fkey
  FOREIGN KEY (owner_user_id) REFERENCES artifact_users(user_id) ON DELETE CASCADE;
ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_dataset_id_fkey;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_dataset_id_fkey
  FOREIGN KEY (dataset_id) REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE;

ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_owner_user_id_fkey;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_owner_user_id_fkey
  FOREIGN KEY (owner_user_id) REFERENCES artifact_users(user_id) ON DELETE CASCADE;
ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_dataset_id_fkey;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_dataset_id_fkey
  FOREIGN KEY (dataset_id) REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE;

-- ---------------------------------------------------------------------------
-- 4. Value rules. The theme enum, the name bounds, the payload shape, and the
--    payload bound stated in BYTES.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_user_preferences
  DROP CONSTRAINT IF EXISTS portal_user_preferences_theme_check;
ALTER TABLE portal_user_preferences
  ADD CONSTRAINT portal_user_preferences_theme_check
  CHECK (theme IN ('auto', 'light', 'dark'));

ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_name_check;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_name_check
  CHECK (view_name <> '' AND char_length(view_name) <= 80);
ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_state_object_check;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_state_object_check
  CHECK (jsonb_typeof(view_state_json) = 'object');
ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_state_bound_check;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_state_bound_check
  CHECK (octet_length(view_state_json::text) <= 8192);

ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_name_check;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_name_check
  CHECK (set_name <> '' AND char_length(set_name) <= 80);
ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_state_object_check;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_state_object_check
  CHECK (jsonb_typeof(layout_state_json) = 'object');
ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_state_bound_check;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_state_bound_check
  CHECK (octet_length(layout_state_json::text) <= 8192);

-- The name bound stays in CHARACTERS deliberately: a name is a label a person
-- reads and types, and "80 characters" is what the approved copy bounds. Only
-- the PAYLOAD was ever described as a byte size.

-- ---------------------------------------------------------------------------
-- 5. Uniqueness and the listing indexes, converged by name.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_database_saved_views
  DROP CONSTRAINT IF EXISTS portal_database_saved_views_owner_dataset_name_key;
ALTER TABLE portal_database_saved_views
  ADD CONSTRAINT portal_database_saved_views_owner_dataset_name_key
  UNIQUE (owner_user_id, dataset_id, view_name);

ALTER TABLE portal_database_column_sets
  DROP CONSTRAINT IF EXISTS portal_database_column_sets_owner_dataset_name_key;
ALTER TABLE portal_database_column_sets
  ADD CONSTRAINT portal_database_column_sets_owner_dataset_name_key
  UNIQUE (owner_user_id, dataset_id, set_name);

DROP INDEX IF EXISTS idx_portal_database_saved_views_owner;
CREATE INDEX idx_portal_database_saved_views_owner
  ON portal_database_saved_views (owner_user_id, dataset_id, view_name ASC);

DROP INDEX IF EXISTS idx_portal_database_column_sets_owner;
CREATE INDEX idx_portal_database_column_sets_owner
  ON portal_database_column_sets (owner_user_id, dataset_id, set_name ASC);

-- ---------------------------------------------------------------------------
-- 6. Postconditions. The migration succeeds only if the exact authorized
--    structure is present; there is no "mostly applied" success.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  spec    TEXT[][] := ARRAY[
    ['portal_user_preferences',     'portal_user_preferences_pkey'],
    ['portal_user_preferences',     'portal_user_preferences_user_id_fkey'],
    ['portal_user_preferences',     'portal_user_preferences_theme_check'],
    ['portal_database_saved_views', 'portal_database_saved_views_pkey'],
    ['portal_database_saved_views', 'portal_database_saved_views_owner_user_id_fkey'],
    ['portal_database_saved_views', 'portal_database_saved_views_dataset_id_fkey'],
    ['portal_database_saved_views', 'portal_database_saved_views_name_check'],
    ['portal_database_saved_views', 'portal_database_saved_views_state_object_check'],
    ['portal_database_saved_views', 'portal_database_saved_views_state_bound_check'],
    ['portal_database_saved_views', 'portal_database_saved_views_owner_dataset_name_key'],
    ['portal_database_column_sets', 'portal_database_column_sets_pkey'],
    ['portal_database_column_sets', 'portal_database_column_sets_owner_user_id_fkey'],
    ['portal_database_column_sets', 'portal_database_column_sets_dataset_id_fkey'],
    ['portal_database_column_sets', 'portal_database_column_sets_name_check'],
    ['portal_database_column_sets', 'portal_database_column_sets_state_object_check'],
    ['portal_database_column_sets', 'portal_database_column_sets_state_bound_check'],
    ['portal_database_column_sets', 'portal_database_column_sets_owner_dataset_name_key']
  ];
  i       INT;
  found   BOOLEAN;
  cascade_missing TEXT;
BEGIN
  FOR i IN 1 .. array_length(spec, 1) LOOP
    SELECT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conrelid = to_regclass('public.' || spec[i][1]) AND conname = spec[i][2]
    ) INTO found;
    IF NOT found THEN
      RAISE EXCEPTION 'S13 schema integrity: constraint %.% is absent after migration 067',
        spec[i][1], spec[i][2] USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END LOOP;

  -- Every S13 foreign key must cascade: an object whose owner or dataset is
  -- gone can never be scoped by an authorization query again.
  SELECT string_agg(conname, ', ') INTO cascade_missing
    FROM pg_constraint
   WHERE contype = 'f'
     AND conrelid IN (
       to_regclass('public.portal_user_preferences'),
       to_regclass('public.portal_database_saved_views'),
       to_regclass('public.portal_database_column_sets'))
     AND confdeltype <> 'c';
  IF cascade_missing IS NOT NULL THEN
    RAISE EXCEPTION 'S13 schema integrity: foreign key(s) % do not cascade on delete', cascade_missing
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF to_regclass('public.idx_portal_database_saved_views_owner') IS NULL
     OR to_regclass('public.idx_portal_database_column_sets_owner') IS NULL THEN
    RAISE EXCEPTION 'S13 schema integrity: a saved-object listing index is absent after migration 067'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
END
$$;

COMMENT ON COLUMN portal_database_saved_views.view_state_json IS
  'Canonical view state written by the server: filters, search, sort, page size and column layout. Never SQL, never a URL, never a row reference. Bounded at 8192 UTF-8 bytes.';
COMMENT ON COLUMN portal_database_column_sets.layout_state_json IS
  'Canonical S5 layout state written by the server: cols, colorder, colw, colpin. Re-intersected with the approved column universe on every apply. Bounded at 8192 UTF-8 bytes.';

COMMIT;
