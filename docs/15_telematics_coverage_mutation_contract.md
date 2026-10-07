# Telematics coverage mutation contract — C6 / C11 / G-COV transaction design

**Status.** C6 is implemented and deployed. The production platform migration ceiling is
`062_workflow_a_multi_cadence_schedule_identity.sql`; `055`–`062` are applied, so the coverage
amendments of §4.0 are in force. **Five** production coverage rows exist — `BRAVO00016`,
`ALPHA00001`, `FOXTROT00001`, `DELTA00001` and `ECHO00001`, all `READY`. C11 — the reviewed
manual-recovery surface and the shared advancement CAS it introduces — **has executed in
production**: migration `058` is applied and `ECHO00001` carries
`covered_through_source = 'manual_recovery'`, which only the C11 path writes. No coverage writer
beyond the two surfaces named in §2 is authorized, and adding one requires a fresh independent
G-COV approval.

**Production runtime.** M5 (migration 062) is `PRODUCTION_COMPLETE` as of 2026-08-17; the active
release is `ac13457a6a99`, a strict descendant of M5's `b682df90c958` with a byte-identical
Workflow A runtime surface. Multi-cadence identity is structurally enabled but **no reconciliation
schedule exists**: all 49 production schedules carry the base role `run_type = 'DAILY'`. The
reconciliation cadences referenced throughout §4.0 remain M6/M7 future work. See
`docs/20_telematics_ingestion_permanent_repair_plan.md` §23, §23.8.

## 1. Documents and authority

The active documents divide responsibility without a precedence rule:

- `docs/13_telematics_trips_stabilization_windows.md` supplies the coverage-domain invariants;
- `docs/14_telematics_trips_compatibility_implementation_plan.md` supplies commit ownership and the
  delivery sequence;
- this document supplies the exact C6 transaction, CAS, crash-reconciliation, error and test
  contract.

All three place a newly detected gap after the durable history claim and before launch, and place
successful `W` advancement only after `rc == 0`.

> **Amended by M3, production-verified 2026-08-14.** `rc == 0` is still necessary and is no longer
> sufficient. `docs/20_telematics_ingestion_permanent_repair_plan.md` §20 adds a fourth authority
> over the *entry* to advancement: on a scheduled compatibility fire the dispatcher must also
> accept the child's terminal execution record — present, strictly parsed, verified against this
> exact claim, and coverage-eligible — before `_finalize_compat_success` is called at all. Read
> every "after `rc == 0`" below as "after `rc == 0` **and** an accepted execution outcome". The
> transaction, CAS, claim-loss, reconciliation and error contracts in this document are
> **unchanged** by M3: the gate is evaluated before the finalizer opens its transaction, so a
> refusal performs no coverage SQL whatsoever. See §5.

> **Historical snapshot — superseded, retained deliberately.** The paragraph below records the
> environment as it stood at the M3 correction round. It is **not** current: migrations `055`–`062`
> are now applied, the coverage table exists with five `READY` rows, and the migration ceiling is
> `062`. See the status header above for current state. It is kept because the reasoning of this
> document was written against it.

Repository and production identity were reverified for this correction: repository
`/opt/log-platform`, branch `main`, starting HEAD
`f0e9ab8dc20d17ced2396c81261bd61714cc00de`, remote `origin/main`
`4ac6ee9f5c713ead15de3e729ff7a722cafedd77`, ahead 3/behind 0, clean worktree, environment
`production`, platform UUID `52517750-7438-4558-8490-2736ae4cc629`, production migration ceiling
`054_environment_identity_resume_contract.sql`, repository migration ceiling
`057_workflow_a_trips_coverage_state.sql`, and installed wrapper SHA-256
`ae67a95f3c517a7d306a1e5fea0d4af4ca4dd7ce2f8feac86d011bd03f1f7bfa`.
Authorized read-only production inspection confirmed migrations 055–057 absent, the coverage table
absent, the five C3 history-evidence columns absent, compatibility columns absent, zero compatibility
clients and therefore zero production coverage rows.

## 2. Ownership and strict isolation

C5 owns coverage loading and pure gate enforcement read-only. Without C6, C5 claims each rejected
fire and finalizes only that history row `FAILED`; it never inserts, updates, deletes or locks a
coverage row.

C6 adds exactly two compatibility-only finalization surfaces:

- `_finalize_compat_gap`: newly disconnected valid `READY` only, post-claim and pre-launch;
- `_finalize_compat_success`: allowed compatibility fire only, after `rc == 0` **and, since M3, an
  accepted execution outcome** (`_require_coverage_eligible_outcome`, evaluated before this
  function is entered).

### 2.0 C11 — the second advancement caller, one contract

C11 (`ops/recover_telematics_trips_window.py`) adds a **reviewed manual recovery** that may also
advance `W`. It is a narrow extension of this contract, not a second mutation model, and it is
enforced structurally rather than by discipline:

- the low-level advancement is extracted once into
  `jobs/api/telematics/coverage_finalization.advance_covered_through_cas`, together with the one
  authorized coverage lock `lock_coverage_row_for_update`;
- `_finalize_compat_success` and the C11 recovery are its **only** callers. Both bind the same
  eleven `claim_*` values from an immutable claim-time snapshot taken before the business work ran,
  both use the same null-safe predicate, the same strict monotonicity guard and the same
  `TRIPS_COVERAGE_ADVANCE_CONFLICT` classification;
- the only difference is the written provenance:
  `covered_through_source = 'scheduled_run'` for the scheduled path and `'manual_recovery'` for the
  reviewed recovery. The value is a bound parameter, never an SQL literal, so one caller cannot
  write the other's provenance;
- C11 additionally sets `require_advance`, so a recovery window that cannot move `W` is a refusal
  rather than a silent no-op. The scheduled path keeps its validated no-op (§5.2);
- C11 never writes `coverage_start_ts` or `bootstrap_status`, never inserts or deletes a coverage
  row, and never performs the gap transition — `_finalize_compat_gap` remains the sole owner of
  `READY → GAP_DETECTED`;
- C11 never creates, edits, deletes, retries or reclassifies a `client_schedule_run_history` row.
  Its identity and evidence live in `workflow_a_control.client_dataset_recovery_run`
  (migration `058`), and its lock order is
  `client_account → client_dataset_schedule → coverage → recovery run` — coverage before the
  owning identity row, exactly as the scheduled path locks coverage before history;
- provider, business, orchestration or transaction failure never reaches the finalizer at all, so
  no coverage statement is issued and the row stays byte-identical;
- nothing is ever retried automatically.

**Therefore, after C11, exactly two reviewed surfaces may advance `W`:** scheduled compatibility
success finalization and C11 manual-recovery success finalization. Ad hoc **manual coverage SQL**
remains forbidden in every environment, and no job, script, API route or provider module may write
coverage at all.

Existing strict/non-trips `_finalize_run` retains its current body, call shape and transaction. It
executes no coverage SQL. Shared low-level SQL utilities are permitted only when they preserve this
literal separation and leave transaction ownership visible at each compatibility call site.
Overloading `_finalize_run` with coverage arguments is prohibited.

### 2.1 Narrow static-guard transition

C11 re-scopes the guard once more, in the same narrowing direction. After C11 the guard permits a
coverage statement only in:

- `ops/bootstrap_telematics_trips_coverage._insert_initial_coverage_row` — the one-shot `INSERT`;
- `coverage_finalization.lock_coverage_row_for_update` — the one advancement lock;
- `coverage_finalization.advance_covered_through_cas` — the one advancement `UPDATE`, whose shape is
  validated (it must `SET covered_through_ts`, must bind `covered_through_source` as a parameter
  rather than inline any provenance literal, and must never `SET coverage_start_ts` or
  `SET bootstrap_status`);
- `dispatcher._finalize_compat_gap` — the gap transition, unchanged.

`_finalize_compat_success` is deliberately **removed** from the authorized set: it now delegates, so
a coverage statement reappearing there is itself a guard failure. The recovery CLI is required not
to name the coverage table at all, which proves statically that it cannot hold a coverage statement
of its own. Every other production module — provider, sync, API, scripts, unknown future packages —
stays forbidden by default.

The C6 implementation must re-scope, not remove, the C5 static guard. It may:

- expose `covered_through_source` and `last_gap_detected_ts` as plain immutable fields in
  `coverage_windows.py`;
- select and retain them in `dispatcher.py`;
- execute coverage `FOR UPDATE` and conditional coverage `UPDATE` only inside the separate
  `_finalize_compat_gap` and `_finalize_compat_success` functions;
- assign `last_gap_detected_ts` and coverage `updated_at` only inside the gap finalizer;
- assign `covered_through_ts`, `covered_through_source` and coverage `updated_at` only inside
  the success finalizer when `W` moves.

It must continue to forbid coverage writes in `coverage_windows.py`, provider code, the sync job,
API routes, scripts/onboarding tools and unknown future production packages; C7/provider pagination
integration; and broad mutation from strict/non-trips finalization. Synthetic guard tests must prove
the approved dispatcher finalizers pass, the same SQL elsewhere fails, and widening the pure helper
does not authorize writes or I/O.

C6 never writes `coverage_start_ts`, creates a row, deletes a row, changes identity, transitions a
row into `READY`, performs bootstrap/reseed/recovery/backfill, or replays a fire. Migration 057 is
sufficient; migration 058 is not required. `docs/12_…` D5 is resolved by
`docs/16_telematics_d5_total_policy_decision.md` (Option B, ACCEPTED 2026-08-03); it blocks nothing, and
C7 — now including its minimum mode propagation through `sync_trips_and_speeding.py` — remains a
forbidden coverage writer.

## 3. Authoritative compatibility sequence

1. Evaluate the due fire `F`.
2. Calculate nominal `[N_start, N_end]`.
3. Execute one lock-free, schedule-keyed, read-only coverage `SELECT`. Widen the current C5
   projection from ten to twelve fields by adding `covered_through_source` and
   `last_gap_detected_ts`; do not add a second pre-claim coverage statement.
4. Construct one immutable 12-field `CoverageState`, evaluate the pure C5 gate (which may ignore
   the two added audit/provenance fields), and retain that same object in `PreparedDispatcherRun`.
5. Claim history `RUNNING`, using the nominal window for rejection or effective window for launch.
6. Branch:

### Branch A — bootstrap-required or non-persisting rejection

Applies to missing or malformed coverage, `UNINITIALIZED`, `RESEED_REQUIRED`, malformed
`GAP_DETECTED`, and an existing structurally valid `GAP_DETECTED`.

Finalize only the claimed history row `FAILED`; emit the bounded structured log and suspected-bug
report; mutate no coverage; launch no subprocess. A structurally valid existing `GAP_DETECTED`
remains loud but is not rewritten. Malformed gap state is never normalized.

### Branch B — newly detected disconnected `READY`

Applies only when `gate.abort_code == TRIPS_COVERAGE_GAP_DETECTED` and
`requires_gap_persistence == true`.

After the committed claim, `_finalize_compat_gap` atomically changes coverage
`READY → GAP_DETECTED` and history `RUNNING → FAILED`, then commits. It runs before
`_build_job_params`, subprocess construction, credential resolution, sockets and provider/client
access. Structured logging and suspected-bug reporting follow the transaction. No subprocess runs.
This transaction is separate from successful finalization.

### Branch C — allowed gate

Launch the subprocess with the effective claimed window and observe `rc`.

- `rc != 0`: history `RUNNING → FAILED`; coverage unchanged.
- `rc == 0` **but the execution outcome is not accepted** (M3): history `RUNNING → FAILED` with a
  deterministic `error_summary` naming the refusal code; coverage unchanged and **no coverage SQL
  is issued at all**, the decision being taken before the finalizer opens its transaction.
- `rc == 0` **and the execution outcome is accepted**: `_finalize_compat_success` locks and
  validates coverage, locks the claimed history row and verifies it remains `RUNNING`, only then
  advances `W` when needed (or executes no coverage `UPDATE` for a no-op), changes that same
  history row `RUNNING → SUCCESS`, and commits both decisions atomically.

No compatibility `SUCCESS` exists without the corresponding atomic coverage decision. No database
row lock crosses subprocess execution. The global row-lock order is coverage row → history row;
this defines row-lock order, not mutation order. Coverage mutation is forbidden until both rows are
locked and the retained coverage snapshot and `RUNNING` history claim are validated.

## 4. Coverage field invariants and authoritative claim-time carrier

### 4.0 Coverage ownership — amended by M5 (migration 062)

**A coverage row is owned by `(client_id, dataset_name)`.** It is a fact about a
dataset's forward completeness, not about one schedule, and every cadence
registered over that dataset — the base schedule, and the reconciliation
schedules M6/M7 will add — shares exactly one row, one `FOR UPDATE` lock and one
compare-and-swap. `uq_client_dataset_coverage_dataset UNIQUE (client_id,
dataset_name)` makes that structural rather than conventional.

`schedule_id` remains on the row and remains one of the eleven CAS fields, but
its role is now stated explicitly, because leaving it ambiguous is what would let
a future reader treat it as a pointer:

* it is **immutable originating-schedule provenance** — the schedule that seeded
  the watermark. It is written exactly once, by the bootstrap writer;
* **no advancement, gap transition or recovery ever rewrites it.** It does not
  track "last advancer" and must not be read as such. The two authorized UPDATE
  shapes (§7.2, §7.5) do not name it in their `SET` lists, and that omission is
  load-bearing;
* it is therefore stable enough to remain a unique key, which is what allows a
  pre-M5 release — whose CAS addresses coverage by `schedule_id` — to keep
  resolving exactly one row after migration 062. Rollback needs no schema
  downgrade **for as long as the physical shape 062 installs is in force**; no
  claim here extends past a later migration that changes coverage identity again.

**Owner/provenance coherence is structural, not conventional.** `coverage.schedule_id`,
`coverage.client_id` and `coverage.dataset_name` are bound together by a composite foreign key
against `uq_client_dataset_schedule_owner_identity`, so the anchoring schedule provably belongs to
the same owner. Migration 057's single-column FK proved only that *some* schedule existed; before M5
the gate's firing-schedule equality check masked the difference, and removing that check — correctly
— exposed it. Independent review classified the gap as blocking.

**Lifecycle.** The same FK is `ON DELETE RESTRICT` and `ON UPDATE RESTRICT`. A schedule that anchors
a coverage row cannot be deleted or re-owned. 057's `ON DELETE CASCADE` was right for a *private*
watermark and is destructive for a *shared* one: it would let one schedule's deletion take a whole
dataset's coverage with it while other cadences kept firing against nothing.

**The base-role half** — that the anchor carries `run_type = 'DAILY'` — is not expressible as a
foreign key, because a partial unique index cannot be an FK target and copying `run_type` onto the
coverage row would invent duplicated business state. It is enforced at the single authorized
coverage INSERT surface and re-checked fail-closed by `dispatcher._load_coverage_state`, which joins
the row to a base schedule of its own owner and treats a failure to resolve as "no usable claim"
(`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`).

**Base-schedule cardinality, stated in its true domain.** `uq_client_dataset_schedule_base` enforces
**at most one** base schedule per `(client, dataset)`. "Exactly one" is false globally and always
was: an enabled client may legitimately own no schedule for a dataset, which the release gate's own
fleet fixture exercises deliberately. The enforceable and load-bearing invariant is scoped —
**every coverage-bearing dataset has exactly one anchoring schedule, of the same owner, which cannot
be deleted or re-owned while it anchors** — and that is what `uq_client_dataset_coverage_dataset`
plus the composite FK with `RESTRICT` guarantee together.

The rollback boundary, with the lifecycle correction folded in: while only base schedules exist, a
pre-M5 release operates unchanged; once a reconciliation cadence exists, a pre-M5 release still
serves the base schedule and **refuses fail-closed** for the reconciliation one, because no coverage
row carries its id; and no lifecycle action available to either release can delete or re-anchor the
shared watermark.

Two consequences follow, and both are deliberate:

1. **The gate no longer compares `schedule_id` to the firing schedule.** For a
   reconciliation fire the two legitimately differ; comparing them would refuse
   exactly the fires M5 exists to enable, and would misreport a well-formed row
   as bootstrap-required. Identity is `client_id` and `dataset_name` (§3, §4.1).
2. **Active manual-recovery exclusivity follows the watermark**, not the
   schedule: `uq_client_dataset_recovery_run_active` is keyed on
   `(client_id, dataset_name)`, and `client_dataset_recovery_run` carries the
   same composite provenance FK, so those owner columns provably describe the
   schedule the row names. Without that, owner-keyed exclusivity could serialize
   the wrong logical owner — the second blocking finding of independent review. Before M5 one schedule *was* one watermark, so
   the schedule-keyed index expressed watermark exclusivity by coincidence;
   after M5 it would not, and two operator recoveries on two cadences could be
   active over one shared watermark. The CAS would keep that safe — the loser is
   refused — but the index exists to make it impossible, and that intent is
   preserved rather than silently downgraded to "survivable".

Nothing else in this document is weakened by M5. The claim-time carrier, the
eleven-field predicate, the null-safety, the monotonicity rule, the post-write
verification, the lock order and the error taxonomy are unchanged. **M5 changed
which row the statements address; it did not change what they compare.**

`A = coverage_start_ts`, `W = covered_through_ts`. The authoritative flow is:

```text
single read-only coverage SELECT
→ immutable CoverageState with 12 fields
→ pure C5 gate
→ durable history claim
→ PreparedDispatcherRun retains the same CoverageState
→ C6 finalizer binds CAS parameters from that retained object
```

The future immutable `CoverageState` contains exactly `schedule_id`, `client_id`,
`client_code`, `dataset_name`, `coverage_start_ts`, `covered_through_ts`,
`bootstrap_status`, `bootstrap_evidence_ref`, `seeded_at`, `seeded_by`,
`covered_through_source` and `last_gap_detected_ts`. It contains no connection, cursor, raw row,
logger, `updated_at`, secret or trip payload.

All twelve values come unchanged from the original pre-claim `SELECT` and the same object survives
subprocess completion. The pure C5 gate may ignore the two newly carried fields; carrying them does
not change classification, window arithmetic or C5 ownership. Mutation-time reread, history
inference, the current coverage row after `FOR UPDATE`, `updated_at`, client trip data, platform
runs, `max(synced_at)` and provider metadata are forbidden claim-time substitutes. The
finalization lock read supplies current values for comparison; it never creates new expected values.

### 4.1 CAS field availability and normalization

| Field | Source / carrier | Claim-time normalization | NULL contract | SQL comparison | Success finalizer | Gap finalizer |
|---|---|---|---|---|---|---|
| `client_id` | retained `CoverageState` | canonical UUID identity; no inference | non-NULL | **owner key**; `= %(claim_client_id)s` | yes | yes |
| `dataset_name` | retained `CoverageState` | raw persisted text | non-NULL, exactly `trips_sync` | **owner key**; `= %(claim_dataset_name)s` | yes | yes |
| `schedule_id` | retained `PreparedDispatcherRun.coverage_state` | canonical UUID identity; no inference | non-NULL | compared, not addressed (M5): `= %(claim_schedule_id)s` | yes | yes |
| `coverage_start_ts` | retained `CoverageState` | aware instant converted to UTC; whole-second gate validation; no rounding | `READY` non-NULL | `IS NOT DISTINCT FROM %(claim_coverage_start_ts)s` | yes | yes |
| `covered_through_ts` | retained `CoverageState` | aware instant converted to UTC; whole-second gate validation; no rounding | `READY` non-NULL | `IS NOT DISTINCT FROM %(claim_covered_through_ts)s` | yes | yes |
| `bootstrap_status` | retained `CoverageState` | raw persisted text | non-NULL, `READY` at claim | `= %(claim_bootstrap_status)s` | yes | yes |
| `bootstrap_evidence_ref` | retained `CoverageState` | raw text retained byte-for-byte; trimming is validation only | `READY` non-NULL/nonblank | `IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s` | yes | yes |
| `covered_through_source` | retained `CoverageState` | raw persisted text | migration-057 non-NULL | `= %(claim_covered_through_source)s` | yes | yes |
| `seeded_at` | retained `CoverageState` | aware instant converted to UTC with precision preserved; no rounding | `READY` non-NULL | `IS NOT DISTINCT FROM %(claim_seeded_at)s` | yes | yes |
| `seeded_by` | retained `CoverageState` | raw text retained byte-for-byte; trimming is validation only | `READY` non-NULL/nonblank | `IS NOT DISTINCT FROM %(claim_seeded_by)s` | yes | yes |
| `last_gap_detected_ts` | retained `CoverageState` | aware instant converted to UTC when present, precision preserved; no rounding | nullable | `IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s` | yes | yes |

Every nullable predicate is null-safe. Timestamp normalization changes representation only, never
precision or value: two valid offsets denoting the same instant compare equal; a different instant
conflicts. Text validation may trim, but CAS binds the raw stored text.

### 4.1.1 Canonical coverage fingerprint

Evidence about a coverage row — in a recovery claim, in a post-recovery verification, in an
operations note — uses exactly one algorithm, owned by
`coverage_finalization.coverage_fingerprint` and versioned
`telematics-coverage-fingerprint/1`:

1. project exactly `schedule_id, client_id, client_code, dataset_name, coverage_start_ts,
   covered_through_ts, bootstrap_status, bootstrap_evidence_ref, covered_through_source, seeded_at,
   seeded_by, last_gap_detected_ts, updated_at`;
2. render every timestamp as an absolute UTC ISO-8601 instant with a `Z` suffix, at the precision
   actually stored — **nothing is rounded or truncated**, so two valid renderings of the same
   instant fingerprint equal and a different instant does not;
3. render identity columns as canonical text and `NULL` as JSON `null`;
4. serialize as compact UTF-8 JSON with sorted keys, including the version string;
5. SHA-256, lowercase hex.

The projection deliberately **includes** `client_code` and `updated_at`, which the CAS predicate
excludes (§4.2). The two roles are different and must not be conflated: a fingerprint is evidence
about the whole stored row, and **a fingerprint never authorizes a mutation**. The CAS predicate
alone decides whether an advancement may commit; the advancement function never reads a fingerprint.
Ad hoc pipe-joined projections are not acceptable substitutes.

### 4.2 Justified exclusions

`client_code` is excluded because `client_id` is canonical, `dataset_name` completes the coverage
owner, and the existing nullable/equal C5 identity rule makes a harmless denormalized-code fill non-
authoritative. A contradictory non-NULL code still fails the next C5 gate.

`schedule_id` is **not** excluded. It stopped being the addressing key at M5 (§4.0) but stayed in
the predicate, so the eleven-field claim is exactly as wide as before. Dropping it would have been
the easy mistake: a row whose provenance had been rewritten underneath a claim would then satisfy
the CAS, and the immutability that rollback compatibility rests on would no longer be enforced by
anything.

`updated_at` is excluded only because every semantically meaningful mutation changes at least one
included CAS field. That exclusion is safe only while `covered_through_source` and
`last_gap_detected_ts` remain in the retained claim-time snapshot and CAS; removing either would
reopen provenance/ABA risk. An `updated_at`-only touch remains cosmetic. C6 writes it only beside
an actual coverage mutation, never a no-op.

### 4.3 Normative operator-race examples

**Provenance-only change.** During the subprocess an operator sets
`covered_through_source = 'operator'` while restoring or retaining identical `A`, `W`, status,
evidence and seed metadata. C6 must report a CAS conflict, must not overwrite operator provenance
with `'scheduled_run'`, and must not pair history `SUCCESS` with an unnoticed operator mutation.

**Prior-gap timestamp change.** During the subprocess an operator changes
`last_gap_detected_ts` while every other CAS value remains identical. C6 must report a CAS conflict
and must not silently lose gap audit history.

**Mutation-time reread anti-pattern.** This is invalid:

```text
SELECT current source/timestamp FOR UPDATE
→ use those current values as expected CAS values
```

It validates only mutation-time self-consistency, not the claim-time decision. The locked current row
must be compared with values retained from the original pre-claim `CoverageState`.

## 5. Successful compatibility finalization

Preconditions: scheduled `trips_sync`, mode `data_invariants_v1`, allowed gate, complete claim-time
snapshot, claimed effective `window_end_ts == E_end`, `rc == 0`, **and an accepted execution
outcome**. Client-business data and the child platform run have already committed before this
transaction begins.

`new_W = max(current_W, E_end)`.

**The M3 outcome precondition, stated exactly.** Added by
`docs/20_telematics_ingestion_permanent_repair_plan.md` §20 and production-verified 2026-08-14. On a
scheduled compatibility fire, `_require_coverage_eligible_outcome` must return before this section
runs. It requires the child's terminal record to be present and to strictly parse
(`read_outcome`); to verify against this exact claim (`verify_outcome` — client, schedule, dataset,
requested window, exact `platform_run_id`, and `recovery_run_id` **absent**, which is what refuses
a manual-recovery record replayed against a scheduled claim); and to be `is_coverage_eligible`
(outcome in the eligible set, `provider_execution_entered`, `business_transaction_entered`,
`transaction_status = COMMITTED`, `¬skipped`). Any failure raises `ScheduledOutcomeRefused`, the
fire finalizes `FAILED`, and this transaction never begins.

Two boundaries that must not be blurred:

- **This document's contract is unchanged.** M3 constrains *entry* to advancement. The CAS,
  claim-loss, atomicity, reconciliation and error semantics below are exactly as before; the
  `advance_covered_through_cas` implementation was not modified.
- **The record is not durable.** It is written to a per-launch temporary path and is gone when the
  launch returns. What survives is the dispatcher's log context — `execution_outcome`,
  `execution_outcome_upserted_count`, `coverage_advanced`, plus the dispatched identity — on the
  `Job finished SUCCESS (rc=0)` line in `public.logs`, together with the finalized history and
  `public.runs` rows. A refusal instead leaves an `ERROR` line carrying `outcome_refusal_code` and
  `outcome_refusal_reason`, and the code in `client_schedule_run_history.error_summary`. Durable
  per-request and per-subwindow evidence — `provider_request_log`, `subwindow_complete`, and
  therefore §6 condition 6 — **is M4 and has been live since release `2782550f8efe`**, active
  2026-08-14T15:27:58Z (`docs/20` §22, closure §22.14). Do not read an accepted outcome as
  proof that the whole window was fetched: `provider_execution_entered` is set before the first
  request, so it attests "entered the provider and committed", not "fetched everything".

### 5.1 Lock, validation and claim-loss order

1. Begin the platform transaction.
2. Lock the coverage row and compare every included field to the claim-time snapshot.
3. Lock the claimed history row and require `status='RUNNING'`.
4. If `E_end > current_W`, issue the CAS coverage update.
5. If `E_end <= current_W`, issue no coverage update.
6. Update the locked history row `RUNNING → SUCCESS` with the same predicate.
7. Commit.

Coverage is locked before history globally. History is verified before coverage mutation. A stale
sweep or manual unblock that wins before step 3 causes `TRIPS_HISTORY_CLAIM_LOST` and no coverage
mutation statement is issued. A failure after the coverage update is issued but before the history
terminal update causes the whole transaction to roll back through the documented conflict/crash
path. No advancement can commit without `SUCCESS` history.

### 5.2 Successful no-op

For `E_end <= current_W`, the transaction still locks the coverage row and validates the complete
snapshot, including `covered_through_source` and `last_gap_detected_ts`, then locks the claimed
history row and requires it still to be `RUNNING`. It issues no coverage `UPDATE`; every coverage
column remains byte-for-byte unchanged. In particular, `covered_through_source` and `updated_at`
do not change. History becomes `SUCCESS` and commits only after both validations. A no-op never
bypasses concurrency or claim-loss validation.

## 6. Gap finalization and timestamps

A newly disconnected valid `READY` decision invokes `_finalize_compat_gap` after claim and before
launch. It follows the same coverage→history lock order and complete snapshot validation, then:

- sets only `bootstrap_status='GAP_DETECTED'`, `last_gap_detected_ts=mutation_ts`, and
  `updated_at=mutation_ts`;
- sets history `RUNNING → FAILED` with
  `error_summary='TRIPS_COVERAGE_GAP_DETECTED'`;
- commits both changes together.

The dispatcher/finalizer generates one UTC-aware whole-second `mutation_ts` once and binds that
same value to both coverage assignments. Scheduled fire `F` is not the mutation timestamp. `A`,
`W`, `bootstrap_evidence_ref`, `covered_through_source`, `seeded_at` and `seeded_by` remain
byte-for-byte unchanged. Existing valid `GAP_DETECTED` and malformed `GAP_DETECTED` rows execute no
coverage statement, so repeated fires refresh neither timestamp. Concurrent reseed/recovery/state
change causes a CAS conflict and no overwrite. No gap transition can commit without `FAILED`
history.

## 7. Normative SQL shapes

All identifiers are literal and schema-qualified; columns are explicit; values are bound; dynamic
identifiers are prohibited. In every example, `claim_*` parameters come from
`PreparedDispatcherRun.coverage_state`; current row values come from `SELECT ... FOR UPDATE`.
The CAS compares the locked current row with that retained claim-time state. Each statement has an
exact row-count assertion. Python performs an
explicit `commit()` or `rollback()`; connection close is never the commit mechanism. No bootstrap
`INSERT`, production command, DDL or coverage delete belongs here.

### 7.1 Coverage lock and complete snapshot validation

```sql
SELECT schedule_id, client_id, client_code, dataset_name,
       coverage_start_ts, covered_through_ts, bootstrap_status,
       bootstrap_evidence_ref, covered_through_source,
       seeded_at, seeded_by, last_gap_detected_ts, updated_at
  FROM workflow_a_control.client_dataset_coverage
 WHERE client_id    = %(claim_client_id)s
   AND dataset_name = %(claim_dataset_name)s
 FOR UPDATE;
```

Addressed by the coverage owner since M5 (§4.0), which is what makes every cadence over one dataset
serialize on one row. Assert exactly one row. Zero rows is the branch conflict code; more than one is critical divergence.
These are current locked-row values. Compare them in Python against the complete §4 snapshot retained
in `PreparedDispatcherRun.coverage_state`. Do not copy the locked values into `claim_*`
parameters or otherwise treat them as claim-time evidence.

```sql
SELECT run_history_id, status, schedule_id, dataset_name,
       window_start_ts, window_end_ts, scheduled_fire_ts
  FROM workflow_a_control.client_schedule_run_history
 WHERE run_history_id = %(run_history_id)s
   AND status = 'RUNNING'
 FOR UPDATE;
```

Assert exactly one row; otherwise rollback, read the observed terminal state separately for evidence, and classify
`TRIPS_HISTORY_CLAIM_LOST` without a terminal overwrite.

### 7.2 Successful advancement with `W` movement

Only after the coverage lock/snapshot validation and history lock/`RUNNING` verification in §7.1
have both succeeded may the finalizer issue this coverage mutation:

```sql
UPDATE workflow_a_control.client_dataset_coverage
   SET covered_through_ts     = %(effective_window_end_ts)s,
       covered_through_source = 'scheduled_run',
       updated_at             = %(mutation_ts)s
 WHERE client_id                = %(claim_client_id)s
   AND dataset_name             = %(claim_dataset_name)s
   AND schedule_id              = %(claim_schedule_id)s
   AND bootstrap_status         = %(claim_bootstrap_status)s
   AND coverage_start_ts        IS NOT DISTINCT FROM %(claim_coverage_start_ts)s
   AND covered_through_ts       IS NOT DISTINCT FROM %(claim_covered_through_ts)s
   AND bootstrap_evidence_ref   IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s
   AND covered_through_source   = %(claim_covered_through_source)s
   AND seeded_at                IS NOT DISTINCT FROM %(claim_seeded_at)s
   AND seeded_by                IS NOT DISTINCT FROM %(claim_seeded_by)s
   AND last_gap_detected_ts     IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s
   AND covered_through_ts < %(effective_window_end_ts)s;
```

Assert rowcount exactly 1; otherwise rollback and classify
`TRIPS_COVERAGE_ADVANCE_CONFLICT`. Then use §7.4 with `SUCCESS`.

### 7.3 Successful no-op

After both §7.1 locks and complete validation, issue no coverage `UPDATE`. Proceed directly to
§7.4 with `SUCCESS`. This shape normatively leaves every coverage column unchanged.

### 7.4 History terminal update

```sql
UPDATE workflow_a_control.client_schedule_run_history
   SET status        = %(terminal_status)s,
       finished_at   = %(finished_at)s,
       error_summary = %(error_summary)s
 WHERE run_history_id = %(run_history_id)s
   AND status = 'RUNNING';
```

Assert rowcount exactly 1. Zero rows is `TRIPS_HISTORY_CLAIM_LOST`; rollback the entire compatibility
transaction. Commit only after the assertion.

### 7.5 Gap persistence

After both §7.1 locks and validations, including the claimed history row `RUNNING` verification:

```sql
UPDATE workflow_a_control.client_dataset_coverage
   SET bootstrap_status     = 'GAP_DETECTED',
       last_gap_detected_ts = %(mutation_ts)s,
       updated_at           = %(mutation_ts)s
 WHERE client_id                = %(claim_client_id)s
   AND dataset_name             = %(claim_dataset_name)s
   AND schedule_id              = %(claim_schedule_id)s
   AND bootstrap_status         = %(claim_bootstrap_status)s
   AND coverage_start_ts        IS NOT DISTINCT FROM %(claim_coverage_start_ts)s
   AND covered_through_ts       IS NOT DISTINCT FROM %(claim_covered_through_ts)s
   AND bootstrap_evidence_ref   IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s
   AND covered_through_source   = %(claim_covered_through_source)s
   AND seeded_at                IS NOT DISTINCT FROM %(claim_seeded_at)s
   AND seeded_by                IS NOT DISTINCT FROM %(claim_seeded_by)s
   AND last_gap_detected_ts     IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s;
```

Assert rowcount exactly 1; otherwise rollback and classify
`TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT`. Then §7.4 uses `FAILED` and the gap abort code.

### 7.6 CAS conflict and history claim loss

A coverage lock/snapshot mismatch or zero-row CAS causes explicit rollback. A separate terminal
`FAILED` transaction is permitted only after a fresh read proves the same history row remains
`RUNNING`; its update uses §7.4. If it is already terminal, return `TRIPS_HISTORY_CLAIM_LOST` and
write nothing. If history is missing or not `RUNNING` when §7.1 attempts to lock it, rollback the
current transaction: no coverage mutation has been issued, the terminal row is not overwritten,
and the business job is not automatically replayed. A later failure after an issued coverage
`UPDATE` but before the history `UPDATE` rolls back both surfaces through the existing documented
conflict/crash path. No partial pair may commit.

### 7.7 Reconciliation read after uncertain `COMMIT`

Open a fresh platform connection, begin `READ ONLY ISOLATION LEVEL REPEATABLE READ`, and take one
snapshot when possible:

```sql
SELECT h.run_history_id, h.status, h.error_summary, h.finished_at,
       h.schedule_id, h.dataset_name, h.window_start_ts, h.window_end_ts,
       c.client_id, c.client_code,
       c.coverage_start_ts, c.covered_through_ts, c.bootstrap_status,
       c.bootstrap_evidence_ref, c.covered_through_source,
       c.seeded_at, c.seeded_by, c.last_gap_detected_ts, c.updated_at
  FROM workflow_a_control.client_schedule_run_history AS h
  LEFT JOIN workflow_a_control.client_dataset_coverage AS c
    ON c.client_id = h.client_id
   AND c.dataset_name = h.dataset_name
 WHERE h.run_history_id = %(run_history_id)s;
```

The join follows the coverage owner (§4.0). Joining on `schedule_id` would find no coverage row for
a reconciliation fire and report that absence as divergence — a false critical, on the one path
whose whole job is to resolve an uncertain commit correctly.

Assert at most one row, classify the pair under §8, and `ROLLBACK` the read-only transaction.

## 8. Commit outcome and mandatory reconciliation

### 8.1 Commit known to have failed before reaching the server

This classification requires driver/server evidence that `COMMIT` was not sent or PostgreSQL
rejected it before applying the transaction. Rollback where possible. Coverage and history remain at
the pre-transaction pair. A new transaction
can finalize history `FAILED` only when a fresh read shows `status='RUNNING'`. Use
`TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED`; if the row is terminal, use
`TRIPS_HISTORY_CLAIM_LOST` as evidence and write nothing.

### 8.2 Commit succeeded

A fresh read shows either:

- success pair: history `SUCCESS` and coverage exactly reflects the committed movement decision, or
  history `SUCCESS` plus the exact pre-transaction coverage row for a validated no-op; or
- gap pair: history `FAILED` with the gap code and coverage `GAP_DETECTED` with the expected shared
  mutation timestamp and unchanged protected fields.

Treat the operation as committed. Do not issue another terminal update.

### 8.3 Commit outcome uncertain

Never attempt an immediate contradictory `FAILED` finalization. Reconnect and perform §7.7.

- **Expected committed pair:** treat as committed; write nothing.
- **Original pre-transaction pair:** the transaction did not commit. A separate `FAILED` update is
  allowed only if history is still `RUNNING`, using
  `TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED`.
- **History terminal but not the expected terminal state, while coverage is exactly the original
  snapshot:** return `TRIPS_HISTORY_CLAIM_LOST`; the claim was revoked independently, so do not
  overwrite it.
- **Impossible atomic pair:** every remaining pair—specifically any coverage movement/gap mutation
  without its expected terminal history, or expected terminal history with neither its committed
  coverage decision nor the exact validated no-op snapshot—returns
  `TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE`; mutate neither surface.
- **Platform database unreachable:** return `TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE`; do
  not attempt a blind terminal write, do not retry the business job, and leave history `RUNNING`
  until the existing stale-run mechanism or a separately authorized reconciliation handles it. Emit
  a critical structured error and `TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE` suspected-bug
  report where the reporting path remains reachable.

Reconciliation never infers coverage from client data, platform `runs`, schedule history alone or
`max(synced_at)`. C6 performs no automatic replay.

## 9. Error taxonomy

All fingerprints use bounded scalar fields only. They exclude `scheduled_fire_ts`, run IDs, history
IDs, raw evidence references, evidence contents, personal/trip data, secrets, secret references,
DSNs, credentials and provider payloads. Repeated occurrences aggregate on the stable fingerprint
and never relax the gate or trigger replay.

| Code | Exact trigger and subprocess state | History outcome | Coverage outcome | Retryability / severity | Fingerprint and repeated behavior | Operator action |
|---|---|---|---|---|---|---|
| `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` | C5 rejects missing/identity-invalid/non-claiming/malformed coverage; subprocess not started | claimed row `FAILED` | unchanged | no automatic retry; `error` | `{code,schedule_id,dataset_name,mode,observed_bootstrap_status}`; every due fire remains loud | read-only inspection; separately reviewed bootstrap/reseed/repair; never seed or force `READY` from the alert |
| `TRIPS_COVERAGE_GAP_DETECTED` | existing valid gap, or new disconnected `READY`; subprocess not started | `FAILED`; for new gap atomic with mutation | existing gap unchanged; new gap one `READY → GAP_DETECTED` transition | no automatic retry; `error` | same stable shape; occurrences aggregate; existing gap timestamps never refresh | inventory gap; separately authorize recover-or-exclude and reseed; no strict fallback |
| `TRIPS_COVERAGE_ADVANCE_CONFLICT` | post-`rc == 0` coverage snapshot/CAS differs before commit; business subprocess already succeeded | separate `FAILED` only if still `RUNNING`; otherwise claim-lost evidence | unchanged by C6 rollback | no automatic retry; `error` | `{code,schedule_id,dataset_name,mode,observed_bootstrap_status}` | inspect concurrent operator state and history; never hand-advance `W`; next ordinary fire re-requests safely |
| `TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT` | pre-launch new-gap snapshot/CAS differs | separate `FAILED` only if still `RUNNING` | unchanged by rollback; detected gap not persisted | no automatic retry; `error` | same stable fields; next fire re-evaluates current state | treat as known unrecorded hole; inspect then separately authorize recovery/reseed; never hand-write gap |
| `TRIPS_HISTORY_CLAIM_LOST` | history missing or not `RUNNING` after coverage lock, after rollback, or during reconciliation | existing terminal state unchanged; no terminal write | unchanged; any in-transaction mutation rolled back | not retryable; `error` | `{code,schedule_id,dataset_name,observed_history_status}`; repeated late finalizers remain no-op | correlate stale sweep/manual unblock and duration; never restore `RUNNING` or advance coverage |
| `TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED` | commit is known not to have reached the server, or reconciliation proves the original pair; subprocess succeeded on success branch and did not run on gap branch | `FAILED` only under fresh `status='RUNNING'` predicate | original unchanged | no automatic retry; `error` | `{code,schedule_id,dataset_name,branch}` | inspect connection failure; let the next ordinary fire self-heal; no replay or hand advancement |
| `TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE` | after expected pair, exact original pair, and terminal-mismatch-with-original-coverage are excluded, any remaining pair: coverage movement/gap without expected terminal history, or expected terminal history without its movement/exact no-op/gap decision | unchanged | unchanged | not retryable; `critical` | `{code,schedule_id,dataset_name,branch,history_status,coverage_status}`; every observation remains a critical occurrence | freeze compatibility scheduling for the client; preserve evidence; inspect database/audit/restore history; require a separate reconciliation design |
| `TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE` | commit outcome uncertain and a fresh read-only platform connection/snapshot cannot be obtained | no new write; observed outcome unknown and the row is allowed to remain `RUNNING` | unknown, never inferred | no automatic retry; `critical` | `{code,schedule_id,dataset_name,branch,exception_class}` with bounded class only; aggregate until connectivity/reconciliation succeeds | restore platform DB access, then perform read-only pair reconciliation; otherwise stale sweep or separately authorized reconciliation owns the row |

For every code, logs state whether the business subprocess already succeeded. It is `false` for
bootstrap rejection, gap rejection and gap-persistence conflict; `true` for advancement conflict;
branch-specific for claim loss, finalization-commit failure, atomic divergence and reconciliation
unavailability, and the recorded branch fixes the value without inference. Redaction is
mandatory as described above. Suspected-bug reporting failure never changes the durable outcome and
is emitted as a bounded warning when logging remains available.

## 10. Crash and recovery semantics

- Crash after claim but before gap/success transaction: history remains `RUNNING`, coverage unchanged.
- Crash during either transaction before commit: PostgreSQL rollback leaves both original.
- Crash after a known commit: both expected surfaces are durable.
- Connection loss during commit: §8 reconciliation is mandatory before any terminal write.
- Stale sweep/manual unblock before finalization wins: history is no longer `RUNNING`; coverage
  mutation is prohibited and no coverage `UPDATE` statement is issued.
- A forced failure after an issued coverage update but before history update rolls back both.
- Client commit before platform success can leave client data present with coverage understating
  reality. The reverse—coverage overclaim without matching terminal history—cannot commit.
- Terminal history is immutable. Recovery creates new run/history rows and never reopens, deletes or
  re-statuses an existing terminal fire.
- Same-fire replay is prohibited by `(schedule_id, scheduled_fire_ts)` uniqueness. C6 performs no
  automatic business retry, recovery, backfill or replay. That key stays **per schedule** after M5
  and is correct there: a fire belongs to the schedule that produced it, even though the watermark
  it moves belongs to the dataset. Two cadences firing at the same instant are two distinct fires,
  and they are serialized against each other by the shared coverage lock (§7.1), not by this key.

## 11. Test architecture for future C6 implementation

Tests use disposable PostgreSQL 16, fixed deterministic inputs and fatal patches for subprocess,
sockets and provider/client access on every rejection path. No production database write, provider
request, secret or network call is permitted.

### 11.1 Documentation-conflict prevention

- statically assert docs/13, docs/14 and docs/15 assign newly detected gap persistence to C6 after
  claim and before launch;
- assert successful advancement appears only after `rc == 0`;
- statically assert docs/14 §7.1 and docs/15 Branch C state the same order: coverage lock and
  retained-snapshot validation, history lock and `RUNNING` verification, optional coverage
  mutation, terminal history mutation, then `COMMIT`;
- assert strict `_finalize_run` is not assigned coverage mutation and both compatibility finalizer
  names are consistent.

### 11.2 Null-safe CAS

- nullable NULL against NULL matches;
- NULL against non-NULL and non-NULL against NULL conflict;
- evidence/seed provenance NULL transitions conflict;
- `last_gap_detected_ts` NULL and non-NULL transitions conflict;
- `covered_through_source` changes conflict;
- harmless `client_code` and `updated_at`-only changes exercise the two justified exclusions.

### 11.3 Claim loss

- history already `FAILED` before finalization, with no coverage `UPDATE` statement issued;
- stale sweep before finalization, with no coverage `UPDATE` statement issued;
- operator manual unblock before finalization, with no coverage `UPDATE` statement issued;
- claim lost after coverage lock and before history lock, with no coverage mutation issued;
- forced exception after an issued coverage update and before terminal history update;
- rollback restores both surfaces to their pre-transaction state and terminal history is never
  overwritten.

### 11.4 Commit uncertainty

- commit succeeded but the client receives an exception;
- commit failed and the original pair remains;
- database unavailable during reconciliation;
- history terminal mismatch;
- every impossible history/coverage pair;
- no contradictory `FAILED` write before reconciliation and no write on the unreachable path.

### 11.5 No-op success

- `W == E_end` and `W > E_end`;
- coverage lock and complete snapshot validation still occur;
- the claimed history row is locked and verified `RUNNING` before `SUCCESS`;
- no coverage `UPDATE`, source change or `updated_at` change;
- history commits `SUCCESS`.

### 11.6 Gap transaction

- one shared UTC-aware whole-second mutation timestamp;
- scheduled fire differs from mutation timestamp;
- `A`, `W`, evidence, seed metadata and source unchanged;
- repeated existing gap refreshes neither timestamp;
- malformed gap never mutates;
- concurrent recovery/reseed causes conflict and no overwrite;
- atomic rollback between gap update and history update.

### 11.7 Success and strict isolation

- monotone movement uses exactly `E_end` and updates source/timestamp only when `W` moves;
- coverage→history lock order under two concurrent sessions, explicitly distinguished from
  mutation order;
- successful advancement proves coverage locks first and history is locked and verified `RUNNING`
  before the coverage `UPDATE` statement;
- no success without the atomic coverage decision;
- strict `trips_sync` and every non-trips dataset execute zero coverage SQL and retain current
  `_finalize_run` behavior;
- no row lock spans the subprocess.

### 11.8 Claim-time availability and races

- the single pre-claim `SELECT` returns both added fields;
- `CoverageState` retains them unchanged, and `PreparedDispatcherRun` carries the same values
  through subprocess completion;
- finalizer `claim_*` parameters originate from that object;
- a mutation-time reread cannot replace either expected value;
- source-only and prior-gap-timestamp operator changes each conflict and are never overwritten.

### 11.9 Timezone-normalized CAS

- identical `A`/`W`/`seeded_at`/`last_gap_detected_ts` instants rendered with different valid
  timezone offsets compare equal after UTC normalization;
- genuinely different instants conflict;
- no timestamp is rounded or truncated.

### 11.10 Replay and dual-database boundary

Tests must inject: client commit succeeds, then platform finalization fails; the next fire overlaps
the same effective window; and no automatic same-fire replay occurs. The replay proof binds to the
existing sync contract, not an abstract claim: `public.client_trips` is unique by
`(client_id, provider_trip_id)`; the job uses `ON CONFLICT` upsert/`DO NOTHING`; speeding counts
are assigned from recomputed bucket values rather than incremented; and all trip/speed writes share
the single client `conn.commit()`. Therefore overlapping re-fetch creates no duplicate trip,
speed-event recomputation does not increment twice, and no non-idempotent client side effect repeats.
A failure before that one commit must leave no partial durable client state.

### 11.11 Static guard transition

- the current C5 guard fails before its deliberate C6 rescope;
- approved dispatcher finalizer lock/update SQL passes after the rescope;
- identical writer SQL in provider, sync, API/script or unknown production packages still fails;
- the widened `coverage_windows.py` remains I/O-free and write-free.

## 12. Delivery, rollback and non-authorizations

C6 implementation follows C5 and required fresh G-COV approval, which it received; migrations
055–057 are applied in production. C11 follows C6 and requires its own independent review before
migration `058` is applied and before any recovery is executed. Deployment, bootstrap, enablement,
recovery and backfill remain separate reviewed operations. A code rollback leaves schema and honest
coverage/history rows intact; it never deletes or regresses `W`.

This document authorizes no migration application, database write, provider request, business job,
dispatcher claim, systemd change, deployment, client enablement, bootstrap, seed, reseed, recovery
execution, backfill, coverage mutation, advancement, replay or push. No **third** coverage writer
may be created without a fresh G-COV approval. `docs/12_…` D5 is resolved by
`docs/16_telematics_d5_total_policy_decision.md` (Option B, ACCEPTED 2026-08-03) and blocks nothing; C7
is unblocked and still must not write coverage.

The C11 change adds the operational procedure for the manual recovery to
`docs/07_operations.md` §5.5, because that behavior is now shipped code rather than architecture.
No recovery has been executed, so no observed-behavior claim is made about it.
