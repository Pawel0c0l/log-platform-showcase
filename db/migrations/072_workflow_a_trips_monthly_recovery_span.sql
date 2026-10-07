-- M7 — raise the `R` ceiling so a rolling 32-day MONTHLY reconciliation window
-- is expressible, and adopt it for the two clients approved to run one.
--
-- Specification of record:
--   docs/20_telematics_ingestion_permanent_repair_plan.md §3.1a (rolling monthly
--     supersedes the calendar-window mode), §3.3 (the `R` clamp made the
--     monthly window infeasible), §3.7c (the clamp condition, correctly
--     stated), §14 (milestone M7)
--   docs/13_telematics_trips_stabilization_windows.md §16.1 (`E_start =
--     max(min(base_start, W − O), E_end − R)` — the arithmetic this bounds)
--   jobs/trips_stabilization_config.py (the same ceiling in code; see ORDERING)
--
-- WHAT CHANGES.
--   1. `ck_client_account_trips_max_recovery_span_seconds` — upper bound
--      2 678 400 -> 2 768 400.
--   2. `trips_max_recovery_span_seconds` for ALPHA00001 and BRAVO00016 only:
--      2 678 400 -> 2 768 400.
--
-- WHY 2 768 400 AND NOT MORE.
--   `derive_effective_window` floors `E_start` at `E_end - R`, so the clamp
--   binds exactly when `R < L*86400 + O`. For the approved monthly `L = 32`
--   with `O = 3 600` that threshold is 32*86400 + 3600 = 2 768 400. At exactly
--   this value `base_start = E_end - R`, so the clamp is inert and the monthly
--   window keeps its overlap pre-roll rather than silently shedding an hour of
--   it. Nothing approved needs a larger ceiling, so nothing larger is granted.
--
-- WHY THE OLD CEILING WAS NOT A PROVIDER CONSTRAINT.
--   Migration 056 set the ceiling to the provider's documented 31-day `/trips`
--   lookup limit. That limit binds a single *request*; `_build_trip_fetch_chunks`
--   already caps every request at `chunk_days` (default 2, cap 5), so the
--   derived window never reaches the provider as one lookup. The 31-day ceiling
--   on `R` therefore constrained the reconciliation horizon for no provider-side
--   reason. This migration corrects that conflation; it does not weaken any
--   provider-facing limit, and `iter_31d_windows` is untouched.
--
-- EFFECT ON THE CADENCES ALREADY RUNNING.
--   `R` floors `E_start`; it never widens a window on its own. For DAILY
--   (`L = 3`/`L = 7`) and WEEKLY (`L = 16`) the candidate start is nowhere near
--   `E_end - R` in normal operation, so their windows are byte-identical before
--   and after. The only behavioural difference is in catch-up after a long
--   outage, where a window may now reach back 32 d + 1 h instead of 31 d before
--   being floored. That is the intended direction of this milestone.
--
-- ORDERING (LOAD-BEARING).
--   `validate_trips_stabilization_config` rejects `R` above the CODE ceiling in
--   `jobs/trips_stabilization_config.py`, and the dispatcher calls it through
--   `evaluate_coverage_gate` on every compatibility fire. Applying the UPDATE
--   below while a release carrying the OLD code ceiling is active would make
--   every subsequent dispatcher tick raise ValueError for these clients.
--   THIS MIGRATION MUST BE APPLIED ONLY AFTER a release containing
--   `TRIPS_MAX_RECOVERY_SPAN_SECONDS_MAX = 2_768_400` is the active release.
--   The constraint change alone is order-independent; the UPDATE is not.
--
-- WHAT DELIBERATELY DOES NOT CHANGE.
--   * `TRIPS_MAX_RECOVERY_SPAN_SECONDS_DEFAULT` and the column DEFAULT stay at
--     2 678 400. A new client inherits the old, narrower span until someone
--     decides it needs a monthly cadence.
--   * `trips_stabilization_delay_seconds` (D) and `trips_overlap_seconds` (O)
--     for every client.
--   * DELTA00001, FOXTROT00001 and ECHO00001 — no monthly cadence is approved for
--     them, so they keep `R = 2 678 400`.
--   * Every `client_dataset_schedule` row. Registering and enabling the monthly
--     cadence belongs to `ops/manage_telematics_reconciliation_schedule.py`,
--     which is the only sanctioned surface for it.

ALTER TABLE workflow_a_control.client_account
  DROP CONSTRAINT IF EXISTS ck_client_account_trips_max_recovery_span_seconds;

ALTER TABLE workflow_a_control.client_account
  ADD CONSTRAINT ck_client_account_trips_max_recovery_span_seconds
  CHECK (
    trips_max_recovery_span_seconds > 0
    AND trips_max_recovery_span_seconds <= 2768400
  );

UPDATE workflow_a_control.client_account
   SET trips_max_recovery_span_seconds = 2768400
 WHERE client_code IN ('ALPHA00001', 'BRAVO00016')
   AND trips_max_recovery_span_seconds < 2768400;

DO $$
DECLARE
  adopted INTEGER;
BEGIN
  SELECT count(*) INTO adopted
    FROM workflow_a_control.client_account
   WHERE client_code IN ('ALPHA00001', 'BRAVO00016')
     AND trips_max_recovery_span_seconds = 2768400;
  IF adopted <> 2 THEN
    RAISE EXCEPTION
      'migration 072: expected 2 clients at R = 2768400, found %', adopted;
  END IF;
END
$$;
