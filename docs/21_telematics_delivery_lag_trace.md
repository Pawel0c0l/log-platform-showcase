# Telematics observed-delivery-lag trace — durable contract

**Status: `IMPLEMENTED_LOCALLY`, 2026-08-17, corrected to an expand-contract rollout after one
independent Codex review. Not deployed, not committed to any release, and no migration has been
applied to production. No production `WEEKLY_RECONCILIATION` schedule exists.**

This document is the specification of record for the M-LAG milestone: the durable evidence that lets
us measure how late the provider delivers a trip, and keep measuring it after the raw request
evidence has been pruned.

Related: `docs/20_telematics_ingestion_permanent_repair_plan.md` §4 (the observability model this
implements), §3.7c (the guaranteed capture horizon the buckets are cut against), §24 (M6, whose
first production fire this milestone exists to precede); `docs/19_telematics_trips_late_arrival_audit.md`
(the retained July 2026 evidence); `db/client_business/048_…`, `db/migrations/063_…`.

---

## 1. Why this exists

M4 records **which** provider request first returned a trip. It does not record **when** that
request's response arrived in a place that outlives the request itself, and two facts make that gap
permanent rather than inconvenient:

1. **The two facts live in different databases.** `client_trips.first_seen_request_id` is in the
   per-client business database; `provider_request_log.response_received_at_utc` is in the platform
   database `logdb`. PostgreSQL cannot join them.
2. **They have different retention.** `api/platform_prune.py` deletes `provider_request_log` rows
   past a fixed **180-day** horizon. `client_trips` retention defaults to 365 days and is typically
   longer. After 180 days `first_seen_request_id` is a UUID that resolves to nothing, and the
   observation instant is gone — unrecoverably, because no other column records when we first saw
   the trip.

M6 is committed and will, on its first production fire, discover trips that arrived late. Without
this milestone those discoveries carry provenance that nothing can read back.

---

## 2. The durable fact

```
client_trips.first_seen_response_received_at_utc  TIMESTAMPTZ NULL
```

Migration `db/client_business/048_client_trips_first_seen_response_received_at.sql`.

It is the instant the provider response that **first** returned this trip was received — i.e.
`provider_request_log.response_received_at_utc` for the request named by `first_seen_request_id`,
copied at the moment it is known.

Deliberately **not** used as the observation instant, and none of these may be substituted later:

| rejected | why |
|---|---|
| `request_started_at_utc` | precedes the payload; the trip may not have existed when we asked |
| `provider_request_log.recorded_at` | when the projection was written, not when we observed |
| `finalized_at` | a coverage fact, produced by the dispatcher after the fetch |
| `client_trips.synced_at` / `sync_run_id` | **last**-touched; every overlapping re-request rewrites them. This is the exact defect M4 exists to remove |
| any projection or job time | records when we noticed, not when we observed |

### Semantics

* **Set on INSERT only**, and never in `ON CONFLICT DO UPDATE SET`. A DAILY,
  `WEEKLY_RECONCILIATION` or `MONTHLY_RECONCILIATION` rediscovery of the same trip cannot restate
  it.
* **Written from the same page record as the identity.** `RequestEvidenceCollector` stores the pair
  `(request_id, response_received_at_utc)` in one mapping entry and the job reads both from that one
  entry, so no code path can produce one without the other.
* **Constrained by the database, in two stages.** During EXPAND an instant may not exist without an
  identity; after CONTRACT the two are strictly paired. See the next subsection — this is the one
  place where the rollout phase changes what the schema permits.
* **`TIMESTAMPTZ`**, an absolute instant. The session timezone cannot change what is stored.

### The constraint, and why it is one-directional during EXPAND

Migration 047 already shipped, and the **currently deployed M4-era writer already populates
`first_seen_request_id`** without the instant. So the moment 048 is applied, live rows legitimately
look like `request_id IS NOT NULL, instant IS NULL`, and the deployed writer keeps producing that
shape until the M-LAG release is activated.

The EXPAND migration therefore installs only

```
ck_client_trips_first_seen_instant_needs_request
    CHECK (first_seen_response_received_at_utc IS NULL
           OR first_seen_request_id IS NOT NULL)
```

— an instant may not exist without an identity. That half carries real integrity (a timestamp with
nothing to attribute it to is unusable evidence) and rejects **nothing** that exists and nothing the
deployed writer can produce. It is added `NOT VALID`: PostgreSQL still enforces a `NOT VALID` CHECK
on every INSERT and UPDATE and only skips the verification scan of pre-existing rows, which here is a
guaranteed no-op — and an expensive one, see §14.

**Strict bidirectional pairing is NOT part of EXPAND.** Enforcing it then would fail to validate
against existing post-M4 rows, reject every insert from the deployed writer, and make rollback to the
M4 release unsafe. It is the CONTRACT step (§13).

### Why this denormalization is not a cache

It is not a copy of a value that will still be there later — see §1. It is a **second attribute of
one observation event**, and the two attributes are produced together and never rewritten. What is
deliberately *not* stored is the lag itself (§3).

---

## 3. The metric

```
observed_delivery_lag_seconds = first_seen_response_received_at_utc - end_timestamp
```

Computed in exactly one place: `jobs.api.telematics.delivery_lag.observed_delivery_lag_seconds`. A
repository-wide test asserts no second subtraction exists, because the trivial part of this metric is
the part two consumers would come to disagree about.

**It is called `observed_` and never `provider_publication_lag`.** It is an **upper bound** on the
provider's unknown publication lag, because our own polling cadence contributes to it: a trip
published one minute after it ended but first requested three days later measures three days.
Nothing in this design emits a provider publication instant. Narrowing the bound requires the
last-absent interval, which is deferred (§9).

**It is never stored.** `end_timestamp` appears in the trip upsert's `DO UPDATE SET` list on
purpose — the documented `/trips` overlap rule returns trips extending past the requested boundary,
so a trip can be observed while still open and have its end corrected later. The lag is therefore an
**immutable minuend and a current subtrahend**, and a stored value would silently go stale.

---

## 4. Edge semantics

| case | behaviour |
|---|---|
| **negative lag** | **Valid and expected.** A trip observed before its final end. Classified `OBSERVED_BEFORE_TRIP_END`, counted in its own bucket, **never clamped to zero** — clamping would erase a real signal about how much ingestion catches trips in flight |
| **NO_PROVENANCE** | Both halves NULL. The metric is `None`, permanently. Pre-M4 rows, the **insert-only backfill** (`backfill_trips_insert_only.py`, which has no provider request to point at) and any `strict_meta` run. Counted in `trips_total`, excluded from every distribution, and the exclusion rate is published beside every percentile |
| **PROVENANCE_TIMESTAMP_PENDING** | Identity present, instant absent — the EXPAND-window state. The observation **did** happen and is recoverable from the exact platform request row, so this is transitional, not permanent. Excluded from the distribution like a no-provenance row, but counted separately as `trips_provenance_pending` so a slice cannot be read as settled while any remain (§10). Resolved by §12 |
| **NULL `end_timestamp`** | The metric is `None`; the trip belongs to no date slice |
| **never backfilled** | There is no evidence from which a historical first observation could be reconstructed. Migration 048 is pure DDL and must stay so |
| **rediscovery** | Cannot create a second first-seen event; the columns are absent from `DO UPDATE SET` |
| **duplicate ingestion** | Collapsed by `ON CONFLICT (client_id, provider_trip_id)` before any metric sees it; the aggregator also de-duplicates defensively |
| **corrected `end_timestamp`** | The lag changes, correctly, and can cross from positive into negative |

**C11 recovery is NOT a no-provenance path — corrected 2026-08-17 from code.** An earlier revision of
this document listed `ops/recover_telematics_trips_window.py` alongside the insert-only backfill. That
was wrong. C11 writes no trips at all: it launches
`jobs.api.telematics.sync_trips_and_speeding` as a subprocess (`SYNC_JOB_MODULE`), which is the normal
writer. A C11 recovery therefore captures ordinary first-seen provenance whenever request evidence
exists, and its rows are ordinary provenance-bearing rows. The absence of first-seen SQL in the C11
tool reflects that it writes no trips, not that its trips lack provenance. C11 runtime behaviour was
**not** changed to match the stale documentation.

---

## 5. The retention boundary

After the referenced `provider_request_log` row is pruned:

* the **lag is still computable** — both operands are on the trip row;
* the **distribution is still aggregatable** — §6;
* only the **discovery attribution** becomes unresolvable, and it degrades to
  `discovered_unattributed`, never to a wrong role. That is why the aggregation resolves and
  **stores** attribution while the evidence is still fresh (§8).

`trip_delivery_lag_daily` itself has **no retention**: one row per client per day is a few thousand
rows a year for the whole fleet, and its purpose is to outlive both horizons.

**A pending row whose request has been pruned is a different, worse case.** It has no instant on the
trip row *and* no evidence left to copy, so it is permanently unenrichable and permanently outside the
distribution. That is the deadline in §12, and it is why historical enrichment is sequenced
immediately after release activation rather than left until convenient.

---

## 6. The aggregate, and the mutable-`end_timestamp` problem

`workflow_a_control.trip_delivery_lag_daily`, migration `db/migrations/063_…`, keyed
`(client_id, trip_end_date)`.

### The problem

An append-only aggregate keyed on the trip-end date **observed at first ingestion** becomes
permanently wrong the moment a trip's `end_timestamp` moves to another date: the old slice keeps a
trip it no longer contains, and the new slice never learns of it. Nothing detects it — both rows look
like ordinary successful aggregations.

### The invariant chosen

> **Every row is the complete, deterministic projection of the current contents of `client_trips`
> for one `(client, trip_end_date)`, rewritten wholesale. No value is ever incremented.**

Consequences, both required and both tested:

* **Idempotent.** Recomputing a slice yields byte-identical metrics; re-running the job cannot
  double-count, because every number comes from a scan.
* **Self-correcting within the recompute horizon.** A trip that moves from the 21st to the 22nd is
  corrected as soon as both dates are recomputed — so the job always recomputes a contiguous
  trailing window, never a single day.

### The horizon is derived, not guessed

`end_timestamp` corrections arrive with the trips themselves, and a trip can only be re-requested
inside a fire's effective window. The **deepest enabled `lookback_days`** across the client's
`trips_sync` schedules therefore bounds how far back a correction can originate; anything older
cannot move again.

```
recompute_horizon_days = max(enabled lookback_days) + 2
```

The `+2` covers the stabilization delay, the overlap, and the fact that a window is absolute seconds
while a slice is a calendar date in the business timezone. Today's DAILY-only fleet gives 9 days;
with M6 enabled it becomes 18. Each row stores the `recompute_horizon_days` and `computed_at` it was
produced with, so a stale slice is visible **in the data** rather than being a property of a job
invocation nobody kept.

### Read model, not a new fact layer

No per-trip platform table was added. The per-trip facts already exist and are already immutable, in
`client_trips`. `ops/aggregate_telematics_delivery_lag.py` reads each database once, joins in Python
and upserts the slice; it makes no provider request, and writes nothing but the slice relation.

---

## 7. Buckets and percentiles

Buckets are disjoint, exhaustive, and **half-open upward** — a lag exactly on a boundary lands in the
upper bucket. Migration 063 asserts by CHECK that they sum to `trips_with_provenance`, so a bucketing
bug cannot present itself as a quiet improvement in the tail.

```
negative | <6 h | 6–24 h | 1–3 d | 3–7 d | 7 d–weekly guarantee | guarantee–15 d | >15 d
```

The **weekly guarantee** boundary is derived, never written as a decimal:

```
guaranteed_horizon_days = L + (D + O)/86400 − P
```

For the committed M6 cadence (`L = 16`, `P = 7`, `D = 10 800 s`, `O = 3 600 s`) that is
**792 000 s = 9.1667 d**, and it is computed by `delivery_lag.guaranteed_horizon_seconds` from the
live schedule. `docs/20` §3.7c once carried the constant as `0.208`; it is `(D + O)/86400 = 0.1667`,
and deriving it is how that class of error stops being possible. Each slice stores the
`weekly_guarantee_seconds` it was cut against, so a future lookback change cannot make old slices
appear to have used a new boundary.

Where no reconciliation cadence is enabled — the state today — the boundary falls back to the
committed M6 value, so bucket edges are comparable from the first slice rather than shifting the day
a schedule is enabled.

**Percentiles are nearest-rank**, so every reported value is a lag some trip actually had; an
interpolated p90 is a number no trip ever measured. p50/p90/p95/min/max are stored and are `NULL`
exactly when the sample is empty — a CHECK enforces the all-or-nothing.

**p99 is deliberately absent.** At per-client-per-day sample sizes it is an artifact of the single
largest value, which `lag_max_seconds` already reports honestly.

### Aggregation completeness during the transition

`trips_provenance_pending` on each slice is its own completeness declaration. **While it is non-zero
the distribution is still growing**: those trips have real observations that will enter the
percentiles once §12 runs. A consumer — and in particular anyone sizing M7 — must read it before
treating a percentile as settled. `ops/aggregate_telematics_delivery_lag.py` also sums it into its
per-client report, so an operator sees it without querying the slices.

Slices may be computed at any point in the rollout; they are recomputed wholesale, so a slice taken
mid-transition is simply superseded by the next pass rather than needing repair.

---

## 8. Discovery attribution

The operational question is: **which reconciliation layer first discovered the trip?** — the number
that says whether the weekly, and later the monthly, layer earns its cost.

Resolved by the aggregation job through existing relationships, not by duplicating role state onto
the trip:

```
client_trips.first_seen_request_id
  → provider_request_log.request_id (.run_history_id)
    → client_schedule_run_history.schedule_id
      → client_dataset_schedule.run_type
```

Only `FINALIZED` request rows carry a `run_history_id` at all, so a `PENDING` fact left behind by a
rolled-back business transaction resolves to nothing — correct, since no committed trip points at it.

The resolved **counts are denormalized into the slice**, and that decision is explicit: the chain
resolves only while the request row survives its 180-day horizon, the recompute horizon is tens of
days, and the question gets asked years later. Storing the counts is the only way it survives the
prune.

`discovered_unattributed` is never merged into `discovered_daily`. Attributing a trip to the base
role because its evidence expired would invent the very answer the metric exists to provide.

---

## 9. Deferred, deliberately

* **The last-absent lower bound.** `docs/19` §5.1 pairs each upper bound with a lower one: the newest
  earlier complete request that covered the trip and did not return it. The evidence and the index
  for it exist (`idx_provider_request_log_client_coverage`), and nothing here forecloses it — but it
  needs a per-trip interval, not a slice, and it would materially widen this milestone. Deferred with
  the evidence preserved.
* **Alert thresholds and anomaly detection.** They need a measured distribution, which is what this
  produces. The July 2026 cohort is explicitly a biased sample (`docs/19` §9.5) and must not be
  fitted against.
* **M7's exact lookback.** Still telemetry-driven and still undecided. Nothing here freezes it, and
  `docs/20` §3.1a's direction (a rolling window, no calendar mode) is unchanged.
* **Scheduling this aggregation.** It is an operator-invoked tool. Registering it as a dispatcher
  dataset needs a schedule row and is separate work.

---

## 10. What is not claimed

* **No M6 production verification.** No `WEEKLY_RECONCILIATION` row exists in production, none has
  fired, and this milestone does not create one.
* **No M7 decision.**
* **No provider publication measurement.** Every value is an observed upper bound.
* **No production migration.** Client 048 and platform 063 exist in the repository and have been
  verified against disposable databases only.
* **No historical enrichment has run**, so no production row has been enriched.
* **The CONTRACT is not closed anywhere.** Strict bidirectional pairing is installed by no migration
  and by no automatic path; until §13 is executed, `PROVENANCE_TIMESTAMP_PENDING` remains a legal
  state and M4-writer rollback remains available.


---

## 11. The expand-contract rollout

Migration 047 shipped before this milestone, so the schema change could not simply arrive with its
final invariant. The rollout is therefore a genuine expand-contract, and the phases are not
interchangeable.

### Phase A — EXPAND (`db/client_business/048_…`)

Adds the nullable column and the one-directional constraint, `NOT VALID`. Catalog-only work: no
scan, no index, no rewrite (§14). Backward-compatible with the deployed M4 writer, so it may be
applied to every client database **before** the M-LAG release is activated, and rolling back the
release afterwards remains safe.

### Phase B — the new writer

`sync_trips_and_speeding` writes both halves from one page record. Activated with the M-LAG release
behind the existing schema preflight, which refuses activation if 048 is missing from any client.

### Phase C — historical enrichment (§12)

Copies the instant onto existing post-M4 rows from the exact platform request row. Operator-invoked,
dry-run first, idempotent, and never imputes.

### Phase D — CONTRACT (§13)

Installs strict pairing. **Intentionally ends M4-writer rollback compatibility.**

### Compatibility matrix

| code | schema | result |
|---|---|---|
| M4 (deployed) | pre-048 | works — today's production |
| **M4 (deployed)** | **EXPAND 048** | **works.** The key requirement: the old writer never names the new column, and the one-directional constraint cannot reject its inserts. This is what makes 048 safe to apply ahead of the release, and rollback safe after it |
| M-LAG | pre-048 | **fails closed before activation.** `db/schema_requirements.json` declares 048, so `ops/release_schema_preflight.py` refuses to activate the release against a client database without the column — the failure the file exists to prevent |
| M-LAG | EXPAND 048 | works. New rows carry complete pairs; historical rows stay `PROVENANCE_TIMESTAMP_PENDING` until Phase C |
| M-LAG | EXPAND 048 + enrichment done | works, and the distribution is complete |
| rollback M-LAG → M4 | EXPAND 048 only | **valid.** Rows the new writer completed keep both halves; the old writer resumes producing pending rows |
| M-LAG | after CONTRACT | works |
| rollback M-LAG → M4 | after CONTRACT | **NOT valid, by design, and now BLOCKED BEFORE ACTIVATION.** The old writer's provenance-bearing inserts are rejected by the strict constraint. This is the cost of closing the contract, and it is why closure now requires G6 to *prove* that `previous` is itself bridge-compatible, on top of the operator's `--rollback-window-closed` acknowledgement. See "the release bridge" below for how the refusal is enforced rather than merely documented |
| rollback M-LAG → previous M-LAG | after CONTRACT | **valid, and only valid once G6 holds.** Both releases must declare the bridge contract below and carry the pair-atomic writer — which is true only after two distinct bridge-compatible releases have been activated in sequence |

### The release bridge — one release, both sides of the DDL step

Closing the contract replaces one constraint with another, and a release declares the schema it
needs. Declared naively that is a deadlock: a release pinned to the EXPAND constraint stops passing
its own preflight the instant the closure drops it — including the release that is currently running
and the one rollback would return to — while a release pinned to the strict constraint cannot be
activated before the closure creates it. There is no ordering that works.

`db/schema_requirements.json` therefore declares the pairing constraint as **two exactly specified
alternative states**, of which **exactly one** must hold:

| state | constraint | required validation |
|---|---|---|
| `EXPAND` | `ck_client_trips_first_seen_instant_needs_request`, one-directional | `NOT VALID`, exactly as 048 leaves it |
| `CONTRACT` | `ck_client_trips_first_seen_pairing`, `(request_id IS NULL) = (instant IS NULL)` | **validated** |

Nothing is loosened. Each state pins the constraint name, the canonical `pg_get_constraintdef` text
and the validation status, so a same-named constraint carrying a different expression is refused
under either state, a state matching nothing is refused, and every other prerequisite in the file is
unaffected. A strict constraint that exists but is still `NOT VALID` is an **interrupted closure**,
not a closed contract, and does not satisfy the `CONTRACT` state.

**The alternatives are exclusive, not a menu.** An early implementation accepted the relation as soon
as one state matched and stopped looking, and independent review reproduced what that permits: a
database carrying a perfect `EXPAND` constraint *and* a malformed constraint under the strict
`CONTRACT` name passed the gate, because `EXPAND` was evaluated first. That malformed CHECK is live
DDL that rejects the pair-atomic writer, so the gate had authorized an activation against a schema
state neither alternative describes. A state is now authoritative only when it holds in full **and**
no constraint belonging to a competing declared alternative is present in the catalog at all,
whatever shape it carries, and the relation is compatible only when exactly one state is
authoritative. Exclusivity is scoped to the constraint names the alternatives themselves declare, so
an unrelated constraint on the same table never causes a refusal.

The bridge is a *release compatibility* declaration and says nothing about permissible data. Before
closure production is already required to be at `PENDING = 0` and `ORPHAN = 0`; the bridge only lets
the same M-LAG-capable release operate on either side of the DDL transition.

**Why it is safe to accept both:** because this release's writer is strict-compatible in the first
place. It inserts `(request_id, response_received_at_utc)` atomically from one first-seen
observation, writes `(NULL, NULL)` when there is no provenance, names neither column in
`ON CONFLICT ... DO UPDATE SET`, and has no reachable request-only path. A release without that
writer is not made safe by this declaration — see the next section.

### Post-CONTRACT prohibition on M4-era writers, enforced before activation

A release declares its own prerequisites, so an old release simply does not carry a requirement it
predates. That is right for a prerequisite and wrong for a *narrowing*: after closure the strict
constraint rejects the M4 writer's request-only inserts, yet the M4 release declares nothing about
it, so preflight would have passed it and the failure would surface on the first production INSERT.

`ops/release_schema_preflight.py` therefore also runs the opposite direction. `SCHEMA_STATE_GUARDS`
reads every enabled client business database for the narrowing state — the presence of
`ck_client_trips_first_seen_pairing`, **in any validation state**, because `ADD CONSTRAINT ... NOT
VALID` already enforces the rule on new writes — and refuses activation unless the release declares
the capability `client_trips_first_seen_pair_contract` in the `capabilities` array of its own
requirements file. Absence of the declaration is a refusal (`RELEASE_SCHEMA_STATE_CAPABILITY_MISSING`),
which is exactly what binds releases built before the key existed, including a release with no
requirements file at all. The guard is evaluated even when the release declares no requirement, so
the "declares nothing, checks nothing" path is closed.

The guard is state-driven, not a blanket ban: while the fleet is in EXPAND an M4-era release is still
accepted, matching the matrix above. The post-closure activation set is therefore exactly the
releases that both carry the pair-atomic writer and declare the bridge contract.

**The guard decision is taken inside the activation fence, not before it.** Reading the client
databases during validation and deciding there was a stale observation: the CONTRACT closure is DDL
in a *client* business database and takes no lock in the platform database, so it could commit
between the capability check and the pointer swap — independent review reproduced exactly that
(`CLIENT_CONTRACT_COMMITTED_INSIDE_ACTIVATION_FENCE`). Activation and closure therefore now
serialise on one platform advisory key, `SCHEMA_TRANSITION_LOCK_KEY`:

| participant | takes the key | holds it across |
|---|---|---|
| `ops/manage_release.py activate` | transaction-scoped, inside `activation_fence`, before the fleet `SHARE` lock | the authoritative state-guard re-read **and** the pointer swap |
| `ops/close_telematics_first_seen_pair_contract.py --execute` | session-scoped, on a dedicated platform connection | the whole per-client swap / validate / ledger sequence |

An advisory lock is the right primitive here for the same reason it was the wrong one for fleet
membership: the only way to cross the EXPAND/CONTRACT boundary is to run the closure tool, so the
constrained population is exactly the two tools that take the key — whereas any session at all can
write `client_account`, which is why the fleet is still held by a real `SHARE` table lock. Both sides
acquire in the same order, so they cannot deadlock. Whichever wins, the other observes a committed
outcome rather than a stale one: a closure cannot commit inside a live activation's window, and a
legacy activation whose unfenced preflight passed against an EXPAND fleet re-reads the state under
the key and is refused **before** the pointer moves.

**Later strict-only cleanup.** Once rollback policy no longer requires reaching a pre-closure
database, a future release may drop the `EXPAND` alternative and require the validated `CONTRACT`
state alone. That is a data edit to `db/schema_requirements.json`, not a code change, and it must not
happen while any activatable release still needs to run against an EXPAND database.

### Approved production sequence

Each step is separately authorized; none is performed by this work.

1. implement, review and ship the bridge capability (the `capabilities` declaration, the alternative
   states and the pair-atomic writer) — **this is the only step performed so far, and it is not yet
   committed**;
2. apply platform migration 063 (inert, independent);
3. apply client migration 048 **EXPAND** to every relevant client database;
4. verify schema / release preflight;
5. prepare and activate the **first** bridge-compatible release, A. The pointers become
   `current = A`, `previous = <the legacy release A displaced>`;
6. run Phase C enrichment (§12);
7. verify the readiness gate (§13, `--check-only`). **It will report G6 NOT READY**, and that is
   correct rather than a defect: `previous` is still a legacy release, so closing now would leave no
   activatable rollback target. The CONTRACT stays OPEN;
8. recompute the durable aggregates, reading `trips_provenance_pending` before treating any
   distribution as settled;
9. later, on the next legitimate change, prepare and activate a **second, distinct**
   bridge-compatible release B. The pointers become `current = B`, `previous = A`, and both are
   bridge-capable. Re-activating A is a no-op and does not count; a fabricated no-op release is not
   an acceptable substitute for a real one;
10. re-run the readiness gate. G6 now verifies both real materialized releases and reports READY —
    `OBSERVATIONAL`, because `--check-only` takes no transition lock. `--execute` re-evaluates the
    same envelope under `SCHEMA_TRANSITION_LOCK_KEY` and decides on that protected result, so a
    pointer moved between the two runs is observed rather than assumed away;
11. acknowledge irreversibility with `--rollback-window-closed` (G7) — an acknowledgement, never a
    substitute for the G6 proof;
12. execute the CONTRACT closure (§13);
13. bidirectional pairing is then universal DB-enforced truth. From that moment arbitrary old
    releases remain prohibited: only releases declaring the pair capability may activate at all;
14. later strict-only cleanup (dropping the `EXPAND` alternative) is a separate, still-future step.

**The second bridge release does not exist yet, and this work does not create one.** Release B is
whatever legitimate future commit next carries the bridge capability; the repository gates correctly
and waits for it.

---

## 12. Historical enrichment — `ops/enrich_telematics_first_seen_timestamps.py`

**Candidate set.** Exactly `first_seen_request_id IS NOT NULL AND
first_seen_response_received_at_utc IS NULL`. A row that already has an instant is never selected,
which is what makes a re-run a no-op rather than a rewrite.

**Valid enrichment.** The instant is taken from the `provider_request_log` row whose `request_id`
**equals** the trip's `first_seen_request_id`, and from nowhere else. There is no fallback to
`synced_at`, the trip timestamps, another request from the same run or sub-window, the nearest
request in time, or any job/projection/finalization time.

**Fail-closed.** An unresolvable candidate is left untouched and reported. The run exits non-zero
(`EXIT_UNRESOLVED_REMAIN = 8`) so it cannot be mistaken for a closure, and prints the unresolved
count plus a bounded sample of **identities only** — `provider_trip_id` and the request UUID, never
payload, never credentials. Rows with an instant but no identity are reported as `orphan_instants`
and also block closure.

**Idempotency and race safety.** Every write is a compare-and-swap:

```
WHERE client_id = … AND provider_trip_id = …
  AND first_seen_request_id = …                      -- unchanged since selection
  AND first_seen_response_received_at_utc IS NULL     -- still pending
```

A row the new writer completed between selection and write matches zero rows and is counted as
`raced` — not overwritten, not an error.

**Not a distributed transaction, and not described as one.** The platform side is a
`REPEATABLE READ READ ONLY` transaction, so nothing there can be left half-done; the client side
writes are individually conditional. A partial run leaves a strictly smaller candidate set, so the
operation is *resumable* rather than atomic across the two databases.

**Retention deadline.** `provider_request_log` is pruned at 180 days. A pending row whose request has
been pruned is **permanently** unenrichable, and the tool reports it rather than inventing anything.
Enrichment must therefore run well inside that window; the report prints the oldest candidate's age
so the margin is visible.

---

## 13. CONTRACT closure — `ops/close_telematics_first_seen_pair_contract.py`

**Why a tool and not a migration file.** `scripts/apply_client_business_migrations.py` applies every
pending file in `db/client_business/` automatically, so a `049_…_contract.sql` could fire before the
rollout conditions existed. A *gated* file would be worse: that applier returns on the first failure,
so a permanently-refusing file would block every later client migration behind it. And the correct
lock behaviour is impossible there anyway (§14).

**The readiness gate.** All must hold, and any failure refuses with `EXIT_NOT_READY = 9`:

| gate | condition |
|---|---|
| G1 | the EXPAND column exists |
| G2 | zero `PROVENANCE_TIMESTAMP_PENDING` rows |
| G3 | zero orphan instants |
| G4 | the EXPAND constraint is present **with its exact canonical definition** — this database really went through the expand phase, rather than carrying a same-named constraint that means something else |
| G5 | the strict constraint is not already installed |
| G6 | the **materialized one-step rollback envelope is proven** from the **authoritative production release inventory**: `current` and `previous` both resolve **canonically inside that inventory's `releases/` directory**, both pass `ops.release_boundary.verify_release` (manifest, commit binding, on-disk bytes, source-tree digest, and the metadata's own `release_id`), they are distinct identities, and each declares the pair capability plus a bridge declaration that **equals** the canonical structure — exactly one relation requirement for `public.client_trips` carrying exactly two alternatives, one exactly the `EXPAND` constraint (`NOT VALID`) and one exactly the validated `CONTRACT` constraint, and nothing else — **and that relation requirement holds *completely*** (columns, unconditional constraints, indexes and alternatives alike) in both canonical transition states, decided by the same `relation_state_defects` release preflight decides with. Evaluated **under `SCHEMA_TRANSITION_LOCK_KEY`** — see below |
| G7 | the operator passes `--rollback-window-closed` |

G1–G5 are per-client and gate a fresh `OPEN → CLOSED` closure; a resume needs G1–G3 only. **G6 and
G7 are fleet-wide preconditions of `--execute`, checked once, inside the transition coordination and
before any client DDL** — including before a resume, because a resume also leaves the fleet in a
state no legacy release can run against.

**G6 replaced an operator assertion with a proof.** `--rollback-window-closed` asserted that rollback
had been accounted for; it could not establish it. Independent review checked the real production
pointers — `current = 18dcda87f16d`, `previous = 20a01b4358b8` — and found that **neither carries the
pair capability**, so the closure would have been permitted while destroying the one-step rollback it
claimed to have accounted for. Worse, one bridge activation does not repair that: it leaves
`current = bridge-A` and `previous = 18dcda87f16d`, still legacy. The envelope exists only once two
distinct bridge-compatible releases have been activated in sequence, leaving `current = bridge-B` and
`previous = bridge-A`. `ops/release_schema_preflight.rollback_envelope_status` reads that answer out
of the release layout, and `--execute` fails closed with `ROLLBACK_ENVELOPE_NOT_MATERIALIZED` when it
is not there. Two aliases for one release, a release directory that is neither pointer, repository
HEAD, and one bridge plus one legacy release are all refused.

**G6 is decided under the transition lock, not before it.** A second independent review reproduced
the remaining hole: G6 reported `READY`, a *supported* activation or rollback then moved
`current`/`previous`, and the closure went on to acquire `SCHEMA_TRANSITION_LOCK_KEY` and act on the
snapshot it had taken before that mutation. The execute path now runs:

```
resolve the authoritative release inventory
  → OBSERVATIONAL G6            (reports; may refuse early; never authorizes)
  → read-only platform identity and fleet enumeration
  → acquire SCHEMA_TRANSITION_LOCK_KEY
  → PROTECTED G6                (re-read of the live pointers, under the lock)
  → refuse, or proceed
  → client DDL / VALIDATE / ledger for every client
  → release the lock
```

Every supported pointer mutation takes the same key transaction-scoped across its swap, so a mutation
that has already committed is visible to the protected read, and one that has not cannot commit while
the closure holds the lock. **Only the protected result authorizes closure**; the report labels each
envelope `OBSERVATIONAL` or `PROTECTED` so a read-only diagnostic can never be mistaken for a durable
authorization. The acquisition order is identical on both sides — platform advisory lock first, then
anything else — so the two cannot deadlock.

**G6 proves declaration, not live state.** It asks whether `current` and `previous` could operate on
either side of the upcoming transition, which is a property of what those releases *packaged*.
Whether the live database is ready to be transitioned at all remains G1–G5 and the state machine.

**An arbitrary release root cannot authorize production closure.** `--release-root` used to be an
ordinary path argument, and the review satisfied G6 with a directory it had built itself — `{}`
release metadata, pointers escaping the inventory, dangling targets, and bridge declarations carrying
`CHECK (false)`. Under `--expected-environment production` only
`ops.manage_release.DEFAULT_RELEASE_ROOT` is accepted, in every mode, and no flag relaxes it. Outside
production identity, `--execute` against another root additionally requires the explicit
`--allow-non-production-release-root`, which exists so the deterministic suites can drive the real
closure against their own temporary inventories.

**Materialization and declaration are both proven, by the authoritative primitives.** Release
authenticity comes from `ops.release_boundary.verify_release` — the same read-only verifier
`ops/manage_release.py verify` uses — so fabricated metadata, a doctored manifest or an edited file in
a release tree fails there rather than downstream. The bridge declaration is compared against the
canonical constraint bodies defined once in `ops/release_schema_preflight.py` and imported by the
closure tool, so `CHECK (false)`, `CHECK (true)`, a wrong `EXPAND` expression, a flipped validation
flag, a name-only requirement, a missing alternative, and both constraints merged into one
non-spanning alternative are each refused by name.

**The declaration must EQUAL the contract, not merely contain it.** A third independent review found
the remaining weakness on both sides of that sentence.

*Declaration.* Per-member exactness still accepted a release whose `EXPAND` alternative carried the
canonical constraint **and** a synthetic extra one: G6 reported `READY` with no declaration defects
while canonical `EXPAND` preflight refused the very same release, because preflight requires every
member of a group to hold. G6's proof was therefore weaker than the contract it claimed to prove. The
decision is now structural equality against `CANONICAL_BRIDGE_ALTERNATIVES`, built once from the
shared constants and normalized with the same rules preflight uses: exactly one participating
relation declaration, on `public.client_trips` in `client_business` scope, carrying exactly the two
canonical groups. An extra member in either group, a duplicated member, a duplicated or third group,
members split across additional groups, a merged group, a missing group, a second relation
collectively declaring the bridge, and the exact structure on the wrong relation are all refused.
Exactness is scoped to the bridge declaration: an unrelated requirement elsewhere in the release —
as every real release carries — does not invalidate it, and normal preflight remains fail-closed on
those requirements, so nothing G6 ignores is thereby waved through activation.

**The alternatives are not the requirement.** A fourth independent review found that structural
equality of `constraint_alternatives` still left the rest of the same relation declaration
unexamined. Two honestly prepared releases carried the exact canonical alternatives while the same
`public.client_trips` relation additionally required an absent unconditional
`ck_synthetic_unconditional`: G6 returned `READY`, and `relation_defects` on the very same release
correctly returned `constraint_absent:public.client_trips.ck_synthetic_unconditional`. A
`RelationRequirement` carries columns, unconditional constraints, indexes **and** alternatives, and
every one of them can make normal preflight fail, so G6 was again weaker than the contract it
claimed to prove.

The correction is one authoritative definition rather than a longer comparison.
`relation_defects` is now the composition of `observe_relation_state` (the only half that reads the
catalog) and `relation_state_defects` (the only definition of what a complete `RelationRequirement`
means), and G6 evaluates the **complete** packaged participating relation with that same
`relation_state_defects` against both canonical transition states —
`CANONICAL_BRIDGE_STATE_EXPAND` and `CANONICAL_BRIDGE_STATE_CONTRACT`, built from the shared
constants. `compatible` therefore *implies* the release's own relation preflight passes in canonical
`EXPAND` and in canonical validated `CONTRACT`, because it is literally the same function answering,
and the two can no longer drift.

The canonical states model only what the transition **governs** — the two paired columns and the one
constraint that names the state — which is a lower bound on any real `client_trips`.
`relation_state_defects` is monotone in what the catalog contains and alternative exclusivity is
scoped to the participating constraint names, so zero defects against that lower bound implies zero
defects against the richer live relation. The residual direction is G6 refusing a declaration a real
database would have satisfied — an index or unconditional constraint outside the bridge contract, say
— which is the intended answer rather than a false negative: G6 may be stricter than relation
preflight, never weaker.

*Materialization.* `verify_release` bound the requested release id to the commit and never checked
the metadata's own `release_id`, so the review rewrote it to `000000000000` in an otherwise genuine
release and verification still passed. One equality now binds all three representations — canonical
directory id, `release_id_for(commit)`, and the metadata's own claim — inside the authoritative
verifier, so G6 inherits the stronger materialization proof rather than restating it.

G7 survives as what it always genuinely was: a clean G2/G3 is not consent — a brand-new client with
no trips passes every automatic check — and irreversibility acknowledgement cannot be read out of the
database. It is an acknowledgement **on top of** the G6 proof, never instead of it.

`--check-only` runs the gate and changes nothing, and takes no transition lock: it is the
pre-contract validation report — pending count, orphan count, complete pairs, total rows, each gate's
state, and the G6 envelope with the exact `current`/`previous` release ids and per-release defects.
Its envelope is reported as `OBSERVATIONAL` and is explicitly **not** a durable authorization; the
`--execute` decision is taken again, under the lock.

**The closure itself** drops the EXPAND constraint and adds the strict one `NOT VALID` in one short
catalog-only transaction — from that moment the writer contract is closed — then `VALIDATE`s it in a
**second** transaction, where the scan takes only `SHARE UPDATE EXCLUSIVE` and does not block readers
or writers. It then records the synthetic ledger entry
`049_client_trips_first_seen_pair_contract.tool`, which is deliberately **not** a file in
`db/client_business/` and must never become one.

### The contract state machine, and resuming an interrupted closure

Splitting the closure across transactions is what makes it lock-safe, and it also means an
interruption can land between them: the strict constraint then exists **unvalidated**, with the
synthetic ledger entry not written. That state was previously reported as `ALREADY_CLOSED` — gate G5
saw the strict constraint and concluded there was nothing to do — so it could never be finished by
rerunning the tool. It now has its own classification and its own resume path.

The tool classifies the client before applying any gate, from `pg_get_constraintdef` and
`pg_constraint.convalidated` rather than from constraint names:

| state | meaning | `--execute` does |
|---|---|---|
| `OPEN` | the exact EXPAND constraint, strict absent | the full closure: swap, validate, ledger |
| `STRICT_PRESENT_NOT_VALIDATED` | the exact strict constraint, `convalidated = false` — an **interrupted** closure | resumes: validates the existing constraint, then records the ledger. Nothing is dropped or re-created |
| `CLOSED` | the exact strict constraint, validated | nothing, if the ledger entry is present. If only the ledger is missing, it records it |
| `INVALID` | a wrong definition under either name, neither constraint present, or **both** present | nothing. Refused; the tool never guesses which DDL repairs a shape it did not create |

Both constraints present is not a state this tool can produce — the swap is one atomic catalog
transaction — so coexistence means something outside the tool altered the schema, and it fails closed.

`--check-only` reports an unfinished closure as `RESUME_REQUIRED` (or `LEDGER_REPAIR_REQUIRED`) with
exit 9 rather than as success. Gates G1–G5 apply to a fresh `OPEN → CLOSED` closure; a resume needs
only the data gates G1–G3, because the EXPAND constraint is legitimately gone by then and requiring
G4/G5 would make the unfinished state unfinishable. A resume against rows that violate the pair
invariant is refused with `RESUME_BLOCKED_BY_DATA_VIOLATION` rather than left to fail inside
`VALIDATE`.

**Resumability contract.** Rerunning converges every client to *strict constraint present, exact
definition, validated, ledger recorded*. Both remaining steps are idempotent — `VALIDATE CONSTRAINT`
on a validated constraint is a PostgreSQL no-op, the ledger insert is `ON CONFLICT DO NOTHING` — so a
rerun never duplicates a constraint, never drops a correct validated one, and is a no-op for a client
that is already fully closed.

**Interaction with release activation.** An interrupted closure is *not* a state a release may
activate against blindly: `ADD CONSTRAINT ... NOT VALID` already rejects request-only inserts, so the
`SCHEMA_STATE_GUARDS` capability check in §11 fires on it exactly as it does on a completed closure.
The bridge release's `CONTRACT` alternative, however, requires `validated = true`, so an interrupted
closure fails the bridge release's schema preflight — the transition must be finished, not left
half-done.

Deterministic evidence:

| suite | establishes |
|---|---|
| `ops/tests_manual/test_first_seen_pair_contract_closure_postgres.py` | the state machine, interruption, resume, post-`VALIDATE`/pre-ledger recovery, idempotency, data-violation refusal |
| `ops/tests_manual/test_mlag_contract_transition_bridge_postgres.py` | the release-side bridge, the ten-case alternative-state **exclusivity** matrix (including the malformed-competing-state reproduction and the runtime proof that it rejects a pair-atomic INSERT), mixed-fleet behaviour and the legacy-writer refusal |
| `ops/tests_manual/test_mlag_activation_contract_race_postgres.py` | both orderings of the activation ↔ closure race through the real `activate_release` and closure entry points, with the blocking wait observed in `pg_locks`; legacy refusal after closure with the pointer unmoved; bridge activation still allowed; resumability under the lock; **the G6 stale window in both directions** — a supported pointer mutation between the snapshot and the lock is observed by the protected re-read and refuses before any client DDL, and a mutation attempted while the closure holds the lock genuinely blocks and cannot invalidate the accepted envelope |
| `ops/tests_manual/test_mlag_rollback_envelope.py` | the eight materialized-envelope cases against real prepared releases, the two-sequential-activation rollout, the closure tool's G6 refusal before any database connection, and the second review's adversarial probes: wrong bridge declarations (`CHECK (false)`/`CHECK (true)`/wrong `EXPAND`/flipped validation/name-only/missing or merged alternative/missing capability), fabricated `{}` and doctored metadata, pointer escape, dangling and aliased targets, a synthetic directory that is not a release inventory, and the production-identity refusal of an arbitrary release root; and the third review's probes: the exact-declaration cases (extra member in either group, duplicated member, duplicated/extra/split/merged groups, a second collectively-declaring relation, the wrong relation), the one-way G6/preflight agreement property against the real relation matcher, and the release-metadata identity mismatches; and the fourth review's complete-relation probes: the `ck_synthetic_unconditional` reproduction on an honestly prepared release, a required column and a required index neither canonical state carries, a column both states do carry (still `READY`), and out-of-contract constraint/index declarations where G6 is deliberately stricter |

**A note on scope.** These suites establish the *mechanism*. They do not, and cannot, establish that
the production envelope is open: that depends on a second bridge-compatible release that does not
exist yet.

---

## 14. Migration lock safety

`scripts/apply_client_business_migrations.py` connects with `autocommit=False`, executes the **whole
file** in a single `cur.execute(...)`, and commits once. Every statement in a client migration
therefore shares one transaction — which means the `ACCESS EXCLUSIVE` lock taken by `ALTER TABLE …
ADD COLUMN` is held until the file ends, and anything scanning or building after it runs with the
table fully locked against readers and writers.

048 therefore contains only catalog-only work, and a static test asserts it stays that way:

* no `VALIDATE CONSTRAINT` — deferred to §13, which owns its own transactions;
* no `CREATE INDEX`. The aggregation-read index was **removed**: it is an optimization, not a
  correctness requirement (the recompute reads a bounded trailing window of `end_timestamp`, served
  by existing access paths, at operator cadence rather than in the ingestion path). If measurement
  later shows it is needed it belongs in its own operator step using `CONCURRENTLY`;
* no `CONCURRENTLY` — PostgreSQL forbids it inside a transaction block, which this runner always
  provides, so it cannot rescue an in-migration index build;
* no `DEFAULT` on the new column, so there is no table rewrite;
* no `UPDATE` — the migration never imputes a timestamp.

---

## 15. Onboarding

A client onboarded after the M-LAG release must not receive a schema older than the running writer
expects — the trip INSERT names the new column unconditionally, so such a client could not ingest at
all. `scripts/onboard_workflow_a_client.py` therefore carries 048 in **both** authoritative lists:

* `CLIENT_BUSINESS_DDL_FILES` — applied directly to the new database;
* `CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED` — recorded in the baseline, so the existing-client
  runner does not later re-apply it and the two rollout paths converge rather than drift.

A new client starts in **EXPAND** state like everyone else. The CONTRACT ledger entry is deliberately
not pre-marked: claiming the contract is closed without installing the constraint would be a lie in
the ledger.
