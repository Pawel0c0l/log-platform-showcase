# Telematics `/trips` compatibility mode and stabilized coverage — implementation delivery plan

**Status: plan only. Nothing in this document is implemented, deployed, enabled, bootstrapped,
recovered or executed.**

This document converts two accepted design documents into an ordered, commit-by-commit delivery
plan. It creates no runtime code, no migration, no configuration change, no provider request and no
operational action. Every commit below is a *proposal for a future, separately reviewed task*.

> **Delivery-boundary clarification after C4 implementation review.** C4 owns only pure effective-
> window arithmetic and its pure connectivity result. C5 adds the coverage-gate types and evaluation
> together with dispatcher integration. This corrects delivery sequencing only; it changes no
> accepted formula, coverage-state semantic, error contract, migration, rollout gate or architecture.

> **Coverage-write ownership clarification after C5 implementation review.** The first C5
> implementation performed a durable `READY → GAP_DETECTED` write inside the dispatcher, which
> contradicted this plan and routed the first coverage writer past **G-COV** (§9). The boundary is
> restated here normatively, and the correction is `fix: keep Telematics C5 coverage gate read-only`:
>
> - **C5 is strictly read-only against `client_dataset_coverage`.** It performs exactly one
>   statement against that table — the gate `SELECT` of §7.1 step 2. “Exactly one statement” fixes
>   the statement count and read-only character, not a frozen ten-column projection. C6 must widen
>   that same `SELECT` to twelve columns by adding `covered_through_source` and
>   `last_gap_detected_ts`; it must not add a second pre-claim read. No `INSERT`, no `UPDATE`, no
>   `DELETE`, no `bootstrap_status` transition, no coverage `updated_at`, no row lock and no
>   transaction coupling a history finalization to a coverage mutation.
> - **C5 may emit `requires_gap_persistence`** on its pure `CoverageGateResult` as an explicit,
>   advisory signal that a durable transition is owed. It must not act on it; the dispatcher logs it
>   alongside `coverage_mutation_performed: false`.
> - **C6, under G-COV, owns every durable coverage mutation** through two separate compatibility-
>   only finalizers. `_finalize_compat_gap` owns the newly disconnected `READY` rejection transaction
>   after claim and before launch. `_finalize_compat_success` owns successful advancement or validated
>   no-op after `rc == 0`. Existing strict/non-trips `_finalize_run` remains unchanged and executes no
>   coverage SQL. Exact CAS, crash and test requirements are in `docs/15_…`; §6.5 and §7.1 below use
>   the same placement.
> - **`GAP_DETECTED` is the one narrow exception to the generic non-`READY` taxonomy only after
>   foundational validation.** Identity is validated first. Then `READY` and `GAP_DETECTED` share
>   the same pure interval/evidence/seed-metadata validator. A structurally valid recorded gap
>   re-emits `TRIPS_COVERAGE_GAP_DETECTED` on every later due fire; a malformed row whose literal
>   status says `GAP_DETECTED` returns `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. The status string alone
>   is not evidence. Neither result is allowed, falls back to strict or is mutated by C5.
>   `UNINITIALIZED`, `RESEED_REQUIRED`, unknown and `NULL` statuses remain bootstrap-required.
> - **No C5 persistence-conflict code exists.** `TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT`
>   is unreachable while C5 has no writer. C5's taxonomy is exactly the two codes above; C6 restores
>   the persistence-conflict code only for its own failed gap CAS, as specified by `docs/15_…`.
>
> This changes no formula, no bootstrap requirement, no migration, no C7/D5 status and no rollout
> gate. It restores the C5/C6 split this plan already specified.

> **Revision note (bootstrap tooling implemented).** C10 has landed, and a paired writer commit
> **C10-W** was added to close the gap this plan previously left open: C10 specified only a
> read-only inventory, while §13.6 step 5 ("coverage state inserted with `bootstrap_status =
> 'READY'`") had no named execution surface at all. The two surfaces are now:
>
> - `ops/audit_telematics_coverage_bootstrap.py` — **C10**, permanently read-only. It has no execute
>   switch, opens the platform connection read-only, and reports facts without recommending `A`,
>   `W` or a `READY` verdict.
> - `ops/bootstrap_telematics_trips_coverage.py` — **C10-W**, dry-run by default. It is the repository's
>   only authorized coverage `INSERT`, gated behind `--execute` plus `--confirm-client-code`, and it
>   consumes a hash-verified audit bundle together with **explicit operator-approved** `A` and `W`.
>   It never infers a bound, never moves one, and refuses any selection intersecting an inventoried
>   unresolved interval.
>
> Both tools are implemented and tested. On `2026-08-03` the writer was executed in production
> exactly once, for `BRAVO00016` / `trips_sync`, so the coverage table now holds **exactly one**
> `READY` row; later that day `BRAVO00016` became the **sole** `data_invariants_v1` canary while the
> other four clients stayed `strict_meta`. Fleet-wide compatibility remains forbidden and the first
> compatibility fire has not happened. **C11 recovery tooling is now implemented (§3/C11) but has
> never been executed in production**, and migration `058` has not been applied to production. This
> note changes no formula,
> no coverage invariant, no C6 mutation contract, no migration and no C7/D5 status. The operator
> `INSERT` prohibition of §5 below is narrowed, not lifted: ad hoc production seed SQL stays
> forbidden, and the reviewed writer is now the only permitted way to create the row.

**Path note.** The requested path `docs/14_telematics_trips_compatibility_implementation_plan.md` is
used unchanged. Ordinals `00`–`13` are occupied (`01_` and `10_` twice: `01_architecture.md` /
`01_architecture_overview.md`, `10_platform_architecture.md` / `10_scheduler_design.md`), and `14_`
is the next free ordinal. This matches how `docs/12_…` and `docs/13_…` were placed. `REPO_MAP.md`'s
`docs/` table does not enumerate `10_`, `12_` or `13_` either, so adding `14_` without editing
`REPO_MAP.md` is consistent with the established practice for this document family. This plan is the
only file changed by the task that produced it.

## Documents of record

| Role | Document |
|---|---|
| Compatibility-mode design | `docs/12_telematics_trips_pagination_compatibility.md` |
| Stabilization / coverage domain invariants (D1) | `docs/13_telematics_trips_stabilization_windows.md` |
| Accepted advisory-`total` policy (D5) and the C7/C8 boundary | `docs/16_telematics_d5_total_policy_decision.md` |
| Commit ownership and delivery sequence | this document |
| Exact C6 transaction, CAS, crash and test contract | `docs/15_telematics_coverage_mutation_contract.md` |
| Job catalog | `docs/05_jobs.md` |
| Operations runbooks | `docs/07_operations.md` §5.4, §5.4.1, §5.5, §5.6 |
| Security and redaction | `docs/06_security.md` |
| Recovery boundaries | `docs/09_disaster_recovery.md` |
| Repository rules | `AGENTS.md` §6, `CONVENTIONS.md` §§2, 4, 6, 7, 8, 9, 10, 11, 12, 13 |

## Code of record (all read read-only for this plan)

`jobs/api/telematics/provider_client.py` (`_fetch_paginated` L560–734, `fetch_trips` L736–758,
`_parse_pagination_meta` L506, `TelematicsFleetProviderClient.__init__` L287, `iter_31d_windows` L37),
`jobs/api/telematics/provider_safety.py` (`SafetyLimits`, `ProviderRunBudget`,
`TelematicsProviderSafetyError`), `jobs/api/telematics/control_plane.py` (`ClientAccountConfig` L36,
`load_client_account_config` L57, `DatasetSchedule` L115), `jobs/api/telematics/dispatcher.py`
(`ScheduleRow` L197, `evaluate_schedule` L296, `_load_enabled_schedules` L445, `_claim_fire` L504,
`_finalize_run` L537, `_build_job_params` L599, `prepare_run` L722, `run_prepared` L827),
`jobs/api/telematics/sync_trips_and_speeding.py` (`_fetch_trips_in_chunks` L1369, provider client
construction L3460, single client-business commit ≈L4883), `db/migrations/018_*`, `042_*`, `054_*`,
`db/migrations/012_*`, `014_*`, `ops/db_migrate.sh`, `ops/tests_manual/test_workflow_a_dispatcher.py`,
`ops/tests_manual/test_workflow_a_trip_chunking.py`,
`ops/tests_manual/test_telematics_trips_pagination_diagnostic.py`.

## Verified baseline for this plan

| Item | Expected | Observed | Result |
|---|---|---|---|
| Branch | `main` | `main` | match |
| HEAD | `aa89d17ee377b83770f5d750daff2392b40e1957` | same | match |
| `origin/main` | same commit | same commit | match |
| Worktree | clean | clean (`git status --porcelain` empty) | match |
| Environment | `production` | `LOG_PLATFORM_TARGET_ENVIRONMENT=production` (`/etc/log-platform/environment-identity.env`) | match |
| Platform UUID | `52517750-7438-4558-8490-2736ae4cc629` | same (`LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID`) | match |
| Migration ceiling | `054_environment_identity_resume_contract.sql` | highest file in `db/migrations/` | match |
| Installed wrapper SHA-256 | `ae67a95f3c517a7d306a1e5fea0d4af4ca4dd7ce2f8feac86d011bd03f1f7bfa` | `/usr/local/bin/log-job-runner.sh` | match |

Next free platform migration ordinals: **055, 056, 057**. `db/client_business/` is at `045` and is
**not touched** by this plan.

---

## 1. Scope boundary

### 1.1 Four workstreams, never combined

| # | Workstream | Contains | Produces | Never contains |
|---|---|---|---|---|
| **W1** | Compatibility runtime implementation | Commits 1–9. Schema, configuration, pure window derivation, bootstrap gate, coverage advancement, pagination state machine, sync integration, telemetry, documentation that accompanies behavior | Merged code with every client still `strict_meta` | Any client enablement, any bootstrap row, any provider request beyond fake sessions |
| **W2** | Disabled deployment and verification | Applying migrations 055–057 to production, deploying the runtime, observing scheduled cycles for **semantic strict-mode equivalence** (§10.3) | Deployment evidence bundle | Any mode flip, any coverage row, any recovery |
| **W3** | Per-client bootstrap and enablement | Read-only inventory (Commit 10 tooling), interval selection, reconciliation, evidence bundle, coverage row insert, mode flip, first compat run observation | `READY` coverage row + one enabled client | Any historical backfill, any change to other clients |
| **W4** | Historical recovery and backfill | Commit 11 tooling, missing-fire recovery, failed-window recovery, aggregate recalculation, Eco Driving snapshot review | Recovered historical windows and new run evidence | Any schema change, any mode flip, any mutation of terminal history rows |

**Normative separation rule.** These four workstreams **must not be combined into one commit, one
deployment or one recovery operation.** Specifically:

- No commit may both change schema and enable a client.
- No deployment may both install new runtime and seed a coverage row.
- No recovery operation may flip a mode, change a schedule, or advance coverage
  for an interval it did not itself fetch and commit. A reviewed C11 recovery
  **may** advance `W` — but only to the end of its own successfully executed
  window, only through the shared expected-old-`W` compare-and-swap, and only
  with `covered_through_source = 'manual_recovery'`.
  After C11, **exactly two reviewed surfaces may advance `W`**: scheduled
  compatibility success finalization and C11 manual-recovery success
  finalization.
- No bootstrap operation may execute a backfill; it may only cite one that a separately authorized
  W4 operation already produced, or exclude the interval with a later `coverage_start_ts`.

### 1.2 Separated: the ALPHA Workflow B failure

The ALPHA00001 **Workflow B** business failure is a **different workflow, a different data path and a
different incident**. It shares only the client code. It is:

- not caused by, not fixed by and not affected by anything in W1–W4;
- explicitly out of scope for every commit in this plan (§14 non-goal N6);
- one of the reasons ALPHA00001 is **not** the first compatibility client (§11.3).

No commit, deployment, bootstrap or recovery step in this plan may bundle an ALPHA Workflow B change.
That failure needs its own ticket, its own diagnosis and its own plan.

---

## 2. Dependency graph

```
              [D5 decision: ACCEPTED docs/16]  [config-vs-schedule coupling check]
                          |                            |
  C1 ─── C2 ─── C3        |                            |
   │      │      │        |                            |
   │      │      └────────┼────────────┐               |
   │      └───────────────┼──────┐     |               |
   └──────────────────────┼───┐  |     |               |
                          |   v  v     v               v
  C4 (pure) ──────────────┼──> C5 (dispatcher gate + effective window) <──┘
                          |         │
                          |         v
                          |        C6 (coverage mutation finalizers)
                          v         │
                        C7 (state machine + essential telemetry)
                          │         │
                          v         │
                        C8 (sync integration)
                          │         │
                          └────┬────┘
                               v
                              C9 (cross-cutting telemetry + privacy suite)
                               │
                               v
                     ┌─── DEPLOY DISABLED (W2) ───┐
                     │                            │
                     v                            v
                   C10 (bootstrap audit)        C11 (manual recovery, unexecuted)
                     │                            │
                     v                            │
              BOOTSTRAP EVIDENCE (W3)             │
                     │                            │
                     v                            │
              FIRST CLIENT ENABLED                │
                     │                            │
                     v                            │
        FIRST SCHEDULED COMPAT RUN OBSERVED ──────┘
                     │                     (authorizes W4 execution)
                     v
                   C12 (consolidated runbook)  ──> WIDER ROLLOUT
```

### 2.1 Classification of every commit

| Commit | Implementable immediately? | Requires unresolved provider evidence? | Requires migration? | Requires disabled deployment? | Requires bootstrap evidence? | Requires explicit production authorization? |
|---|---|---|---|---|---|---|
| C1 configuration contract | **Yes** | No | Writes `055` | No | No | No (merge only) |
| C2 stabilization configuration | **Yes** | No | Writes `056` | No | No | No |
| C3 coverage state schema | **Yes** | No | Writes `057` | No | No | No |
| C4 pure window derivation | **Yes** | No | No (consumes none) | No | No | No |
| C5 dispatcher gate + effective window | After C1–C4 | No | Reads `055`,`056`,`057` | No | No | No |
| C6 coverage mutation finalizers | After C5 | No | Reads/writes `057` | No | No | No |
| C7 pagination state machine **+ minimum mode propagation** | **Yes** — D5 is resolved (Option B, `docs/16_…`) | No for implementation; `docs/12_…` §0.3 U3–U8 gate *enablement*, not merge | No | No | No | No |
| C8 broader integration and rollout work | After C7 | Same as C7 | No | No | No | No |
| C9 telemetry + privacy suite | After C6 and C8 | No | No | No | No | No |
| C10 bootstrap audit tooling | After C3 | No | Reads `057` | **Yes** (deployed after W2 starts) | No | Read-only production DB access |
| C10-W bootstrap writer | After C10 | No for implementation | Writes one row into `057` | **Yes** | No to merge; **yes to execute** | **Yes, per client bootstrap** |
| C11 recovery tooling | After C10 | No for implementation | No | **Yes** | No to merge; **yes to execute** | **Yes, per execution** |
| C12 consolidated runbook | After C11 | No | No | Yes (documents observed behavior) | Yes (documents the real procedure) | No |

### 2.2 Blocking vs non-blocking dependencies

**Blocking (a commit cannot be written or cannot be correct without them):**

- C5 ← C1, C2, C3, C4 (needs the columns, the loader fields and the pure helper).
- C6 ← C5 (advancement is meaningless without a validated effective window and a passing gate).
- C7 ← **D5 — satisfied.** The treatment of an absent `meta.total` is part of the failure taxonomy the
  state machine implements, so C7 required a separate documented architecture decision first. That
  decision exists and is accepted: **`docs/16_telematics_d5_total_policy_decision.md` — Option B, absent
  `total` permitted under data invariants, ACCEPTED 2026-08-03.** C7 implementation and C7 review are
  therefore authorized and must implement `docs/16_…` §5 verbatim; they must not re-open, re-litigate
  or re-decide D5, and must not create `PAGINATION_COMPAT_TOTAL_ABSENT`. The earlier non-binding
  preference for Option A recorded in this plan is **superseded history**, not an active rule.
- C8 ← C7 (broader integration builds on the delivered state machine). **C8 is not a prerequisite for
  the minimum mode propagation** needed to execute C7: per `docs/16_…` §7, C7 itself owns the
  `sync_trips_and_speeding.py` propagation edit that makes the state machine reachable through the
  existing dispatcher and C11 runner paths.
- C10 ← C3 (inventory reads the coverage table shape).
- C11 execution ← the §3/C11 gate ordering (reviewed tooling + applied migration `058` + reviewed
  dry-run + per-window authorization). It is deliberately **not** chained to a prior successful
  scheduled compatibility run, because for a leading-edge canary interval that chaining is circular.
- W3 ← W2 complete (§10 stage 3 evidence).

**Non-blocking (ordering preference only):**

- C2/C3 ← C1: independent schema objects; sequenced only to keep migration ordinals monotone and to
  keep each review small.
- C9 ← C6/C8: the cross-cutting telemetry is additive; the *essential* telemetry ships inside C7
  (§ decision 5).
- C11 ← C10: recovery tooling could be written first, but writing the read-only inventory first
  produces the vocabulary (segments, missing fires, uncovered intervals) that recovery consumes.
- C12 ← everything: consolidation, not first documentation.

**Explicitly not a blocker for merging W1:** U3–U8 of `docs/12_…` §0.3. They gate **enablement**
(§10 stage 6, §11), never merge. Merging code that is inert for every client changes no production
behavior; refusing to merge it would keep the safety machinery unbuilt while the incident continues.

### 2.3 The recovery-ordering conflict, resolved

`docs/12_…` §14.1 step 4 requires a **proven current scheduled run before any historical work**.
`docs/13_…` §13.6 requires **in-interval gaps recovered before seeding `READY`**. Taken together
they appear circular for a first client whose candidate interval contains the 2026-07-30 /
2026-07-31 hole.

**Resolution (decision P7, §16):** the first client's bootstrap selects a `coverage_start_ts` **after**
the known hole (`docs/13_…` §13.3, §13.7 — narrowing the claim is always permitted and always safe).
The interval therefore contains no unrecovered gap, `READY` is honest, and no recovery is required
before enablement. Recovery of 2026-07-30 / 2026-07-31 / 2026-08-01 then happens in W4, **after** the
first scheduled compatibility run has proven the path, exactly as `docs/12_…` §14.1 step 4 demands.
Neither document is violated; the circularity was an artifact of assuming the interval must include
the hole.

---

## 3. Commit inventory

Twelve commits. §3.13 records where and why this differs from the candidate split in the task brief.

Common to every commit below unless stated otherwise:

- **Files explicitly forbidden in all of C1–C11:** `db/migrations/0[0-4]*.sql` and any other applied
  migration (`AGENTS.md` §6, `CONVENTIONS.md` §12); `db/client_business/**`; `.env`,
  `/etc/log-platform/**`, `ops/systemd/**`; `api/main.py`; `ops/runner.py`; `api/client.py`;
  `jobs/mail/**`, `jobs/reports/**` (Workflow B); anything under `backups/`, `snapshots/`, `tmp/`.
- **Deployment requirement:** none of C1–C9 may be deployed to production individually; W2 deploys
  C1–C9 as one unit (§10 stage 1–2).
- **Universal blocking gate:** the commit must leave `trips_pagination_mode = 'strict_meta'` for
  every client and must not create, update or delete any `client_dataset_coverage` row.

---

### C1 — configuration contract

- **Sequence:** 1
- **Commit message:** `feat: add Telematics pagination mode configuration`
- **Objective:** introduce the per-client pagination-mode selector as inert, validated, fail-closed
  configuration. No runtime consumer.
- **Expected files:**
  - `db/migrations/055_workflow_a_trips_pagination_mode.sql` (new)
  - `jobs/api/telematics/control_plane.py` (frozen `ClientAccountConfig` + `SELECT` + validation)
  - `jobs/trips_pagination_mode.py` (new; mirrors `jobs/trip_metrics_population_source.py` — the
    allowlist, the default constant and `normalize_trips_pagination_mode(...)`)
  - `ops/tests_manual/test_telematics_trips_pagination_mode_config.py` (new)
  - `docs/05_jobs.md` (one paragraph under `sync_trips_and_speeding`: the column exists, defaults to
    `strict_meta`, is not yet consumed)
- **Files explicitly forbidden:** `provider_client.py`, `provider_safety.py`,
  `sync_trips_and_speeding.py`, `dispatcher.py`, migrations `056`/`057`.
- **Schema impact:** additive `workflow_a_control.client_account.trips_pagination_mode TEXT NOT NULL
  DEFAULT 'strict_meta'` with `CHECK (trips_pagination_mode IN
  ('strict_meta','data_invariants_v1'))`. Pattern: `018_workflow_a_schedule_event_enrichment_mode.sql`
  (add column → backfill → `SET DEFAULT` → `SET NOT NULL` → drop-if-exists + add `CHECK`).
- **Runtime impact:** **none.** The loader gains a field nothing reads. A `NULL`, absent, unknown or
  unreadable value resolves to `strict_meta`; a value that passes the DB `CHECK` but fails the Python
  allowlist raises (drift is fail-closed, `docs/12_…` §3.3).
- **Required tests:** config-loading suite (§8 S2) — default resolution, `NULL` → `strict_meta`,
  unknown string → raise, frozen-dataclass immutability; schema suite (§8 S1) on temporary
  PostgreSQL — `CHECK` rejects `'loose'`, `NOT NULL` holds, re-applying the migration is idempotent.
- **Documentation update:** `docs/05_jobs.md` as above. `docs/02_infrastructure.md` is **not**
  touched — this is control-plane configuration, not ENV.
- **Deployment requirement:** none standalone; part of W2.
- **Rollback:** revert the code commit. **No migration rollback** — the column is additive and inert
  at its default (`CONVENTIONS.md` §12). An old runtime against the new schema simply never selects
  the column (§4.7).
- **Prerequisites:** none.
- **Blocking gates:** independent migration review (§9).
- **Cursor task classification:** `TELEMATICS_PAGINATION_MODE_CONFIG_COMMIT_READY`.

---

### C2 — stabilization configuration

- **Sequence:** 2
- **Commit message:** `feat: add Telematics stabilization configuration`
- **Objective:** add the three stabilization numerics as inert, bounded, validated client
  configuration.
- **Expected files:**
  - `db/migrations/056_workflow_a_trips_stabilization_config.sql` (new)
  - `jobs/api/telematics/control_plane.py` (three frozen fields, `SELECT`, range validation)
  - `ops/tests_manual/test_telematics_trips_stabilization_config.py` (new)
  - `docs/05_jobs.md` (configuration table row)
- **Files explicitly forbidden:** `dispatcher.py`, `provider_client.py`,
  `sync_trips_and_speeding.py`, migration `057`.
- **Schema impact:** additive on `client_account`:
  `trips_stabilization_delay_seconds INTEGER NOT NULL DEFAULT 10800 CHECK (>= 0)`,
  `trips_overlap_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (>= 0)`,
  `trips_max_recovery_span_seconds INTEGER NOT NULL DEFAULT 2678400 CHECK (> 0 AND <= 2678400)`.
  Same additive pattern as C1.
- **Runtime impact:** **none.** Values are loaded and validated; nothing consumes them.
- **Required tests:** schema constraint suite (temporary PostgreSQL): each bound rejected at its
  edge, defaults applied to pre-existing rows; config suite (pure): parsing, type, immutability,
  and the **schedule-coupling validator** of §5.5 (`Δ_max ≤ L + O + 1 s` for every enabled
  `trips_sync` schedule of the client) raising when violated.
- **Documentation update:** `docs/05_jobs.md` configuration table.
- **Deployment requirement:** none standalone.
- **Rollback:** revert the code commit; migration stays (additive, inert at defaults).
- **Prerequisites:** C1 (ordinal ordering and one shared loader edit surface).
- **Blocking gates:** independent migration review.
- **Cursor task classification:** `TELEMATICS_STABILIZATION_CONFIG_COMMIT_READY`.

---

### C3 — coverage state schema

- **Sequence:** 3
- **Commit message:** `feat: add Telematics stabilized coverage state`
- **Objective:** create the bounded coverage-interval table and the schedule-history evidence
  columns, with the database-level guarantees that make the §5.2.1 fail-closed rule enforceable in
  SQL and not only in Python.
- **Expected files:**
  - `db/migrations/057_workflow_a_trips_coverage_state.sql` (new)
  - `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` (new)
  - `docs/05_jobs.md` (schema note under the dispatcher section)
- **Files explicitly forbidden:** every `jobs/**` file (this commit adds **no** Python runtime),
  migrations `055`/`056`.
- **Schema impact:** creates `workflow_a_control.client_dataset_coverage` exactly as §6, plus five
  additive nullable columns on `client_schedule_run_history`
  (`nominal_window_start_ts`, `nominal_window_end_ts`, `stabilization_delay_seconds`,
  `overlap_seconds`, `trips_pagination_mode`). `UNIQUE (schedule_id, scheduled_fire_ts)` is
  untouched; no existing column changes type, nullability or default.
- **Runtime impact:** **none.** No Python reads or writes the new objects yet. Every new coverage row
  would default to `UNINITIALIZED`, which §5.2.1 refuses — the safe direction.
- **Required tests (temporary PostgreSQL, §8 S1):** the FK cascade from
  `client_dataset_schedule(schedule_id)`; `PRIMARY KEY (schedule_id)` uniqueness; the bounds `CHECK`
  (`coverage_start_ts <= covered_through_ts`); the `READY` `CHECK` (a `READY` row without both
  bounds, without `bootstrap_evidence_ref`, without `seeded_at` or without `seeded_by` is
  **rejected**); the `bootstrap_status` vocabulary `CHECK`; the `covered_through_source` vocabulary
  `CHECK`; deleting a schedule cascades the coverage row; `enabled = false` does **not**; the
  history evidence columns are nullable and default `NULL` on existing rows.
- **Documentation update:** `docs/05_jobs.md` schema note.
- **Deployment requirement:** none standalone.
- **Rollback:** revert the code commit. **No `DROP TABLE`, no `DROP COLUMN`** — a destructive
  rollback is prohibited (§4.8). An unused table with zero rows is inert.
- **Prerequisites:** C2.
- **Blocking gates:** **independent migration review in a fresh session** (§9) — this is the
  highest-risk schema object in the plan. Independence is process-based (§9); a different reviewer
  model is optional and is not a blocking condition.
- **Cursor task classification:** `TELEMATICS_COVERAGE_STATE_SCHEMA_COMMIT_READY`.

---

### C4 — pure effective-window derivation

- **Sequence:** 4
- **Commit message:** `feat: derive stabilized Telematics schedule windows`
- **Objective:** a pure, side-effect-free helper implementing `docs/13_…` §16.1 effective-window
  arithmetic, input validation and the pure connectivity result, unit-testable with no database and
  no network. It performs no readiness or operational gate evaluation.
- **Expected files:**
  - `jobs/api/telematics/coverage_windows.py` (new module: `EffectiveWindow`,
    `derive_effective_window(...)`)
  - `ops/tests_manual/test_telematics_trips_stabilization_windows.py` (new; pure arithmetic,
    UTC/DST, recovery-cap, closed-interval, connectivity, monotonicity, determinism and immutability
    coverage derived from `docs/13_…` §18; no readiness or state-transition tests)
  - `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` (shared C3 suite, adapted
    narrowly so exactly the four pure arithmetic field names are permitted in
    `coverage_windows.py`, while coverage-table/bootstrap vocabulary stays forbidden and runtime
    imports of the helper remain prohibited)
  - `docs/05_jobs.md` (documents the pure helper and its runtime-inert state)
- **Files explicitly forbidden:** `dispatcher.py`, `control_plane.py`, any migration,
  `provider_client.py`, `sync_trips_and_speeding.py`.
- **Schema impact:** none.
- **Runtime impact:** **none** — the module is imported by nothing until C5.
- **Required tests:** pure unit suite only. Exhaustive UTC/DST/closed-interval coverage: a full-year
  2026 sweep for each of the four production schedule shapes asserting `E_start(n+1) ≤ E_end(n) + 1 s`
  on every fire; both Warsaw transitions with the exact instants of `docs/13_…` §11; `D`/`O` applied
  as absolute UTC seconds **after** local→UTC conversion (a deliberately wrong local-wall-clock
  implementation must fail the suite); the `R` cap; the no-inversion invariant
  `E_start ≤ E_end`, including explicit `E_start == E_end` coverage for the valid degenerate
  closed interval `[t, t]` and rejection or arithmetic impossibility of `E_start > E_end`; the
  `BRAVO00016` autumn regression guard (§11.3 of `docs/13_…`). C4 imposes no blanket strict-positive-
  duration requirement and makes no operational launch decision for a degenerate window.
- **Documentation update:** `docs/05_jobs.md` documents the pure helper, the fact that the dispatcher
  does not import it and the formulas' lack of runtime effect. It provides no operator procedure or
  enablement instruction; C12 remains later runbook consolidation, not the first documentation of
  C4 behavior. The module docstring cites `docs/13_…` §16.1 as its specification.
- **Deployment requirement:** none.
- **Rollback:** revert; nothing imports it.
- **Prerequisites:** none technically; sequenced after C3 for delivery order only. C4 has no
  `CoverageState` type and consumes no schema.
- **Blocking gates:** none beyond normal review.
- **Cursor task classification:** `TELEMATICS_WINDOW_DERIVATION_COMMIT_READY`.

---

### C5 — dispatcher integration and coverage bootstrap gate

- **Sequence:** 5
- **Commit message:** `feat: enforce Telematics coverage bootstrap gate`
- **Objective:** add `CoverageState`, `CoverageGateResult` and
  `evaluate_coverage_gate(...)`, then wire the C4 arithmetic into the dispatcher **together with**
  that fail-closed gate, following §7.1 so that no compatibility window is claimed for launch
  without a passing gate, and every gate failure still leaves durable claim+finalize evidence.
- **Expected files:**
  - `jobs/api/telematics/coverage_windows.py` (extended with `CoverageState`,
    `CoverageGateResult` and pure `evaluate_coverage_gate(...)`; no I/O)
  - `jobs/api/telematics/dispatcher.py` (`ScheduleRow` gains the four client-scoped fields;
    `_load_enabled_schedules` `SELECT` extended — the join already reaches `client_account`;
    coverage row loaded under the existing advisory lock when mode is compat; gate+derive before
    claim; `PreparedDispatcherRun` gains `gate_failure`; `_claim_fire` writes the five evidence
    columns with nominal-or-effective windows per §7.1; `_build_job_params` adds
    `scheduled_fire_ts` for `trips_sync` plus the nominal window and numerics; `run_prepared`
    enforces gate failure as `FAILED` without launching a subprocess)
  - `ops/tests_manual/test_workflow_a_dispatcher.py` (**extended, not replaced** — existing
    `window_end == scheduled_fire_ts` assertions remain valid for `strict_meta`)
  - `ops/tests_manual/test_telematics_coverage_bootstrap_gate.py` (new)
  - `docs/05_jobs.md` (dispatcher section: nominal vs effective, new `trips_sync` params)
  - `docs/07_operations.md` §5.5 (`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` triage)
- **Files explicitly forbidden:** `provider_client.py`, `provider_safety.py`,
  `sync_trips_and_speeding.py`, any migration, `registry.py`.
- **Schema impact:** none (consumes 055/056/057).
- **Runtime impact:** **`strict_meta` must remain semantically equivalent** (§10.3). In strict mode
  the dispatcher must not read the coverage table at all, must pass `NULL` for every new evidence
  column, and must produce exactly today's `window_start_ts`/`window_end_ts` arithmetic. In
  `data_invariants_v1` — which no client has — the authoritative order of §7.1 applies: gate+derive
  are evaluated before the claim INSERT so the correct window is stored; gate enforcement occurs
  immediately after claim and before launch; on failure the fire is finalized `FAILED` with
  `error_summary` equal to the pure gate's abort classification. Missing/identity-invalid state,
  `UNINITIALIZED`, `RESEED_REQUIRED`, unknown/`NULL` status, malformed `READY` and malformed
  `GAP_DETECTED` use `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. Only a structurally valid recorded
  `GAP_DETECTED`, or a structurally valid disconnected `READY`, uses
  `TRIPS_COVERAGE_GAP_DETECTED`. C5 itself writes no coverage. With C6 absent, neither code mutates
  coverage; with C6 present, only the newly disconnected `READY` case receives the atomic pre-launch
  C6 gap transaction. Neither code launches a subprocess or reaches parameters, credentials or
  sockets.
- **Required tests:** `ops/tests_manual/test_telematics_coverage_bootstrap_gate.py` owns the pure C5
  taxonomy. It covers missing row; identity precedence; `UNINITIALIZED`, `RESEED_REQUIRED`, unknown
  and `NULL` status; and the shared foundational matrix for both `READY` and `GAP_DETECTED`: missing,
  non-datetime, naive, sub-second, reversed or future bounds, blank evidence, and missing/invalid seed
  metadata. A valid recorded gap must re-emit `TRIPS_COVERAGE_GAP_DETECTED`, preserve normalized
  `A/W`, expose no launch window and set `requires_gap_persistence=false`; every malformed recorded
  gap must return `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. The same suite owns fatal side-effect patches,
  bounded/redacted logs, suspected-bug fingerprinting, reporting-failure durability and disposable-
  PostgreSQL claim/finalize proof. Migration-057 constraints make reversed bounds impossible to
  persist and `TIMESTAMPTZ` adapters cannot return naive values, so those shapes remain explicit pure
  gate cases rather than reasons to weaken schema constraints. Strict-mode, claim evidence and
  `scheduled_fire_ts` regressions remain required.
- **Documentation update:** as listed — this is a behavior-changing commit and carries its own docs.
- **Deployment requirement:** none standalone; part of W2.
- **Rollback:** revert the commit. Schema stays. Because strict mode never touches coverage state,
  reverting cannot orphan data.
- **Prerequisites:** C1, C2, C3, C4.
- **Blocking gates:** independent review (dispatcher control flow + claim semantics).
- **Cursor task classification:** `TELEMATICS_COVERAGE_BOOTSTRAP_GATE_COMMIT_READY`.

---

### C6 — compatibility coverage finalization

- **Sequence:** 6
- **Commit message:** `feat: finalize Telematics compatibility coverage atomically`
- **Objective:** add two separate compatibility-only finalization surfaces while leaving existing
  strict/non-trips `_finalize_run` unchanged:
  - `_finalize_compat_gap` performs the newly disconnected `READY` post-claim/pre-launch atomic
    coverage `READY → GAP_DETECTED` plus history `RUNNING → FAILED` transaction;
  - `_finalize_compat_success` performs post-`rc == 0` atomic snapshot validation, conditional
    monotone `W` advancement, and history `RUNNING → SUCCESS`.
- **Detailed design:** `docs/15_telematics_coverage_mutation_contract.md` is the G-COV transaction,
  CAS, crash-reconciliation, taxonomy and test contract. `docs/13_…` §5.3 supplies the domain
  invariants; this plan supplies ownership and delivery order.
- **Expected files:**
  - `jobs/api/telematics/dispatcher.py` (the two compatibility-only finalizers; strict `_finalize_run`
    body and call surface unchanged; widen the existing single compatibility coverage `SELECT`,
    retain its immutable state through subprocess completion, and bind CAS from that retained object;
    shared low-level SQL utilities only when they cannot route strict execution through coverage SQL
    or blur transaction ownership)
  - `jobs/api/telematics/coverage_windows.py` only to add `covered_through_source` and
    `last_gap_detected_ts` as plain fields on immutable `CoverageState`; the pure gate may ignore
    them and the module remains I/O-free
  - `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` to deliberately re-scope,
    never remove, the static guard for the widened read and two named dispatcher finalizers while
    retaining fail-closed production-source discovery
  - `ops/tests_manual/test_telematics_coverage_bootstrap_gate.py` when immutable-state fixtures must
    change, proving the two added carrier fields do not affect the pure gate or window arithmetic
  - `ops/tests_manual/test_workflow_a_dispatcher.py` when retained-snapshot and strict-isolation
    integration requires extension
  - `ops/tests_manual/test_telematics_coverage_advancement.py`
  - `ops/tests_manual/test_telematics_coverage_advancement_postgres.py` using disposable PostgreSQL 16
  - `docs/07_operations.md` §5.5 only in the future implementation commit, when the new codes become
    reachable
- **Files explicitly forbidden:** `provider_client.py`, `sync_trips_and_speeding.py`, any migration.
- **Schema impact:** none. Migration 057 remains sufficient.
- **Runtime impact:**
  - C5 remains coverage-read-only when C6 is absent.
  - A newly disconnected valid `READY` fire is claimed, then `_finalize_compat_gap` runs before
    `_build_job_params`, subprocess construction, credential resolution, sockets and provider/client
    access. One UTC-aware whole-second mutation instant is bound to both gap timestamps. No
    subprocess runs.
  - Existing or malformed `GAP_DETECTED` and every bootstrap-required state use the C5 non-mutating
    rejection finalization; neither gap timestamp is refreshed.
  - An allowed fire launches. `rc != 0` uses unchanged failure finalization and leaves coverage
    unchanged. `rc == 0` invokes `_finalize_compat_success`; no compatibility `SUCCESS` can commit
    without complete claim-time coverage validation and the corresponding advancement/no-op decision.
  - Manual/backfill runs and strict/non-trips paths never execute coverage SQL.
- **Required transaction rules:** global row-lock order coverage row → history row; this is
  lock-order-only, and the history row must be locked and verified `RUNNING` before any coverage
  mutation; complete claim-time CAS with null-safe comparison for every nullable field; exact row
  counts; rollback on conflict or claim loss; no row lock across subprocess execution;
  uncertain `COMMIT` reconciled on a fresh connection before any new terminal write.
- **Required tests:** the full `docs/15_…` §11 matrix: movement/no-op, the retained 12-field
  carrier, null-safe and timezone-normalized CAS, source-only and prior-gap-timestamp races,
  reseed/recovery and claim-loss races, one-shot gap timestamps, strict isolation,
  client-commit/platform-finalization replay safety, deliberate static-guard rescope, commit
  uncertainty and impossible-pair reconciliation. No production database writes or provider
  requests.
- **Deployment requirement:** none standalone; part of W2 after G-COV approval.
- **Rollback:** revert C6 code. Any already-advanced `W` remains an honest record; do not delete or
  regress it.
- **Prerequisites:** C5.
- **Blocking gates:** fresh independent G-COV review (§9); independence is process-based and a
  different reviewer model is optional.
- **Cursor task classification:** `TELEMATICS_COVERAGE_ADVANCEMENT_COMMIT_READY`.

**C5 and C6 remain separate.** C5 computes and enforces the gate read-only. C6 extends only the
newly disconnected `READY` rejection branch and the allowed-success branch with atomic
coverage/history transactions. Reverting C6 leaves the read-only C5 gate protecting production.

---

### C7 — data-invariant pagination state machine and minimum mode propagation

- **Sequence:** 7
- **Commit message:** `feat: implement Telematics trips compatibility pagination`
- **Objective:** implement `docs/12_…` §4–§8 and `docs/16_…` §5–§6 in the provider client: the
  compatibility state machine, identity extraction, page-local and cross-page invariants, short-page
  termination, the new budgets, the accepted failure taxonomy, and the **essential** per-page evidence
  needed to prove those invariants — **plus the minimum runtime propagation that makes the state
  machine reachable** (`docs/16_…` §7.1).
- **Expected files:**
  - `jobs/api/telematics/provider_client.py` (new `_fetch_paginated_data_invariants_v1`;
    `fetch_trips` dispatches on a typed mode; `_fetch_paginated` body **unchanged**;
    `TelematicsFleetProviderClient.__init__` gains a typed `trips_pagination_mode` defaulting to
    `strict_meta`)
  - `jobs/api/telematics/provider_safety.py` (new bounded budgets: rows per sub-window, response bytes
    per response and per sub-window, elapsed per sub-window; new `TELEMATICS_PROVIDER_COMPAT_*` ENV
    with the `docs/12_…` §5.5 defaults; new taxonomy constants/helpers if required)
  - `jobs/api/telematics/sync_trips_and_speeding.py` (**propagation only** — resolve the frozen or
    parameter-supplied normalized mode and pass it to the provider client at construction ≈L3460)
  - `jobs/trips_pagination_mode.py` (only if the existing normalized API needs a narrow extension)
  - `ops/tests_manual/test_telematics_trips_pagination_compat.py` (new; `docs/12_…` §13 T1–T29, T35 and
    the `docs/16_…` §5.4 empty/short-page cases)
  - narrowly related updates to existing provider/pagination tests, plus focused sync-propagation
    coverage proving the frozen client mode reaches the `/trips` path
  - `docs/02_infrastructure.md` (the three new `TELEMATICS_PROVIDER_COMPAT_*` variables — C7 owns both
    the budgets and their ENV documentation, `docs/16_…` §7.4)
  - `docs/05_jobs.md` (C7 implementation status and the propagated parameter)
  - `docs/07_operations.md` §5.4 (new abort codes in the safety-limit table)
- **Files explicitly forbidden:** `dispatcher.py`, `control_plane.py`, `coverage_windows.py`,
  `coverage_finalization.py`, `ops/recover_telematics_trips_window.py`, any migration. **No client
  configuration is changed.** The dispatcher and C11 runner already emit `trips_pagination_mode` in
  job params, so no change is needed on their side.
- **Schema impact:** none.
- **Runtime impact:** **zero for every existing `strict_meta` caller.** `fetch_trips` defaults to
  `strict_meta`; `_fetch_paginated` is not edited; `fetch_vehicles_fleet`, `fetch_drivers_fleet`,
  `fetch_vehicle_events_*`, `fetch_fuel_*` and `fetch_notifications` are untouched (`docs/12_…` N7).
  New budgets may only *reduce* what is already permitted. The sync edit is propagation only: it must
  not change the write boundary (fetch-before-connect, one commit), the `ON CONFLICT` targets,
  `overwrite_existing` semantics, or any coverage state — **C7 remains a forbidden coverage writer**
  (`docs/15_…`, `docs/16_…` §7.1).
- **Required tests:** pagination generated sequences and the full compatibility failure taxonomy
  (§8 S7, S9) against a **fake provider session** — no network, no DB, no secrets, stdlib only,
  fixed seed. T35 is the load-bearing property: *for any generated page sequence the state machine
  either returns a complete duplicate-free row set or aborts — never a silent partial success.*
  Plus strict-mode regression (§8 S8): T1/T2 must remain semantically equivalent to today,
  including the same `PAGINATION_MISMATCH` safety classification on the current broken metadata.
  Plus mode-propagation regression (§8 S8a): a `data_invariants_v1` client reaches the compatibility
  path, an unknown/missing mode fails closed to strict, and non-`/trips` endpoints stay strict.
- **Documentation update:** as listed.
- **Rollback:** revert. The strict path is untouched and the propagation edit is inert for
  `strict_meta`, so reverting cannot regress production.
- **Deployment requirement:** none standalone; part of W2.
- **Prerequisites:** **D5 resolved — satisfied** by the accepted decision
  `docs/16_telematics_d5_total_policy_decision.md` (Option B, ACCEPTED 2026-08-03). C7 must implement
  that decision as written and must not re-decide it.
- **Blocking gates:** **satisfied.** The required independent review was **an independent review in
  a fresh session, reading the code before reading `docs/12_…`** (§9). The prior documentation review
  was partially a self-review and was explicitly **not** sufficient review for this commit; the
  delivered G-SM review was a separate, fresh, code-first review that applied `docs/16_…` §5 as the
  accepted contract rather than re-opening the `total` policy. That review is **APPROVED** (fresh
  independent `Opus 5` session, `2026-08-03` — `docs/07_operations.md` §5.5). Reviewer-model
  diversity is optional (§9), so same-model review is a valid approval and **no new review is
  required**.
- **Cursor task classification:** `TELEMATICS_COMPAT_STATE_MACHINE_COMMIT_READY`.
- **Implementation status (2026-08-03): IMPLEMENTED, DEPLOYED TO PRODUCTION AND G-SM APPROVED.** The delivered
  files are exactly the expected ones: `provider_client.py`
  (`_fetch_paginated_data_invariants_v1`, typed `trips_pagination_mode` on the constructor,
  `fetch_trips` mode dispatch, `_fetch_paginated` body untouched), `provider_safety.py`
  (`CompatibilitySafetyLimits` plus the three `TELEMATICS_PROVIDER_COMPAT_*` variables and the
  compatibility taxonomy constants), `sync_trips_and_speeding.py` (propagation only:
  `_trips_pagination_mode` and the constructor hand-off), the new
  `ops/tests_manual/test_telematics_trips_pagination_compat.py`, and the documentation listed above.
  `jobs/trips_pagination_mode.py` needed no extension. No forbidden file was touched: `dispatcher.py`,
  `control_plane.py`, `coverage_windows.py`, `coverage_finalization.py`,
  `ops/recover_telematics_trips_window.py` and every migration are unchanged, and no client
  configuration was changed. One classification was added beyond the docs/12 §8 list —
  `PAGINATION_COMPAT_CONFIG_INVALID`, required by the fail-closed budget-configuration rule — and is
  documented there. The window-eligibility guard, the pre-commit duplicate assertion and the write
  boundary remain untouched C8 work. **G-SM review, push and deployment are complete** — the commit
  is pushed, the runtime is deployed production-side from
  `22db25e9cbc7ada60bf62c9a691957653d0c6cdd`, and G-SM is APPROVED (`docs/07_operations.md` §5.5).

---

### C8 — broader sync integration and write-boundary hardening

- **Sequence:** 8
- **Commit message:** `feat: integrate Telematics compatibility pagination`
- **Objective:** the sync-side work that is **not** required to execute the first canary. Minimum mode
  propagation has moved to C7 (`docs/16_…` §7.1); C8 keeps write-boundary hardening, the pre-commit
  duplicate assertion, the eligibility guard, broader cross-job integration and rollout-support work.
- **Expected files:**
  - `jobs/api/telematics/sync_trips_and_speeding.py` (`docs/12_…` §6.4 eligibility guard
    retained as a *guard*, never a shifter; new params consumed **for logging only**; pre-commit
    assertion that the prepared upsert batch contains no duplicate `(client_id, provider_trip_id)`.
    The mode hand-off to `TelematicsFleetProviderClient(...)` ≈L3460 already landed in C7 and is not
    re-implemented here)
  - `ops/tests_manual/test_telematics_trips_compat_sync_integration.py` (new)
  - `docs/05_jobs.md` (`sync_trips_and_speeding` params and mode behavior)
- **Files explicitly forbidden:** `provider_client.py`, `provider_safety.py`, `dispatcher.py`, any
  migration, `backfill_trips_insert_only.py`, `aggregate_trip_fuel_daily.py`.
- **Schema impact:** none.
- **Runtime impact:** for `strict_meta` clients the only observable change is additional log context
  and the new pre-commit assertion — which, per `docs/12_…` §9.2, can only fire on a condition that
  today is silently collapsed by `ON CONFLICT`. **The job never shifts a window it is given**
  (`docs/13_…` §12): windows arrive already effective from the dispatcher, or literally from an
  operator.
- **Required tests:** complete fetch validation before any write (T30, T31); no streaming or
  incremental client writes — a mid-fetch abort in the last sub-window of a multi-chunk run leaves
  zero rows (sandbox client DB, §8 S10); pre-commit duplicate assertion (T32); ineligible-window
  abort with no fallback to `strict_meta` (T28, T29). End-to-end mode plumbing from a fake
  control-plane row to the provider client is a **C7** assertion (§8 S8a) and must remain green here.
- **Documentation update:** `docs/05_jobs.md`.
- **Deployment requirement:** none standalone.
- **Rollback:** revert.
- **Prerequisites:** C7.
- **Blocking gates:** independent review (write-boundary preservation).
- **Cursor task classification:** `TELEMATICS_COMPAT_SYNC_INTEGRATION_COMMIT_READY`.

**Is C8 actually separate from C7? — Yes, with a corrected boundary.** The two review questions stay
different: C7 asks *are the pagination invariants sound?*, C8 asks *did the write boundary survive?*
Merging them would put both analyses in front of one reviewer at once, which is exactly the
split-attention failure the small-commit rule exists to prevent, and they still roll back
independently.

What changed (`docs/16_…` §7) is **where the boundary sits inside the sync job.** The original split
forbade `sync_trips_and_speeding.py` in C7 while also making C8 depend on C7, which produced a C7 that
could be merged but never executed — nothing passed the client's frozen mode into the provider client,
so the state machine was unreachable and could not be exercised end to end for the `BRAVO00016`
canary. C7 therefore now owns the **propagation-only** edit (resolve the normalized mode, pass it at
provider-client construction). C8 retains everything that changes the write path or the job's broader
behavior: the pre-commit duplicate assertion, the eligibility guard, write-boundary hardening, wider
integration and rollout-support work. A C7 diff that touches the write boundary, the `ON CONFLICT`
targets or coverage is out of scope and must be rejected in review.

---

### C9 — cross-cutting telemetry and privacy evidence

- **Sequence:** 9
- **Commit message:** `feat: add Telematics pagination compatibility telemetry`
- **Objective:** the observability that spans C5/C6/C8 rather than living inside one of them, plus the
  privacy assertion suite.
- **Expected files:**
  - `jobs/api/telematics/dispatcher.py` (coverage-transition context keys: `coverage_start_ts`,
    `coverage_watermark_before/after`, `coverage_expanded_seconds`, `coverage_gap_detected`,
    `bootstrap_status`, nominal/effective window keys)
  - `jobs/api/telematics/sync_trips_and_speeding.py` (run-level compatibility summary record)
  - `jobs/api/telematics/provider_client.py` (sub-window summary record only — per-page evidence
    already shipped in C7)
  - `ops/tests_manual/test_telematics_compat_redaction.py` (new; T33, T34)
  - `docs/06_security.md` (compatibility-mode redaction and per-execution HMAC salt rules)
- **Files explicitly forbidden:** any migration; any change to control flow — this commit adds
  logging and assertions only.
- **Schema impact:** none.
- **Runtime impact:** additional `logs` rows with flat scalar keys (`CONVENTIONS.md` §8). No control
  flow depends on any of them.
- **Required tests:** privacy and redaction suite (§8 S12) capturing **every** log emitted across the
  full T1–T35 matrix and asserting that no raw `provider_trip_id`, registration, coordinate, address,
  geofence name, driver name, tag, restriction, raw payload, `Authorization` header, secret ref,
  secret value or DSN appears; the per-execution 32-byte salt never appears in any context and
  digests differ between two executions over identical data.
- **Documentation update:** `docs/06_security.md`.
- **Deployment requirement:** none standalone.
- **Rollback:** revert.
- **Prerequisites:** C6, C8.
- **Blocking gates:** none beyond normal review.
- **Cursor task classification:** `TELEMATICS_COMPAT_TELEMETRY_COMMIT_READY`.

**Does essential telemetry belong in C7 instead of being deferred? — Yes, and it is.** The per-page
record (`requested_page`, `requested_limit`, `returned_count`, `accumulated_count`, ordered and
unordered identity fingerprints, `unique_identity_count`, `overlap_count`, `meta_*` diagnostics,
budget headroom, `response_bytes`, `elapsed_seconds`, `termination_reason`), the HMAC salt lifecycle
and every abort's structured context ship **inside C7**. They are how the invariants are proven at
runtime; a state machine whose decisions cannot be reconstructed from `logs.context` is not
reviewable and must never reach production even disabled. C9 keeps only what genuinely spans
commits: coverage-transition keys (C5/C6 objects observed from the dispatcher), the run-level
summary (C8 object), and the cross-cutting redaction suite that can only be written once all
emitters exist.

---

### C10 — bootstrap inventory tooling

- **Sequence:** 10
- **Commit message:** `feat: add Telematics coverage bootstrap audit`
- **Objective:** a strictly read-only inventory implementing `docs/13_…` §13.2 items 1–10, producing
  a sanitized evidence bundle.
- **Expected files:**
  - `ops/audit_telematics_coverage_bootstrap.py` (new)
  - `ops/tests_manual/test_telematics_coverage_bootstrap_audit.py` (new)
  - `docs/07_operations.md` §5.5 (invocation, output contract)
  - `docs/09_disaster_recovery.md` (where the bundle fits in the recovery boundary)
- **Files explicitly forbidden:** every `jobs/**` file; every migration; anything that opens a
  provider session.
- **Schema impact:** none.
- **Runtime impact:** read-only. The tool must **fail closed** if handed write-capable credentials it
  would use: it opens the platform connection read-only, runs no `INSERT`/`UPDATE`/`DELETE`/DDL,
  launches no job, issues no provider request and never touches `client_dataset_coverage` except with
  `SELECT`.
- **Contract:** per schedule in scope — schedule configuration; every `client_schedule_run_history`
  row in the candidate range with status, `scheduled_fire_ts`, nominal and effective windows;
  **enumerated missing fires** (fires the schedule definition implies, diffed against existing rows —
  the only way a no-row-at-all gap becomes visible); terminal `FAILED` rows with abort codes;
  successful rows; client-DB reconciliation (`min`/`max` trip `start_timestamp`, per-segment row and
  distinct-identity counts, `synced_at` bounds) as **corroboration only, never as the primary
  claim**; duplicate provider identities; overlapping known incidents; and the resulting list of
  uncovered intervals. Output is `0600` files in a `0700` directory outside the repository tree,
  carrying counts, identifiers, hashes and the environment/repository identity block of
  `docs/13_…` §13.5 — **never** raw trip payloads, personal data, credentials or secret refs.
- **Required tests:** temporary PostgreSQL with a synthetic schedule and history — missing-fire
  enumeration is correct across daily, weekly and monthly cadences and across both DST transitions;
  a `FAILED` row is never counted as coverage; a `SUCCESS` row alone never yields a `READY`
  recommendation (the tool **recommends nothing** — it reports); write attempts are impossible;
  bundle sanitization asserted field by field.
- **Documentation update:** as listed.
- **Deployment requirement:** deployed as part of W2 or immediately after; used in W3.
- **Rollback:** revert; the tool is standalone.
- **Prerequisites:** C3 (reads the coverage shape), W2 in progress.
- **Blocking gates:** **independent review** (§9) — a read-only tool whose output is trusted for a
  correctness claim carries the same review weight as a writer.
- **Cursor task classification:** `TELEMATICS_BOOTSTRAP_AUDIT_COMMIT_READY`.
- **Implementation status:** implemented as `ops/audit_telematics_coverage_bootstrap.py` with
  `ops/tests_manual/test_telematics_coverage_bootstrap_audit.py`. Never executed against production.

---

### C10-W — bootstrap writer

- **Sequence:** 10b (paired with C10)
- **Commit message:** `feat: add Telematics coverage bootstrap tooling`
- **Objective:** the single reviewed execution surface for `docs/13_…` §13.6 step 5 — inserting one
  initial `READY` coverage row from explicit operator-approved bounds and a hash-verified C10 bundle.
  Without it, §13.6 step 5 has no implementation and the only alternative is ad hoc production SQL,
  which §5 forbids.
- **Expected files:**
  - `ops/bootstrap_telematics_trips_coverage.py` (new)
  - `ops/tests_manual/test_telematics_coverage_bootstrap_writer_postgres.py` (new)
  - `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` (guard rescope)
  - `docs/07_operations.md` §5.5 (invocation and prohibitions)
- **Files explicitly forbidden:** every `jobs/**` file; every migration; `dispatcher.py`; anything
  that opens a provider session; anything that changes `trips_pagination_mode`, a schedule row or
  `client_schedule_run_history`.
- **Schema impact:** none. It writes one row into the existing `057` shape.
- **Runtime impact:** dry-run by default and inert in that mode. A write requires **both**
  `--execute` and `--confirm-client-code` matching `--client-code`. One explicit transaction locks
  the authoritative schedule and client rows **of the target**, re-verifies identity, mode,
  configuration drift, absence of a coverage row, absence of a recovery run and absence of a
  `RUNNING` history row, inserts exactly one row, re-reads it and verifies every stored value before
  `COMMIT`. Any mismatch, conflict or affected-row count other than `1` rolls back with a stable
  sanitized code and is never retried.
- **Per-client scope (correction, 2026-08-03):** every gate is scoped to the one target client.
  The writer originally also required the whole fleet to carry zero `data_invariants_v1` clients;
  that initial-rollout assumption blocked every bootstrap after `BRAVO00016` was accepted and has
  been removed. Other approved compatibility clients are permitted and are reported, not gated on.
  The target itself must still be `strict_meta`, and the writer never changes a pagination mode.
  Initial and subsequent per-client bootstrap are both supported, each with its own inventory,
  evidence bundle, review and execution authorization.
- **Contract:** `bootstrap_status = 'READY'`, `covered_through_source = 'bootstrap'`,
  `last_gap_detected_ts = NULL`, both bounds aware whole-second UTC with `W >= A` and `W` not in the
  future, `bootstrap_evidence_ref` a safe versioned reference carrying the bundle hash and approval
  ticket — never bundle content. `A` and `W` are supplied by the operator and are never inferred,
  widened, narrowed or moved. A selection intersecting any inventoried unresolved interval is
  refused with that interval named. **No `UPDATE`, no `DELETE`, no upsert, no repair**: every later
  mutation stays with the C6 finalizers under `docs/15_…`.
- **Required tests:** disposable PostgreSQL 16 — dry-run leaves the database byte-identical; gap
  before `A` and gap after `W` accepted; intersecting gap refused; `W = A` accepted and `W < A`
  refused; timezone-equivalent instants compared as instants; stale, future-dated, hash-mismatched
  and identity-mismatched bundles refused; client-mode, schedule-configuration, existing-row and
  `RUNNING`-history states refused; execute inserts exactly one row whose fields match the approved
  inputs and which the C5 gate then accepts; duplicate execution, concurrent insert, mode race and
  schedule race refused; post-write verification failure rolls back. The static guard proves the
  approved `INSERT` passes only in the named writer function and fails everywhere else.
- **Documentation update:** as listed.
- **Deployment requirement:** may be merged and deployed with W2/W3; **execution requires explicit
  per-client production authorization** after the C10 bundle is reviewed.
- **Rollback:** revert; the tool is standalone and, having never executed, leaves no state.
- **Prerequisites:** C3, C10.
- **Blocking gates:** **independent review** (§9) — the only authorized coverage writer outside C6.
- **Cursor task classification:** `TELEMATICS_BOOTSTRAP_WRITER_COMMIT_READY_EXECUTION_UNAUTHORIZED`.
- **Implementation status:** implemented and tested. **Never executed**; zero production coverage
  rows exist.

---

### C11 — recovery tooling

- **Sequence:** 11
- **Commit message:** `feat: add Telematics manual compatibility recovery`
- **Objective:** a dry-run-by-default, separately identifiable manual recovery over an explicitly
  authorized historical window, which exercises the production compatibility path and closes the
  interval it actually fetched.
- **Expected files:**
  - `db/migrations/058_telematics_trips_manual_recovery.sql` (new)
  - `jobs/api/telematics/coverage_finalization.py` (new, narrowly shared C6/C11 CAS)
  - `ops/recover_telematics_trips_window.py` (new)
  - `ops/tests_manual/test_telematics_trips_recovery_workflow.py` (new)
  - `ops/tests_manual/test_telematics_trips_recovery_postgres.py` (new)
  - `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` (guard rescope)
  - `jobs/api/telematics/dispatcher.py` (delegation only — no semantic change)
  - `docs/13_…`, `docs/14_…`, `docs/15_…`, `docs/07_operations.md`, `docs/05_jobs.md`
- **Files explicitly forbidden:** any applied migration; `provider_client.py`; the pagination state
  machine; schedule definitions; systemd units; unrelated jobs; client configuration; C7 code.
  Anything that **creates, edits, deletes, retries or reclassifies** a
  `client_schedule_run_history` row.
- **Schema impact:** migration `058` adds `workflow_a_control.client_dataset_recovery_run` and widens
  the `covered_through_source` vocabulary with `manual_recovery`. Nothing existing is rewritten.
- **Runtime impact when executed:** dry-run is the default; execution requires **both** `--execute`
  and `--confirm-client-code` equal to `--client-code`. It resolves exactly one client and one
  authoritative enabled schedule, requires `data_invariants_v1`, requires exactly one `READY`
  coverage row whose `covered_through_ts` equals `--expected-old-covered-through`, requires
  `--window-start` to equal that watermark, and then runs
  `jobs.api.telematics.sync_trips_and_speeding` **once** through `ops/runner.py` with the literal
  window and the compatibility mode. Only after `rc == 0` does it advance `covered_through_ts` to
  `window_end_ts`, atomically with its own terminal state, through the shared CAS of
  `docs/15_…`. It **never** mutates, creates or reuses a schedule-history row, **never**
  back-inserts a missing fire, **never** touches `coverage_start_ts` or `bootstrap_status`, and
  **never** retries.
- **Required tests:** dry-run leaves the database byte-identical, performs zero provider requests and
  launches no business subprocess (the two local read-only `git` identity commands are not the
  business execution); strict client, missing/non-`READY` coverage, old-`W` mismatch, start ≠ `W`,
  non-forward or oversized interval, future/ineligible boundary, `RUNNING` scheduled fire, active
  recovery and duplicate approval all refuse; execution claims exactly one `RUNNING` recovery row and
  creates no history row; success advances `W` exactly once and leaves `A` unchanged; provider,
  business and orchestration failure leave coverage byte-identical; a finalization conflict leaves
  coverage untouched and emits a stable code; the failed scheduled-fire row is unchanged throughout;
  the static guard permits the advancement only in the shared function.
- **Documentation update:** as listed.
- **Deployment requirement:** may be **merged and deployed** with W2/W3; migration `058` is applied
  through its own gate; **execution remains separately authorized per window**.
- **Rollback:** revert the code; migration `058` is additive and leaves honest evidence in place. A
  failed recovery advanced nothing, so there is nothing to undo.
- **Prerequisites:** C10 and a bootstrapped `READY` coverage row for the target client.
- **Blocking gates:** **independent review** (§9); **execution additionally requires explicit
  production authorization per window**.
- **Cursor task classification:** `TELEMATICS_RECOVERY_TOOLING_COMMIT_READY_EXECUTION_UNAUTHORIZED`.

**Must recovery tooling be postponed until after a successful scheduled compatibility run? — No, and
that ordering is deliberately revised.** The earlier plan pinned the first recovery execution behind
"§10 stage 7 observed", which assumed recovery could only ever repair *historical* data with an
already-proven path. That assumption does not hold for the state this project actually reached: the
first compatibility fire for the canary client **failed**, its interval lies entirely after `W`, and
its `(schedule_id, scheduled_fire_ts)` key makes it single-shot — so the only way to obtain a
successful scheduled compatibility run over that interval is to wait for the next natural fire.
Requiring a successful scheduled run before a controlled canary recovery is therefore circular.

The revised gate ordering for the **first** controlled canary recovery is:

1. the C11 tooling and migration `058` are independently reviewed;
2. the target client is the sole compatibility client (the tool reports this; it is an authorization
   gate, not a code refusal, so the same tool serves later rollout);
3. a `READY` bootstrap coverage row exists for the target schedule;
4. the explicit interval begins exactly at `W`;
5. the dry-run plan is reviewed;
6. production execution is separately authorized for that exact window.

**Fleet-wide rollout gates are unchanged and are not weakened by this revision** (§14 N7): every
further client still requires its own inventory, interval, evidence bundle and review, and no
fleet-wide enablement follows from a successful canary recovery.

---

### C12 — consolidated operations runbook

- **Sequence:** 12
- **Commit message:** `docs: add Telematics compatibility operations runbook`
- **Objective:** consolidate the enable/disable/rollback procedure, configuration examples and the
  recovery ordering into the canonical operational documents — **after** the behavior they describe
  has been observed, and **not** as the first documentation of any behavior.
- **Expected files:** `docs/07_operations.md` (new §5.4.2 consolidated compatibility runbook and
  §5.5 coverage-state operations), `docs/09_disaster_recovery.md` (ordered recovery procedure),
  `docs/06_security.md` (operator-access note for the mode switch), `docs/05_jobs.md`
  (cross-references), `docs/12_…` / `docs/13_…` (status notes only: "implemented by commits X–Y").
- **Files explicitly forbidden:** every `jobs/**`, `db/**`, `api/**`, `ops/*.py` file. Documentation
  only.
- **Schema impact / runtime impact:** none.
- **Required tests:** none executable; review checks that every documented command matches shipped
  code and that no ENV or module name drifted (`CONVENTIONS.md` §13).
- **Deployment requirement:** none.
- **Rollback:** revert.
- **Prerequisites:** C11 and at least §10 stage 7 observed.
- **Blocking gates:** none.
- **Cursor task classification:** `TELEMATICS_COMPAT_RUNBOOK_COMMIT_READY`.

**Which documentation must accompany earlier commits rather than being deferred here?** Everything
that describes a behavior the commit itself introduces, per `AGENTS.md` §6 and `CONVENTIONS.md` §13:
C1/C2 the configuration fields (`docs/05_jobs.md`); C3 the schema (`docs/05_jobs.md`); C4 the new
pure domain helper and its externally reviewable arithmetic contract (`docs/05_jobs.md`), despite
having no runtime effect; C5 the dispatcher's nominal/effective distinction, the new `trips_sync`
params and
`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` triage (`05_jobs`, `07_operations` §5.5); C6
`TRIPS_COVERAGE_GAP_DETECTED` and reseed (`07_operations` §5.5); C7 the new ENV budgets
(`02_infrastructure`) and abort codes (`07_operations` §5.4); C8 the job params (`05_jobs`); C9 the
redaction rules (`06_security`); C10/C11 their own invocation contracts. C12 adds only the ordered
end-to-end procedure that no single commit owns.

### 3.13 How this differs from the candidate split, and why

| Candidate | This plan | Reason |
|---|---|---|
| 2 — one commit for stabilization config **and** coverage schema | **Split into C2 and C3** | Two different tables, two different risk profiles. C2 mirrors `018`/`040` and is trivially reviewable; C3 creates a table with an FK cascade, a status vocabulary and two `CHECK`s that encode the fail-closed rule. Bundling them would hide C3's review surface behind C2's triviality. |
| 3 + 4 — pure helper, then gate | **C4 pure; C5 = gate *and* dispatcher wiring** | The wiring is what first makes a compatibility window derivable. Shipping wiring without the gate — even inertly — creates a tree state in which a mode flip alone would produce an ungated compat window. Merging them makes "no compat window without a gate" a property of the commit graph, not of operator discipline. |
| 4 + 5 — gate and advancement | **Kept separate (C5, C6)** | Different failure semantics, different review question, different rollback granularity (see C6 note). |
| 6 + 7 — state machine, then telemetry | **Essential telemetry moved into C7; C9 keeps only cross-cutting telemetry** | A state machine whose page decisions cannot be reconstructed from logs is not reviewable (see C9 note). |
| 6 + 8 — state machine and sync integration | **Kept separate (C7, C8)** | Different modules, different review questions, independent rollback (see C8 note). |
| 11 — one documentation commit | **Docs distributed across C1–C11; C12 consolidates** | Repository rule: docs accompany behavior changes. A single trailing docs commit would leave every intermediate commit non-compliant. |

---

## 4. Migration strategy

### 4.1 Exact proposed sequence

| Ordinal | File | Commit | Objects |
|---|---|---|---|
| `055` | `055_workflow_a_trips_pagination_mode.sql` | C1 | `client_account.trips_pagination_mode` |
| `056` | `056_workflow_a_trips_stabilization_config.sql` | C2 | `client_account.trips_stabilization_delay_seconds`, `trips_overlap_seconds`, `trips_max_recovery_span_seconds` |
| `057` | `057_workflow_a_trips_coverage_state.sql` | C3 | `workflow_a_control.client_dataset_coverage`; five nullable evidence columns on `client_schedule_run_history` |

**Three migrations, not one and not five.** One migration would make the highest-risk object
(`client_dataset_coverage`) reviewable only together with four trivial column adds. Five would split
`client_dataset_coverage` from the history evidence columns that are written in the same dispatcher
transaction and are meaningless without it. The chosen split gives each review exactly one question:
*is this selector correct* (055), *are these bounds correct* (056), *is this state model enforceable
in SQL* (057).

Ordinals were chosen **after** reading the actual ceiling: `db/migrations/` ends at
`054_environment_identity_resume_contract.sql`, so `055`–`057` are the next free slots.
`ops/db_migrate.sh` applies `db/migrations/*.sql` in `sort` order and tracks
`public.schema_migrations`, so the three files apply in ordinal order on the next run. The
intentional `009_` gap in platform migrations is preserved (`CURRENT_TASK_CONTEXT.md` §4.2).
`db/client_business/` is untouched, so `scripts/onboard_workflow_a_client.py`'s
`CLIENT_BUSINESS_DDL_FILES` list needs no edit.

### 4.2 Additive-only rules

- No `DROP`, no `ALTER … TYPE`, no `RENAME`, no change to an existing column's nullability or
  default, no change to an existing constraint's semantics.
- No applied migration is edited (`AGENTS.md` §6, `CONVENTIONS.md` §12).
- Every statement is idempotent: `ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`,
  `DROP CONSTRAINT IF EXISTS` before `ADD CONSTRAINT`, `CREATE INDEX IF NOT EXISTS`, guarded `DO $$`
  blocks for constraint existence — mirroring `018_*` and `054_*`.
- `CREATE SCHEMA IF NOT EXISTS workflow_a_control;` at the top of each file, as `018_*` does.

### 4.3 Safe defaults and backfill behavior

Every added column carries a default that reproduces today's behavior, and the backfill is the
explicit `UPDATE … WHERE col IS NULL` step of the `018_*` pattern before `SET NOT NULL`:

| Column | Default | Backfill result for existing rows |
|---|---|---|
| `trips_pagination_mode` | `'strict_meta'` | every existing client → `strict_meta` |
| `trips_stabilization_delay_seconds` | `10800` | inert while strict |
| `trips_overlap_seconds` | `3600` | inert while strict |
| `trips_max_recovery_span_seconds` | `2678400` | inert while strict |
| `client_dataset_coverage.bootstrap_status` | `'UNINITIALIZED'` | **no rows are created by the migration** — a schedule with no coverage row and a schedule with an `UNINITIALIZED` row are both refused by §5.2.1 |
| `client_schedule_run_history.*` evidence columns | `NULL` | historical rows keep `NULL`, which correctly means "not applicable / strict" |

**No migration inserts a coverage row.** Auto-creating rows would be the automatic initialization
that `docs/13_…` §5.2.1 and §13.0 forbid.

### 4.4 Constraints

`055`: `CHECK (trips_pagination_mode IN ('strict_meta','data_invariants_v1'))`.
`056`: `CHECK (trips_stabilization_delay_seconds >= 0)`,
`CHECK (trips_overlap_seconds >= 0)`,
`CHECK (trips_max_recovery_span_seconds > 0 AND trips_max_recovery_span_seconds <= 2678400)`.
`057`: `PRIMARY KEY (schedule_id)`; `FOREIGN KEY (schedule_id) REFERENCES
workflow_a_control.client_dataset_schedule (schedule_id) ON DELETE CASCADE`;
`CHECK (bootstrap_status IN ('UNINITIALIZED','READY','GAP_DETECTED','RESEED_REQUIRED'))`;
`CHECK (covered_through_source IN ('bootstrap','scheduled_run','operator'))`;
`CHECK (coverage_start_ts IS NULL OR covered_through_ts IS NULL OR coverage_start_ts <=
covered_through_ts)`; and the `READY` completeness `CHECK` of §6.2 — the constraint that makes
§5.2.1 enforceable in the database and not only in Python.

### 4.5 Indexes

`client_dataset_coverage` is keyed by `schedule_id` and read one row at a time under the dispatcher
advisory lock, so the primary key is the only access path required. Add
`INDEX (bootstrap_status) WHERE bootstrap_status <> 'READY'` **only** as a partial index for
operator triage queries. This is a future operational optimization outside C6 and must not be
justified by runtime need. No index is added
to `client_schedule_run_history` — the new columns are never predicates.

### 4.6 Ownership and permissions

The migrations run as the platform migration role via `ops/db_migrate.sh`, exactly like `018`–`054`.
No new role, no new `GRANT`, no new schema. `client_dataset_coverage` lives in the existing
`workflow_a_control` schema and inherits its ownership and privileges; the dispatcher already
connects with the same platform credentials it uses for `client_schedule_run_history`. No client
business database and no client role is affected — `db/client_business/` is untouched.

### 4.7 Old runtime, new schema

Safe and explicitly supported. `control_plane.load_client_account_config` and
`dispatcher._load_enabled_schedules` both use **explicit column lists**, so an old runtime simply
never selects the new columns. `_claim_fire` inserts an explicit column list, so the new nullable
history columns default to `NULL`. `client_dataset_coverage` is read and written by nothing. This is
why §10 deploys schema before runtime: the intermediate state is inert in both directions.

### 4.8 New runtime, every client strict

Also inert, and this is the state W2 verifies. In `strict_meta` the dispatcher must not read the
coverage table, must write `NULL` into every new evidence column, and must preserve today's window
arithmetic; the provider client defaults to `strict_meta` and `_fetch_paginated` is unedited; the
sync job's only permitted observable delta is additive log context plus the pre-commit duplicate
assertion. **The verification target is semantic strict-mode equivalence (§10.3), which in the
current provider state means the same `PAGINATION_MISMATCH` safety classification on the same
broken-metadata path** — not byte-identical logs, timestamps, run IDs, history IDs or evidence
bundles.

### 4.9 Downgrade / rollback policy

**No destructive rollback.** No `DROP TABLE`, no `DROP COLUMN`, no `DROP CONSTRAINT` as a rollback
step, and no down-migration files. Rollback of behavior is always a code revert plus, where
applicable, a control-plane `UPDATE` back to `strict_meta`. Additive, defaulted, unread schema costs
nothing and preserves the coverage record needed if the mode is re-enabled (`docs/13_…` §16.10
step 2). Migration 054's identity contract, the `ops_control.environment_identity_promotion` column
positions (`attnum` 22/23 are a hard invariant per `CURRENT_TASK_CONTEXT.md` §7) and the existing
environment-promotion restrictions are **untouched**: 055–057 add objects only in
`workflow_a_control` and never in `ops_control`, so no promotion contract, plan hash or attestation
is invalidated.

---

## 5. Configuration contract

### 5.1 `trips_pagination_mode`

| Item | Value |
|---|---|
| Location | `workflow_a_control.client_account` |
| Type | `TEXT NOT NULL DEFAULT 'strict_meta'` |
| Allowed | `'strict_meta'`, `'data_invariants_v1'` |
| Semantics | Selects the `/trips` pagination contract. `strict_meta` = today's `_fetch_paginated` code path and safety classifications, including `PAGINATION_MISMATCH`. `data_invariants_v1` = the `docs/12_…` §4 state machine. |
| Scope of effect | `/trips` only. Never `/vehicles`, `/drivers`, `/vehicles/events`, `/alerts/notifications`, `/fuel/*`. |
| Fail-closed rule | `NULL`, absent, unknown, or unreadable ⇒ `strict_meta`. A value that passes the DB `CHECK` but fails the Python allowlist ⇒ **raise** (DB/Python drift is not a default-able condition). |
| Behavior in strict mode | Coverage state is neither read nor written; the three numerics are loaded but not applied. |
| Effective when | Next run start — `control_plane` is loaded fresh per run, no caches (`CONVENTIONS.md` §10). |

### 5.2 `trips_stabilization_delay_seconds` (`D`)

Default `10800` (180 min). Bounds `>= 0`; recommended operating range `3600`–`86400`. Absolute UTC
seconds, subtracted from the UTC-converted fire instant — **never** local wall-clock arithmetic and
never before the local→UTC conversion. In strict mode: loaded, validated, unused.

### 5.3 `trips_overlap_seconds` (`O`)

Default `3600`. Bounds `>= 0`; **recommended minimum `3600`**, which is the maximum DST-induced
inter-fire shortfall and is what makes contiguity analytic rather than state-dependent. Absolute UTC
seconds. In strict mode: loaded, validated, unused.

### 5.4 `trips_max_recovery_span_seconds` (`R`)

Default `2678400` (31 d) — the provider's documented lookup limit and the hard ceiling. Bounds
`> 0 AND <= 2678400`. Caps how far back a coverage-expanded window may reach:
`E_start = max(E_start, E_end − R)`. Reducing it can only expose a gap (loudly), never permit a
coverage jump. In strict mode: loaded, validated, unused.

### 5.5 Where configuration lives — decision and justification

**Decision: all four fields live at the client-account level (`workflow_a_control.client_account`).
Not schedule level. Not split.**

Justification:

1. **`docs/13_…` §16.3 is accepted architecture** and places all four on `client_account`; this plan
   does not relitigate an accepted decision without a safety reason.
2. `trips_pagination_mode` is a property of the **provider account and credential**, not of a
   schedule (`docs/12_…` §3.1). A manual run must receive the same treatment as a scheduled one, and
   manual runs have no `schedule_id`.
3. `D` is a property of **when the provider's data settles** for that account — the same physical
   fact regardless of which schedule reads it.
4. `R` is a **recovery policy** bounded by a provider limit, again account-scoped.
5. Splitting the four across two tables would give the mode and its own eligibility parameters two
   different lifecycles and two different rollback surfaces, and would make the "one reviewed
   `UPDATE` disables everything" property of `docs/12_…` §3.6 false.

**The one genuine counter-argument, and how it is handled.** The contiguity condition
`Δ ≤ L + O + 1 s` couples `O` to `lookback_days` and to the fire cadence — both **schedule-scoped**.
An account-level `O` can therefore be right for one schedule of a client and wrong for another.
Today every enabled client has exactly one `trips_sync` schedule, so the coupling is latent, and
`uq_client_dataset_schedule (client_id, dataset_name)` means a client can only ever have one
`trips_sync` schedule at a time — the coupling cannot become multi-valued for this dataset.

**Required mitigation (part of C2):** the configuration validator must, for the client's enabled
`trips_sync` schedule, compute `Δ_max` (the worst-case inter-fire interval including DST) and assert
`Δ_max ≤ L + O + 1 s`. A violation raises at load time rather than silently producing a gap. This
converts the residual coupling risk into a fail-closed check, at the cost of one pure function.

### 5.6 Configuration change semantics

Any change to `D`, `O` or `R` requires next-window revalidation before it is applied
(`docs/13_…` §13.10): recompute `E_start`/`E_end` for the next fire under the new value and confirm
`E_start ≤ W + 1 s`. A **material** change — one that breaks next-window contiguity, an `R` reduction
that exposes a previously healed interval, or a change concurrent with a `lookback_days`,
`frequency` or `timezone` change — sets `bootstrap_status = 'RESEED_REQUIRED'`, after which §5.2.1
refuses to run until a reviewed reseed lands.

---

## 6. Coverage-state contract

### 6.1 Schema fixed by migration 057

```sql
CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_coverage (
  schedule_id            UUID PRIMARY KEY
                           REFERENCES workflow_a_control.client_dataset_schedule (schedule_id)
                           ON DELETE CASCADE,
  client_id              UUID        NOT NULL,
  client_code            TEXT        NULL,
  dataset_name           TEXT        NOT NULL,

  coverage_start_ts      TIMESTAMPTZ NULL,   -- A: earliest instant of the verified closed interval
  covered_through_ts     TIMESTAMPTZ NULL,   -- W: latest instant of the verified closed interval

  bootstrap_status       TEXT        NOT NULL DEFAULT 'UNINITIALIZED',
  bootstrap_evidence_ref TEXT        NULL,
  seeded_at              TIMESTAMPTZ NULL,
  seeded_by              TEXT        NULL,

  covered_through_source TEXT        NOT NULL DEFAULT 'bootstrap',
  last_gap_detected_ts   TIMESTAMPTZ NULL,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Field semantics: `schedule_id` is the identity and the lifecycle anchor; `client_id`/`client_code`/
`dataset_name` are denormalized for operator queries and follow the repository's
`client_id` + `client_code` rule (`CONVENTIONS.md` §3) — never substituted for one another;
`coverage_start_ts` is the **explicit** lower bound of the claim and is never inferred;
`covered_through_ts` is the upper bound; `bootstrap_status` gates whether the pair is usable;
`bootstrap_evidence_ref` points at the reviewed §13.5 bundle; `seeded_at`/`seeded_by` record the
reviewed operation; `covered_through_source` records what last moved `W`;
`last_gap_detected_ts` and `updated_at` are visibility aids.

### 6.2 Constraints

- `PRIMARY KEY (schedule_id)` — **uniqueness is one row per schedule**, which is the correct grain:
  a disabled-then-re-enabled schedule keeps its `schedule_id` and therefore its claim, while a
  deleted-and-recreated schedule gets a new id and must be bootstrapped from scratch.
- `FOREIGN KEY (schedule_id) → client_dataset_schedule(schedule_id) ON DELETE CASCADE`.
- `CHECK (bootstrap_status IN ('UNINITIALIZED','READY','GAP_DETECTED','RESEED_REQUIRED'))`.
- `CHECK (covered_through_source IN ('bootstrap','scheduled_run','operator'))`.
- `CHECK (coverage_start_ts IS NULL OR covered_through_ts IS NULL OR coverage_start_ts <= covered_through_ts)`.
- `CHECK (bootstrap_status <> 'READY' OR (coverage_start_ts IS NOT NULL AND covered_through_ts IS NOT
  NULL AND bootstrap_evidence_ref IS NOT NULL AND seeded_at IS NOT NULL AND seeded_by IS NOT NULL))`
  — a `READY` row without a complete, evidenced claim cannot exist.

The runtime check of §5.2.1 remains in Python as well, because the database can verify that an
evidence reference is *present*, never that it is *meaningful*.

### 6.3 Allowed transitions

| From | To | Who | Precondition |
|---|---|---|---|
| (no row) | `UNINITIALIZED` | operator | Reviewed insert during bootstrap preparation |
| `UNINITIALIZED` | `READY` | operator (reviewed §13 bootstrap) | Both bounds set, evidence bundle reviewed, in-interval gaps recovered **or** excluded by a later `A` |
| `READY` | `READY` | dispatcher (C6) | Connected window, `rc == 0`, finalize commits; **only `covered_through_ts` moves, by `max()`** |
| `READY` | `GAP_DETECTED` | dispatcher (C6) | `E_start > W + 1 s`; `A` and `W` unchanged |
| `READY` | `RESEED_REQUIRED` | operator | Strict round-trip, material `D`/`O`/`R` change, schedule reconfiguration |
| `GAP_DETECTED` | `READY` | operator (reviewed reseed) | Gap recovered or excluded; fresh evidence bundle |
| `RESEED_REQUIRED` | `READY` | operator (reviewed reseed) | Revalidation; `A` may be reused when the earlier proof still holds |

### 6.4 Forbidden transitions

- Any automatic creation of a row. **Coverage is never initialized by a run.**
- Any automatic transition **into** `READY`. Only a reviewed operator action.
- Any write of `coverage_start_ts` by a scheduled run, in either direction — forwards would abandon
  verified history, backwards would manufacture an unreviewed claim.
- Any regression of `covered_through_ts` (`max()` only).
- Any advancement across a detected hole, or while `bootstrap_status <> 'READY'`.
- Any advancement by a manual run, a backfill, a recovery run or a compare-only run.
- Any derivation of a bound from a run's own window, from the newest `SUCCESS` history row, or from
  `max(synced_at)` (`docs/13_…` §13.0).
- Deletion of the row as a rollback step.

### 6.5 C6 mutations are separate branches

**Newly disconnected `READY`.** When the read-only gate returns
`TRIPS_COVERAGE_GAP_DETECTED` with `requires_gap_persistence=true`, the dispatcher first commits the
`RUNNING` claim, then calls `_finalize_compat_gap` before launch. That transaction locks coverage,
validates the claim-time snapshot, locks/verifies history, writes `READY → GAP_DETECTED` with one
shared UTC whole-second `last_gap_detected_ts = updated_at`, writes history `RUNNING → FAILED`, and
commits. `A`, `W`, evidence, seed metadata and `covered_through_source` remain unchanged. Existing
or malformed `GAP_DETECTED` rows are never mutated during ordinary rejection.

**Allowed success.** Only after `rc == 0`, `_finalize_compat_success` locks and validates the same
claim-time snapshot, then locks the claimed history row and requires it still to be `RUNNING`.
Only after both validations succeed does it compute `new_W = max(current_W, E_end)`. If `new_W`
moves, it updates `W`, `covered_through_source='scheduled_run'` and explicit `updated_at`; otherwise
it issues no coverage `UPDATE`. It then changes that same locked history row `RUNNING → SUCCESS`
and commits both decisions atomically. The no-op still locks and validates both rows, changes no
coverage column byte-for-byte, and does not weaken claim-loss protection. Gap persistence is never
routed through this transaction.

### 6.6 Reseed, mode rollback, schedule deletion, disablement

- **Reseed:** a reviewed operator operation that may set both bounds together, always with a fresh
  `bootstrap_evidence_ref`, `seeded_at`, `seeded_by` and `covered_through_source = 'bootstrap'`.
- **Mode rollback to `strict_meta`:** the row is **not deleted**. In the same reviewed change, set
  `bootstrap_status = 'RESEED_REQUIRED'` so that re-enabling later cannot silently reuse a watermark
  that strict-mode operation left stale.
- **Schedule disablement (`enabled = false`):** no fires, the interval freezes, the row survives.
- **Schedule deletion:** `ON DELETE CASCADE` removes the row. A recreated schedule is a new claim.

### 6.7 Concurrency and row locking

The dispatcher holds `pg_try_advisory_lock(DISPATCHER_ADVISORY_LOCK_KEY)` for the whole tick and runs
at most one job (`_count_running() > 0` ⇒ no-op), so dispatcher-vs-dispatcher races are already
excluded. **Additionally required (C6):** each compatibility finalizer locks the coverage row,
validates the complete claim-time CAS snapshot, then locks and requires the claimed history row to
remain `RUNNING`. The global order is coverage row → history row. The advisory lock does not
constrain a `psql` session. The C5 gate read does not take `FOR UPDATE`; no row lock spans subprocess
execution. Gap enforcement is after the durable claim and before launch; successful finalization is
only after `rc == 0`.

---

## 7. Runtime integration sequence

### 7.1 Authoritative ordering (normative)

This is the authoritative compatibility-mode order. C5 owns the read-only gate and its enforcement;
C6 owns only the two explicitly named compatibility transactions. No row lock crosses subprocess
execution.

1. Evaluate the latest due fire `F` and nominal window `[N_start, N_end]`.
2. Load compatibility mode and coverage with exactly one lock-free, schedule-keyed, read-only
   `SELECT`. Widen its ten-column projection with `covered_through_source` and
   `last_gap_detected_ts` to produce one immutable 12-field `CoverageState`.
3. Evaluate the pure gate and derive the effective window when allowed. The gate may ignore the two
   audit/provenance fields; `PreparedDispatcherRun` retains the exact same `CoverageState` through
   subprocess completion.
4. Claim one `client_schedule_run_history` row as `RUNNING`: nominal window for rejection,
   effective window for allowance, and five claim-time evidence fields exactly once.
5. Branch on the gate result:
   - **Bootstrap-required or non-persisting rejection:** missing/malformed coverage,
     `UNINITIALIZED`, `RESEED_REQUIRED`, malformed `GAP_DETECTED`, or structurally valid existing
     `GAP_DETECTED`. Finalize only the claimed history row `FAILED`, emit structured log and
     suspected-bug evidence, mutate no coverage, launch no subprocess. Existing valid gap remains
     loud without timestamp refresh.
   - **Newly disconnected `READY`:** exactly
     `gate.abort_code == TRIPS_COVERAGE_GAP_DETECTED` and
     `requires_gap_persistence == true`. Call `_finalize_compat_gap`: coverage
     `READY → GAP_DETECTED` and history `RUNNING → FAILED` atomically, then commit. This is after
     durable claim and before `_build_job_params`, subprocess construction, credential resolution,
     sockets or provider/client access. Log/report after the transaction; launch nothing.
   - **Allowed gate:** continue with the effective claimed window.
6. Build parameters and launch the subprocess only for the allowed branch.
7. Fetch and validate completely before the client-business connection opens; commit the single
   client-business transaction; observe `rc`.
8. For `rc != 0`, finalize history `RUNNING → FAILED`; coverage remains unchanged.
9. For `rc == 0`, call `_finalize_compat_success` in this order: lock the coverage row; validate it
   against the retained claim-time snapshot; lock the claimed history row and require it still to be
   `RUNNING`; only then conditionally advance `W`; finalize that same locked history row
   `RUNNING → SUCCESS`; commit both decisions atomically. Every `claim_*` CAS parameter comes from
   `PreparedDispatcherRun.coverage_state`; current values come from `SELECT ... FOR UPDATE`. The
   coverage lock read compares current state to the retained claim snapshot; it never creates a
   replacement expected snapshot. A no-op (`E_end <= W`) still locks and validates both rows,
   executes no coverage `UPDATE`, leaves `covered_through_source` and coverage `updated_at`
   unchanged, and then finalizes history `SUCCESS`.
10. Reconcile any uncertain platform `COMMIT` outcome on a fresh connection before considering a
    separate `FAILED` write, exactly as `docs/15_…` specifies.

The global row-lock order for both C6 transactions is coverage row → history row. This notation
defines **row-lock order only**, never mutation-first order: a compatibility coverage mutation may
be issued only while the originally claimed history row is locked and verified as `RUNNING`.
History missing or no longer `RUNNING` after the coverage lock causes rollback,
`TRIPS_HISTORY_CLAIM_LOST`, no coverage mutation statement, no terminal overwrite and no automatic
business-job replay. A failure after a coverage `UPDATE` is issued but before the paired history
`UPDATE` rolls back both surfaces through the documented conflict/crash path; no partial pair may
commit. Strict mode and every non-`trips_sync` dataset short-circuit all coverage reads and writes
and continue through today's unchanged `_finalize_run`.

Normative carrier sequence:

```text
single read-only coverage SELECT
→ immutable CoverageState with 12 fields
→ pure C5 gate
→ durable history claim
→ PreparedDispatcherRun retains the same CoverageState
→ C6 finalizer binds CAS parameters from that retained object
```

Values must come from the original pre-claim `SELECT`. Mutation-time reread, history inference,
the current row after `FOR UPDATE`, `updated_at`, client trip data, platform runs,
`max(synced_at)` and provider metadata are forbidden claim-time substitutes. No second
gate/claim coverage `SELECT` is authorized.

### 7.2 Commit ownership relative to today

| Surface | Owner after W1 | Contract |
|---|---|---|
| Fire/mode/coverage load, pure gate, claim and non-persisting rejection | C5 | Coverage read-only; durable history evidence; no launch on rejection |
| Existing strict/non-trips `_finalize_run` | existing code | Body and behavior unchanged; never executes coverage SQL |
| `_finalize_compat_gap` | C6 | Newly disconnected `READY` only; post-claim/pre-launch atomic gap + `FAILED` |
| `_finalize_compat_success` | C6 | Allowed `rc == 0` only; atomic validated advancement/no-op + `SUCCESS` |
| Shared SQL helpers | C6 internal detail | Permitted only when strict behavior cannot change and transaction ownership remains explicit |
| Provider/client-business body | C7/C8 | Outside both platform finalization transactions |

The C6 guard transition is narrow: `coverage_windows.py` may expose the two plain fields but stays
I/O-free and may never write coverage. `dispatcher.py` may select and retain them; only
`_finalize_compat_gap` and `_finalize_compat_success` may execute coverage `FOR UPDATE` and
conditional `UPDATE`. Only the gap finalizer assigns `last_gap_detected_ts` and coverage
`updated_at`; only a moving-`W` success finalizer assigns `covered_through_ts`,
`covered_through_source` and coverage `updated_at`. Provider/sync code, API routes,
scripts/onboarding, unknown production packages, C7 pagination and strict/non-trips finalization
remain forbidden writers. Synthetic tests prove approved finalizer SQL passes, identical SQL
elsewhere fails, and the widened helper gains no write authority.

`docs/15_…` is the detailed G-COV transaction design. This section fixes commit ownership and
sequence; `docs/13_…` fixes domain invariants.

---

## 8. Test architecture

Repository convention (`CONVENTIONS.md` §11): no CI, no pytest config; focused manual scripts in
`ops/tests_manual/` with a `main()`, run by hand. Disposable-PostgreSQL tests have precedent
(`test_environment_identity_promotion_postgres.py`, `test_suspected_bug_outbox_postgres.py`). **No
new third-party dependency**; generated sequences use stdlib `random`/`itertools` with a fixed seed.

| # | Suite | File | Environment | Commit |
|---|---|---|---|---|
| S1 | Migration and schema constraints | `test_telematics_coverage_state_schema_postgres.py` (+ mode/config cases) | **Temporary PostgreSQL** | C1, C2, C3 |
| S2 | Configuration loading | `test_telematics_trips_pagination_mode_config.py`, `test_telematics_trips_stabilization_config.py` | **Pure unit** (fake row dicts) | C1, C2 |
| S3 | Pure window derivation and runtime-inert vocabulary guard | `test_telematics_trips_stabilization_windows.py`; shared `test_telematics_coverage_state_schema_postgres.py` guard adaptation | **Pure unit**; static tracked-source scan (the shared suite's PostgreSQL coverage remains C3) | C4 |
| S4 | DST | same file, dedicated section | **Pure unit** (`zoneinfo`, full-year 2026 sweep, both transitions, all four schedule shapes) | C4 |
| S5 | Bootstrap fail-closed behavior and recorded-gap precedence | `test_telematics_coverage_bootstrap_gate.py` owns the pure identity/status/foundational matrix, valid and malformed `GAP_DETECTED`, bounded/redacted logging, fatal side-effect patches and temporary-PostgreSQL claim/finalize paths. Migration-impossible shapes stay explicit pure cases rather than weakening migration 057. | **Pure unit** + **temporary PostgreSQL** | C5 |
| S6 | C6 coverage finalization | `test_telematics_coverage_advancement.py` / `…_postgres.py` plus the authorized guard/fixture/dispatcher suites: retained 12-field carrier; movement/no-op; null-safe and timezone-normalized CAS without rounding; source-only/gap-timestamp races; claim loss; one-shot gaps; concurrent reseed; client-commit/platform-finalization replay safety; guard rescope; commit reconciliation; strict isolation | **Pure unit** + **disposable PostgreSQL 16** + **sandbox client DB** | C6 |
| S7 | Pagination generated sequences | `test_telematics_trips_pagination_compat.py` (T35) | **Fake provider session** | C7 |
| S8 | Strict-mode regression | same file (T1, T2) + extended `test_workflow_a_dispatcher.py` | **Fake provider session** + **pure unit** | C5, C7 |
| S8a | Minimum mode propagation | focused sync-propagation coverage: frozen/parameter mode reaches the provider `/trips` path; unknown or missing mode fails closed to strict; non-`/trips` endpoints unchanged | **Fake provider session** + **pure unit** | C7 |
| S9 | Compatibility failure taxonomy, including the accepted `total` policy | `test_telematics_trips_pagination_compat.py` (T3–T29 and the `docs/16_…` §5.4 empty/short-page cases) | **Fake provider session** | C7 |
| S10 | Transaction rollback | `test_telematics_trips_compat_sync_integration.py` (T30–T32) | **Sandbox client database** | C8 |
| S11 | Dual-database failure modes | `test_telematics_coverage_advancement_postgres.py` | **Temporary PostgreSQL** + **sandbox client database** | C6 |
| S12 | Privacy and redaction | `test_telematics_compat_redaction.py` (T33, T34) | **Pure unit** (log capture over the full matrix) | C9 |
| S13 | Bootstrap audit tooling | `test_telematics_coverage_bootstrap_audit.py` | **Temporary PostgreSQL** (synthetic schedule + history) | C10 |
| S14 | Recovery tooling | `test_telematics_trips_recovery_workflow.py` (pure/static) + `test_telematics_trips_recovery_postgres.py` (migration `058`, gates, claim, success/failure/conflict) | **Pure unit** + **disposable PostgreSQL 16** with mocked business execution | C11 |
| S15 | Integration tests | end-to-end dispatcher → fake runner subprocess → fake provider → sandbox client DB | **Fake provider session** + **sandbox client database** + **temporary PostgreSQL** | C8, C9 |
| S16 | Production read-only evidence | inventory bundles from C10; `logs`/`runs`/history queries | **Production read-only** | W2, W3 |
| S17 | Manual production verification | disabled-deployment observation; first compat run observation; rollback proof | **Production read-only evidence** + the already-completed page-2/3 probe | W2, W3 |
| S18 | Separately authorized live request | only if a new provider probe becomes necessary (e.g. U4/U6 for a second client) | **Separately authorized live request** | W3, per client |

**Production writes are never used as tests.** S16/S17 are observation of operations authorized on
their own merits; no suite writes to a production database, and no suite issues a provider request
except S18, which requires its own authorization per execution.

The future S6 suites must distinguish the coverage-row lock from a coverage mutation and assert the
exact statement order deterministically:

- successful advancement locks coverage first, validates the retained snapshot, then locks and
  verifies the claimed history row `RUNNING` before any coverage `UPDATE`;
- successful no-op locks and validates coverage, locks and verifies history before `SUCCESS`, and
  executes no coverage `UPDATE`;
- history already `FAILED` before finalization, a stale sweep before finalization, and a manual
  unblock before finalization each produce `TRIPS_HISTORY_CLAIM_LOST` and execute no coverage
  `UPDATE` statement;
- a forced exception after the coverage `UPDATE` but before the history `UPDATE` rolls back both
  surfaces to their pre-transaction state;
- a static documentation assertion requires docs/14 §7.1 and docs/15 Branch C to state the same
  order: coverage lock/validation → history lock/`RUNNING` verification → optional coverage
  mutation → terminal history mutation → `COMMIT`.

These assertions preserve the global coverage-row-before-history-row lock order while proving that
coverage mutation occurs only after both locks and validations.

---

## 9. Independent-review gates

**Rule (process-based independence).** An independent review runs in a **fresh session**, reads the
**code before** the design document, may not be performed by the author of the change under review,
and must not modify the implementation while reviewing it. Review and implementation remain separate
operations, the runtime code is reviewed independently, and the reviewer reports its actual model
identity truthfully. **The prior final documentation review was partially a self-review and must
not be treated as sufficient review for any runtime implementation below.**

**Reviewer-model diversity is optional.** By operator decision (`2026-08-03`) the requirement that
the reviewer's model differ from the implementer's model is **withdrawn**. A same-model review is
allowed and can approve any gate below, including G-SM, provided the process conditions in the rule
above hold. Model diversity remains a nice-to-have and **must not be treated as a blocking gate**;
no gate may be re-opened, and no review may be re-run, on model-identity grounds alone.

| Gate | Applies to | Why | Reviewer must be |
|---|---|---|---|
| G-MIG | C1, C2, **C3** | Applied migrations are irreversible in practice (no destructive rollback); C3 encodes the fail-closed rule in SQL | Fresh session, not the author |
| G-COV | **C6** | The only writer of a durable correctness claim; dual-database failure semantics | Fresh session, not the author |
| G-SM | **C7** | Replaces the safety check that currently stops ingestion; a defect here corrupts business data silently. Also confirms the C7 sync edit is propagation-only and writes no coverage | Fresh session, not the author, **code-first**. **Satisfied — APPROVED `2026-08-03`** by a fresh independent `Opus 5` session (`docs/07_operations.md` §5.5) |
| G-WRITE | C8 | Write-boundary preservation (fetch-before-connect, single commit). Any C7 diff that touches the write path is out of C7 scope and is rejected rather than reviewed here | Fresh session, not the author |
| G-TOOL | C10, C11 | A read-only tool whose output backs a correctness claim, and a tool that writes client business data | Fresh session, not the author |
| G-PROD | Every W2 stage transition, the first client enablement, and each W4 execution | Production authorization is an operator decision backed by evidence, not a code review | Fresh session, not the author, **plus** named operator authorization |

**Execution gate for the C11 recovery.** With G-SM approved and reviewer-model diversity withdrawn
as a requirement, the recovery-execution gate is **open**, subject only to the production
preconditions and the per-window operator authorization required by §12 and
`docs/07_operations.md` §5.5. No further code review is a precondition for that execution.

C4, C5, C9 and C12 take normal review. C5 is borderline (it changes dispatcher control flow); a
reviewer may escalate it to G-COV terms at their discretion.

For C5 review, acceptance additionally requires code-first proof that identity checks precede status
semantics; `READY` and `GAP_DETECTED` share one pure foundational validator; only a structurally valid
recorded gap receives the gap code; the malformed-gap matrix is bootstrap-required; and the
read-only guard shows no SQL write, row lock, dispatcher mutation path or new allowlist. These C5
criteria do not move any write out of C6 or weaken G-COV.

---

## 10. Deployment stages

### 10.1 Stage list and evidence

| # | Stage | Action | Evidence required to pass |
|---|---|---|---|
| 1 | **Schema deployed** | `ops/db_migrate.sh` applies 055, 056, 057 | `public.schema_migrations` shows the three filenames; `\d+` output for `client_account`, `client_dataset_coverage`, `client_schedule_run_history`; every existing `client_account` row reads `strict_meta`; **zero** rows in `client_dataset_coverage`; the old runtime still runs a normal dispatcher tick unchanged (§4.7) |
| 2 | **Runtime deployed, all clients strict** | Deploy C1–C11 code; no configuration change | Deployed commit SHA; `trips_pagination_mode = 'strict_meta'` for every client (read-only query); zero coverage rows; wrapper SHA-256 unchanged (`ae67a95f…`); environment identity and platform UUID re-verified |
| 3 | **One complete scheduled cycle observed** | Observe, change nothing | For every enabled `trips_sync` schedule, **≥ 2 consecutive fires** and **≥ 48 h**; each fire's `client_schedule_run_history` row shows the same `window_start_ts`/`window_end_ts` arithmetic as before deployment, `NULL` in all five new evidence columns, and the **same terminal status and abort classification as the pre-deployment baseline** (§10.3); ≥ 1 complete daily dispatcher cycle covering all other datasets (Eco Driving, aggregation, retention) with unchanged outcomes; any `logs.context` diff shows only additive keys (timestamps/run IDs/history IDs need not match) |
| 4 | **Bootstrap audit tool deployed** | Make C10 runnable on the host | Tool version/commit recorded; a dry inventory run against one schedule completes read-only; a write attempt is provably impossible (S13 evidence) |
| 5 | **First client bootstrap evidence prepared** | Run C10 for the candidate client; select `[A, W]`; reconcile; assemble the bundle | The complete `docs/13_…` §13.5 bundle: bounds and rationale, schedule identity, expected-segment inventory **including fires that produced no row**, backing `runs.run_id` values, per-sub-window request/page evidence, returned and unique identity counts, client-DB committed counts, gap reconciliation (each gap → recovery run **or** the `A` choice that excludes it), the §2.2 continuity proof, environment/repo identity, reviewer metadata, checksums |
| 6 | **First client mode enabled** | Insert the coverage row as `READY` **and** flip `trips_pagination_mode`, preferably in one reviewed platform-DB transaction | The transaction script, reviewed before execution; post-state read-back showing `READY`, both bounds, evidence ref, `seeded_at`, `seeded_by`; ticket recording client code, mode, operator and UTC timestamp |
| 7 | **First scheduled compatibility run observed** | Observe one fire | History row with distinct nominal and effective windows, `stabilization_delay_seconds`, `overlap_seconds`, mode; per-page logs with zero `overlap_count`, a termination reason and `total_reconciliation = exact`; `SUCCESS`; `coverage_start_ts` unchanged before and after; `covered_through_ts` advanced to `E_end`; client-DB row counts reconciled against the returned identity counts |
| 8 | **Rollback proof** | Deliberately exercise the documented rollback on the first client, then restore | The `UPDATE … SET trips_pagination_mode = 'strict_meta'` and the accompanying `bootstrap_status = 'RESEED_REQUIRED'`; evidence that the next fire used nominal windows and touched no coverage state; then a reviewed reseed back to `READY` and one further successful compat fire. **Rollback must be proven before wider rollout, not discovered during an incident.** |
| 9 | **Wider rollout** | One additional client at a time | Per client: its own §13.2 inventory, its own interval, its own bundle, its own review, its own ticket; plus §10.2 observation satisfied on the first client |

### 10.2 Minimum observation periods (decisions P9, P10)

- **Before first-client enablement (stage 5→6):** the disabled deployment must have been observed for
  **≥ 2 consecutive scheduled fires of every enabled `trips_sync` schedule and ≥ 48 hours**, plus
  **≥ 1 complete daily dispatcher cycle** across all other datasets. Rationale: two fires is the
  minimum that can show the *arithmetic between consecutive fires* is unchanged; 48 h covers a full
  daily cadence for the three daily schedules; the full dispatcher cycle catches collateral damage to
  Eco Driving, aggregation and retention.
- **Before broader rollout (stage 8→9):** the first client must have completed **≥ 7 consecutive
  successful scheduled compatibility fires** (≥ 7 days for a daily schedule), **including at least
  one fire whose effective window required more than one `/trips` page**, with `coverage_start_ts`
  unchanged on every fire and `covered_through_ts` strictly monotone, **plus** the stage-8 rollback
  proof. A weekly client (`BRAVO00016`) additionally requires **≥ 2 consecutive weekly fires**
  (≥ 14 days) before it is considered rolled out. Rationale: seven fires exercises a full week of
  provider behavior including weekend/weekday volume differences; the multi-page requirement is what
  actually tests the state machine rather than the single-page fast path; the weekly requirement
  exists because `BRAVO00016` is the tightest contiguity case in production.

### 10.3 Strict-mode deployment gate — semantic equivalence (not byte identity)

**Normative deployment invariant for W2 / stage 3:** disabled deployment must preserve **semantic
and code-path equivalence** for every client that remains `strict_meta`. It does **not** require
byte-identical logs, timestamps, run IDs, history IDs, evidence-bundle filenames, or incidental
`logs.context` key order.

Required, auditable equivalence:

1. the strict pagination algorithm (`_fetch_paginated`) remains unchanged;
2. strict clients still raise the same safety classifications on the same provider conditions;
3. the currently observed provider metadata defect still produces `PAGINATION_MISMATCH`;
4. no compatibility code path is entered for any strict client;
5. no coverage state is required, read, written or advanced for any strict client;
6. non-`/trips` workloads remain unaffected in outcome (terminal status / abort classification).

**Important and easy to misread.** In the current provider state, `strict_meta` `/trips` runs
**fail** with `PAGINATION_MISMATCH`. Stage 3 therefore verifies that the failure remains the same
classification with the same window arithmetic and the same history-row terminal shape — **not**
that ingestion succeeds, and **not** that log bytes or run identifiers match. Stage 3 additionally
requires at least one non-`/trips` dataset to complete successfully, proving the deployment did not
break anything that still worked. Anyone reading stage 3 as "production is healthy" or as
"byte-identical artifacts" has misread it.

Permitted under the gate: additive log keys, new nullable history columns remaining `NULL`, and
new unread schema objects. Forbidden under the gate: any change to strict abort classification,
window arithmetic for strict clients, coverage interaction while strict, or entry into
`data_invariants_v1` pagination.

---

## 11. First-client selection

### 11.1 Comparison

| Criterion | `DELTA00001` | `ALPHA00001` | `FOXTROT00001` | `BRAVO00016` |
|---|---|---|---|---|
| Timezone / fire | `Europe/Warsaw` 02:00 | `UTC` 02:00 | `UTC` 02:00 | `Europe/Warsaw` Mon 02:00 |
| Frequency | daily | daily | daily | **weekly** |
| Lookback `L` | **7 d** | 1 d | 1 d | 7 d |
| Slack (`L − Δ`) | **≈ 6 d** | **0** | **0** | **0** (weekly, `Δ = L`) |
| Contiguity margin with `O = 3600` | ≈ 6 d | +3600 s | +3600 s | **one grid step** — tightest in production |
| DST exposure | yes, absorbed by slack | none | none | yes, **pre-existing autumn gap (R5)** |
| Observed pagination evidence | **yes** — pages 1/2 and 2/3 disjoint at limit 25, page 3 short, `total = 58` stable | none | none | none |
| Volume signal | 58 trips/hour observed in one window | unknown | unknown | unknown |
| Current gaps | 2026-07-30, 07-31 missing; 2026-08-01 `FAILED` | same, **plus** an unrelated Workflow B failure | same | separate cadence; weekly window not yet inventoried |
| Downstream dependencies | `aggregate_trip_fuel_daily`, Eco Driving aggregates | Workflow B `Dysponent_ID` chain, `event_enrichment_mode = disabled` | Eco Driving aggregates | **Eco Person weekly aggregation + weekly email** (`BRAVO_ECO_WEEKLY_EMAIL_*`) |
| Email / aggregation blast radius | moderate | moderate, entangled with a live unrelated incident | moderate | **highest — a sent email cannot be corrected by re-sending** |

### 11.2 Recommendation

**Provisional first client: `DELTA00001`.**

1. It is the **only** client with production pagination evidence, and that evidence covers exactly
   the multi-page path the state machine implements.
2. Its 7-day lookback against a 24-hour cadence gives ≈ 6 days of slack, so a single bad fire is
   absorbed by the lookback alone, before the coverage-expansion mechanism is even consulted — the
   safest possible first exposure of an unproven advancement path.
3. Its DST exposure is real but fully absorbed by that slack, so the first client exercises the
   Warsaw conversion without depending on it.
4. Its downstream blast radius is aggregation and Eco Driving statistics, both recomputable — unlike
   `BRAVO00016`, whose weekly email is not.

**This selection is provisional until the C10 bootstrap inventory is complete.** It can change if the
inventory shows, for example, that `DELTA00001`'s recent history contains gaps that make an honest
narrow `[A, W]` impractical, or that its 7-day window regularly needs more pages than the budget
allows.

### 11.3 Why not the others

- **`ALPHA00001`** — zero contiguity slack, `event_enrichment_mode = disabled` (a different code
  path), no pagination evidence, and an **unrelated live Workflow B failure**. Enabling compatibility
  mode for a client with a concurrent unrelated incident would make attribution of any anomaly
  ambiguous. Excluded on incident hygiene alone.
- **`FOXTROT00001`** — zero slack, no evidence, no distinguishing advantage over `DELTA00001`. A
  reasonable **second** client precisely because its `L = Δ = 1 d` shape is the one that most needs
  the coverage-expansion mechanism proven first elsewhere.
- **`BRAVO00016`** — the tightest contiguity case in production, a pre-existing autumn DST gap
  (`docs/13_…` R5, independent of this work), weekly cadence (so each observation costs a week), and
  an irreversible email dependency. It must be **last**, and its R5 gap deserves its own ticket
  regardless of this plan.

---

## 12. Recovery ordering

**No recovery is authorized or executed by this plan.** The following is the safe *order*, to be
executed only under separate per-step authorization.

| # | Step | Precondition | Notes |
|---|---|---|---|
| R1 | **Compatibility-path validation** | Either §10 stage 7 passed for the client, **or** the C11 gate ordering of §3/C11 is satisfied for a controlled canary recovery | `docs/12_…` §14.1 step 4 still governs recovery of *historical* data with an unproven path. It does not govern the leading-edge interval of a canary whose only scheduled fire failed and cannot be re-fired — that case is exactly what the C11 controlled recovery exists for |
| R2 | **Missing-fire inventory** | C10 deployed | Read-only. Enumerate expected fires vs existing rows per schedule; translate into concrete `[window_start_ts, window_end_ts]` ranges; produce a reviewed inventory. Change nothing |
| R3 | **Recovery of 2026-07-30** | R1, R2, explicit authorization for that window | Historical-hole repair *behind* `A`. Dry-run first; `insert_only = true` via `backfill_trips_insert_only`; new run evidence only; terminal history rows untouched; coverage untouched, because the interval is not adjacent to `W` |
| R3b | **Controlled canary recovery at the leading edge** | §3/C11 gate ordering, migration `058` applied, dry-run reviewed, per-window authorization | `ops/recover_telematics_trips_window.py`; interval anchored exactly at `W`; runs the production sync once; advances `W` to `window_end_ts` only on `rc == 0`; terminal history rows untouched |
| R4 | **Recovery of 2026-07-31** | R3 verified | One range at a time, each verified before the next |
| R5 | **Recovery of the 2026-08-01 failed windows** | R4 verified | The terminal `FAILED` rows stay immutable — recovery is a *new* run, never a re-status |
| R6 | **Aggregate recalculation** | R3–R5 verified, row counts reconciled | `aggregate_trip_fuel_daily` for the affected days; daily fuel is derived from tank levels, so it must be recomputed after trips land, not before |
| R7 | **Eco Driving snapshots** | R6 complete | Re-derive affected weekly/monthly stats and rankings for the recovered periods; verify against the recovered trip counts |
| R8 | **Mailing dependencies** | R7 reviewed | **Emails are never a recovery mechanism.** Sends are idempotent by design; a message already delivered over incomplete data is a communication matter for the operator, not a re-send. `archive_only = true` exists solely to append an already-sent MIME to the Sent folder |
| R9 | **Coverage reconciliation** | R3–R7 verified | Widening `A` **backwards** to include recovered history is a **reviewed reseed** with a fresh evidence bundle — never an automatic consequence of recovery, and never performed by C11. Moving `W` **forwards** over an interval a C11 recovery itself fetched and committed is the R3b success finalization and needs no separate reseed |

Prohibited throughout: mutating terminal history rows; back-inserting missing fires; deleting
`client_trips` rows to "clean up"; automatic catch-up; automatic retry; re-sending Eco Driving
emails; ad hoc manual coverage SQL; advancing `W` over any interval the recovery did not itself
fetch and commit; recovering before R1.

---

## 13. Rollback matrix

| # | State | Rollback action | What survives | What must not be done |
|---|---|---|---|---|
| 1 | **Schema present, old runtime** | None needed | Everything; the columns are unread and the coverage table is empty | Do not `DROP` anything; do not roll back a migration |
| 2 | **New runtime, strict mode** | `git revert` the runtime commits and redeploy | Schema, all data | Do not revert migrations; do not "clean up" the new columns |
| 3 | **Bootstrap state prepared, mode still strict** | Set `bootstrap_status = 'RESEED_REQUIRED'` (or leave `UNINITIALIZED`) | The row and its evidence ref | Do not delete the coverage row; do not flip the mode "to test" |
| 4 | **First client enabled** | `UPDATE client_account SET trips_pagination_mode = 'strict_meta' WHERE client_code = '<CODE>'` **and** `UPDATE client_dataset_coverage SET bootstrap_status = 'RESEED_REQUIRED' WHERE schedule_id = '<ID>'`, one reviewed change | Coverage bounds as a record of what was covered; all history | Do not delete the coverage row; do not roll back schema; do not kill an in-flight run without noting that its single-commit boundary means it wrote nothing |
| 5 | **Scheduled compatibility run fails** | Read `logs.context.abort_code`. Critical codes (`_PAGE_OVERLAP`, `_PAGE_REPEATED`, `_ROWS_EXCEED_LIMIT`, `_TOTAL_EXCEEDED`, `_TOTAL_RECONCILIATION_FAILED`) ⇒ roll back to strict per row 4 and escalate. Retryable codes (`_TOTAL_UNSTABLE`, `_ELAPSED_BUDGET_EXCEEDED`, `_WINDOW_INELIGIBLE`) ⇒ wait for the next fire | The `FAILED` history row as the incident record; coverage unchanged (advancement never ran) | Do not re-status the failed row; do not re-fire the same `scheduled_fire_ts` (the UNIQUE key makes a fire single-shot) |
| 6 | **Gap-status row** (`GAP_DETECTED`) | First inspect read-only. If identity/bounds/evidence/seed metadata are valid, leave it loud, inventory the hole (C10), decide recover-or-exclude, then perform a reviewed reseed. If malformed, treat it as bootstrap-required and escalate for reviewed bootstrap/reseed/repair. | The unchanged row and all history evidence | Do not retry, fall back to strict, advance `W`, force `READY` or treat the status string alone as proof. C5 writes nothing; C6/G-COV owns future mutations. |
| 7 | **Provider behavior changes again** | Roll every enabled client back to strict per row 4; re-run the diagnostic (`ops/diagnose_telematics_trips_pagination.py`) under separate authorization; escalate to the provider | All evidence | Do not adjust invariants to accommodate new provider behavior without a new design gate |
| 8 | **Switch back to strict** | Row 4's two statements, together | Coverage row (now stale, marked `RESEED_REQUIRED`) | Do not re-enable later without revalidation — a stale `W` understates coverage and was proven against a configuration that may no longer hold |
| 9 | **Stale watermark** | Set `RESEED_REQUIRED`; run C10 inventory; select a fresh `[A, W]`; reviewed reseed with a new bundle | Historical evidence | Do not reuse the old `bootstrap_evidence_ref`; the `CHECK` detects a *missing* reference, never a *stale* one |
| 10 | **Recovery partially completes** | Stop. Record which ranges completed. Because writes are `insert_only` and keyed on `(client_id, provider_trip_id)`, a partial recovery is **resumed**, never undone | Every committed range; all run evidence | Do not delete recovered rows; do not advance coverage for a partially recovered interval; do not proceed to R6–R8 with an incomplete range set |

---

## 14. Explicit non-goals

- **N1** No implementation in the planning task that produced this document — no runtime code, no
  migration, no test, no tooling.
- **N2** No client enablement. `trips_pagination_mode` stays `strict_meta` for every client until
  §10 stage 6, which is a separate authorized operation.
- **N3** No bootstrap execution. No coverage row is created, seeded or modified.
- **N4** No backfill and no recovery execution.
- **N5** No mutation of terminal `SUCCESS` or `FAILED` rows in `client_schedule_run_history` or
  `runs`, ever, by any commit or operation in this plan.
- **N6** No attempt to diagnose or fix the ALPHA Workflow B business failure.
- **N7** No global fallback to compatibility mode — no ENV switch, no host-wide default, no implicit
  "retry loosely on `PAGINATION_MISMATCH`", no automatic re-enable.
- **N8** No disabling, weakening or raising of any existing safety budget. New budgets may only
  reduce what is already permitted (`CURRENT_TASK_CONTEXT.md` §5.2 — the defaults are a contract).
- **N9** No change to the strict path's semantic / code-path behavior (§10.3).
- **N10** No application of compatibility mode to any endpoint other than `/trips`.
- **N11** No change to `db/client_business/`, to the environment-identity contract, to migration 054,
  to systemd units, to `.env` or to `/etc/log-platform/**`.

---

## 15. Prompt map

The one hard invariant is **process-based**: a review must run in a **fresh session**, must not be
performed by the author of the change, and must not modify the implementation under review (§9).
**The `Model` column below is a non-binding recommendation, not a requirement**, and reviewer-model
diversity is optional — a `-R` row may be executed by the same model that implemented its commit.
As a heuristic only, heavy architectural-judgment work suits `Opus 5` / `GPT 5.6 Sol`, mechanical
schema and configuration work suits `GPT 5.6 Terra`, and adversarial verification suits `Grok 4.5`.
Rows that are already delivered record the model that actually performed the work as historical
provenance; that provenance never re-opens a completed gate.

| Commit | Model (recommended, non-binding) | Task objective | Required input context | Write authorization | Forbidden actions | Expected classification |
|---|---|---|---|---|---|---|
| **C1** | `GPT 5.6 Terra` | Add migration 055 + frozen config field + fail-closed validation | This plan §3/C1, §4, §5.1; `docs/12_…` §3.2–§3.3; `control_plane.py`; `018_*.sql`; `jobs/trip_metrics_population_source.py` | `db/migrations/055_*`, `control_plane.py`, `jobs/trips_pagination_mode.py`, one new test, `docs/05_jobs.md` | Any other migration; any `jobs/api/telematics/*` runtime; applying the migration; any DB write | `TELEMATICS_PAGINATION_MODE_CONFIG_COMMIT_READY` |
| **C1-R** | `Grok 4.5` | Independent migration review (G-MIG) | The diff; §4; `CONVENTIONS.md` §12 | None | Implementing fixes itself | `TELEMATICS_PAGINATION_MODE_CONFIG_REVIEW_*` |
| **C2** | `GPT 5.6 Terra` | Migration 056 + three numerics + bounds + the §5.5 schedule-coupling validator | §3/C2, §4, §5.2–§5.5; `docs/13_…` §16.3 | `db/migrations/056_*`, `control_plane.py`, one new test, `docs/05_jobs.md` | Migrations 055/057; dispatcher; applying anything | `TELEMATICS_STABILIZATION_CONFIG_COMMIT_READY` |
| **C3** | `Opus 5` | Migration 057: coverage table + history evidence columns + the two `CHECK`s | §3/C3, §4, §6; `docs/13_…` §5.5, §5.2.1; `012_*`, `014_*` | `db/migrations/057_*`, one new PostgreSQL test, `docs/05_jobs.md` | Any Python runtime file; any coverage row insert; applying the migration | `TELEMATICS_COVERAGE_STATE_SCHEMA_COMMIT_READY` |
| **C3-R** | `GPT 5.6 Sol` | Independent migration review (G-MIG), constraint-by-constraint | The diff; §6; `docs/13_…` §5.2.1, §5.5, §13 | None | Relaxing any `CHECK` | `TELEMATICS_COVERAGE_STATE_SCHEMA_REVIEW_*` |
| **C4** | `Opus 5` | Pure window arithmetic + connectivity result + exhaustive UTC/DST tests, including `E_start == E_end` and no inverted interval | §3/C4, §7; `docs/13_…` §2 closed intervals, §4, §11, §16.1, §18; delivered C3 inertness guard | `jobs/api/telematics/coverage_windows.py`, `ops/tests_manual/test_telematics_trips_stabilization_windows.py`, `ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` only for the narrow pure-vocabulary/runtime-import guard adaptation, `docs/05_jobs.md` | Gate types/evaluation; `dispatcher.py` or any other production runtime integration; coverage loading or READY enforcement; any DB or network access; any migration; operator procedure or enablement instruction | `TELEMATICS_WINDOW_DERIVATION_COMMIT_READY` |
| **C5** | `GPT 5.6 Sol` | Add the pure coverage-gate types/evaluation and wire them with C4 arithmetic into the dispatcher | §3/C5, §7; `docs/13_…` §5.2.1, §14, §16.1; `coverage_windows.py`; `dispatcher.py` | `coverage_windows.py`, `dispatcher.py`, two tests, `docs/05_jobs.md`, `docs/07_operations.md` §5.5 | Provider client; sync job; migrations; enabling any client | `TELEMATICS_COVERAGE_BOOTSTRAP_GATE_COMMIT_READY` |
| **C6** | `Opus 5` | Add `_finalize_compat_gap` and `_finalize_compat_success`; preserve strict `_finalize_run`; widen and retain the single claim snapshot | §3/C6, §6.5, §7.1, §8/S6/S11; `docs/13_…` §5.3; full `docs/15_…` | `dispatcher.py`; `coverage_windows.py` only for two carrier fields; `test_telematics_coverage_state_schema_postgres.py` for guard rescope; `test_telematics_coverage_bootstrap_gate.py` and `test_workflow_a_dispatcher.py` when fixtures/retention/isolation require them; both advancement tests; `docs/07_operations.md` §5.5 | Overloading strict `_finalize_run`; strict coverage SQL; writing `coverage_start_ts`; provider files; migrations | `TELEMATICS_COVERAGE_ADVANCEMENT_COMMIT_READY` |
| **C6-R** | `Grok 4.5` | Independent G-COV review: transactions, monotonicity, retained-snapshot provenance, dual-DB failure | Implementation diff; relevant `docs/13_…` invariants; docs/14 C6 ownership/delivery; `docs/15_telematics_coverage_mutation_contract.md` in full; migration 057; focused C5/C6 tests; guard changes | None | Accepting pre-client-commit advancement, coverage mutation before locked `RUNNING` history verification, finalization-reread expected values, or review without the authoritative contract | `TELEMATICS_COVERAGE_ADVANCEMENT_REVIEW_*` |
| **C7** | `Opus 5` | Compatibility state machine + budgets + accepted `total` taxonomy + essential per-page telemetry + minimum mode propagation | §3/C7, §8; **the accepted D5 decision `docs/16_…` §5–§7**; `docs/12_…` §4–§8, §10, §13 | `provider_client.py`, `provider_safety.py`, `sync_trips_and_speeding.py` (propagation only), `jobs/trips_pagination_mode.py` (narrow extension only), the new compatibility test plus narrow provider/pagination and sync-propagation test updates, `docs/02_infrastructure.md`, `docs/05_jobs.md`, `docs/07_operations.md` §5.4 | Re-deciding or re-opening D5; creating `PAGINATION_COMPAT_TOTAL_ABSENT`; editing `_fetch_paginated`; touching any non-`/trips` fetch; dispatcher; coverage code; any write-boundary, `ON CONFLICT` or coverage change; any client config; any migration; any live request | `TELEMATICS_COMPAT_STATE_MACHINE_COMMIT_READY` |
| **C7-R** | **DONE — performed by a fresh independent `Opus 5` session (`2026-08-03`)** | Independent review (G-SM), **code before design**, adversarial on the invariants | The diff; then `docs/12_…` §5–§8 | None | Treating the prior documentation review as sufficient | `TELEMATICS_COMPAT_STATE_MACHINE_REVIEW_*` — delivered **APPROVED** |
| **C8** | `GPT 5.6 Sol` | Broader sync integration, write-boundary preservation, pre-commit duplicate assertion, eligibility guard | §3/C8, §7; `docs/12_…` §9, §6.4; `docs/16_…` §7.2; `sync_trips_and_speeding.py` | `sync_trips_and_speeding.py`, one new test, `docs/05_jobs.md` | Re-implementing the C7 mode propagation; streaming or incremental writes; a second commit; changing `ON CONFLICT` targets; changing any client config | `TELEMATICS_COMPAT_SYNC_INTEGRATION_COMMIT_READY` |
| **C8-R** | `Grok 4.5` | Independent review (G-WRITE): fetch-before-connect and single-commit still hold | The diff; §7.1 steps 8–10; `docs/12_…` §9 | None | Approving any intermediate commit or savepoint | `TELEMATICS_COMPAT_SYNC_INTEGRATION_REVIEW_*` |
| **C9** | `GPT 5.6 Terra` | Cross-cutting telemetry + the privacy/redaction suite | §3/C9, §8/S12; `docs/12_…` §10; `docs/06_security.md` | `dispatcher.py`, `sync_trips_and_speeding.py`, `provider_client.py` (summary log only), one new test, `docs/06_security.md` | Any control-flow change; logging any raw identity or personal field | `TELEMATICS_COMPAT_TELEMETRY_COMMIT_READY` |
| **C10** | `Opus 5` | Read-only bootstrap inventory tool + sanitized bundle | §3/C10, §8/S13; `docs/13_…` §13.2, §13.5 | `ops/audit_telematics_coverage_bootstrap.py`, one new test, `docs/07_operations.md`, `docs/09_disaster_recovery.md` | Any write of any kind; any provider request; any job launch; recommending a `READY` decision | `TELEMATICS_BOOTSTRAP_AUDIT_COMMIT_READY` |
| **C10-R** | `Grok 4.5` | Independent review (G-TOOL): prove read-only, prove missing-fire enumeration | The diff; §8/S13 | None | Accepting `SUCCESS`-row-only or `max(synced_at)` inference | `TELEMATICS_BOOTSTRAP_AUDIT_REVIEW_*` |
| **C11** | `Opus 5` | Dry-run-default manual compatibility recovery with its own durable identity, execution refused without explicit authorization | §3/C11, §12; `docs/12_…` §14; `docs/15_…` §4–§5 | `db/migrations/058_…`, `jobs/api/telematics/coverage_finalization.py`, `ops/recover_telematics_trips_window.py`, two new tests, guard rescope, `docs/13_…`/`14_…`/`15_…`/`07_operations.md`/`05_jobs.md` | Executing a recovery; applying migration `058` to production; mutating any schedule-history row; touching `coverage_start_ts`; back-inserting a fire; automatic retry | `TELEMATICS_RECOVERY_TOOLING_COMMIT_READY_EXECUTION_UNAUTHORIZED` |
| **C11-R** | `GPT 5.6 Sol` | Independent review (G-TOOL): authorization gate, idempotency, immutability | The diff; §12; §13 row 10 | None | Approving a default-execute path | `TELEMATICS_RECOVERY_TOOLING_REVIEW_*` |
| **C12** | `GPT 5.6 Terra` | Consolidated runbook after observed behavior | §10, §12, §13; the deployment and first-run evidence bundles | `docs/07_operations.md`, `docs/09_disaster_recovery.md`, `docs/06_security.md`, `docs/05_jobs.md`, status notes in `docs/12_…`/`13_…` | Any code, schema or ops file; documenting unobserved behavior as observed | `TELEMATICS_COMPAT_RUNBOOK_COMMIT_READY` |

These are prompt **specifications**, not the prompts themselves. Each future task is generated from
its row plus the referenced plan sections, without rediscovering architecture.

---

## 16. Required decisions, stated

Plan decisions are numbered **P1–P13** to keep them distinct from the open decisions **D1–D9** of
`docs/12_…` §17.

| # | Decision | Resolution |
|---|---|---|
| **P1** | Exact commit split | **Twelve commits, C1–C12**, as enumerated in §3, with the six deviations from the candidate split justified in §3.13. |
| **P2** | Migration split | **Three migrations: 055, 056, 057**, across C1, C2 and C3 (§4.1). Ordinals verified against the real ceiling `054_environment_identity_resume_contract.sql`. |
| **P3** | Where the pagination mode is stored | **`workflow_a_control.client_account.trips_pagination_mode`** — client-scoped, `NOT NULL`, `CHECK`-constrained, default `strict_meta` (§5.1, §5.5). Not ENV, not job-parameter-primary, not schedule-scoped. |
| **P4** | Where stabilization values are stored | **All three on `client_account`** (§5.5), matching `docs/13_…` §16.3, **with a mandatory schedule-coupling validator** (`Δ_max ≤ L + O + 1 s` for the client's enabled `trips_sync` schedule) added in C2 to convert the one real counter-argument into a fail-closed check. |
| **P5** | Is telemetry part of the state-machine commit | **Partly, and deliberately.** Per-page evidence, identity digests, HMAC salt lifecycle and all abort contexts ship **inside C7**. Only cross-cutting telemetry (coverage transitions, run-level summary) and the redaction suite are deferred to C9 (§3/C9). |
| **P6** | Provider state machine and sync integration — one commit or two | **Two: C7 and C8** (§3/C8 note), with the boundary corrected by `docs/16_…` §7: C7 owns the propagation-only sync edit that makes the state machine executable; C8 owns the write-boundary and broader integration work. Different review questions, independent rollback. |
| **P7** | Recovery tooling before or after first scheduled validation | **Implemented before (C11, after C10). Execution is gated by §3/C11, not by a prior successful scheduled fire.** The original ordering assumed recovery only ever repairs *historical* data behind `A`; that still holds for R3–R5. It does not hold for a leading-edge interval whose only scheduled fire failed and cannot be re-fired, where waiting for a successful scheduled run over that same interval is impossible. The bootstrap still avoids the historical circularity by choosing a `coverage_start_ts` **after** the known 2026-07-30/31 hole (§2.3). |
| **P8** | First provisional client | **`DELTA00001`**, provisional until the C10 inventory is complete (§11.2). Second candidate `FOXTROT00001`; `BRAVO00016` last; `ALPHA00001` excluded while its unrelated Workflow B incident is open. |
| **P9** | Minimum observation before client enablement | **≥ 2 consecutive scheduled fires of every enabled `trips_sync` schedule and ≥ 48 h of disabled deployment, plus ≥ 1 complete daily dispatcher cycle** across all other datasets (§10.2). |
| **P10** | Minimum observation before broader rollout | **≥ 7 consecutive successful scheduled compatibility fires on the first client (≥ 7 days), including ≥ 1 multi-page fire, with `coverage_start_ts` unchanged and `covered_through_ts` monotone, plus the stage-8 rollback proof.** Weekly clients additionally require ≥ 2 consecutive weekly fires (≥ 14 days) (§10.2). |
| **P11** | Authoritative runtime ordering | **§7.1** — evaluate → nominal window → read-only coverage → pure gate → durable claim → branch. Newly disconnected `READY` uses the pre-launch C6 gap transaction; non-persisting rejections mutate no coverage; allowed `rc == 0` uses the separate C6 success transaction. Strict `_finalize_run` remains unchanged. |
| **P12** | D5 handling | **Resolved — Option B, accepted.** `docs/16_telematics_d5_total_policy_decision.md` (ACCEPTED 2026-08-03) is the separate architecture decision this plan required: an absent `meta.total` is a permitted compatibility state, termination is the authoritative short-page rule, a present `total` is strictly validated and reconciled to exact equality, and the taxonomy is the four codes of `docs/16_…` §5.3. C7 is unblocked and must implement that decision as written; C7 coding and review must not re-decide it (§2.2). The plan's earlier Option-C stance and its non-binding Option-A preference are superseded history. |
| **P13** | Strict-mode deployment gate wording | **Semantic / code-path equivalence** per §10.3 — not byte-identical logs, timestamps, run IDs, history IDs or evidence bundles. |

**Closed since this plan was written:** `docs/12_…` **D5** (absent `meta.total`) and the matching row
**U8 of §17 below** — resolved by the accepted decision
`docs/16_telematics_d5_total_policy_decision.md` (Option B, ACCEPTED 2026-08-03). C7 is no longer
blocked, and no `PAGINATION_COMPAT_TOTAL_ABSENT` code may be created. Note the numbering divergence:
`docs/12_…` §0.3 numbers its own **U8** as "all clients can safely enable the mode immediately", which
is a rollout unknown and remains open.

**Still open, and gating enablement rather than merge:** `docs/12_…` D2 (page limit in compatibility
mode), D3, D4, D8, D9; the provider unknowns **U3–U7 of §17 below**; and all of `docs/12_…` §0.3
U3–U8, which gate enablement (§10 stages 6–9) and never merge.

---

## 17. Unresolved provider evidence as explicit gates

Except for U8, which the accepted decision `docs/16_telematics_d5_total_policy_decision.md` closes, none
of the following is treated as solved anywhere in this plan.

| Unknown | Status | Gate |
|---|---|---|
| Short-page termination (U1/U2) | **Confirmed for one window only** — pages 1/2 and 2/3 disjoint at limit 25, page 3 returned the expected short page, `total = 58` stable in that closed window | Sufficient to *implement* C7; **not** sufficient to enable a second client or a larger window |
| **U3 — ordering stability across larger and recent windows** | **Open** | §10 stage 7 must include ≥ 1 multi-page fire; any `_PAGE_OVERLAP`/`_PAGE_REPEATED` stops the rollout (§13 row 5) |
| **U4 — larger-window disjointness** | **Open** | P10 requires a multi-page fire before wider rollout; per-client review of fleet size and window length at each rollout step |
| **U5 — `total` stability across clients and windows** | **Open** | `docs/12_…` §5.3 invariant C5 / `_TOTAL_UNSTABLE` is retryable-once, then widen `D`; every new client's first fires are reviewed for `total` behavior |
| **U6 — behavior across other clients, credentials and regions** | **Open** | Per-client enablement only; each client gets its own inventory, bundle and review; a separately authorized live probe (§8 S18) if a client's behavior looks different |
| **U7 — provider mutation during pagination** | **Open, and partly undetectable** — disjointness proves no duplication, never completeness | The stabilization delay `D` and the closed-window eligibility rule are the mitigation; `total` reconciliation is the only observable signal; documented as a residual risk in C12 |
| **U8 — missing or malformed `total`** (this table's numbering; `docs/12_…` §0.3 uses U8 for the separate "enable all clients" unknown, which stays open) | **Closed — it was D5, now decided** | Resolved by `docs/16_telematics_d5_total_policy_decision.md` (Option B, ACCEPTED 2026-08-03): absent `total` is permitted and never aborts, a present `total` is strictly validated/stability-checked/bound-checked and reconciled to exact equality, and the taxonomy is fixed to the four `docs/16_…` §5.3 codes. Gates nothing; C7 is authorized. |

---

## 18. Non-authorizations

This plan authorizes **nothing**. It does not authorize a migration application, a deployment, a
mode flip, a coverage-row insert, a bootstrap, a recovery, a backfill, a provider request, a
dispatcher run, a `trips_sync` run, a systemd change, an email send or a push. Every stage in §10 and
every step in §12 requires its own review and its own explicit operator authorization at the time it
is performed.
