-- 061_workflow_a_provider_request_log.sql
-- M4 — durable provider-request facts and sub-window completeness evidence.
--
-- Specification of record:
--   docs/20_telematics_ingestion_permanent_repair_plan.md
--     §4.3  (the projected field set and why it is a projection, not new
--            instrumentation)
--     §4.3a D1 (placement: platform `logdb`, beside the watermark it gates)
--     §4.3a D3 (hinge 2: finalization and the coverage CAS share one transaction)
--     §4.5  (retention: 180 days, independent of the 60-day log prune)
--     §21.2 condition 6, §21.4 (the schema direction this implements)
--     §22.2 (the two-state lifecycle this table carries, and why)
--   docs/12_telematics_trips_pagination_compatibility.md §4–§8
--     (`data_invariants_v1`, whose terminal state this records and never
--      redefines)
--
-- ============================================================================
-- TWO STATES, AND WHY THIS TABLE HAS THEM
-- ============================================================================
--
-- This relation answers two different questions, and conflating them was a
-- real defect (found by independent review of the first M4 candidate):
--
--   PENDING   "this provider request happened."
--             A durable REQUEST FACT. Written by the business job, in its own
--             platform transaction, BEFORE the client business transaction
--             commits. It is what makes `client_trips.first_seen_request_id`
--             resolvable: by the time a trip row can commit carrying a
--             first-seen identity, the request bearing that identity is
--             already durable here.
--
--             It proves NOTHING about coverage. It does not say the sub-window
--             completed, and it cannot advance a watermark.
--
--   FINALIZED "this request belongs to a fire whose effective window was
--             verified exactly tiled and complete."
--             COVERAGE EVIDENCE. Written only by the dispatcher, only by
--             promoting existing PENDING rows, inside the same platform
--             transaction as the coverage CAS.
--
-- The first candidate had only the second state, written after the business
-- commit. A platform failure between the two therefore left a committed
-- `first_seen_request_id` pointing at evidence that never became durable —
-- permanently, because the value is correctly immutable. Pre-persisting the
-- request fact removes that window: the ordering is now
--
--     request facts durable (PENDING)
--       -> business transaction commits (trips carry first_seen_request_id)
--         -> dispatcher verifies M3 + M4
--           -> promote to FINALIZED + coverage CAS, one transaction
--
-- and a failure at any later step leaves the request fact durable and the
-- watermark untouched. The inverse failure — request facts written, business
-- transaction rolled back — leaves unreferenced PENDING rows, which are inert
-- forensic records that no trip points at and that no coverage read accepts.
--
-- ============================================================================
-- WHAT THIS IS NOT
-- ============================================================================
--
-- Not a raw-response archive. Identity, position, timing, counts and terminal
-- state only; never provider payloads.
--
-- Not a distributed transaction. There is no XA and no two-phase commit
-- between the platform database and a client business database, and none is
-- introduced here. What replaces it is ordering plus a durable, retryable
-- record — see docs/20 §22.4.
--
-- INERTNESS OF THIS MIGRATION.
--   Creates one table and its indexes. Seeds no row, reads no client data,
--   changes no schedule, no coverage row, no client business database and no
--   existing table.
--
-- WHY THERE IS NO FOREIGN KEY TO `public.runs`.
--   `platform_run_id` is an unenforced reference, exactly as
--   `workflow_a_control.client_schedule_run_history.platform_run_id` already is
--   (migration 008). This is not an oversight and must not be "fixed":
--   `api/platform_prune.py` DELETEs `public.runs` rows older than its retention
--   horizon, and a real FK would either abort that transaction or cascade-delete
--   evidence this table is required to keep for 180 days.
--
-- WHY THERE IS NO FOREIGN KEY TO `client_trips.first_seen_request_id`.
--   That column lives in each per-client BUSINESS database; this table lives in
--   the platform database `logdb`. PostgreSQL has no cross-database foreign key,
--   so the relationship is an application-level UUID convention the writer
--   upholds — stated plainly here rather than implied by a constraint that
--   cannot exist (docs/20 §4.3 correction, §4.3a D2).
--
-- ORDERING, AND ONE CONSEQUENCE OF THE RUN-HISTORY FOREIGN KEY.
--   This migration must be applied AFTER 014, which it is by number, and a
--   fresh in-order apply is therefore unaffected. But note the consequence
--   deliberately: 014 contains
--   `TRUNCATE workflow_a_control.client_schedule_run_history`, and once this
--   table references that one, that TRUNCATE can no longer run. Re-applying 014
--   to a database that already has 061 would fail with
--   `cannot truncate a table referenced in a foreign key constraint`.
--
--   That is not a production path — `ops/db_migrate.sh` records applied
--   filenames and applies each file exactly once, and 014's truncate was a
--   one-shot re-key of a table documented as empty in v1 — but it is real, and
--   anyone rebuilding by replaying the whole set onto an already-migrated
--   database needs to know it rather than discover it.
--
-- ROLLBACK.
--   `DROP TABLE workflow_a_control.provider_request_log;` is safe while
--   condition 6 is not enabled, because nothing else reads it. The asymmetric
--   half of the rollback is deliberate: the additive, nullable
--   `first_seen_request_id` column on a live client business table is LEFT IN
--   PLACE, since dropping it would destroy provenance that cannot be
--   reconstructed (docs/20 §21.4).

-- Preflight — name the prerequisite rather than failing on the first statement
-- that happens to touch it, matching the 055-060 convention.
DO $$
BEGIN
  IF to_regclass('workflow_a_control.client_schedule_run_history') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_schedule_run_history is absent; apply migrations 008 and 014 before 061';
  END IF;
  IF to_regclass('workflow_a_control.client_dataset_coverage') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_dataset_coverage is absent; apply migration 057 before 061';
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS workflow_a_control.provider_request_log (
  -- Identity of one provider page request, minted in the child at request time.
  -- It is the value a newly INSERTed `client_trips` row points at through
  -- `first_seen_request_id`, which is why it is generated once, in the job,
  -- written here BEFORE the business transaction commits, and never re-minted.
  request_id                UUID        PRIMARY KEY,

  -- Lifecycle. See the header. `PENDING` is a request fact; `FINALIZED` is
  -- coverage evidence. Only the dispatcher may write `FINALIZED`, and only by
  -- promoting an existing `PENDING` row inside the coverage transaction.
  status                    TEXT        NOT NULL
    CHECK (status IN ('PENDING', 'FINALIZED')),

  -- Written by the CHILD at request time. The child owns its own platform run
  -- identity — `ops/runner.py` created it — and M3 independently verifies that
  -- the identity in the terminal record equals the one the launcher observed,
  -- so this is not a fact the dispatcher has to take on trust.
  platform_run_id           UUID        NOT NULL,   -- unenforced; see header

  -- Written by the DISPATCHER at finalization, bound from its own claim and
  -- never from the child's payload. NULL while PENDING: a request fact is not
  -- attributed to a fire until a verified fire claims it.
  run_history_id            UUID        NULL
    REFERENCES workflow_a_control.client_schedule_run_history (run_history_id)
    ON DELETE CASCADE,
  client_id                 UUID        NULL
    REFERENCES workflow_a_control.client_account (client_id) ON DELETE CASCADE,
  client_code               TEXT        NULL,
  schedule_id               UUID        NULL,
  dataset_name              TEXT        NULL,

  -- What was being covered. Written by the child; cross-checked by the
  -- dispatcher against its own claim before promotion.
  endpoint                  TEXT        NOT NULL,
  effective_window_start_ts TIMESTAMPTZ NOT NULL,
  effective_window_end_ts   TIMESTAMPTZ NOT NULL,

  -- The tiling unit. `covers_*` is the HALF-OPEN slice of the effective window
  -- this unit is responsible for, and is what the tiling check adds up;
  -- `requested_*` is what actually went on the wire, whose end is INCLUSIVE and
  -- therefore one boundary step short of `covers_to_ts` for every non-final
  -- unit. Conflating the two turns a tiling proof into a rounding argument, so
  -- both are stored.
  sub_window_index          INTEGER     NOT NULL,
  sub_window_label          TEXT        NULL,
  covers_from_ts            TIMESTAMPTZ NOT NULL,
  covers_to_ts              TIMESTAMPTZ NOT NULL,
  requested_from_ts         TIMESTAMPTZ NOT NULL,
  requested_to_ts           TIMESTAMPTZ NOT NULL,

  -- The literal Europe/Warsaw wall-clock numerals put on the wire (§1.4). Kept
  -- alongside the absolute instants because only the instants are comparable to
  -- a coverage window, and only the numerals reproduce the request.
  wire_start_value          TEXT        NOT NULL,
  wire_end_value            TEXT        NOT NULL,

  -- Pagination position and the two instants the publication-lag bounds need
  -- (§4.4). These are wall-clock UTC, not the monotonic clock the budgets use.
  page                      INTEGER     NOT NULL,
  request_started_at_utc    TIMESTAMPTZ NOT NULL,
  response_received_at_utc  TIMESTAMPTZ NOT NULL,

  http_status               INTEGER     NOT NULL,
  row_count                 INTEGER     NOT NULL,

  -- Completeness. NULL while PENDING, and that is the whole point: a request
  -- fact carries no claim about whether its sub-window finished. Populated only
  -- at promotion, from the completeness proof the dispatcher has already
  -- verified. `termination_reason` is `short_page` — the one authoritative
  -- terminal condition of `data_invariants_v1` — and `total_reconciliation` is
  -- `absent` or `exact`. `absent` is a PERMITTED state under the accepted D5
  -- Option B rule (docs/16 §5): only a *present* advisory total that fails to
  -- reconcile is a failure, and that raises inside the fetch contract long
  -- before a row is written here.
  subwindow_complete        BOOLEAN     NULL,
  termination_reason        TEXT        NULL,
  total_reconciliation      TEXT        NULL,

  recorded_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalized_at              TIMESTAMPTZ NULL,

  -- ------------------------------------------------------------------------
  -- Natural uniqueness. Keyed on `platform_run_id`, NOT `run_history_id`:
  -- the request fact is written before any fire has claimed it, so
  -- `run_history_id` is still NULL at that moment and a key containing it
  -- would constrain nothing (NULLs are distinct in a UNIQUE index). The
  -- platform run is the identity the child actually owns at write time.
  --
  -- Keyed on `sub_window_index`, NOT `sub_window_label`: the index is the
  -- tiling position and is NOT NULL for every row, including a unit that never
  -- reached the provider and therefore has no label. A key on the nullable
  -- label would silently stop constraining exactly the rows most worth
  -- constraining. This resolves a documentation/schema mismatch found by
  -- independent review — see docs/20 §22.3.
  --
  -- The effect: a duplicate request record is structurally impossible rather
  -- than a failure class someone has to detect at read time.
  -- ------------------------------------------------------------------------
  CONSTRAINT uq_provider_request_log_identity
    UNIQUE (platform_run_id, endpoint, sub_window_index, page),

  -- A tiling unit that covers nothing cannot prove anything.
  CONSTRAINT ck_provider_request_log_covers_forward
    CHECK (covers_to_ts > covers_from_ts),
  CONSTRAINT ck_provider_request_log_requested_forward
    CHECK (requested_to_ts >= requested_from_ts),
  CONSTRAINT ck_provider_request_log_window_forward
    CHECK (effective_window_end_ts > effective_window_start_ts),
  CONSTRAINT ck_provider_request_log_page_positive
    CHECK (page >= 1),
  CONSTRAINT ck_provider_request_log_sub_window_positive
    CHECK (sub_window_index >= 1),
  CONSTRAINT ck_provider_request_log_row_count_nonnegative
    CHECK (row_count >= 0),
  CONSTRAINT ck_provider_request_log_response_after_request
    CHECK (response_received_at_utc >= request_started_at_utc),

  -- A PENDING row is a request fact and nothing more: it must not be attributed
  -- to a fire and must not carry any completeness claim. This is what makes
  -- "provenance exists" structurally unable to masquerade as "coverage-complete".
  CONSTRAINT ck_provider_request_log_pending_claims_nothing
    CHECK (
      status <> 'PENDING'
      OR (run_history_id IS NULL
          AND client_id IS NULL
          AND client_code IS NULL
          AND schedule_id IS NULL
          AND dataset_name IS NULL
          AND subwindow_complete IS NULL
          AND termination_reason IS NULL
          AND total_reconciliation IS NULL
          AND finalized_at IS NULL)
    ),

  -- A FINALIZED row is coverage evidence and must be fully attributed.
  CONSTRAINT ck_provider_request_log_finalized_is_attributed
    CHECK (
      status <> 'FINALIZED'
      OR (run_history_id IS NOT NULL
          AND client_id IS NOT NULL
          AND schedule_id IS NOT NULL
          AND dataset_name IS NOT NULL
          AND subwindow_complete IS NOT NULL
          AND finalized_at IS NOT NULL)
    ),

  -- A COMPLETE unit must name a valid termination and a known reconciliation
  -- state; an incomplete one must claim neither. The vocabulary is closed here
  -- as well as in Python so a direct SQL writer cannot invent a third state.
  CONSTRAINT ck_provider_request_log_terminal_state
    CHECK (
      subwindow_complete IS NULL
      OR (subwindow_complete
          AND termination_reason = 'short_page'
          AND total_reconciliation IN ('absent', 'exact'))
      OR (NOT subwindow_complete
          AND termination_reason IS NULL
          AND total_reconciliation IS NULL)
    )
);

-- The advancement predicate's read path: everything condition 6 needs about one
-- fire is one index scan.
CREATE INDEX IF NOT EXISTS idx_provider_request_log_run_history
  ON workflow_a_control.provider_request_log
     (run_history_id, endpoint, sub_window_index, page)
  WHERE run_history_id IS NOT NULL;

-- The promotion path: the dispatcher locates this launch's PENDING rows.
CREATE INDEX IF NOT EXISTS idx_provider_request_log_pending_run
  ON workflow_a_control.provider_request_log (platform_run_id)
  WHERE status = 'PENDING';

-- The late-arrival derivation (§4.3): the newest COMPLETE request for a client
-- and endpoint whose requested range brackets a trip start.
CREATE INDEX IF NOT EXISTS idx_provider_request_log_client_coverage
  ON workflow_a_control.provider_request_log
     (client_id, endpoint, covers_from_ts, covers_to_ts)
  WHERE subwindow_complete;

-- Retention (§4.5): 180 days for BOTH states, applied by `api/platform_prune.py`
-- against its own fixed horizon rather than the worker's `--days` value.
-- PENDING rows are pruned on the same horizon deliberately: they are the
-- resolvability guarantee for `first_seen_request_id`, so they must not expire
-- before the evidence they stand in for would have.
CREATE INDEX IF NOT EXISTS idx_provider_request_log_recorded_at
  ON workflow_a_control.provider_request_log (recorded_at);

COMMENT ON TABLE workflow_a_control.provider_request_log IS
  'M4 provider-request facts (status=PENDING) and coverage completeness evidence '
  '(status=FINALIZED). Insert-only apart from the single PENDING->FINALIZED promotion. '
  '180-day retention for both states. A PENDING row makes client_trips.first_seen_request_id '
  'resolvable and proves nothing about coverage; only FINALIZED rows are coverage evidence.';

COMMENT ON COLUMN workflow_a_control.provider_request_log.status IS
  'PENDING = durable request fact, written by the business job before its business '
  'transaction commits. FINALIZED = coverage evidence, written only by the dispatcher '
  'by promoting a PENDING row inside the coverage CAS transaction.';

COMMENT ON COLUMN workflow_a_control.provider_request_log.platform_run_id IS
  'Unenforced reference to public.runs.run_id. Deliberately not a foreign key: '
  'api/platform_prune.py deletes runs rows on its own horizon, and an enforced FK '
  'would either abort that prune or cascade-delete evidence this table must retain.';

COMMENT ON COLUMN workflow_a_control.provider_request_log.subwindow_complete IS
  'NULL while PENDING. At promotion: whether this sub-window finished all pages without '
  'provider error, pagination invariant failure or budget exhaustion. An absence observed '
  'through a request whose sub-window did not complete proves nothing.';
