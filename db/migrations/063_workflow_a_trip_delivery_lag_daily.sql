-- 063_workflow_a_trip_delivery_lag_daily.sql
-- M-LAG — the durable daily observed-delivery-lag slice.
--
-- Specification of record:
--   docs/21_telematics_delivery_lag_trace.md §3 (the metric), §6 (why this is a
--     RECOMPUTED slice and not an append-only aggregate), §7 (buckets and the
--     weekly-guarantee boundary), §8 (discovery attribution and why it is
--     denormalized here)
--   db/client_business/048_client_trips_first_seen_response_received_at.sql
--     (the immutable fact every metric here is computed from)
--
-- ============================================================================
-- WHY THIS IS A RECOMPUTED SLICE, NOT AN APPEND-ONLY AGGREGATE
-- ============================================================================
--
-- `client_trips.end_timestamp` is MUTABLE. It appears in the trip upsert's
-- `DO UPDATE SET` list on purpose: the documented `/trips` overlap rule returns
-- trips that extend past the requested boundary, so a trip can first be observed
-- while still open and have its end corrected by a later fire.
--
-- That breaks the obvious design. An append-only counter keyed on the
-- trip-end date OBSERVED AT FIRST INGESTION becomes permanently wrong the moment
-- a trip's end moves to another date: the old slice keeps a trip it no longer
-- contains, and the new slice never learns about it. Nothing detects it, because
-- both rows look like ordinary successful aggregations.
--
-- So this relation stores no counters that are incremented. Every row is the
-- COMPLETE, DETERMINISTIC PROJECTION of the current contents of `client_trips`
-- for one `(client_id, trip_end_date)`, and the aggregation job rewrites it
-- wholesale:
--
--     DELETE nothing, INSERT ... ON CONFLICT (client_id, trip_end_date)
--     DO UPDATE SET <every metric column>
--
-- Two consequences, both required:
--
--   * IDEMPOTENT. Recomputing a slice yields byte-identical metrics. Running the
--     job twice, or re-running it after a failure, cannot double-count — the
--     numbers come from a scan, never from an increment. Duplicate ingestion of
--     the same trip likewise cannot inflate anything, because
--     `(client_id, provider_trip_id)` collapses it to one row before this
--     relation ever sees it.
--   * SELF-CORRECTING WITHIN THE RECOMPUTE HORIZON. A trip that moves from
--     2026-07-21 to 2026-07-22 is corrected as soon as BOTH dates are
--     recomputed, so the job always recomputes a contiguous trailing window
--     rather than a single day. `recompute_horizon_days` on each row records how
--     deep that window was, so a stale slice is visible in the data instead of
--     being a property of a job invocation nobody kept.
--
-- The horizon is not a guess: `end_timestamp` corrections arrive with the trips
-- themselves, and a trip can only be re-requested inside a fire's effective
-- window. The deepest enabled `lookback_days` for the client therefore bounds
-- how far back a correction can originate, and the job derives the horizon from
-- the live schedule rather than hard-coding a number
-- (`jobs/api/telematics/delivery_lag.py`).
--
-- ============================================================================
-- WHAT THIS IS NOT
-- ============================================================================
--
-- Not a per-trip table. The per-trip facts already exist and are already
-- immutable: `client_trips.first_seen_response_received_at_utc` in the client
-- business database. Copying them per-trip into the platform database would
-- duplicate the authoritative fact for no gain and would need its own
-- correction story.
--
-- Not an alerting table. No thresholds are stored and none are implied. Choosing
-- them needs a measured distribution, which is what this relation exists to
-- produce (docs/21 §10).
--
-- Not a provider-publication measurement. Every value here is bounded below by
-- our own polling cadence, which is why the metric is named `observed_`.
--
-- INERTNESS. Creates one table and its indexes. Seeds no row, reads no client
-- data, changes no schedule, no coverage row and no existing relation.
--
-- RETENTION. None, deliberately. One row per client per day is on the order of
-- a few thousand rows a year for the whole fleet, and its entire purpose is to
-- outlive both the 180-day `provider_request_log` horizon and any client trip
-- retention. `api/platform_prune.py` is not extended to touch it.
--
-- ROLLBACK. `DROP TABLE workflow_a_control.trip_delivery_lag_daily;` is safe:
-- nothing reads it in the ingestion or coverage path, and every value in it is
-- recomputable from `client_trips` for as long as those rows exist.

DO $$
BEGIN
  IF to_regclass('workflow_a_control.client_account') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_account is absent; apply migration 008 before 063';
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS workflow_a_control.trip_delivery_lag_daily (
  -- Identity. One slice per client per trip-end date. `trip_end_date` is the
  -- date of the trip's CURRENT `end_timestamp` in the client's business
  -- timezone, which is the dimension an operator reasons in.
  client_id                 UUID        NOT NULL
    REFERENCES workflow_a_control.client_account (client_id) ON DELETE CASCADE,
  client_code               TEXT        NULL,
  trip_end_date             DATE        NOT NULL,

  -- Provenance denominator. `trips_total` counts every trip in the slice;
  -- `trips_with_provenance` counts those carrying an immutable first-seen
  -- observation. Every percentile and bucket below describes ONLY the latter.
  -- Both are stored so a consumer can never quote a percentile without being
  -- able to see what share of the slice it actually covers — an eroding
  -- denominator is the one failure mode that would make these numbers lie
  -- quietly.
  trips_total               BIGINT      NOT NULL,
  trips_with_provenance     BIGINT      NOT NULL,

  -- Rows in the EXPAND-window PROVENANCE_TIMESTAMP_PENDING state: the first-seen
  -- identity is present but the instant has not been copied to the trip row yet.
  -- They are excluded from the distribution like a no-provenance row, but they
  -- are NOT the same thing: their observation is real and recoverable from the
  -- exact platform request row, so they WILL enter the distribution once
  -- `ops/enrich_telematics_first_seen_timestamps.py` runs.
  --
  -- Stored because it is the slice's own completeness declaration. While it is
  -- non-zero the percentiles describe a subset that is still growing, and a
  -- consumer that cannot see that would read a transitional distribution as
  -- settled — which is exactly the wrong input to an M7 depth decision. Defaults
  -- to 0 so a pre-M-LAG-writer row shape remains insertable.
  trips_provenance_pending  BIGINT      NOT NULL DEFAULT 0,

  -- Distribution of `observed_delivery_lag_seconds`, over the rows with
  -- provenance. NULL when `trips_with_provenance = 0` — an empty sample has no
  -- median, and storing 0 would be a fabricated observation.
  --
  -- p99 is deliberately absent: at per-client-per-day sample sizes it is an
  -- artifact of the single largest value, which `lag_max_seconds` already
  -- reports honestly (docs/21 §7).
  lag_p50_seconds           BIGINT      NULL,
  lag_p90_seconds           BIGINT      NULL,
  lag_p95_seconds           BIGINT      NULL,
  lag_max_seconds           BIGINT      NULL,
  lag_min_seconds           BIGINT      NULL,

  -- Buckets. Disjoint, exhaustive over the provenance-bearing rows, and they
  -- MUST sum to `trips_with_provenance` — asserted by a CHECK below rather than
  -- trusted, because a bucketing bug that loses rows would otherwise look like
  -- a quiet improvement in the tail.
  --
  -- `bucket_negative` is not an error class. A trip observed before its final
  -- end is expected: `/trips` returns records overlapping the requested range,
  -- so a still-open trip can be returned and its `end_timestamp` corrected
  -- later. Clamping those to zero would erase a real signal about how much of
  -- our ingestion is catching trips in flight (docs/21 §4).
  bucket_negative           BIGINT      NOT NULL,
  bucket_under_6h           BIGINT      NOT NULL,
  bucket_6h_to_24h          BIGINT      NOT NULL,
  bucket_1d_to_3d           BIGINT      NOT NULL,
  bucket_3d_to_7d           BIGINT      NOT NULL,
  bucket_7d_to_weekly_guarantee   BIGINT NOT NULL,
  bucket_weekly_guarantee_to_15d  BIGINT NOT NULL,
  bucket_over_15d           BIGINT      NOT NULL,

  -- The boundary the two guarantee buckets were computed against, in seconds.
  -- Stored per row because it is a property of the cadence in force when the
  -- slice was computed, not a universal constant: if a future lookback change
  -- moves the guarantee, old slices must not silently appear to have used the
  -- new boundary. Derived, never hard-coded — see
  -- `jobs/api/telematics/delivery_lag.py`.
  weekly_guarantee_seconds  BIGINT      NOT NULL,

  -- Discovery attribution: which cadence first observed each trip. Resolved by
  -- the aggregation job from `first_seen_request_id` ->
  -- `provider_request_log.run_history_id` -> `client_schedule_run_history` ->
  -- `client_dataset_schedule.run_type`, and DENORMALIZED here on purpose.
  --
  -- That chain only resolves while the request row survives its 180-day
  -- horizon. The recompute horizon is bounded by the deepest enabled lookback
  -- (tens of days), so attribution is always resolvable at recompute time — but
  -- it will NOT be resolvable years later, which is exactly when the question
  -- "did the weekly layer earn its cost?" gets asked. Storing the resolved
  -- counts is the only way that question survives the prune.
  --
  -- `discovered_unattributed` counts rows whose provenance exists but whose
  -- request row could not be resolved. It is never merged into the base role:
  -- attributing a trip to DAILY because the evidence expired would invent the
  -- very answer the metric is for.
  discovered_daily          BIGINT      NOT NULL,
  discovered_weekly_reconciliation  BIGINT NOT NULL,
  discovered_monthly_reconciliation BIGINT NOT NULL,
  discovered_unattributed   BIGINT      NOT NULL,

  -- How the slice was produced. `recompute_horizon_days` is how far back the
  -- run that wrote this row recomputed; `computed_at` is when. Together they
  -- answer "could this slice still be stale?" without needing the job's logs.
  recompute_horizon_days    INTEGER     NOT NULL,
  computed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  computed_by               TEXT        NULL,

  CONSTRAINT pk_trip_delivery_lag_daily PRIMARY KEY (client_id, trip_end_date),

  CONSTRAINT ck_trip_delivery_lag_counts_nonnegative
    CHECK (
      trips_total >= 0 AND trips_with_provenance >= 0
      AND bucket_negative >= 0 AND bucket_under_6h >= 0
      AND bucket_6h_to_24h >= 0 AND bucket_1d_to_3d >= 0
      AND bucket_3d_to_7d >= 0 AND bucket_7d_to_weekly_guarantee >= 0
      AND bucket_weekly_guarantee_to_15d >= 0 AND bucket_over_15d >= 0
      AND discovered_daily >= 0 AND discovered_weekly_reconciliation >= 0
      AND discovered_monthly_reconciliation >= 0
      AND discovered_unattributed >= 0
      AND trips_provenance_pending >= 0
    ),

  CONSTRAINT ck_trip_delivery_lag_provenance_subset
    CHECK (trips_with_provenance <= trips_total),

  -- The pending rows are disjoint from the measured ones and both are within the
  -- total, so a slice can never claim more evidence than it has trips.
  CONSTRAINT ck_trip_delivery_lag_pending_subset
    CHECK (trips_with_provenance + trips_provenance_pending <= trips_total),

  -- The bucketing must lose nothing.
  CONSTRAINT ck_trip_delivery_lag_buckets_partition
    CHECK (
      bucket_negative + bucket_under_6h + bucket_6h_to_24h + bucket_1d_to_3d
      + bucket_3d_to_7d + bucket_7d_to_weekly_guarantee
      + bucket_weekly_guarantee_to_15d + bucket_over_15d
      = trips_with_provenance
    ),

  -- The attribution must lose nothing either, and must not gain anything.
  CONSTRAINT ck_trip_delivery_lag_attribution_partition
    CHECK (
      discovered_daily + discovered_weekly_reconciliation
      + discovered_monthly_reconciliation + discovered_unattributed
      = trips_with_provenance
    ),

  -- An empty sample has no percentiles; a non-empty one has all of them. Either
  -- state is fine, a mixture is a bug.
  CONSTRAINT ck_trip_delivery_lag_percentiles_present
    CHECK (
      (trips_with_provenance = 0
       AND lag_p50_seconds IS NULL AND lag_p90_seconds IS NULL
       AND lag_p95_seconds IS NULL AND lag_max_seconds IS NULL
       AND lag_min_seconds IS NULL)
      OR
      (trips_with_provenance > 0
       AND lag_p50_seconds IS NOT NULL AND lag_p90_seconds IS NOT NULL
       AND lag_p95_seconds IS NOT NULL AND lag_max_seconds IS NOT NULL
       AND lag_min_seconds IS NOT NULL)
    ),

  -- Percentiles are monotone by construction; if they are not, the estimator is
  -- wrong and every number in the row is suspect.
  CONSTRAINT ck_trip_delivery_lag_percentiles_ordered
    CHECK (
      trips_with_provenance = 0
      OR (lag_min_seconds <= lag_p50_seconds
          AND lag_p50_seconds <= lag_p90_seconds
          AND lag_p90_seconds <= lag_p95_seconds
          AND lag_p95_seconds <= lag_max_seconds)
    ),

  CONSTRAINT ck_trip_delivery_lag_horizon_positive
    CHECK (recompute_horizon_days >= 1 AND weekly_guarantee_seconds > 0)
);

-- Trend reads: one client over time, which is the operational question.
CREATE INDEX IF NOT EXISTS idx_trip_delivery_lag_daily_client_date
  ON workflow_a_control.trip_delivery_lag_daily (client_id, trip_end_date DESC);

-- Fleet reads: one date across clients, for the multi-client view.
CREATE INDEX IF NOT EXISTS idx_trip_delivery_lag_daily_date
  ON workflow_a_control.trip_delivery_lag_daily (trip_end_date DESC);

-- Staleness reads: which slices have not been recomputed recently.
CREATE INDEX IF NOT EXISTS idx_trip_delivery_lag_daily_computed_at
  ON workflow_a_control.trip_delivery_lag_daily (computed_at);

COMMENT ON TABLE workflow_a_control.trip_delivery_lag_daily IS
  'M-LAG observed-delivery-lag distribution, one COMPLETE RECOMPUTED slice per '
  '(client, trip end date). Never incremented: each row is the deterministic '
  'projection of the current client_trips contents for that date, upserted '
  'wholesale, so a trip whose mutable end_timestamp moves between dates is '
  'self-correcting once both dates are recomputed. Percentiles and buckets '
  'describe only trips_with_provenance; trips_total is the honest denominator and '
  'trips_provenance_pending is the slice''s own completeness declaration - while it '
  'is non-zero the distribution is still growing and must not be read as settled. '
  'Retained indefinitely - it must outlive both the 180-day provider_request_log '
  'horizon and client trip retention.';
