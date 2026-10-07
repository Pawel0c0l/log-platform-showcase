-- S15 corrective integrity migration for the generated-report persistence.
--
-- WHY A SECOND MIGRATION AND NOT AN EDIT OF 068.
--   `068_portal_generated_reports.sql` is reachable from `origin/main`
--   (commit `2756f9b`), so it is shared history and immutable under the
--   repository's migration policy (`AGENTS.md` §4). This file is therefore the
--   next repository-valid migration and is written to be correct when applied
--   AFTER 068, on a database that already carries 068 and on a clean database
--   that replays the whole chain. Neither 068 nor this file has been applied to
--   production.
--
-- WHAT THE INDEPENDENT REVIEW ESTABLISHED, AND WHAT IS CORRECTED HERE.
--
--   1. TWO COMPETING LOGICAL IDENTITIES. 068 declared BOTH
--      `UNIQUE (client_code, definition_id, period_key)` and
--      `UNIQUE (client_code, definition_id, period_kind, period_start)` while
--      leaving `period_key` a free string. Two concurrent generations of one
--      logical period could therefore escape as an unhandled `23505` on the
--      period-start index (the service's `ON CONFLICT` named the period-key
--      index only), and a caller-chosen key could describe canonical dates
--      belonging to a different logical period, so a later canonical retry
--      collided. Corrected by collapsing to ONE identity: the period-key
--      uniqueness is dropped and `period_key` becomes a DERIVED fact, bound by
--      CHECK to the calendar period `(period_kind, period_start)` names. Key
--      uniqueness is then implied, not separately declared.
--
--   2. UNFENCED PUBLICATION ATTEMPTS. A single claim could `publish()`
--      repeatedly, two concurrent `publish()` calls on one claim could both
--      replace the member set, and `fail()` could run after a successful
--      publication and turn a published report into a failed one. Corrected by
--      making an attempt TERMINAL: `claim_token` is the ACTIVE claim and is
--      consumed by the completion that closes it, and `completed_claim_token` /
--      `completed_claim_outcome` record which attempt closed it and how. A
--      token can be active or completed, never both.
--
--   3. A FORGEABLE AVAILABILITY SUMMARY. 068 derived the summary only from the
--      MEMBER side, so an ordinary `UPDATE` on the instance could set
--      `available_member_count = 1` over a report with no available member and
--      the read path rendered it as ready. Corrected by a BEFORE trigger on the
--      instance that OVERWRITES the three summary columns from membership truth
--      on every insert and update: the columns become a projection of the
--      member rows rather than a claim about them.
--
--   4. PRESENTATION METADATA OVERRIDING THE STORED ARTIFACT. A member could
--      declare `file_format='PDF'`, `content_type='text/html'` and
--      `is_previewable=true`, and the preview route embedded it. Corrected by
--      binding format to content type structurally, and by allowing
--      `is_previewable` only for the one content type this platform serves
--      inline. Active same-origin content can no longer be described as a
--      previewable PDF.
--
--   5. UNSAFE ARCHIVE MEMBER NAMES. `Pobierz wszystkie` builds a ZIP from
--      `display_filename`. A path-shaped or control-character name is refused
--      at the column, so the archive builder never has to repair one.
--
-- NOTHING ELSE CHANGES. No relation outside the three S15 relations is touched,
-- no row is read, inserted, copied or backfilled, and the S15 relations are
-- empty everywhere this can be applied.
-- ---------------------------------------------------------------------------

BEGIN;

-- Fail closed if 068 is not present: this file corrects that migration and has
-- no meaning without it.
DO $$
DECLARE
  missing TEXT;
BEGIN
  FOREACH missing IN ARRAY ARRAY[
    'portal_generated_report_definitions',
    'portal_generated_report_instances',
    'portal_generated_report_files'
  ] LOOP
    IF to_regclass('public.' || missing) IS NULL THEN
      RAISE EXCEPTION
        'migration 069 requires 068_portal_generated_reports.sql: relation %.% is absent',
        'public', missing;
    END IF;
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. ONE canonical logical identity per (client, report type, reporting period)
--
-- `period_key` stops being an independent identity and becomes the NAME of the
-- calendar period the dates already describe. Combined with 068's alignment
-- CHECK — which forces `period_start` to be the aligned first day of its kind —
-- the key is a pure function of `(period_kind, period_start)`, so:
--
--   * `UNIQUE (client_code, definition_id, period_kind, period_start)` is now
--     the only logical identity, and key uniqueness follows from it rather than
--     competing with it (one definition has exactly one `period_kind`, which
--     068's `(definition_id, period_kind)` foreign key already guarantees);
--   * a caller can no longer persist `2026-W01` over the dates of week 28 and
--     make the canonical retry collide;
--   * `ON CONFLICT (client_code, definition_id, period_kind, period_start)` has
--     a single arbiter, so a concurrent first generation converges on the
--     existing row instead of escaping as `23505`.
--
-- The expression is built from `EXTRACT` + `lpad` + concatenation only. Every
-- one of those is IMMUTABLE, which a CHECK requires; `to_char` and `date::text`
-- are NOT (they depend on `DateStyle`/`lc_time`) and are deliberately avoided.
-- It reproduces exactly the keys `api/report_explorer/periods.py` derives.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_generated_report_instances
  DROP CONSTRAINT IF EXISTS portal_generated_report_instances_period_key_uniq;

ALTER TABLE portal_generated_report_instances
  DROP CONSTRAINT IF EXISTS portal_generated_report_instances_period_key_canonical_check;
ALTER TABLE portal_generated_report_instances
  ADD CONSTRAINT portal_generated_report_instances_period_key_canonical_check
  CHECK (
    period_key = CASE period_kind
      WHEN 'week' THEN
        lpad(EXTRACT(ISOYEAR FROM period_start)::text, 4, '0') || '-W' ||
        lpad(EXTRACT(WEEK    FROM period_start)::text, 2, '0')
      WHEN 'month' THEN
        lpad(EXTRACT(YEAR  FROM period_start)::text, 4, '0') || '-' ||
        lpad(EXTRACT(MONTH FROM period_start)::text, 2, '0')
      WHEN 'quarter' THEN
        lpad(EXTRACT(YEAR    FROM period_start)::text, 4, '0') || '-Q' ||
             EXTRACT(QUARTER FROM period_start)::text
      ELSE
        lpad(EXTRACT(YEAR  FROM period_start)::text, 4, '0') || '-' ||
        lpad(EXTRACT(MONTH FROM period_start)::text, 2, '0') || '-' ||
        lpad(EXTRACT(DAY   FROM period_start)::text, 2, '0')
    END
  );

COMMENT ON COLUMN portal_generated_report_instances.period_key IS
  'DERIVED name of the reporting period, bound by CHECK to (period_kind, period_start). It is a label and a search axis, never a second identity: the one logical identity is UNIQUE (client_code, definition_id, period_kind, period_start).';

-- ---------------------------------------------------------------------------
-- 2. Terminal attempt semantics
--
-- 068 fenced on `claim_token` alone, which answers "is this the current
-- attempt?" but never "has this attempt already finished?". The two questions
-- are different, and only the second can refuse a replayed or concurrent
-- callback. `claim_token` now means ACTIVE and is consumed by completion;
-- `completed_claim_token` records the attempt that closed the instance and
-- `completed_claim_outcome` how it closed.
--
--   active claim        -> may publish or fail, exactly once
--   completed claim     -> a replay is recognised and mutates nothing
--   neither             -> stale: a newer attempt owns the instance
-- ---------------------------------------------------------------------------
ALTER TABLE portal_generated_report_instances
  ADD COLUMN IF NOT EXISTS completed_claim_token UUID;
ALTER TABLE portal_generated_report_instances
  ADD COLUMN IF NOT EXISTS completed_claim_outcome TEXT;

COMMENT ON COLUMN portal_generated_report_instances.claim_token IS
  'The ACTIVE generation claim, or NULL when no attempt is in flight. Completion consumes it, so a claim authorizes exactly one terminal callback.';
COMMENT ON COLUMN portal_generated_report_instances.completed_claim_token IS
  'The claim that last closed this instance. A callback carrying it is a REPLAY: it is answered deterministically and mutates nothing.';
COMMENT ON COLUMN portal_generated_report_instances.completed_claim_outcome IS
  'How the completed claim closed: succeeded (published) or failed. A replayed successful attempt can never be turned into a failure.';

ALTER TABLE portal_generated_report_instances
  DROP CONSTRAINT IF EXISTS portal_generated_report_instances_completed_claim_check;
ALTER TABLE portal_generated_report_instances
  ADD CONSTRAINT portal_generated_report_instances_completed_claim_check
  CHECK (
    (completed_claim_token IS NULL) = (completed_claim_outcome IS NULL)
    AND (completed_claim_outcome IS NULL
         OR completed_claim_outcome IN ('succeeded', 'failed'))
  );

-- A token is the active claim or a completed one. Never both, because "may
-- still mutate" and "has already mutated" would then be simultaneously true.
ALTER TABLE portal_generated_report_instances
  DROP CONSTRAINT IF EXISTS portal_generated_report_instances_claim_exclusive_check;
ALTER TABLE portal_generated_report_instances
  ADD CONSTRAINT portal_generated_report_instances_claim_exclusive_check
  CHECK (claim_token IS NULL
         OR completed_claim_token IS NULL
         OR claim_token <> completed_claim_token);

-- ---------------------------------------------------------------------------
-- 3. The availability summary is a PROJECTION, not a claim
--
-- 068 refreshed the summary from the member triggers, which is correct for
-- member changes and silent about instance changes. The reviewed forgery was an
-- ordinary UPDATE on the instance itself. Deriving the three columns in a
-- BEFORE trigger makes them unwritable by any statement: whatever a caller sets
-- is replaced by what the member rows actually say, in the same statement,
-- before the row is stored.
--
-- The horizon rule is 068's, unchanged: a NULL member expiry among available
-- members means unbounded and wins over any finite maximum.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION portal_generated_report_instance_summary_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  total INT;
  available INT;
  horizon TIMESTAMPTZ;
BEGIN
  SELECT count(*)::int,
         count(*) FILTER (WHERE f.is_available)::int,
         CASE
           WHEN bool_or(f.is_available AND f.expires_at IS NULL) THEN NULL
           ELSE max(f.expires_at) FILTER (WHERE f.is_available)
         END
    INTO total, available, horizon
    FROM portal_generated_report_files f
   WHERE f.instance_id = NEW.instance_id;

  NEW.published_member_count := COALESCE(total, 0);
  NEW.available_member_count := COALESCE(available, 0);
  NEW.available_expires_at   :=
    CASE WHEN COALESCE(available, 0) > 0 THEN horizon ELSE NULL END;
  RETURN NEW;
END
$$;

-- Fires after `portal_generated_report_instance_identity` (BEFORE row triggers
-- run in name order, and `identity` sorts before `summary_guard`), so an
-- attempt to change client, type or period is still refused first and the
-- summary is derived for the row that is actually about to be stored.
DROP TRIGGER IF EXISTS portal_generated_report_instance_summary_guard
  ON portal_generated_report_instances;
CREATE TRIGGER portal_generated_report_instance_summary_guard
  BEFORE INSERT OR UPDATE ON portal_generated_report_instances
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_instance_summary_guard();

-- ---------------------------------------------------------------------------
-- 4. Presentation metadata cannot describe different content
--
-- `file_format` is the badge, `content_type` is what the browser is told, and a
-- member that disagrees with itself is what made the reviewed HTML-as-PDF
-- preview possible. The pair is now constrained to the formats the platform
-- actually produces. The application additionally binds both to the stored
-- artifact's own metadata, so this CHECK is the floor, not the whole rule.
--
-- `is_previewable` is narrower still: `api/artifacts/preview.py` serves exactly
-- one content type inline (`pdf_inline`), and everything else is decoded
-- server-side or downloaded. A member may therefore be previewable only if its
-- bytes are a PDF. Active same-origin content can never be marked previewable.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_generated_report_files
  DROP CONSTRAINT IF EXISTS portal_generated_report_files_format_content_type_check;
ALTER TABLE portal_generated_report_files
  ADD CONSTRAINT portal_generated_report_files_format_content_type_check
  CHECK (
    CASE file_format
      WHEN 'PDF'  THEN content_type = 'application/pdf'
      WHEN 'XLSX' THEN content_type IN (
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         'application/vnd.ms-excel')
      WHEN 'CSV'  THEN content_type IN ('text/csv', 'application/csv')
      WHEN 'JSON' THEN content_type IN ('application/json', 'text/json')
      WHEN 'TXT'  THEN content_type = 'text/plain'
      WHEN 'ZIP'  THEN content_type IN ('application/zip', 'application/x-zip-compressed')
      ELSE FALSE
    END
  );

ALTER TABLE portal_generated_report_files
  DROP CONSTRAINT IF EXISTS portal_generated_report_files_previewable_content_check;
ALTER TABLE portal_generated_report_files
  ADD CONSTRAINT portal_generated_report_files_previewable_content_check
  CHECK (NOT is_previewable OR content_type = 'application/pdf');

COMMENT ON COLUMN portal_generated_report_files.is_previewable IS
  'RP-14 previewability. Only a PDF member may be previewable, because application/pdf is the only content type this platform embeds inline (api/artifacts/preview.py). The preview route re-checks this independently on every request.';

-- ---------------------------------------------------------------------------
-- 5. A display filename is a NAME, never a path
--
-- It is the entry name inside `Pobierz wszystkie` and the `Content-Disposition`
-- of a single download. Separators, control characters, `.` and `..` are
-- refused here so no delivery path has to sanitise a stored value, and so a
-- traversal-shaped name cannot be persisted at all.
-- ---------------------------------------------------------------------------
ALTER TABLE portal_generated_report_files
  DROP CONSTRAINT IF EXISTS portal_generated_report_files_filename_safe_check;
ALTER TABLE portal_generated_report_files
  ADD CONSTRAINT portal_generated_report_files_filename_safe_check
  CHECK (
    display_filename ~ '^[^\x00-\x1F\x7F/\\]+$'
    AND display_filename NOT IN ('.', '..')
    AND btrim(display_filename) <> ''
  );

COMMIT;
