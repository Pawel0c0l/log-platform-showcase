-- Generated-report persistence for Report Explorer (approved stage S15).
--
-- Implements exactly the three capabilities the owner authorized in
-- `docs/40_report_generation_framework_persistence_contract.md` §14:
--
--   1. `portal_generated_report_definitions` — the report TYPE. Cadence,
--      period vocabulary, source-dataset binding, declared file contract,
--      retention policy, enablement.
--   2. `portal_generated_report_instances`   — one logical occurrence of a type
--      for one client and one reporting period.
--   3. `portal_generated_report_files`       — the files that occurrence
--      published, each with an explicit semantic role and an explicit main-file
--      flag.
--
-- Capability 4 of that document — per-account `NOWY` seen state — was NOT
-- authorized and is deliberately absent (`docs/40` §13.2 = B). Nothing here
-- reserves a column, an index or a name for it; re-adding it later is one
-- additive relation that references the instance and is referenced by nothing.
--
-- WHAT THIS IS NOT.
--   It is not a retrofit of `artifacts`, `portal_report_folders`,
--   `database_export_jobs` or `ingest.*`: not one of them is altered, and not
--   one row of any of them is read, rewritten or copied. There is no backfill
--   and no seed. `docs/39` §1 settles that Report Explorer shows reports the
--   platform GENERATES, so an existing artifact is not, and can never become,
--   a report instance by applying this file. A database export reaches Report
--   Explorer through a read-only adapter over `database_export_jobs`
--   (`docs/40` §10.2) and is never copied into these relations.
--
-- WHY BYTES STAY IN `artifacts`.
--   `db/migrations/043` already extended `artifacts` with `owner_user_id`,
--   `expires_at` and `expired_at` precisely so a PLATFORM-GENERATED file could
--   be stored, expired and delivered, and `ops/database_export_worker.py`
--   already publishes one that way while keeping its domain identity in its own
--   relation. This follows the identical split: domain meaning here, bytes and
--   delivery in `artifacts`. A member's artifact reference is an implementation
--   pointer and carries no Workflow B lineage — no `run_id`, no `raw_file_id`,
--   no `artifact_role`, no `report_type`.
--
-- WHY THE MEMBER OUTLIVES ITS BYTES.
--   `ON DELETE SET NULL` on the artifact reference, so object cleanup can never
--   destroy the member record. `Pliki wygasły` is exactly the state where the
--   instance and its member metadata survive the objects
--   (`PRODUCT_BEHAVIOR_CONTRACT.md` §3.3 — "history retains the record"), and a
--   cascade from `artifacts` would delete the history the product promises.
--
-- IDENTITY IS NEVER A FILENAME.
--   The instance's identity is a surrogate key; its period is a declaration of
--   the report definition; a file's role is an explicit column. Nothing in this
--   schema derives identity, period, ordering or role from a filename, a folder
--   name, a display label or a file extension (`docs/39` §1, §5).
--
-- Additive and forward-only, per the repository migration contract. Rollout
-- order is this migration FIRST, then the application: `S15` declares this file
-- in `db/schema_requirements.json`, so a release carrying the Report Explorer
-- code refuses to activate over a database that has not applied it.
--
-- NOT APPLIED TO PRODUCTION BY THIS CHANGE.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Prerequisites.
--
-- Every relation this migration references must already exist. Creating a
-- half-connected schema that "looks applied" is exactly the failure mode
-- migration 067 was written to close, so absence RAISES here rather than
-- degrading.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  missing TEXT;
BEGIN
  FOREACH missing IN ARRAY ARRAY['portal_clients', 'portal_database_datasets', 'artifacts'] LOOP
    IF to_regclass('public.' || missing) IS NULL THEN
      RAISE EXCEPTION
        'migration 068 requires public.% to exist; apply the earlier migrations first', missing;
    END IF;
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Report definition — the report TYPE (`docs/40` §1.1, §14 capability 1)
--
-- One row per canonical report type. It owns everything that is true of the
-- type rather than of one period: cadence, period vocabulary, source binding,
-- the declared file contract and retention.
--
-- CLIENT SCOPE IS NOT A COLUMN AND NOT A CHILD RELATION.
--   `docs/40` §1.1 allows either a per-client enablement relation or a
--   definition-per-client row, and requires neither. This schema takes the third
--   reading the document also permits: the definition is CLIENT-INDEPENDENT —
--   which §12 already forces for the source binding, since a definition binds a
--   dataset by `slug` and `portal_database_datasets` is `UNIQUE (client_code,
--   slug)` — and the client is an input to generation, persisted on the
--   instance. The read path therefore derives the rail from the instances the
--   account may actually see, so a type can never be advertised for a client it
--   has produced nothing for, and `RP-7` (rail counts sum to the library total)
--   holds by construction rather than by keeping two tables in agreement.
--
-- PERIOD KIND LIVES HERE AND IS PART OF THE TYPE'S IDENTITY.
--   A weekly type does not emit a monthly period. Making `period_kind` a
--   property of the definition, and referencing it from the instance through a
--   composite foreign key (§2), is what makes "the adjacent period" a
--   well-defined question: within one `(client, definition)` every instance
--   belongs to one calendar vocabulary, so a total order exists (`I-4`).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_generated_report_definitions (
  definition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- `I-1`. The stable machine identity. Never a display string, never a
  -- filename, never derived from one.
  type_key TEXT NOT NULL,

  -- Product copy: `Typ raportu` in the rail, the type filter and the detail
  -- metadata grid; the description is the detail header's account of what the
  -- report covers (`PBC` §3.6).
  display_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',

  -- `Cykl` (`RP-2`, `PBC` §3.1, §3.6). The class is the approved vocabulary;
  -- the detail is its human schedule suffix, so the rail renders
  -- `tygodniowy · pon. 04:00` without re-deriving anything. Cadence is a
  -- PRODUCT statement about the type's business cycle and is never read out of
  -- a systemd timer, a cron entry or a Workflow B mail schedule (`docs/39` §12).
  cadence_class TEXT NOT NULL,
  cadence_detail TEXT NOT NULL DEFAULT '',

  -- The period vocabulary this type produces, and the boundary semantics a
  -- reader must never assume (`docs/40` §5). Platform default: calendar dates
  -- in `Europe/Warsaw`, inclusive at both ends.
  period_kind TEXT NOT NULL,
  period_timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',

  -- `RP-18` source binding, declared CLIENT-INDEPENDENTLY by dataset slug and
  -- resolved per client at generation time (`docs/40` §12). The date column is
  -- the one the period filter is applied to; null means this type offers no
  -- source-data navigation.
  source_dataset_slug TEXT,
  source_date_column TEXT,

  -- A stable reference to the report's SQL/report definition in repository
  -- code. The SQL text itself is code, not a database payload.
  generation_definition_ref TEXT NOT NULL,

  -- The declared member set a successful generation produces: format, semantic
  -- role, and which one is the main file. A JSON document because it is a
  -- DECLARATION the generator reads, never a query axis.
  file_contract_json JSONB NOT NULL DEFAULT '[]'::jsonb,

  -- `Retencja plików` (`PBC` §3.6) when no member remains to read an expiry
  -- from. Null means the type declares no retention window.
  retention_months INTEGER,

  -- Enablement, so a retired type stops generating without deleting its
  -- history (`docs/40` §1.1).
  is_active BOOLEAN NOT NULL DEFAULT TRUE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT portal_generated_report_definitions_type_key_key UNIQUE (type_key),
  CONSTRAINT portal_generated_report_definitions_type_key_check
    CHECK (type_key ~ '^[a-z0-9][a-z0-9_]{1,62}[a-z0-9]$'),
  CONSTRAINT portal_generated_report_definitions_display_name_check
    CHECK (char_length(display_name) BETWEEN 1 AND 120),
  CONSTRAINT portal_generated_report_definitions_cadence_class_check
    CHECK (cadence_class IN ('weekly', 'monthly', 'quarterly', 'on_demand')),
  CONSTRAINT portal_generated_report_definitions_period_kind_check
    CHECK (period_kind IN ('week', 'month', 'quarter', 'day', 'none')),
  CONSTRAINT portal_generated_report_definitions_retention_months_check
    CHECK (retention_months IS NULL OR retention_months BETWEEN 1 AND 600),
  CONSTRAINT portal_generated_report_definitions_file_contract_check
    CHECK (jsonb_typeof(file_contract_json) = 'array' AND octet_length(file_contract_json::text) <= 8192),
  -- A source binding is a pair. Half of one would make `RP-18` render a link
  -- with no period filter, or a period filter against no dataset.
  CONSTRAINT portal_generated_report_definitions_source_binding_check
    CHECK ((source_dataset_slug IS NULL) = (source_date_column IS NULL))
);

-- The composite target the instance's foreign key points at (§2). Redundant as
-- a uniqueness rule — `definition_id` is already the primary key — and required
-- as a referencable key.
CREATE UNIQUE INDEX IF NOT EXISTS portal_generated_report_definitions_id_period_kind_key
  ON portal_generated_report_definitions (definition_id, period_kind);

COMMENT ON TABLE portal_generated_report_definitions IS
  'S15 generated-report TYPE: cadence, period vocabulary, source binding, declared file contract, retention. Client-independent; the client is an input to generation and is persisted on the instance.';
COMMENT ON COLUMN portal_generated_report_definitions.type_key IS
  'Stable machine identity of the report type (I-1). Immutable once instances exist; never a display string and never derived from a filename.';
COMMENT ON COLUMN portal_generated_report_definitions.period_kind IS
  'The calendar vocabulary every instance of this type belongs to. Referenced by the instance through a composite FK so one type cannot mix vocabularies, which is what makes period adjacency well defined (I-4).';

-- ---------------------------------------------------------------------------
-- 2. Report instance — one occurrence for one client and one period
--    (`docs/40` §1.2, §14 capability 2)
--
-- IDENTITY. `instance_id` is a surrogate key and is the detail URL (`RP-11`,
-- `SH-12`, `SH-13`). It survives regeneration, so a link a user kept keeps
-- resolving to the same period.
--
-- IDEMPOTENCY (`I-2`). `UNIQUE (client_code, definition_id, period_key)` is the
-- idempotency key of the whole framework. A retry, a re-run or a crash recovery
-- upserts on it and therefore cannot produce a second logical instance for the
-- same period. The generator never reads-then-writes.
--
-- NON-OVERLAP (`I-4`). `UNIQUE (client_code, definition_id, period_kind,
-- period_start)` plus the calendar-alignment CHECKs below. Two aligned periods
-- of the same kind that start on different days cannot overlap, and the
-- composite FK to the definition guarantees one kind per type — so previous/next
-- period is a total order, not a guess. This is deliberately NOT an `EXCLUDE`
-- constraint: that would require the `btree_gist` extension to state something
-- two btree indexes and three immutable CHECKs already state exactly.
--
-- LIFECYCLE vs FILE AVAILABILITY (`docs/40` §3.3). `generation_state` is the
-- state of the LATEST ATTEMPT; `last_published_at` is the durable fact that some
-- attempt published. They are separate on purpose: a failed retry over a
-- published instance must keep rendering `Gotowy`, because its files are still
-- there and removing working actions to report an operational failure would
-- contradict `RP-8`. File expiry never rewrites generation state and generation
-- state never decides whether bytes exist.
--
-- THE FOUR APPROVED STATUSES ARE DERIVED, NEVER STORED. `PBC` §3.3 is a
-- presentation over (published?) × (any available member?), and is computed by
-- the read path from the denormalized counters below.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_generated_report_instances (
  instance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  definition_id UUID NOT NULL,
  -- RESTRICT: a type with history is never deleted out from under its
  -- instances (`docs/40` §14.1).
  CONSTRAINT portal_generated_report_instances_definition_fkey
    FOREIGN KEY (definition_id) REFERENCES portal_generated_report_definitions(definition_id) ON DELETE RESTRICT,

  -- The owning client, in platform terms. RESTRICT for the same reason: report
  -- history is not collateral of a client row being removed.
  client_code TEXT NOT NULL
    REFERENCES portal_clients(client_code) ON DELETE RESTRICT,

  -- Reporting period. ALWAYS supplied by the generation contract and NEVER
  -- inferred from generation time, artifact time, e-mail receipt time, a
  -- filename or a folder name (`docs/39` §4, §5). `period_end` is INCLUSIVE:
  -- the approved rendering `06–12.07.2026` is inclusive at both ends.
  period_kind TEXT NOT NULL,
  period_key TEXT NOT NULL,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,

  -- The instance label the library renders and text search matches
  -- (`Tydzień 28 · 2026`). Persisted so search and ordering read one
  -- authoritative string (`PBC` §3.4).
  display_name TEXT NOT NULL,

  -- Latest-attempt state plus the durable publication fact.
  generation_state TEXT NOT NULL DEFAULT 'pending',
  generation_started_at TIMESTAMPTZ,
  generation_finished_at TIMESTAMPTZ,
  last_published_at TIMESTAMPTZ,

  -- `docs/40` §3.4: the single derived value the library groups by, orders by
  -- and filters `Rok` on. Persisted rather than expressed, so one btree index
  -- serves grouping, ordering and pagination for a running instance that has no
  -- finish time yet.
  library_timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Crash-safe retry bookkeeping, following the proven `database_export_jobs`
  -- shape (`db/migrations/043`): the holder of the current attempt fences its
  -- own publication, so a stale attempt cannot publish over a newer one.
  attempt_count INTEGER NOT NULL DEFAULT 0,
  claim_token UUID,

  -- `Wiersze w raporcie` (`PBC` §3.6). Captured at generation and RETAINED
  -- after the files expire — the number is a fact about the report, not about
  -- the bytes.
  row_count BIGINT,

  -- `docs/40` §3.5 availability denormalization. Both counters and the expiry
  -- horizon are DERIVED BY TRIGGER from the member rows (§4), never asserted by
  -- application code, so `Status` can be a filter and the footer counter can be
  -- exact without scanning members per row.
  --
  -- `available_expires_at` is the horizon after which NO member is available:
  -- null means at least one available member never expires. Null is therefore
  -- "unbounded", not "unknown".
  published_member_count INTEGER NOT NULL DEFAULT 0,
  available_member_count INTEGER NOT NULL DEFAULT 0,
  available_expires_at TIMESTAMPTZ,

  -- `RP-18` provenance, snapshotted at generation. The dataset reference is
  -- nullable and SET NULL on delete, because a dataset that is renamed,
  -- re-pointed or removed must not delete or falsify report history; the JSON
  -- snapshot keeps the link truthful enough to explain itself years later
  -- (`docs/40` §12).
  source_dataset_id UUID REFERENCES portal_database_datasets(dataset_id) ON DELETE SET NULL,
  source_provenance_json JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Operational failure detail for `Zgłoś problem` (`RP-9`). Safe strings only:
  -- no DSN, no storage key, no driver text.
  safe_error_code TEXT,
  safe_error_message TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- One type, one calendar vocabulary (`I-4`'s precondition).
  CONSTRAINT portal_generated_report_instances_definition_period_kind_fkey
    FOREIGN KEY (definition_id, period_kind)
    REFERENCES portal_generated_report_definitions(definition_id, period_kind),

  -- `I-2`. The idempotency key.
  CONSTRAINT portal_generated_report_instances_period_key_uniq
    UNIQUE (client_code, definition_id, period_key),
  -- `I-4`. Two aligned periods of one kind cannot share a start.
  CONSTRAINT portal_generated_report_instances_period_start_uniq
    UNIQUE (client_code, definition_id, period_kind, period_start),

  CONSTRAINT portal_generated_report_instances_period_order_check
    CHECK (period_start <= period_end),
  CONSTRAINT portal_generated_report_instances_period_key_check
    CHECK (char_length(period_key) BETWEEN 1 AND 40),
  CONSTRAINT portal_generated_report_instances_display_name_check
    CHECK (char_length(display_name) BETWEEN 1 AND 200),

  -- `I-3`, expressed with immutable expressions only, so it is a real CHECK and
  -- not a comment. A week starts on a Monday and covers seven days; a month and
  -- a quarter are calendar-aligned; a day is a single date. These are what make
  -- the `period_start` uniqueness above equal to non-overlap.
  CONSTRAINT portal_generated_report_instances_period_alignment_check
    CHECK (
      CASE period_kind
        WHEN 'week' THEN
          EXTRACT(ISODOW FROM period_start) = 1 AND period_end = period_start + 6
        WHEN 'month' THEN
          date_trunc('month', period_start::timestamp)::date = period_start
          AND period_end = (date_trunc('month', period_start::timestamp) + INTERVAL '1 month' - INTERVAL '1 day')::date
        WHEN 'quarter' THEN
          date_trunc('quarter', period_start::timestamp)::date = period_start
          AND period_end = (date_trunc('quarter', period_start::timestamp) + INTERVAL '3 months' - INTERVAL '1 day')::date
        WHEN 'day' THEN
          period_end = period_start
        WHEN 'none' THEN
          period_end = period_start
        ELSE FALSE
      END
    ),

  CONSTRAINT portal_generated_report_instances_state_check
    CHECK (generation_state IN ('pending', 'running', 'succeeded', 'failed')),

  -- A succeeded attempt is one that PUBLISHED. The two facts cannot disagree.
  CONSTRAINT portal_generated_report_instances_succeeded_published_check
    CHECK (generation_state <> 'succeeded' OR last_published_at IS NOT NULL),
  -- "Published implies members" and "members imply published" are TRANSACTION
  -- truths, not row truths: publication inserts members before it stamps
  -- `last_published_at`, and regeneration empties the member set before
  -- refilling it. A row-local CHECK would reject both legitimate intermediate
  -- states, so `I-7` is enforced by the deferred constraint trigger in §5 and
  -- only the arithmetic relation between the counters is checked here.
  CONSTRAINT portal_generated_report_instances_member_counts_check
    CHECK (published_member_count >= 0
           AND available_member_count >= 0
           AND available_member_count <= published_member_count),
  CONSTRAINT portal_generated_report_instances_available_expiry_check
    CHECK (available_member_count > 0 OR available_expires_at IS NULL),

  CONSTRAINT portal_generated_report_instances_row_count_check
    CHECK (row_count IS NULL OR row_count >= 0),
  CONSTRAINT portal_generated_report_instances_attempt_count_check
    CHECK (attempt_count >= 0),
  CONSTRAINT portal_generated_report_instances_provenance_check
    CHECK (jsonb_typeof(source_provenance_json) = 'object'
           AND octet_length(source_provenance_json::text) <= 8192),
  CONSTRAINT portal_generated_report_instances_safe_error_check
    CHECK ((safe_error_code IS NULL OR char_length(safe_error_code) <= 80)
           AND (safe_error_message IS NULL OR char_length(safe_error_message) <= 500))
);

COMMENT ON TABLE portal_generated_report_instances IS
  'S15 generated-report INSTANCE: exactly one logical occurrence per (client, report type, reporting period). Identity is a surrogate key and is the detail URL; the period is declared by the generation contract and never inferred.';
COMMENT ON COLUMN portal_generated_report_instances.period_end IS
  'INCLUSIVE last day covered. The approved rendering 06-12.07.2026 is inclusive at both ends; an instant range is derived as [period_start 00:00, period_end + 1 day 00:00) in the definition timezone.';
COMMENT ON COLUMN portal_generated_report_instances.library_timestamp IS
  'COALESCE(generation_finished_at, generation_started_at) - the single axis the library groups, orders and filters by (docs/40 3.4). Persisted so a running instance with no finish time still pages stably.';
COMMENT ON COLUMN portal_generated_report_instances.available_expires_at IS
  'Instant after which no member is available. NULL means at least one available member has no expiry - unbounded, not unknown. Maintained by trigger from the member rows.';

-- Library page: client-scoped first, then type, ordered by the library axis.
CREATE INDEX IF NOT EXISTS idx_portal_generated_report_instances_library
  ON portal_generated_report_instances (client_code, definition_id, library_timestamp DESC, instance_id DESC);
-- Counters, the `Rok` filter and the unfiltered library page.
CREATE INDEX IF NOT EXISTS idx_portal_generated_report_instances_client_timestamp
  ON portal_generated_report_instances (client_code, library_timestamp DESC, instance_id DESC);
-- Period siblings and the history panel, both ordered by period, not by key text.
CREATE INDEX IF NOT EXISTS idx_portal_generated_report_instances_period_order
  ON portal_generated_report_instances (client_code, definition_id, period_start DESC);

-- ---------------------------------------------------------------------------
-- 3. File member (`docs/40` §1.3, §14 capability 3)
--
-- MAIN FILE IS A FLAG, NOT A POSITION. `RP-13` requires the main file to be
-- distinguished visually rather than by ordering, and `docs/39` forbids reading
-- meaning out of filenames — so neither `display_order` nor the extension nor
-- the name may decide it. A partial unique index enforces at most one per
-- instance structurally (`I-6`); the deferred check in §5 adds "exactly one
-- while members exist".
--
-- THE MEMBER OUTLIVES ITS BYTES. `artifact_id` is nullable and SET NULL, and
-- `is_available` is the marker the read path filters on. A member whose object
-- was cleaned up keeps its filename, format, size, role and expiry, which is
-- what makes `Pliki wygasły` and the history panel truthful (`docs/40` §11.1).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_generated_report_files (
  member_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- CASCADE: a member has no meaning without its instance. The reverse
  -- direction is what must never cascade, and does not.
  instance_id UUID NOT NULL
    REFERENCES portal_generated_report_instances(instance_id) ON DELETE CASCADE,

  -- Bytes and delivery. SET NULL so object cleanup can never destroy the
  -- member record.
  artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL,

  display_filename TEXT NOT NULL,
  file_format TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,

  -- `RP-14`: a non-previewable member offers download only. This is a FILE
  -- property, never a permission.
  is_previewable BOOLEAN NOT NULL DEFAULT FALSE,

  -- `PBC` §3.5 semantic note, declared by the definition's file contract and
  -- stamped on the member. Deliberately NOT `artifacts.artifact_role`, which is
  -- Workflow B pipeline vocabulary.
  semantic_role TEXT NOT NULL,
  is_main_file BOOLEAN NOT NULL DEFAULT FALSE,

  -- The secondary fact the panel renders beside the size: `12 stron`,
  -- `3 arkusze`, `4 118 wierszy`.
  content_metric_kind TEXT,
  content_metric_value BIGINT,

  expires_at TIMESTAMPTZ,
  is_available BOOLEAN NOT NULL DEFAULT TRUE,

  -- Presentation only. Carries no semantics and decides nothing.
  display_order INTEGER NOT NULL DEFAULT 0,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT portal_generated_report_files_filename_uniq
    UNIQUE (instance_id, display_filename),
  CONSTRAINT portal_generated_report_files_format_check
    CHECK (file_format IN ('PDF', 'XLSX', 'CSV', 'JSON', 'TXT', 'ZIP')),
  CONSTRAINT portal_generated_report_files_role_check
    CHECK (semantic_role IN ('main_document', 'detailed_data', 'raw_data')),
  CONSTRAINT portal_generated_report_files_filename_length_check
    CHECK (char_length(display_filename) BETWEEN 1 AND 260),
  CONSTRAINT portal_generated_report_files_size_check
    CHECK (size_bytes >= 0),
  CONSTRAINT portal_generated_report_files_metric_check
    CHECK ((content_metric_kind IS NULL) = (content_metric_value IS NULL)
           AND (content_metric_kind IS NULL
                OR (content_metric_kind IN ('pages', 'sheets', 'rows') AND content_metric_value >= 0))),
  -- An available member is one whose bytes can actually be served. Losing the
  -- artifact reference and staying "available" would render an action that
  -- 410s.
  CONSTRAINT portal_generated_report_files_available_needs_object_check
    CHECK (NOT is_available OR artifact_id IS NOT NULL)
);

-- `I-5`. One member per stored object within an instance, so a retry cannot
-- attach the same artifact twice.
CREATE UNIQUE INDEX IF NOT EXISTS portal_generated_report_files_artifact_uniq
  ON portal_generated_report_files (instance_id, artifact_id)
  WHERE artifact_id IS NOT NULL;

-- `I-6`, first half: AT MOST one main file per instance, enforced structurally.
CREATE UNIQUE INDEX IF NOT EXISTS portal_generated_report_files_main_uniq
  ON portal_generated_report_files (instance_id)
  WHERE is_main_file;

CREATE INDEX IF NOT EXISTS idx_portal_generated_report_files_instance
  ON portal_generated_report_files (instance_id, display_order, member_id);
-- The retention sweep: members due to expire, cheapest first.
CREATE INDEX IF NOT EXISTS idx_portal_generated_report_files_expiry
  ON portal_generated_report_files (expires_at)
  WHERE is_available AND expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portal_generated_report_files_artifact
  ON portal_generated_report_files (artifact_id)
  WHERE artifact_id IS NOT NULL;

COMMENT ON TABLE portal_generated_report_files IS
  'S15 generated-report FILE MEMBER. Bytes live in artifacts; this row carries the product meaning - role, main-file flag, size, previewability, expiry - and survives the artifact row so expired reports stay in history.';
COMMENT ON COLUMN portal_generated_report_files.is_main_file IS
  'Explicit main-file flag (RP-13, I-6). Main-file status is never inferred from display_order, filename or extension.';

-- ---------------------------------------------------------------------------
-- 4. Availability summary — DERIVED, not asserted
--
-- `docs/40` §3.5 requires the instance to carry an availability summary
-- maintained in the same transaction as any member change. Application code
-- could write those numbers, and then `I-7` would only be as true as the last
-- code path that remembered to. Deriving them here makes the summary a fact
-- ABOUT the member rows rather than a claim about them, and the row-local
-- CHECKs in §2 become real invariants.
--
-- `available_expires_at` is a horizon, so a null member expiry means unbounded
-- and wins over any finite maximum.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION portal_generated_report_refresh_summary(target UUID)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE portal_generated_report_instances i
     SET published_member_count = s.total,
         available_member_count = s.available,
         available_expires_at   = s.horizon,
         updated_at             = now()
    FROM (
      SELECT count(*)::int AS total,
             count(*) FILTER (WHERE f.is_available)::int AS available,
             CASE
               WHEN bool_or(f.is_available AND f.expires_at IS NULL) THEN NULL
               ELSE max(f.expires_at) FILTER (WHERE f.is_available)
             END AS horizon
        FROM portal_generated_report_files f
       WHERE f.instance_id = target
    ) s
   WHERE i.instance_id = target
     AND (i.published_member_count IS DISTINCT FROM s.total
          OR i.available_member_count IS DISTINCT FROM s.available
          OR i.available_expires_at IS DISTINCT FROM s.horizon);
END
$$;

-- Object cleanup must never be blocked by, and must never contradict, the
-- availability marker. When the artifact row goes away the FK sets the
-- reference to null; this trigger makes the member unavailable in the same
-- statement, so `Pliki wygasły` becomes true automatically instead of leaving a
-- member that claims to be downloadable and 410s. The member row itself —
-- filename, format, size, role, expiry — survives, which is what keeps the
-- history panel truthful (`docs/40` §11.1).
CREATE OR REPLACE FUNCTION portal_generated_report_files_availability_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.artifact_id IS NULL AND NEW.is_available THEN
    NEW.is_available := FALSE;
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS portal_generated_report_files_availability
  ON portal_generated_report_files;
CREATE TRIGGER portal_generated_report_files_availability
  BEFORE INSERT OR UPDATE ON portal_generated_report_files
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_files_availability_trigger();

CREATE OR REPLACE FUNCTION portal_generated_report_files_summary_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.instance_id IS DISTINCT FROM OLD.instance_id THEN
    -- `I-9` in the membership direction: a member belongs to exactly one
    -- instance, for its whole life. Moving one would silently rewrite two
    -- reports' file lists.
    RAISE EXCEPTION 'a generated report file member cannot be moved between instances';
  END IF;
  IF TG_OP <> 'INSERT' THEN
    PERFORM portal_generated_report_refresh_summary(OLD.instance_id);
  END IF;
  IF TG_OP <> 'DELETE' THEN
    PERFORM portal_generated_report_refresh_summary(NEW.instance_id);
  END IF;
  RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS portal_generated_report_files_summary
  ON portal_generated_report_files;
CREATE TRIGGER portal_generated_report_files_summary
  AFTER INSERT OR UPDATE OR DELETE ON portal_generated_report_files
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_files_summary_trigger();

-- ---------------------------------------------------------------------------
-- 5. `I-6` / `I-7` — deferred, because they are transaction-level truths
--
-- "A successful publication has at least one member" and "exactly one of those
-- members is the main file" cannot be row-local CHECKs: publication inserts
-- members and updates the instance in one transaction, and regeneration deletes
-- the old member set before inserting the new one. A row-local check would fail
-- on a legitimate intermediate state; a deferred CONSTRAINT TRIGGER checks the
-- state that actually commits.
--
-- The function deliberately RE-READS the instance rather than trusting the row
-- image the event carried, because the row image is a snapshot from the moment
-- the statement ran, not from commit.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION portal_generated_report_validate_instance(target UUID)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  inst RECORD;
  member_total INT;
  main_total INT;
BEGIN
  SELECT instance_id, generation_state, last_published_at, published_member_count
    INTO inst
    FROM portal_generated_report_instances
   WHERE instance_id = target;
  IF NOT FOUND THEN
    -- The instance was deleted in this transaction; its members went with it.
    RETURN;
  END IF;

  SELECT count(*)::int,
         count(*) FILTER (WHERE is_main_file)::int
    INTO member_total, main_total
    FROM portal_generated_report_files
   WHERE instance_id = target;

  -- `I-7`. A published report with no files would render `Pliki wygasły`, which
  -- would be a lie about a report that never had any.
  IF inst.last_published_at IS NOT NULL AND member_total = 0 THEN
    RAISE EXCEPTION
      'generated report instance % is published but has no file member (I-7)', target;
  END IF;
  IF inst.generation_state = 'succeeded' AND member_total = 0 THEN
    RAISE EXCEPTION
      'generated report instance % succeeded with no file member (I-7)', target;
  END IF;
  -- Members without a publication would be files nobody published.
  IF member_total > 0 AND inst.last_published_at IS NULL THEN
    RAISE EXCEPTION
      'generated report instance % has file members but was never published', target;
  END IF;

  -- `I-6`, second half. At most one is the partial unique index's job.
  IF member_total > 0 AND main_total <> 1 THEN
    RAISE EXCEPTION
      'generated report instance % has % main files; exactly one is required while members exist (I-6)',
      target, main_total;
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION portal_generated_report_instance_integrity_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM portal_generated_report_validate_instance(NEW.instance_id);
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION portal_generated_report_member_integrity_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    PERFORM portal_generated_report_validate_instance(OLD.instance_id);
  END IF;
  IF TG_OP <> 'DELETE' THEN
    PERFORM portal_generated_report_validate_instance(NEW.instance_id);
  END IF;
  RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS portal_generated_report_instance_integrity
  ON portal_generated_report_instances;
CREATE CONSTRAINT TRIGGER portal_generated_report_instance_integrity
  AFTER INSERT OR UPDATE ON portal_generated_report_instances
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_instance_integrity_trigger();

DROP TRIGGER IF EXISTS portal_generated_report_member_integrity
  ON portal_generated_report_files;
CREATE CONSTRAINT TRIGGER portal_generated_report_member_integrity
  AFTER INSERT OR UPDATE OR DELETE ON portal_generated_report_files
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_member_integrity_trigger();

-- ---------------------------------------------------------------------------
-- 6. Identity immutability
--
-- Regeneration REPLACES, it never forks (`I-8`): the same period keeps the same
-- identity, the same detail URL and the same place in the period sequence. And
-- an instance's client scope must not be able to change by accident — a report
-- silently changing owner is a cross-tenant disclosure, not a data-quality
-- issue. Both are the same rule: the identity columns are write-once.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION portal_generated_report_instance_identity_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.client_code IS DISTINCT FROM OLD.client_code THEN
    RAISE EXCEPTION 'a generated report instance cannot change client (% -> %)',
      OLD.client_code, NEW.client_code;
  END IF;
  IF NEW.definition_id IS DISTINCT FROM OLD.definition_id
     OR NEW.period_key IS DISTINCT FROM OLD.period_key
     OR NEW.period_kind IS DISTINCT FROM OLD.period_kind
     OR NEW.period_start IS DISTINCT FROM OLD.period_start
     OR NEW.period_end IS DISTINCT FROM OLD.period_end THEN
    RAISE EXCEPTION
      'a generated report instance cannot change its report type or reporting period (I-8)';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS portal_generated_report_instance_identity
  ON portal_generated_report_instances;
CREATE TRIGGER portal_generated_report_instance_identity
  BEFORE UPDATE ON portal_generated_report_instances
  FOR EACH ROW EXECUTE FUNCTION portal_generated_report_instance_identity_trigger();

-- ---------------------------------------------------------------------------
-- 7. No seed, no backfill.
--
-- This migration inserts no definition, no instance and no member. It reads no
-- `artifacts` row, no `ingest.*` row and no `database_export_jobs` row. Report
-- Explorer starts empty and fills with reports published into it going forward
-- (`docs/39` §6). A deterministic test asserts exactly that against a database
-- pre-loaded with artifact rows.
-- ---------------------------------------------------------------------------

COMMIT;
