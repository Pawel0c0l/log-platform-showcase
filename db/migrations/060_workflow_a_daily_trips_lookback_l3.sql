-- M2 — DAILY L = 3 for the ALPHA00001 Telematics `/trips` reconciliation horizon.
--
-- Specification of record:
--   docs/20_telematics_ingestion_permanent_repair_plan.md §3.1 (target semantics),
--     §3.6 (DAILY normalization), §14 (milestone M2), §15 (M2 verification rows)
--   docs/13_telematics_trips_stabilization_windows.md §16.1 (the window arithmetic
--     this value feeds)
--   docs/18_telematics_trips_request_time_contract.md §3.3 + docs/19 (the measured
--     publication-lag distribution that makes L = 1 structurally insufficient)
--
-- WHY THIS IS A MIGRATION AND NOT A CODE CONSTANT.
--   The daily lookback is not a Python default anywhere in the ingestion path.
--   `jobs.api.telematics.sync_trips_and_speeding` REQUIRES an explicit
--   `window_start_ts`/`window_end_ts` param pair and never derives a lookback of
--   its own; the sole production reader of `lookback_days` is
--   `jobs.api.telematics.dispatcher.evaluate_schedule`, which reads it from this
--   row and hands the derived window to the job. L therefore lives exactly here,
--   which is why docs/20 §14 sizes M2 as "one control-plane row; no schema
--   change". The other `lookback_days` literals in the tree are unrelated:
--   `client_dataset_schedule.lookback_days DEFAULT 7` (migration 012) applies
--   only to newly seeded rows, and `control_plane._default_schedule`'s
--   `lookback_days=1` is a documented placeholder on the no-row fallback that no
--   trips caller reads.
--
-- WHAT CHANGES.
--   Exactly one column of exactly one row: the daily `trips_sync` schedule of
--   ALPHA00001, from `lookback_days = 1` to `lookback_days = 3`. `enabled` is
--   deliberately NOT part of the predicate -- a temporarily paused schedule must
--   still receive the approved horizon, so that re-enabling it does not silently
--   resurrect L = 1.
--
-- WHAT DELIBERATELY DOES NOT CHANGE.
--   * `timezone` and `run_time` — docs/20 §3.6 states the pair "must be made
--     together or not at all" and §16 decision 3 leaves it open for the
--     approver. The approved M2 target is the lookback alone, so the ALPHA fire
--     keeps landing at 04:00 Europe/Warsaw during CEST.
--   * `frequency`, `day_of_week`, `day_of_month`, `enabled`,
--     `overwrite_existing`, `event_enrichment_mode` — cadence and persistence
--     policy are outside M2.
--   * Every other client. FOXTROT00001 is also daily at L = 1, but the late-arrival
--     evidence (docs/19) is ALPHA-specific and no approved target exists for it;
--     widening the horizon would change another client's provider load without
--     analysis. Left at L = 1 deliberately, not by oversight.
--   * The `client_account` stabilization tuning D/O/R (migration 056). L = 3
--     sits far inside `trips_max_recovery_span_seconds = 2678400`, so the clamp
--     does not bind and needs no adjustment; raising R is M7's business
--     (docs/20 §3.3).
--
-- IDEMPOTENCE. Re-applying once the row already reads 3 performs no UPDATE and
-- reports NOTICE-level convergence rather than failing.
--
-- FAIL-CLOSED. A missing target, a duplicate target, or a current value that is
-- neither the expected 1 nor the target 3 raises. This migration never "repairs"
-- an unexpected control-plane state into the one it wanted to find.

-- Preflight, matching the 055-058 convention: name the prerequisite explicitly so
-- a missing schema fails with the reason rather than a raw "relation does not
-- exist" from the first statement that happens to touch it.
DO $$
BEGIN
  IF to_regclass('workflow_a_control.client_dataset_schedule') IS NULL THEN
    RAISE EXCEPTION
      'migration 012_workflow_a_client_dataset_schedule.sql must be applied first';
  END IF;
  IF to_regclass('workflow_a_control.client_account') IS NULL THEN
    RAISE EXCEPTION
      'workflow_a_control.client_account is absent; apply the Workflow A control-plane migrations first';
  END IF;
END;
$$;

DO $$
DECLARE
  target_client_code  CONSTANT TEXT    := 'ALPHA00001';
  target_dataset      CONSTANT TEXT    := 'trips_sync';
  expected_lookback   CONSTANT INTEGER := 1;
  approved_lookback   CONSTANT INTEGER := 3;
  target_schedule_id  UUID;
  target_frequency    TEXT;
  current_lookback    INTEGER;
  matched             INTEGER;
  affected            INTEGER;
BEGIN
  SELECT count(*) INTO matched
    FROM workflow_a_control.client_dataset_schedule cds
    JOIN workflow_a_control.client_account ca
      ON ca.client_id = cds.client_id
   WHERE ca.client_code = target_client_code
     AND cds.dataset_name = target_dataset;

  IF matched = 0 THEN
    RAISE EXCEPTION
      'M2 target absent: no client_dataset_schedule row for %/%',
      target_client_code, target_dataset;
  END IF;

  -- Unreachable under the constraints that should exist:
  -- `uq_client_dataset_schedule UNIQUE (client_id, dataset_name)` bounds it to one
  -- row per client, and `idx_client_account_client_code` (migration 010) bounds
  -- `client_code` to one client_account. Reaching here therefore means one of those
  -- is missing or was dropped — a control-plane defect this migration must not
  -- paper over by updating an arbitrary one of the matches.
  IF matched > 1 THEN
    RAISE EXCEPTION
      'M2 target ambiguous: % client_dataset_schedule rows match %/%',
      matched, target_client_code, target_dataset;
  END IF;

  SELECT cds.schedule_id, cds.frequency, cds.lookback_days
    INTO target_schedule_id, target_frequency, current_lookback
    FROM workflow_a_control.client_dataset_schedule cds
    JOIN workflow_a_control.client_account ca
      ON ca.client_id = cds.client_id
   WHERE ca.client_code = target_client_code
     AND cds.dataset_name = target_dataset;

  -- M2 is defined for the DAILY path only (docs/20 §3.1). A row that has since
  -- become weekly or monthly is a different contract and must not silently
  -- inherit the daily horizon.
  IF target_frequency IS DISTINCT FROM 'daily' THEN
    RAISE EXCEPTION
      'M2 applies to the DAILY schedule only; %/% is frequency=%',
      target_client_code, target_dataset, coalesce(target_frequency, '<null>');
  END IF;

  IF current_lookback = approved_lookback THEN
    RAISE NOTICE
      'M2 already converged: %/% (schedule_id=%) already reads lookback_days=%',
      target_client_code, target_dataset, target_schedule_id, approved_lookback;
    RETURN;
  END IF;

  IF current_lookback IS DISTINCT FROM expected_lookback THEN
    RAISE EXCEPTION
      'M2 precondition failed: %/% reads lookback_days=%, expected % (pre-M2) or % (post-M2)',
      target_client_code, target_dataset,
      coalesce(current_lookback::TEXT, '<null>'), expected_lookback, approved_lookback;
  END IF;

  UPDATE workflow_a_control.client_dataset_schedule
     SET lookback_days = approved_lookback,
         -- Honest provenance for a genuine configuration mutation.
         --
         -- `ops/execution_watchdog.py` consults the row's `updated_at`/`created_at`
         -- as the eligibility epoch ONLY for a subject it has never observed
         -- (`execution_watchdog.py` ~line 709: the fallback branch is reached when
         -- `previous is None`). For an already-observed subject the stored epoch
         -- wins and this bump is inert. Migration 059 is recent, so before
         -- applying, confirm read-only that the observation row exists:
         --
         --   SELECT 1 FROM ops_control.watchdog_observation
         --    WHERE subject_key = 'workflow_a:ALPHA00001:trips_sync';
         --
         -- If it does NOT exist, this bump would become the epoch and would
         -- suppress missing-run detection for fires before the apply.
         updated_at = now()
   WHERE schedule_id = target_schedule_id
     AND lookback_days = expected_lookback;

  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 1 THEN
    RAISE EXCEPTION
      'M2 write anomaly: expected exactly 1 updated row for schedule_id=%, got %',
      target_schedule_id, affected;
  END IF;

  RAISE NOTICE
    'M2 applied: %/% (schedule_id=%) lookback_days % -> %',
    target_client_code, target_dataset, target_schedule_id,
    expected_lookback, approved_lookback;
END;
$$;
