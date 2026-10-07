# Telematics `trips_sync` stabilization windows — architecture decision (D1)

**Status: architecture decision only. Nothing in this document is implemented.**

This document resolves blocking open decision **D1** of
`docs/12_telematics_trips_pagination_compatibility.md` §17: *how scheduled Telematics `trips_sync`
jobs fetch only stabilized data while preserving complete, gap-free temporal coverage.*

It changes no runtime code, no provider client, no schedule row, no migration and no systemd unit.
Every item below is a *proposal* that requires its own reviewed implementation task and its own
validation gate.

**Path note.** The task requested `docs/13_telematics_trips_stabilization_windows.md`. Ordinals
`00`–`12` are occupied (`10_` twice: `10_platform_architecture.md`, `10_scheduler_design.md`), and
`13_` is the next free ordinal. The requested path is therefore already the nearest consistent path
in the existing documentation map and is used unchanged. The original task changed no other
documentation file; the subsequent bootstrap correction additionally makes a small additive status
note in `docs/12_telematics_trips_pagination_compatibility.md` §17.1, and nothing else.

Related canonical documents:

- `docs/12_telematics_trips_pagination_compatibility.md` — the compatibility-mode design this
  decision unblocks (§3 activation, §6.4 eligibility, §9 write boundary, §17 D1).
- `docs/05_jobs.md` § `jobs.api.telematics.sync_trips_and_speeding`, § dispatcher,
  § `backfill_trips_insert_only`.
- `docs/07_operations.md` §5.4 (Telematics hard safety limits), §5.4.1 (`/trips` diagnostic),
  §5.5 (dispatcher).
- `docs/09_disaster_recovery.md` — recovery/backfill boundaries.
- `docs/10_scheduler_design.md` — scheduler model.

Code and configuration of record for this decision (all read read-only for this task):

- `jobs/api/telematics/dispatcher.py` — `ScheduleRow`, `latest_scheduled_fire_local`,
  `evaluate_schedule`, `select_next_due`, `_claim_fire`, `_build_job_params`, `_launch_job`,
  `_finalize_run`.
- `jobs/api/telematics/sync_trips_and_speeding.py` — `run()`, `_parse_runner_iso_ts`,
  `_build_trip_fetch_chunks`, `_fetch_trips_in_chunks`, `_compute_speeding_violation_counts`,
  the single client-business transaction.
- `jobs/api/telematics/provider_client.py` — `iter_31d_windows`, `_provider_dt_str`,
  `sub_window_label`, `fetch_trips`.
- `jobs/api/telematics/control_plane.py` — `ClientAccountConfig`, `load_client_account_config`,
  `DatasetSchedule`, `load_dataset_schedule`.
- `db/migrations/008_workflow_a_control_plane.sql`,
  `db/migrations/012_workflow_a_client_dataset_schedule.sql`,
  `db/migrations/014_workflow_a_dispatcher_v1.sql`,
  `db/migrations/018_workflow_a_schedule_event_enrichment_mode.sql`,
  `db/migrations/040_workflow_a_trip_metrics_population_source.sql`.
- `ops/tests_manual/test_workflow_a_dispatcher.py`,
  `ops/tests_manual/test_workflow_a_trip_chunking.py`.

---

## 0. Decision in one paragraph

> **Revision note (bootstrap correction).** The first version of this document was accepted on its
> window mathematics, missed-fire expansion, transaction ordering, DST analysis and overlap model,
> and **rejected on its bootstrap and uninitialized-coverage semantics**
> (`TELEMATICS_STABILIZATION_ARCH_REVIEW_BLOCKED_BOOTSTRAP_UNSAFE`). It permitted a first
> compatibility run against an unset watermark and then set the watermark from that run's own end.
> A single `covered_through_ts` with no lower boundary implicitly claims contiguity all the way back
> to the beginning of client history, even though the run verified only its own window — which in
> the present production incident would have permanently hidden the 2026-07-30 / 2026-07-31 hole.
> **Coverage state is now an explicitly bounded closed interval
> `[coverage_start_ts, covered_through_ts]` with a `bootstrap_status`, and compatibility mode fails
> closed with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` until that state is seeded from reviewed
> evidence** (§5.2, §5.5, §13). No statement in this document permits `W IS NULL` as an acceptable
> first-run state. The accepted mathematics of §4, §8, §9, §10, §11, §12, §14 and §15 are unchanged.

**Architecture A (full-window shift), plus a minimal per-schedule coverage interval — an explicitly
bounded `[coverage_start_ts, covered_through_ts]` with a `bootstrap_status` — that is used only for
missed-fire recovery and gap detection, never for normal window derivation.** The
dispatcher, and only the dispatcher, shifts a scheduled window back by a fixed UTC
`stabilization_delay` and pre-rolls its start by a fixed UTC `overlap`. Normal consecutive runs are
gap-free by arithmetic alone, with no state involved. The coverage interval exists solely so that a
missed or failed fire — which the dispatcher never catches up — is healed by the next successful run
instead of leaving a permanent hole, so that a hole too large to heal is surfaced loudly rather than
silently, and so that the platform's continuity claim states **where it begins** and not only where
it currently ends. Compatibility mode does not run at all until that interval has been seeded from
reviewed evidence. Missing, identity-invalid, non-claiming or malformed state aborts with
`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`; only a structurally valid recorded `GAP_DETECTED` re-emits
`TRIPS_COVERAGE_GAP_DETECTED`. Both occur before any provider request. Jobs never shift a window they are
given, which makes operator-supplied historical windows immune to double-shifting by construction.
Everything is client-scoped, inert while `trips_pagination_mode = 'strict_meta'`, and reversible by
a single control-plane `UPDATE`.

Architectures B, C, D and E are evaluated in §5–§9 and explicitly rejected, with B's persisted state
retained in the reduced, non-load-bearing, two-bounded role described above.

---

## 1. Current window derivation — reconstructed from code and persisted configuration

### 1.1 Exact production path

| # | Stage | Code of record | Behaviour |
|---|---|---|---|
| 1 | Schedule definition | `workflow_a_control.client_dataset_schedule` (migrations `012`, `014`, `018`) | `frequency`, `day_of_week`, `day_of_month`, `day_of_month_last`, `run_time TIME`, `timezone TEXT`, `lookback_days INTEGER NOT NULL DEFAULT 7 CHECK (lookback_days >= 0)`, `overwrite_existing`, `event_enrichment_mode`. `run_time` is a **naive local wall-clock time** interpreted in `timezone`. |
| 2 | Schedule load | `dispatcher._load_enabled_schedules` | Joins `client_dataset_schedule` × `client_account` × `dataset_registry`, `WHERE cds.enabled AND ca.enabled`. No caching between ticks. |
| 3 | Registry allowlist | `dispatcher._validate_against_registry` | `dataset_name` must be in `jobs/api/telematics/registry.py`; DB `job_module` must equal the Python one. |
| 4 | Fire evaluation | `dispatcher.latest_scheduled_fire_local` | `now_local = now_utc.astimezone(ZoneInfo(timezone_name))`; the **latest** fire at or before `now_local` is built with `datetime.combine(local_date, run_time, tzinfo=tz)`. No multi-fire enumeration. |
| 5 | `scheduled_fire_ts` | `dispatcher.evaluate_schedule` | `fire_utc = fire_local.astimezone(timezone.utc)`. Returns `None` if `fire_utc > now_utc`. `now_utc = datetime.now(timezone.utc).replace(microsecond=0)` (`prepare_run`), so every timestamp on this path is second-precision. |
| 6 | Window derivation | `dispatcher.evaluate_schedule` lines 322–324 | `window_end = fire_utc`; `window_start = window_end - timedelta(days=max(lookback_days, 0))`. **The window end is the fire timestamp, which is at or before "now" at execution time.** |
| 7 | Ordering + claim | `select_next_due`, `_claim_fire` | Sorted by `(scheduled_fire_ts, client_code, dataset_name)`. `INSERT … ON CONFLICT (schedule_id, scheduled_fire_ts) DO NOTHING` into `client_schedule_run_history` with `window_start_ts`, `window_end_ts`, `scheduled_fire_ts`, `status='RUNNING'`. |
| 8 | Job parameters | `dispatcher._build_job_params` | For `trips_sync`: `client_id`, `client_code`, `trigger="SCHEDULED"`, `window_start_ts`, `window_end_ts` (both `_iso_z`, i.e. `…T…Z`), `event_enrichment_mode`. **`scheduled_fire_ts` is *not* passed to `trips_sync`** — only Eco Driving datasets receive it. |
| 9 | Subprocess | `dispatcher._launch_job` | `python ops/runner.py <job_module> <params_json>`, `LOG_PLATFORM_RUN_ID_FILE` set; `rc == 0` ⇒ history row `SUCCESS`. |
| 10 | Job parse | `sync_trips_and_speeding.run` + `_parse_runner_iso_ts` | Accepts `Z`/`+00:00`/`%Y-%m-%d %H:%M:%S`; naive input is assumed UTC; result is always `astimezone(utc)`. Rejects `window_end_ts < window_start_ts`. The job **does not** recompute or adjust the window — it uses what it is given, literally. |
| 11 | Chunking | `_build_trip_fetch_chunks` (`chunk_days` default `2`, cap `5`, `TRIPS_CHUNK_BOUNDARY_STEP = 1s`) | Partitions `[W_start, W_end]`: non-final chunk `i` is requested as `[s_i, e_i − 1s]` and the next chunk starts at `e_i`; the final chunk is requested as `[s_n, W_end]`. `start == end` yields one degenerate chunk. |
| 12 | Sub-window split | `provider_client.iter_31d_windows(max_days=31, overlap_seconds=1)` | With `chunk_days ≤ 5` the 31-day split never triggers, so exactly one sub-window per chunk, identical to the chunk. The 1-second overlap branch is unreachable on the `trips_sync` path. |
| 13 | Provider format | `provider_client._provider_dt_str` | `to_utc(dt).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")` — UTC, **second-truncated**, space-separated. Sent as `start_timestamp` / `end_timestamp`. |
| 14 | DB upsert | `sync_trips_and_speeding` ≈ lines 4700–4890 | `INSERT … ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET …` when `overwrite_existing`, else `DO NOTHING`; then an absolute (`SET =`, not `+=`) speeding-bucket `UPDATE` scoped by `AND sync_run_id = %s`. One connection, opened only after all fetching, one `conn.commit()`. |

### 1.2 Timezone behaviour, exactly

- **Storage.** `client_dataset_schedule.run_time` is `TIME` (naive) and `timezone` is `TEXT`; the
  pairing is resolved only in Python by `zoneinfo`. `client_schedule_run_history.window_start_ts`,
  `window_end_ts`, `scheduled_fire_ts` are `timestamptz` — absolute instants, unambiguous.
- **Fire computation is local-wall-clock first, UTC second.** `datetime.combine(date, run_time,
  tzinfo=ZoneInfo(tz))` then `.astimezone(timezone.utc)`. A schedule in `Europe/Warsaw` therefore
  fires at a *different UTC instant* in winter and summer.
- **Everything downstream is UTC.** `_iso_z`, `_parse_runner_iso_ts`, `_to_utc`, `_provider_dt_str`
  all normalise to UTC. `api/timezone_utils.set_pg_session_timezone` sets the *session display*
  timezone (default `Europe/Warsaw`); it does not affect `timestamptz` semantics.
- **Precision is one second everywhere.** `now_utc` is `microsecond=0`; `run_time` carries whole
  seconds; `_provider_dt_str` truncates to seconds. Sub-second reasoning is not meaningful on this
  path.

### 1.3 Enabled `trips_sync` schedules (read-only from the production control plane)

| Client | Dataset | Schedule timezone | Run time | Frequency | Lookback | Nominal window formula (UTC) |
|---|---|---|---|---|---|---|
| `DELTA00001` | `trips_sync` | `Europe/Warsaw` | `02:00:00` | daily | 7 d | `[F − 7 d, F]`, `F = 02:00 Warsaw → 01:00Z` (CET) / `00:00Z` (CEST) |
| `ALPHA00001` | `trips_sync` | `UTC` | `02:00:00` | daily | 1 d | `[F − 1 d, F]`, `F = 02:00:00Z` |
| `FOXTROT00001` | `trips_sync` | `UTC` | `02:00:00` | daily | 1 d | `[F − 1 d, F]`, `F = 02:00:00Z` |
| `BRAVO00016` | `trips_sync` | `Europe/Warsaw` | `02:00:00` | weekly, `day_of_week=0` (Mon) | 7 d | `[F − 7 d, F]`, `F = Monday 02:00 Warsaw → 01:00Z` (CET) / `00:00Z` (CEST) |
| `ECHO00001` | `trips_sync` | `UTC` | `02:00:00` | daily | 1 d | **disabled** (`enabled = false`) — has never fired; onboarded through the §13.6a cold-start path, not through §13.2–§13.6 |

`overwrite_existing = true` and `event_enrichment_mode` is `enabled` for all of the above except
`ALPHA00001`, which is `disabled`.

Two facts from this table drive the rest of the document:

1. **Only `DELTA00001` has a lookback comfortably larger than its fire interval.** `ALPHA00001` and
   `FOXTROT00001` run daily with a 1-day lookback — consecutive windows *abut* with no slack.
   `BRAVO00016` runs weekly with a 7-day lookback — consecutive windows also abut with no slack.
2. **The two schedules with zero slack that are also DST-exposed matter differently.** `ALPHA00001`
   and `FOXTROT00001` are `UTC`, so they have no DST exposure at all. `BRAVO00016` is
   `Europe/Warsaw` with a 7-day lookback and a 7-day fire interval, which is *already* defective
   across the autumn transition — see §11.3.

---

## 2. Coverage contract

### 2.1 Interval semantics as implemented

The provider request is a **closed interval at one-second resolution**:
`start_timestamp` and `end_timestamp` are both sent as `%Y-%m-%d %H:%M:%S` UTC, and the provider is
assumed to include both endpoints. Every layer above it preserves that reading:

- `_build_trip_fetch_chunks` subtracts exactly one second from the end of every non-final chunk and
  starts the next chunk at the un-subtracted boundary. That is only correct if the request end is
  **inclusive** — otherwise the boundary second would be dropped. `ops/tests_manual/
  test_workflow_a_trip_chunking.py` asserts precisely this
  (`six_days[0].request_end_ts == 2026-04-05T23:59:59Z` and
  `six_days[1].request_start_ts == 2026-04-06T00:00:00Z`).
- `iter_31d_windows` uses `overlap_seconds = 1` for the same reason ("overlap by a second to avoid
  missing events that fall exactly on boundaries").
- The DB write is keyed on `(client_id, provider_trip_id)`, not on the window, so re-requesting a
  second twice is harmless.

There is **no** `23:59:59`-style end-of-day adjustment anywhere in the scheduled path; the
`2026-03-25T23:59:59Z` example in `docs/07_operations.md` is an operator-typed manual window, not a
derived one.

### 2.2 Definition of "no gap"

> **Coverage contract.** Let `S` be the set of one-second instants on the UTC timeline. A schedule
> provides gap-free coverage of an interval `I` if every instant `s ∈ I ∩ S` is contained in at
> least one *closed* requested window `[W_start, W_end]` of a run that reached `SUCCESS`.

Two consequences that are used throughout:

- Adjacent windows are **contiguous** if `next_start ≤ prev_end + 1 s`. Equality of endpoints
  (`next_start == prev_end`) is contiguity with exactly **one duplicated representable timestamp**
  and **zero duration of overlap**; it is *not* a gap. See §2.4 for why those two are not the same
  measurement.
- Coverage is defined over *requested* windows, not over rows. A window that was requested and
  returned nothing still counts as covered. This is the only definition the system can actually
  verify, because the provider's completeness cannot be proven from our side — see
  `docs/12_…` §6.1.

### 2.3 Which convention adjacent windows use

The existing repository convention is **`[start, end]` closed on both ends, with adjacent
scheduled windows sharing exactly the boundary second**: run *N* ends at `F` inclusive and run
*N+1* starts at `F` inclusive. This decision **preserves that convention unchanged**. Half-open
`[start, end)` is rejected: adopting it would require changing `_build_trip_fetch_chunks`'s
one-second boundary step and `iter_31d_windows`'s overlap, i.e. altering the strict-mode data path,
which is out of scope and explicitly forbidden by `docs/12_…` §2.1 G3 and §19.2.

All arithmetic below is therefore expressed in **absolute UTC seconds**, and contiguity is tested
as `next_start ≤ prev_end + 1 s`.

### 2.4 Overlap terminology under closed intervals

Two different quantities are easy to conflate, and the first version of this document conflated
them. They are kept distinct from here on.

| Quantity | Definition | Units |
|---|---|---|
| **Overlap duration** | The length of the continuous-time intersection `[a₂, b₁]` of two windows `[a₁, b₁]`, `[a₂, b₂]` with `a₂ ≤ b₁`, i.e. `b₁ − a₂`. | seconds of elapsed time |
| **Duplicated representable timestamps** | The count of one-second grid points contained in both windows: `b₁ − a₂ + 1` (both endpoints inclusive). | count of points |

Consequences at the one-second precision this path actually uses (§1.2):

- A configured `O = 3600` means **3600 seconds of continuous-time overlap**. Because both endpoints
  are included, the same configuration corresponds to **3601 duplicated representable second
  timestamps**.
- A **shared endpoint** (`a₂ = b₁`) is **zero duration of overlap** and **one duplicated
  representable timestamp**. It is *not* "one second of duration overlap", and this document does
  not describe it that way.
- The `1 s` in the contiguity test `next_start ≤ prev_end + 1 s` is a *grid step*, not a duration of
  overlap. It expresses "the next window may start at the very next representable instant".

This is a **naming correction only**. No formula, no default, no runtime convention changes:
`_build_trip_fetch_chunks` still steps by one second, `iter_31d_windows` still uses
`overlap_seconds = 1`, `O` is still stored as a plain second count, and the idempotent
`(client_id, provider_trip_id)` upsert still makes any duplicated instant harmless.

---

## 3. The D1 problem restated precisely

`docs/12_…` §6.4 requires, for `trips_pagination_mode = 'data_invariants_v1'`, that every
sub-window satisfy `sub_window_end_ts < now_utc − stabilization_delay`, with a proposed default
delay of 180 minutes.

Scheduled windows end at `F`, and the dispatcher only fires when `F ≤ now_utc`, so
`window_end ≈ now_utc`. Every scheduled window therefore fails the eligibility rule and would abort
with `PAGINATION_COMPAT_WINDOW_INELIGIBLE`. Compatibility mode would be usable only for manual and
recovery runs — which is not a usable production state, since the strict path currently aborts with
`PAGINATION_MISMATCH` and scheduled ingestion is stopped entirely.

The naive fix — truncate the end only — is architecture E and is proven defective in §9.

---

## 4. Architecture A — shift the entire window (**selected**, with the §5 coverage interval)

### 4.1 Formula

```
effective_start = nominal_start − stabilization_delay − overlap
effective_end   = nominal_end   − stabilization_delay
```

### 4.2 Mathematical continuity between consecutive fires

Let `F_n` and `F_{n+1}` be consecutive fires, `Δ = F_{n+1} − F_n`, `L` the lookback in seconds,
`D` the delay and `O` the overlap (all in absolute UTC seconds).

```
E_end(n)     = F_n − D
E_start(n+1) = F_{n+1} − L − D − O
```

Contiguity requires `E_start(n+1) ≤ E_end(n) + 1 s`:

```
F_{n+1} − L − D − O ≤ F_n − D + 1 s
⇔ Δ − L − O ≤ 1 s
⇔ Δ ≤ L + O + 1 s
```

**`D` cancels completely.** The stabilization delay cannot create a gap under a full-window shift,
for any value, because it translates both endpoints by the same amount. The contiguity condition is
exactly the one that already governs the *unshifted* schedule (`Δ ≤ L + 1 s`), relaxed by the
overlap. This is the decisive property that architectures B, C, D and E do not have.

For the four enabled schedules, with `Δ` at its DST-worst value:

| Client | `L` | `Δ` (nominal) | `Δ` (DST worst) | `L + O + 1 s` with `O = 3600 s` | Contiguous? |
|---|---|---|---|---|---|
| `DELTA00001` | 604 800 s (7 d) | 86 400 s | 90 000 s (25 h, autumn) | 608 401 s | Yes, with 6 days of slack |
| `ALPHA00001` | 86 400 s (1 d) | 86 400 s | 86 400 s (UTC, no DST) | 90 001 s | Yes, exactly + 3600 s slack |
| `FOXTROT00001` | 86 400 s (1 d) | 86 400 s | 86 400 s (UTC, no DST) | 90 001 s | Yes, exactly + 3600 s slack |
| `BRAVO00016` | 604 800 s (7 d) | 604 800 s | 608 400 s (169 h, autumn) | 608 401 s | Yes — by exactly one grid step of margin (§2.4) |

The `BRAVO00016` row is the tightest case in production and is the reason `O ≥ 3600 s` is the
recommended default rather than an arbitrary small number: see §11.3.

### 4.3 Fixed processing latency

The shift introduces a *fixed, known* data latency of `D` seconds. With `D = 10 800 s` (180 min),
data is ingested at most 3 hours later than today. For `DELTA00001` (`F = 01:00Z` winter) the
effective end becomes `22:00Z` of the previous day. No downstream consumer in this repository reads
`client_trips` with an assumption tighter than "yesterday's data is present after the nightly run";
`aggregate_trip_fuel_daily` and the Eco Driving aggregations are themselves scheduled jobs reading
closed historical periods. **A 3-hour shift moves the daily boundary for a `02:00`-local schedule
to `23:00`/`22:00` local of the previous day, which is still inside the same calendar day.** A
delay large enough to cross a local midnight (`D > 2 h` for a `02:00` local fire) would move
`effective_end` into the previous local day — which is exactly what `D = 3 h` does. That is
acceptable *because the window is 1–7 days long and its end is not a day boundary in the first
place*, but it is a fact operators must know, and it is why `D` is a per-client column rather than
a constant.

### 4.4 One-day and seven-day lookbacks

Both are handled identically by §4.2; only the slack differs. The one-day lookback schedules
(`ALPHA00001`, `FOXTROT00001`) have zero slack in `Δ ≤ L + 1 s` today and gain exactly `O` seconds of
slack under this design. The seven-day daily schedule (`DELTA00001`) re-fetches six days of already
ingested data on every fire; that is pre-existing behaviour and is unchanged.

### 4.5 DST transitions and timezone conversion

**Rule: `D` and `O` are absolute UTC durations, applied *after* the local-to-UTC conversion of the
fire timestamp.** They are stored in *seconds* precisely so that no implementation can apply them
as local wall-clock arithmetic. Applying them in local wall-clock time would make the shift one
hour longer or shorter across a transition and would reintroduce exactly the duplicated/missing
real-time intervals this design exists to prevent.

Full proof with concrete instants in §11.

### 4.6 Manual runs and backfills

Under this architecture the shift is computed **only in the dispatcher**, at claim time, from
`(F, L, D, O)`. The job receives an already-effective window and never adjusts it. Therefore an
operator-supplied window is used literally, and double-shifting is impossible by construction, not
by a flag check. See §12.

### 4.7 First run after enablement

`E_end` for the first compatibility-mode fire is `F − D`; the last strict-mode fire covered up to
`F_prev`. Since `F − D > F_prev` whenever `D < Δ` (true for `D = 3 h` against `Δ ≥ 24 h`), coverage
moves *forward* at the switch and no interval is skipped. The interval `(F − D, F]` — the
"stabilization tail" — is not covered by the first compat run; it is covered by the *next* run,
whose `E_start ≤ E_end` of this one. The tail is therefore **deferred by exactly one fire
interval, never lost**.

This paragraph describes the *arithmetic* of the first shifted window only. It is **not** a
statement that the first compatibility fire may run against an unseeded coverage state. Enablement
requires a `READY` coverage row seeded from reviewed evidence **before** the mode flip; without one
the run aborts with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` before any provider request. See §13.

### 4.8 Changing the delay later

Let the delay change from `D₁` to `D₂` between fire *n* and fire *n+1*.

- **Increase (`D₂ > D₁`).** `E_start(n+1) = F_{n+1} − L − D₂ − O` moves *earlier*, so contiguity is
  strictly easier. `E_end(n+1) = F_{n+1} − D₂` is later than `E_end(n) = F_n − D₁` as long as
  `D₂ − D₁ < Δ`. An increase of a full fire interval or more makes `E_end` regress; the watermark's
  `max()` rule (§5.3) prevents that from corrupting recorded coverage, and the run harmlessly
  re-fetches.
- **Decrease (`D₂ < D₁`).** `E_end` jumps forward by `D₁ − D₂`, and pure arithmetic contiguity
  requires `D₁ − D₂ ≤ O`. A decrease larger than the overlap would open a gap under architecture A
  alone. **The watermark expansion in §5.2 closes it automatically** — this is one of the two
  concrete reasons the watermark is worth its cost.

### 4.9 Schedule-history observability; must both windows be logged?

**Yes — both, and they must be distinguishable.** Under `strict_meta` the nominal and effective
windows are identical, so no existing evidence changes meaning. Under `data_invariants_v1` they
differ by `D`/`O`, and an operator reconstructing coverage after an incident needs the *effective*
window (what was actually requested) while an auditor reviewing the derivation needs the *nominal*
one (what the schedule said). Neither is recoverable from the other after the fact, because
`lookback_days`, `stabilization_delay` and `overlap` are all mutable configuration. See §14.

### 4.10 Should an overlap additionally be applied?

Yes. It is not needed for arithmetic continuity in the ideal case (`Δ ≤ L` already holds for every
enabled schedule at nominal `Δ`), but it is needed for three separate reasons:

1. **DST slack.** The maximum inter-fire shortfall introduced by a one-hour DST transition is
   exactly 3600 s. `O ≥ 3600 s` makes the shifted arithmetic DST-safe *without* consulting any
   state — including for `BRAVO00016`, whose current configuration is already defective across the
   autumn transition (§11.3).
2. **Bounded tolerance for late corrections** at the trailing edge of the previous window — the
   youngest and therefore most correction-prone data. See §10.
3. **Margin against a delay decrease** (§4.8).

**Proposed default: `O = 3600 s`.**

---

## 5. Architecture B — stable cutoff plus persisted watermark (**rejected as primary; its
watermark adopted in a reduced role**)

### 5.1 Why B is rejected as the primary derivation

The B formula is `effective_end = min(nominal_end, now − delay)`, `effective_start =
previous_successful_effective_end − overlap`.

| Concern | Assessment |
|---|---|
| **`min()` is decorative** | The dispatcher only evaluates a schedule when `F ≤ now_utc`, so `nominal_end = F ≤ now`, and therefore `min(F, now − D) = now − D` whenever `D > 0` and the tick is prompt. B's window end is, in practice, always execution-time-derived. |
| **Non-reproducible windows** | `now` is the moment a 5-minute-timer tick happened to claim the row. Two runs of the same logical fire would produce different windows; a delayed or backlogged tick silently changes the data boundary. `scheduled_fire_ts` stops being a description of the data. |
| **Variable window length** | `E_end − E_start` varies run to run with tick jitter, so `chunk_days` behaviour, page counts and provider budget consumption become non-deterministic. |
| **The watermark becomes load-bearing** | Under B, a wrong or corrupt watermark silently changes *normal* coverage with no analytic fallback. Under A it can only affect *recovery*. |
| **Concurrency and failure semantics** | The watermark must be advanced exactly once per successful run. Under B this is on the critical path for every run; under A it is not. |
| **Schema coupling** | B makes the new state table mandatory for correctness; A makes it a recovery aid. |
| **Rollback** | Reverting B to `strict_meta` leaves a stale watermark that was load-bearing; reverting A leaves a stale watermark that was advisory. |

### 5.2 What is adopted: a per-schedule **bounded coverage interval**, expansion-only

The adopted state is **not** a bare watermark. It is an explicitly bounded, closed, operationally
verified interval:

```
A = coverage_start_ts     — the earliest instant for which contiguous coverage is claimed
W = covered_through_ts    — the latest instant for which contiguous coverage is claimed
```

> **Semantic contract (normative).** Coverage has been operationally verified **only** for the
> closed interval `[A, W]`. Nothing in this design permits `W` to imply verified coverage back to
> the beginning of client history, back to client onboarding, or back to any instant earlier than
> `A`. A coverage row without `A` is not a weaker claim than `[A, W]` — it is **no claim at all**,
> and it is refused.

Both bounds are mandatory. `bootstrap_status` records whether the pair is usable:

| Status | Meaning | Compatibility mode may run normally? |
|---|---|---|
| `UNINITIALIZED` | The row exists but no reviewed evidence has been bound to it. `A`/`W` may be `NULL`. | **No** |
| `READY` | `A` and `W` are both set, internally valid, and backed by a reviewed bootstrap evidence bundle. | **Yes** |
| `GAP_DETECTED` | A run observed `E_start > W + 1 s`, or bootstrap evidence failed to establish contiguity. The interval is frozen. | **No** |
| `RESEED_REQUIRED` | The stored proof has been invalidated (strict round-trip, material configuration change, schedule re-creation). | **No** |

No further states are introduced. `GAP_DETECTED` and `RESEED_REQUIRED` are distinct because they
have different remedies: the first needs recovery of a *known* interval, the second needs a fresh
proof of an interval that may in fact be intact.

For a schedule in `READY` status, architecture A's start is *expanded backwards* — never forwards —
to whatever the interval shows is still uncovered:

```
E_end   = F − D
base    = (F − L) − D − O
E_start = min(base, W − O)
E_start = max(E_start, E_end − R)       # recovery span cap, R default 31 d
```

`W` is never `NULL` on this path, because a non-`READY` state never reaches it (§5.2.1). Because it
is a `min`, the interval can only ever make the window **wider**. It can never shorten a window,
never move `E_end`, and never override the schedule's own lookback. In normal operation with
`Δ ≤ L` the `min` selects `base` and the coverage state is arithmetically inert. `R` is capped at
31 days to stay inside the provider's documented lookup limit and inside `_build_trip_fetch_chunks`
+ `iter_31d_windows` behaviour.

**Requesting earlier than `A` does not extend the claim.** `min(base, W − O)` and the `R` cap are
purely *request* arithmetic; a run whose `E_start < A` simply re-requests data outside the managed
interval. It does **not** move `A`, and it does **not** convert unverified history into claimed
coverage. Only a reviewed bootstrap or reseed (§13) may set or move `A`.

### 5.2.1 Fail-closed preconditions and recorded-gap precedence

While `trips_pagination_mode = 'data_invariants_v1'`, the read-only C5 gate applies this exact order:

1. A missing coverage row returns **`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`**.
2. The row must match the fired schedule ID, client ID, dataset name and the accepted nullable/equal
   client-code contract. Any identity mismatch returns `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`; no
   status-specific classification is attempted.
3. Only `READY` and `GAP_DETECTED` claim that the row represents or preserves a previously verified
   interval. Before either status is accepted semantically, both bounds must exist, be datetime
   values, be timezone-aware absolute instants that normalize to UTC, use whole-second precision,
   satisfy `coverage_start_ts <= covered_through_ts`, and have `covered_through_ts <= now_utc` under
   the scheduled-fire contract. `bootstrap_evidence_ref` and `seeded_by` must exist and remain
   nonblank after trimming; `seeded_at` must exist and be a timezone-aware datetime. The accepted
   C5 seed-timestamp contract does not add whole-second precision. Failure of any shared
   foundational requirement returns `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` for either status.
4. Only after steps 1–3 pass, an existing `GAP_DETECTED` row returns
   **`TRIPS_COVERAGE_GAP_DETECTED`**, exposes no effective launch window, preserves normalized `A/W`,
   sets `requires_gap_persistence=false`, and remains loud on every later due fire.
5. `UNINITIALIZED`, `RESEED_REQUIRED`, an unknown status and a `NULL` status return
   `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`; they never receive the gap code.
6. Only a structurally valid `READY` reaches effective-window derivation. A connected result is
   allowed. A disconnected result returns `TRIPS_COVERAGE_GAP_DETECTED`, exposes no effective launch
   window, preserves `A/W`, and sets `requires_gap_persistence=true` for future C6 ownership.

The `GAP_DETECTED` literal is therefore not proof of a trustworthy recorded gap. It is a narrow
exception to generic non-`READY` taxonomy only while the row still satisfies the same foundational
identity, interval, evidence and seed-metadata integrity used to recognize verified `READY` state.

Every rejection occurs before any provider request, credential resolution, socket, subprocess or
client-business write. The claimed fire is finalized as durable `FAILED` with `error_summary` equal
to the abort code, an `ERROR` log and `suspected_bug` evidence. C5 performs no coverage `INSERT`,
`UPDATE`, `DELETE`, status transition, timestamp write or advancement. Under C6, only the newly
disconnected valid `READY` rejection adds the atomic pre-launch gap/history transaction of §5.3;
every other rejection leaves coverage unchanged. Neither C5 nor C6 self-initializes from schedule
history, `max(synced_at)`, client trips or the fire's own window.

`strict_meta` is unaffected: it neither reads nor writes coverage state, so an invalid coverage row
cannot block current production behaviour.

### 5.3 C6 mutation rules — successful advancement and durable gap persistence

**Document ownership.** This document supplies the domain invariants. `docs/14_…` supplies C6
commit ownership and delivery sequence. `docs/15_telematics_coverage_mutation_contract.md` supplies
the exact transaction, compare-and-swap (CAS), crash-reconciliation and test contract. The three
documents describe the same placement; no precedence choice is required.

**Current delivery boundary.** C5 implements only the read-only gate. It never executes either C6
mutation below. A structurally valid recorded `GAP_DETECTED` re-emits
`TRIPS_COVERAGE_GAP_DETECTED` on every later due fire with `requires_gap_persistence=false` and is
not rewritten. Malformed recorded-gap state is bootstrap-required and is not mutated.

There are two distinct C6 mutations on two distinct branches.

**Successful advancement.** This branch is reached only for an allowed `READY` fire after the
subprocess returns `rc == 0`. `_finalize_compat_success` locks and validates the claim-time coverage
snapshot, computes:

```
new_W = max(current_W, E_end)
```

and, when `new_W > current_W`, advances `W` atomically with history `RUNNING → SUCCESS`. When
`E_end <= current_W`, it still locks and validates the complete claim-time snapshot, issues no
coverage `UPDATE`, leaves every coverage column byte-for-byte unchanged, finalizes history
`RUNNING → SUCCESS`, and commits. No compatibility `SUCCESS` exists without that atomic coverage
decision.

**Durable gap persistence.** This branch is reached only when a valid `READY` row produces
`gate.abort_code == TRIPS_COVERAGE_GAP_DETECTED` and `requires_gap_persistence == true`.
After the durable `RUNNING` claim and before `_build_job_params`, subprocess construction,
credential resolution, sockets or provider/client access, `_finalize_compat_gap` locks and
validates the claim-time coverage snapshot, changes `READY → GAP_DETECTED`, and changes history
`RUNNING → FAILED` in one transaction. It generates one UTC-aware whole-second `mutation_ts` once
and binds it to both `last_gap_detected_ts` and `updated_at`. `F` is not the mutation timestamp.
No subprocess runs. This transaction is not part of, inside, or after successful advancement.

Both mutations preserve these properties:

- **`A` is immutable during ordinary scheduled advancement.** No scheduled run writes
  `coverage_start_ts`, in either direction.
- **`W` is monotone**, by `max()`.
- **A normal run cannot move the start boundary backwards** and so cannot manufacture an earlier
  coverage claim than the one an operator reviewed.
- **A normal run cannot move the start boundary forwards** either, which would silently *abandon*
  verified history.
- **Any change to `A` is an explicit bootstrap or reseed operation** under §13, with its own
  evidence bundle and its own review.

- **Only after client commit.** The dispatcher advances `W` in the same transaction as
  `_finalize_compat_success`, and only when the subprocess returned `rc == 0`. `rc == 0`
  means `ops/runner.py` PATCHed the platform run to `SUCCESS`, which means the job's single
  `conn.commit()` on the client-business DB completed. There is no earlier honest point.
- **`max()` makes it monotone**, so a delay increase, a clock anomaly or a re-run can never regress
  recorded coverage.
- **The pure connectivity guard** means the interval never silently jumps over a hole. A hole larger
  than `R` takes the pre-launch gap-persistence branch: `W` remains pinned, the status changes once
  from `READY` to `GAP_DETECTED`, and later fires remain loud without refreshing either gap
  timestamp until an operator recovers the hole and performs a reviewed reseed.
- **Neither branch touches `A`.** A gap at the leading edge is a `W` problem; it is never repaired
  by redefining where verified coverage began.
- Gap persistence also leaves `bootstrap_evidence_ref`, `seeded_at`, `seeded_by` and
  `covered_through_source` unchanged.

### 5.4 Concurrency, failures, retries, terminal rows

| Case | Behaviour |
|---|---|
| **Concurrency** | The dispatcher advisory lock excludes cooperating dispatchers but not operator sessions. Each C6 finalizer uses the global row-lock order **coverage row → history row**, validates the complete claim-time CAS snapshot, and commits or rolls back both surfaces together. No row lock crosses subprocess execution. |
| **Manual runs** | An ordinary ad hoc or manual run never reads and never writes `A` or `W`. This prevents an operator backfill from silently jumping the interval past data it did not fetch. The single deliberate exception is the reviewed C11 manual-recovery surface (`ops/recover_telematics_trips_window.py`), which is not an ad hoc run: it carries its own durable identity, is anchored at `W`, and advances `W` only through the same expected-old-`W` compare-and-swap the scheduled finalizer uses. **After C11, exactly two reviewed surfaces may advance `W`:** scheduled compatibility success finalization and C11 manual-recovery success finalization. Everything else — every job, every script, every hand-written statement — still writes no coverage. |
| **Failed run** | `rc != 0` ⇒ history row `FAILED`, `A` and `W` unchanged. The next successful fire expands over the failed interval via §5.2. |
| **Uninitialized / non-`READY` state** | `UNINITIALIZED`, `RESEED_REQUIRED`, unknown and `NULL` statuses fail closed with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. A structurally valid recorded `GAP_DETECTED` is the narrow exception and re-emits `TRIPS_COVERAGE_GAP_DETECTED`; a malformed one remains bootstrap-required. State is unchanged and C5 launches nothing (§5.2.1). |
| **Retries** | There are none at the fire level: `UNIQUE (schedule_id, scheduled_fire_ts)` makes a fire single-shot. A retry is a *new* fire with a later `F`, and it self-heals via the coverage interval. |
| **Terminal `FAILED` history rows** | Immutable. Nothing in this design updates, deletes or re-statuses them. Recovery is a new run with new rows, per `docs/12_…` §14.2. |
| **Stale `RUNNING` auto-failed** | `_mark_stale_running` flips the row to `FAILED` after `stale_running_timeout_minutes` (default 720). `W` was never advanced for it. Correct by construction. |
| **Corruption risk from a wrong `W`** | Bounded on both sides: too-old `W` ⇒ a wider window, capped at `R`, idempotent by upsert, costing provider budget only; too-new `W` ⇒ the `min` selects `base` instead, so the schedule's own lookback still governs. A too-new `W` can never shrink a window below `base`. **This bound holds only because `A` exists**: without a start boundary, a too-new `W` would additionally be an unbounded *claim* about unverified history, which no arithmetic can bound. |
| **Corruption risk from a wrong `A`** | `A` never affects window arithmetic, so a wrong `A` cannot change what is fetched. It changes only what the platform *claims*, which is why it is settable exclusively by the reviewed §13 procedure and is recorded with its evidence reference, seeder and timestamp. |
| **Rollback** | Set `trips_pagination_mode = 'strict_meta'`. Coverage state is then neither read nor written; the row is inert and becomes stale (§13.4). No migration rollback. |
| **Operational complexity** | One additive table, one read in `prepare_run`, and two separate compatibility-only finalizers: `_finalize_compat_success` and `_finalize_compat_gap`. Existing strict/non-trips `_finalize_run` remains unchanged and executes no coverage SQL. No new process, unit or connection. |

### 5.5 Where the coverage interval lives

**Schema fixed by migration 057: an additive table keyed on `schedule_id`.** The exact names and
semantics below are normative.

```
workflow_a_control.client_dataset_coverage (
  schedule_id UUID PRIMARY KEY
    REFERENCES workflow_a_control.client_dataset_schedule (schedule_id) ON DELETE CASCADE,
  client_id UUID NOT NULL,
  client_code TEXT NULL,
  dataset_name TEXT NOT NULL,

  coverage_start_ts TIMESTAMPTZ NULL,      -- A: earliest instant of the verified closed interval
  covered_through_ts TIMESTAMPTZ NULL,     -- W: latest instant of the verified closed interval

  bootstrap_status TEXT NOT NULL DEFAULT 'UNINITIALIZED',
    -- CHECK (bootstrap_status IN
    --   ('UNINITIALIZED','READY','GAP_DETECTED','RESEED_REQUIRED'))
  bootstrap_evidence_ref TEXT NULL,        -- artifact id / ticket ref for the §13.5 bundle
  seeded_at TIMESTAMPTZ NULL,
  seeded_by TEXT NULL,                     -- operator identity that seeded or reseeded

  covered_through_source TEXT NOT NULL,    -- 'bootstrap' | 'scheduled_run' | 'operator'
  last_gap_detected_ts TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CHECK (coverage_start_ts IS NULL OR covered_through_ts IS NULL
         OR coverage_start_ts <= covered_through_ts),
  CHECK (bootstrap_status <> 'READY'
         OR (coverage_start_ts IS NOT NULL
             AND covered_through_ts IS NOT NULL
             AND bootstrap_evidence_ref IS NOT NULL
             AND seeded_at IS NOT NULL
             AND seeded_by IS NOT NULL))
)
```

The second `CHECK` is what makes §5.2.1 enforceable in the database and not only in Python: a
`READY` row without both bounds and without an evidence reference cannot exist. The runtime check
remains as well, because the database cannot verify that the evidence reference is *meaningful* —
only that it is present.

**Why not reuse `workflow_a_control.client_sync_state`.** That table exists and is documented as
unused (`CONVENTIONS.md` §10, `CURRENT_TASK_CONTEXT.md` §4.2), but it is keyed
`(client_id, dataset)` with `CHECK (dataset IN ('trips','speeding_notifications'))` — the dataset
vocabulary does not include `trips_sync`, so reuse would require altering an applied migration's
constraint semantics, and its key is not the schedule. It also carries only a single
`last_success_window_end_ts`, i.e. exactly the unbounded-watermark shape this correction rejects.
Keying on `schedule_id` gives the right lifecycle: disabling and re-enabling a schedule preserves
`schedule_id` and therefore the interval, while deleting and recreating a schedule row removes it
via `ON DELETE CASCADE` — which is correct, because a recreated schedule is a new coverage claim
and must be bootstrapped again rather than inheriting an unrelated proof.

---

## 6. Architecture C — delayed execution with a logical fire boundary (**rejected**)

Three distinct readings, all rejected:

1. **Change the actual schedule fire time** (e.g. move `run_time` from `02:00` to `05:00`). This is
   explicitly out of scope ("do not modify schedules") and does not solve anything: the window end
   would still equal the new fire timestamp, so it would still be "now". It relabels the problem.
2. **Retain the business fire timestamp and add an execution delay.** The dispatcher would have to
   claim a fire and then *not* execute it for `D` seconds. This collides with the strict queue:
   a claimed row is `RUNNING`, `_count_running() > 0` makes every subsequent tick a no-op for
   *every* schedule, and `_mark_stale_running` would eventually auto-fail it. Holding a claim idle
   for 3 hours would stall the entire dispatcher for 3 hours. Implementing a "scheduled but not yet
   runnable" state means a new status value, a new index, changes to the `ck_run_history_status`
   CHECK and a new gate in `prepare_run` — a large change to the scheduler's core contract to
   achieve what a subtraction achieves.
3. **Derive the data cutoff independently from execution time.** This *is* architecture A, restated.
   Its only difference is presentational — it would keep `window_end_ts = F` in evidence while
   requesting something else, which is strictly worse observability than A's explicit
   nominal/effective pair.

**Verdict: rejected.** C either modifies schedules, breaks the dispatcher's queue invariants, or
reduces to A with worse auditability. The existing schedule machinery does not support it without
semantic confusion about what `scheduled_fire_ts` means.

---

## 7. Architecture D — stable core plus recent tail (**rejected**)

Split the window into a stabilized core handled by `data_invariants_v1` and a recent tail handled
by some other strategy.

| Candidate tail strategy | Why it is rejected |
|---|---|
| Tail via `strict_meta` | `strict_meta` currently aborts on this provider with `PAGINATION_MISMATCH` (`docs/12_…` §1.4). The tail would fetch nothing and fail the whole run — `TelematicsProviderSafetyError` is fatal at run level (`CONVENTIONS.md` §4). |
| Tail assumed single-page at `limit = 1000` | Explicitly forbidden by the task and by `docs/12_…` §0.3 U1–U8. The one measured data point is 58 trips in **one hour** for `DELTA00001`; a 3-hour tail extrapolates to ≈174 rows, and a 25-hour tail (autumn `Δ` for a daily schedule) to ≈1450 rows — already over `limit = 1000`. Fleet growth, a longer delay or a busier client breaks it silently, and the broken `meta` cannot tell us it broke. |
| Tail via a third pagination contract | Inventing a second unproven termination rule doubles the surface that `docs/12_…` §5 exists to constrain, with no evidence base whatsoever. |
| Tail written separately | Breaks the single-commit boundary of `docs/12_…` §9.2. A core-succeeds/tail-fails run would leave half a window ingested — precisely the corruption the compatibility design exists to prevent. |

**Verdict: rejected.** Every tail strategy either depends on broken metadata, assumes single-page
behaviour that cannot be proven, or breaks the write boundary.

---

## 8. Architecture E — truncate only the end (**rejected, with proof**)

```
effective_start = nominal_start                (unchanged)
effective_end   = nominal_end − D
```

### 8.1 The gap

```
E_end(n)     = F_n − D
E_start(n+1) = F_{n+1} − L
```

Contiguity requires `F_{n+1} − L ≤ F_n − D + 1 s`, i.e.

```
Δ ≤ L − D + 1 s
```

Compare with architecture A's `Δ ≤ L + O + 1 s`. **Unlike A, `D` does not cancel — it is
subtracted from the available slack.** Every fire loses exactly `D` seconds of coverage that no
later run ever requests, unless the lookback exceeds the fire interval by more than `D`.

The user's example is the `L = Δ = 86 400 s` case: nominal `[T−24 h, T]` truncated to
`[T−24 h, T−3 h]`, next nominal `[T, T+24 h]` truncated to `[T, T+21 h]`. The interval
`(T−3 h, T]` is requested by no run, ever.

### 8.2 Applied to the actual production schedules

| Client | `L` | `Δ` | `L − D + 1 s` with `D = 10 800 s` | Gap-free under E? |
|---|---|---|---|---|
| `DELTA00001` | 604 800 s | 86 400 s | 594 001 s | **Yes** (large lookback masks it) |
| `ALPHA00001` | 86 400 s | 86 400 s | 75 601 s | **No** — 3 h lost per day, permanently |
| `FOXTROT00001` | 86 400 s | 86 400 s | 75 601 s | **No** — 3 h lost per day, permanently |
| `BRAVO00016` | 604 800 s | 604 800 s | 594 001 s | **No** — 3 h lost per week, permanently |

Three of four enabled schedules are broken by E, including the weekly one, whose 7-day lookback
*looks* generous but exactly equals its fire interval. The single schedule where E happens to work
does so by accident of configuration, and would break the moment `lookback_days` were reduced.

### 8.3 Verdict

**Rejected.** E must not be selected unless start boundaries are also changed (which makes it A) or
state is retained (which makes it A+B). No complete gap-closing mechanism for E is specified here,
because the two mechanisms that would close it are precisely the selected architecture.

---

## 9. Comparison table

Scores: **++** strong, **+** adequate, **0** neutral, **−** weak, **−−** disqualifying.

| Criterion | A — full-window shift | B — persisted watermark | C — delayed logical boundary | D — stable core + tail | E — end truncation |
|---|---|---|---|---|---|
| Gap-free normal operation | **++** `D` cancels analytically | + correct but state-dependent | + (reduces to A) | − depends on unproven tail | **−−** fails for 3 of 4 schedules |
| Missed-fire recovery | − none on its own | **++** inherent | − none | − none | **−−** none, plus a per-run gap |
| Implementation complexity | **++** one subtraction in `evaluate_schedule` | − new table + write path on the critical path | **−−** new run status, queue semantics | **−−** two fetch strategies, two write paths | ++ trivial (and wrong) |
| Schema requirements | ++ config columns only | − config + state table (load-bearing) | − new status value + CHECK change | 0 config only | ++ none |
| Auditability | **++** nominal/effective both derivable and logged | + effective logged, nominal implicit | − `scheduled_fire_ts` loses meaning | − two provenance paths per window | + simple but hides the loss |
| Rollback | **++** one `UPDATE`, no state to unwind | − stale load-bearing state | − schema change to unwind | − partial-ingest risk on revert | ++ one `UPDATE` |
| DST safety | **++** with `O ≥ 3600 s`, proven in §11 | + watermark absorbs it silently | + (reduces to A) | 0 orthogonal | − DST loss compounds the truncation loss |
| Manual-run clarity | **++** job never shifts ⇒ no double-shift possible | − manual runs must be excluded from watermark writes | − ambiguous fire semantics | − which strategy for a manual window? | + literal |
| Backfill compatibility | **++** backfills are closed windows, ideal for §6.4 | + but must not advance `W` | 0 | − tail strategy meaningless for history | + |
| Late-data correction | + bounded by `O` and `L` | + bounded by `O` and `L` | + same | ++ tail is the freshest data | − truncation removes the freshest data entirely |
| Production risk | **++** lowest — arithmetic only | − a wrong watermark changes normal coverage | **−−** stalls the dispatcher | **−−** silent partial ingestion | **−−** silent permanent data loss |

**Selection: A as the authoritative derivation, with B's watermark demoted to an expansion-only
recovery mechanism.** This keeps A's analytic guarantee for the normal path (the common case,
which must never depend on state) and buys B's missed-fire healing for the exceptional path (the
rare case, where state is the only thing that can help).

---

## 10. Late-arriving and corrected trips

### 10.1 What a stabilization delay does and does not handle

| Case | Handled by `D` alone? | Actually handled by |
|---|---|---|
| Late provider ingestion within `D` of the trip | **Yes** | `D`. This is exactly what `D` is for. |
| Late ingestion after `D`, within `O` of the previous window's tail | No | `O`, on the next fire. |
| Late ingestion later than `D + O` | **No** | Only the lookback: schedules with `L > Δ` (`DELTA00001`) re-request the last `L` seconds daily and pick it up; `ALPHA00001`/`FOXTROT00001`/`BRAVO00016` have `L ≈ Δ` and will **not**. Requires an operator backfill. |
| Edits to historical trips (times, distance, odometer) | **No** | Only re-requesting a window that contains the trip's `start_timestamp`. `D` does not help at all: it delays the *first* read, it never schedules a *second* one. |
| Changed driver or vehicle assignment | **No** | Same as edits. When re-fetched, `ON CONFLICT … DO UPDATE` overwrites `driver_name`, `driver_surname`, `driver_tag_description`, `identification_tag_id`, `vehicle_id`, `registration`, `vehicle_name`, `vehicle_description`, `chassis_number` and `"Driver_Restrictions"` — verified in the `SET` list. With `overwrite_existing = false` the `DO NOTHING` branch silently ignores corrections; all four enabled schedules use `true`. |
| Changed speeding fields | Partly | Buckets are recomputed absolutely per run and written with `SET speeding_… = %s … WHERE sync_run_id = %s`, not incremented. Re-processing a trip therefore replaces rather than accumulates. |
| Deleted trips | **No** | **Not representable locally at all.** The write path only inserts and updates; nothing deletes. A trip removed at the provider remains in `client_trips` indefinitely. See §17 risk R3. |

**Conclusion: a stabilization delay is not a correction mechanism.** It bounds *when we first
look*; only overlap and lookback bound *how often we look again*. This must be stated plainly in
the operations runbook so nobody reads `D` as a data-quality guarantee.

### 10.2 Overlap specification

- **Proposed duration: `O = 3600 s` (1 hour).** Justification, in order of weight: (1) it is
  ≥ the maximum DST-induced inter-fire shortfall, which makes §4.2 hold without consulting state
  and independently repairs the pre-existing `BRAVO00016` autumn defect (§11.3); (2) it is
  `D / 3`, so it never approaches the stabilization boundary; (3) at `chunk_days = 2` a one-hour
  pre-roll adds at most one extra `/trips` sub-window per run, well inside every budget in
  `docs/12_…` §5.5.
- **Why it is safe with the existing upsert.** The conflict target is
  `(client_id, provider_trip_id)`. A trip re-returned in the overlap resolves to the same row.
  With `overwrite_existing = true` it is refreshed from the newest provider payload — which is the
  desired behaviour for a correction. `docs/12_…` §9.2 additionally proposes a pre-commit assertion
  that the prepared batch contains no duplicate `(client_id, provider_trip_id)`; the overlap does
  not violate it, because the overlap causes a trip to appear in two *different runs*, never twice
  in one batch.
- **How repeated rows affect speeding aggregation.** They do not accumulate. `_compute_speeding_
  violation_counts` recomputes counts from scratch for every trip in the run, matching violations
  to trips by normalised registration and `start_ts ≤ event_ts ≤ end_ts` containment, and the
  `UPDATE` assigns absolute values. There is one directional subtlety worth recording: vehicle
  events are fetched for the *same* `[W_start, W_end]` as trips, so a trip lying across the window
  end can be undercounted in run *n*; in run *n+1* the same trip sits near the window *start* with
  its full event span inside, so the recount is complete and overwrites the undercount. **The
  overlap therefore strictly improves speeding accuracy at the trailing edge.** The reverse
  (a complete count overwritten by a truncated one) cannot occur for forward-advancing scheduled
  windows; it *can* occur for an arbitrary operator window, which is why
  `docs/05_jobs.md`'s repair path mandates `insert_only = true`.
- **Do corrections overwrite all relevant mutable fields?** Yes, when `overwrite_existing = true`:
  every business column in the `INSERT` list except the conflict key appears in the `DO UPDATE SET`
  list, plus `record_id`, `sync_run_id` and `synced_at`. The metric columns
  (`high_rpm_events_count`, `overrev_events_count`, `speeding_*`) are included only when
  `trip_metrics_population_source` grants this job ownership.
- **Can provider deletes be represented locally?** No. Recording this as an accepted, documented
  limitation (R3), not as something the overlap or the delay fixes.

---

## 11. DST and timezone proof

**Rule (normative): `D` and `O` are absolute UTC second counts, subtracted from the UTC-converted
fire instant. Never applied in schedule-local wall-clock time, and never before the local→UTC
conversion.**

All instants below were computed against the actual `zoneinfo` behaviour used by
`dispatcher.latest_scheduled_fire_local`, with `D = 10 800 s`, `O = 3600 s`.

### 11.1 UTC schedules (`ALPHA00001`, `FOXTROT00001`) — daily 02:00Z, `L = 1 d`

No transitions exist. `Δ = 86 400 s` exactly, always.

| Fire | `F` | `E_start = F − 1 d − D − O` | `E_end = F − D` |
|---|---|---|---|
| n | `2026-03-28T02:00:00Z` | `2026-03-26T22:00:00Z` | `2026-03-27T23:00:00Z` |
| n+1 | `2026-03-29T02:00:00Z` | `2026-03-27T22:00:00Z` | `2026-03-28T23:00:00Z` |
| n+2 | `2026-03-30T02:00:00Z` | `2026-03-28T22:00:00Z` | `2026-03-29T23:00:00Z` |

`E_start(n+1) = 2026-03-27T22:00:00Z ≤ E_end(n) + 1 s = 2026-03-27T23:00:01Z`. Contiguous with
3600 s of overlap on every fire. Identical arithmetic across the autumn transition.

### 11.2 Europe/Warsaw daily (`DELTA00001`) — 02:00 local, `L = 7 d`

Spring 2026 (transition 2026-03-29, 02:00→03:00 local):

| Local date | Resolved `F` (UTC) | Note |
|---|---|---|
| 2026-03-27 | `2026-03-27T01:00:00Z` | CET, UTC+1 |
| 2026-03-28 | `2026-03-28T01:00:00Z` | CET |
| 2026-03-29 | `2026-03-29T01:00:00Z` | **02:00 local does not exist on this date.** `zoneinfo` with `fold=0` resolves it via the pre-transition offset (CET), i.e. the job fires at 03:00 CEST local. No fire is skipped and none is duplicated. |
| 2026-03-30 | `2026-03-30T00:00:00Z` | CEST, UTC+2 |

`Δ(29→30) = 23 h = 82 800 s ≤ L + O + 1 s`. Contiguous with ≈6 days of slack.

Autumn 2026 (transition 2026-10-25, 03:00→02:00 local):

| Local date | Resolved `F` (UTC) | Note |
|---|---|---|
| 2026-10-24 | `2026-10-24T00:00:00Z` | CEST |
| 2026-10-25 | `2026-10-25T00:00:00Z` | **02:00 local occurs twice.** `fold=0` selects the first (CEST) occurrence. |
| 2026-10-26 | `2026-10-26T01:00:00Z` | CET |

`Δ(25→26) = 25 h = 90 000 s ≤ 604 800 + 3600 + 1 s`. Contiguous with ≈6 days of slack.

### 11.3 Europe/Warsaw weekly (`BRAVO00016`) — Monday 02:00 local, `L = 7 d` — the tight case

**Finding: this schedule already has a one-hour coverage gap across the autumn transition, today,
under the current unshifted derivation. It is independent of D1 and pre-dates this design.**

| Fire | Resolved `F` (UTC) | Nominal window (current production) |
|---|---|---|
| Mon 2026-10-19 | `2026-10-19T00:00:00Z` | `[2026-10-12T00:00:00Z, 2026-10-19T00:00:00Z]` |
| Mon 2026-10-26 | `2026-10-26T01:00:00Z` | `[2026-10-19T01:00:00Z, 2026-10-26T01:00:00Z]` |

`Δ = 169 h = 608 400 s > L + 1 s = 604 801 s`. The interval
`(2026-10-19T00:00:00Z, 2026-10-19T01:00:00Z]` is requested by neither fire — a **3600-second
permanent gap**, every autumn.

Under the selected architecture with `O = 3600 s`:

```
E_end(Oct 19)   = 2026-10-19T00:00:00Z − D
E_start(Oct 26) = 2026-10-19T01:00:00Z − D − 3600 s = 2026-10-19T00:00:00Z − D
```

`E_start(Oct 26) = E_end(Oct 19)` exactly — contiguous, with zero duration of overlap and exactly
one duplicated representable timestamp (§2.4), matching
the §2.3 convention. **The `O = 3600 s` default repairs the pre-existing autumn gap as a side
effect, and the §5.2 watermark expansion would close it independently even if `O` were smaller.**

Spring for the same schedule: `F(Mon 2026-03-23) = 2026-03-23T01:00:00Z`,
`F(Mon 2026-03-30) = 2026-03-30T00:00:00Z`, `Δ = 167 h`. `E_start(Mar 30) = 2026-03-23T00:00:00Z −
D − O = 2026-03-22T20:00:00Z` vs `E_end(Mar 23) = 2026-03-23T01:00:00Z − D = 2026-03-22T22:00:00Z`
— a two-hour overlap, no duplication problem because the upsert is idempotent.

### 11.4 Conclusion

Applying `D` and `O` as absolute UTC durations after conversion yields **no duplicated real-time
interval that is not already absorbed by the idempotent upsert, and no missing real-time interval**
for any of the four enabled schedules, at either transition. The `O ≥ 3600 s` requirement is not
cosmetic: with `O = 0` the `BRAVO00016` autumn case would remain defective under architecture A
alone and would depend entirely on the watermark.

---

## 12. Manual runs, recovery runs and backfills

**Normative rule: shifting happens exactly once, in `dispatcher.evaluate_schedule`. No job ever
shifts a window it is given.**

This single rule answers every case and makes accidental double-shifting structurally impossible
rather than flag-dependent.

| Case | `window_*_ts` semantics | Shift applied? | Watermark |
|---|---|---|---|
| Normal scheduled run | Effective (already shifted by the dispatcher) | Yes, once, in the dispatcher | Read for expansion; advanced on `SUCCESS` |
| Manually specified historical window | Literal operator input | **No** | Not read, not written |
| **Reviewed C11 manual recovery** (`ops/recover_telematics_trips_window.py`) | Literal operator input, anchored at `W` | **No** | Read under lock; advanced to `window_end_ts` **only** after `rc == 0`, through the shared expected-old-`W` CAS, with `covered_through_source = 'manual_recovery'` |
| Backfill (`jobs.api.telematics.backfill_trips_insert_only`) | Literal, `insert_only = true` required | **No** | Not written |
| Dry-run / compare-only (`docs/12_…` §3.8) | Literal | **No** | Not written |

**Why C11 uses the normal synchronization and not the insert-only backfill.** The
purpose of a compatibility recovery is to exercise and close a *compatibility*
interval, so it must run the same production code path a scheduled compatibility
fire would run — otherwise a green recovery proves nothing about the path being
validated, and the interval it closes was fetched by different code than the one
`W` now claims. `jobs.api.telematics.backfill_trips_insert_only` remains a separate
business-data repair tool for known ingestion holes: it is insert-only, it never
reads or writes coverage, and it is **not** the canary or recovery mechanism.

**What is still forbidden.** Ad hoc manual coverage SQL, a hand-written `UPDATE`
on `covered_through_ts`, any coverage write from a job, a script, the provider
client or an API route, and any coverage mutation on a failure path. A failure
never advances `W`.

Consequences:

- **Explicit historical manual windows never get shifted**, so they are never double-shifted. The
  question "should the shift be skipped when `explicit_end ≤ now − D`?" does not arise, because
  the shift is not in the job at all.
- **The eligibility check remains, as a guard, not as a shifter.** In `data_invariants_v1` the job
  still verifies `sub_window_end ≤ now_utc − D` per `docs/12_…` §6.4 and aborts with
  `PAGINATION_COMPAT_WINDOW_INELIGIBLE` otherwise. For a **recent manual window** this is exactly
  the desired failure: the run fails closed before any provider request, and the operator either
  waits or runs the client in `strict_meta`.
- **The guard is automatically satisfied for every scheduled run.** Proof: the dispatcher only
  fires when `F ≤ now_utc`, and `E_end = F − D ≤ now_utc − D`; every sub-window end is `≤ E_end`.
  So the shift makes eligibility a theorem rather than a runtime hope — which is the concrete
  reason D1 is answered by shifting rather than by relaxing §6.4.
- **Schedule-derived manual runs are explicitly not offered.** A `derive_window_from_schedule=true`
  parameter would create a second shifting site and reintroduce the double-shift risk. Operators
  who want the shifted window compute it from the formula in §15.1 and pass it literally.

---

## 13. Bootstrap contract, first enablement and mode changes

**This section supersedes the bootstrap treatment of the first version of this document.** Every
earlier statement that a first compatibility run may proceed with an unset watermark, or that
`covered_through_ts` may be seeded from the most recent `SUCCESS` history row alone, is withdrawn.

### 13.0 Why seeding from the latest `SUCCESS` is not sufficient evidence

A `SUCCESS` schedule-history row proves that *one* window was requested and that the run's single
client-business transaction committed. It proves nothing about:

- whether *earlier* windows were ever requested at all (the dispatcher never catches up, so a
  missing fire leaves **no row of any kind**, and absence is invisible when you read only the newest
  row);
- whether the intervals between accepted windows are connected;
- whether a `FAILED` fire inside the range was ever recovered;
- whether the provider returned complete pages for the window (`docs/12_…` §6.1 — provider
  completeness is not provable from our side, which is exactly why this design claims only
  *operational* completeness).

`max(synced_at)` in the client business database is weaker still: it is the timestamp of the last
write, not a statement about which temporal segments were requested, and it advances even for a run
that fetched a one-hour window.

Seeding `W` from the newest `SUCCESS` while leaving the lower bound implicit therefore converts
"we successfully fetched one recent window" into "all history through that instant is contiguous" —
a claim nobody verified. In the present production state that single step would permanently conceal
the 2026-07-30 / 2026-07-31 hole (§13.6).

### 13.1 Fail-closed enablement precondition

Compatibility mode must not be enabled, and must not run normally, without a `READY` coverage row.
The runtime abort classification is **`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`**, specified in §5.2.1:
it fires before any provider request and before any client-business write, leaves coverage state
unchanged, leaves terminal history rows immutable, and produces operator-visible evidence. Coverage
is **never** initialized automatically from a successful scheduled run.

### 13.2 Gate 1 — read-only inventory, *before* the mode flip

The inventory is a precondition of choosing an interval, so it is performed while the client is
still `strict_meta` and compatibility is still disabled. Doing it after the flip would mean the
first compat fire could race the review. It must cover, per schedule in scope:

1. enabled schedule configuration (`frequency`, `run_time`, `timezone`, `lookback_days`,
   `overwrite_existing`, `event_enrichment_mode`, and the client's `trips_pagination_mode`);
2. schedule fire history — every `client_schedule_run_history` row for the schedule in the candidate
   range, with status, `scheduled_fire_ts`, `window_start_ts`, `window_end_ts`;
3. **missing fire timestamps** — fires the schedule *should* have produced, enumerated from the
   schedule definition and diffed against the rows that exist. This is the only way a
   no-row-at-all gap becomes visible;
4. terminal `FAILED` rows, with their abort codes;
5. successful rows;
6. nominal and effective windows for each row (identical while `strict_meta`);
7. client-database synchronization bounds — `min`/`max` trip `start_timestamp`, row counts and
   `synced_at` bounds per segment, as corroboration only, never as the primary claim;
8. duplicate provider-trip identities, if any;
9. known source-side and ingestion incidents overlapping the range (including the
   `PAGINATION_MISMATCH` incident of `docs/12_…` §1.4);
10. the resulting list of **current gaps requiring recovery**.

The inventory is read-only: no writes, no provider requests, no job launches. §19 already proposes
the read-only coverage-inventory query that produces items 2–3 and 10.

### 13.3 Gate 2 — select an explicit managed coverage interval

The operator chooses **two** values and records the reasoning:

```
bootstrap_coverage_start_ts     → A
bootstrap_covered_through_ts    → W
```

`A` must be an explicit instant. It must **not** be inferred as negative infinity, as the client's
onboarding date, as the earliest trip in the client database, or as "the beginning of client
history". The bootstrap ticket must state *why that instant is the point from which the platform is
willing to claim operational continuity* — typically the start of the segment the operator has
actually reconciled under §13.4, chosen to be short enough to be provable and long enough to cover
the recovery need.

Choosing a **narrow** `A` is always permitted and is the correct response to weak evidence: a narrow
verified interval is an honest claim, whereas a wide unverified one is the defect this correction
removes. History earlier than `A` is not asserted to be either present or absent — it is simply
outside the managed interval.

### 13.4 Gate 3 — reconcile committed client data

**Definition (operational completeness).** For bootstrap purposes an interval may be accepted as
operationally complete only when the evidence shows *all* of:

1. every required temporal segment in the declared interval was requested by a successful, validated
   fetch, or by a separately authorized recovery run;
2. no pagination safety abort occurred for those segments (no `PAGINATION_MISMATCH`,
   `PAGINATION_LOOP`, `PAGINATION_NON_PROGRESS`, or compatibility-mode abort);
3. provider identity invariants passed — page-local and cross-page checks of `docs/12_…` §5.2/§5.3,
   including disjointness of provider trip identities across pages;
4. the client-business transaction committed for each such run (`rc == 0`, run `SUCCESS`);
5. expected row counts and unique provider-trip identities were reconciled between what the provider
   returned and what the client database holds;
6. no unexplained internal gap remains between accepted segments — the segments connect under the
   §2.2 contiguity test;
7. no known missing fire and no unrecovered terminal `FAILED` fire remains inside the interval.

**This is explicitly not a claim of absolute provider truth.** It is a claim that the platform
requested every segment and committed what it received. `docs/12_…` §6.1 remains in force: the
provider's own completeness cannot be proven from our side, and no bootstrap evidence asserts it.

Restated for emphasis, because the rejected version relied on each of these:

- **A `SUCCESS` schedule-history row alone is insufficient.**
- **`max(synced_at)` alone is insufficient.**

### 13.5 Gate 4 — recover known gaps before enablement, and Gate 5 — the evidence bundle

**Gate 4 (recovery).** Every missing fire and every unrecovered terminal `FAILED` fire that falls
**inside the selected `[A, W]`** must be recovered before the interval can be declared `READY`. For
the current production incident this means, concretely, that the missing fires of **2026-07-30** and
**2026-07-31** and the terminal `FAILED` fires of **2026-08-01** must be inventoried and, where they
fall inside the chosen interval, recovered through **separately authorized manual recovery runs**
(`docs/12_…` §14, `docs/09_disaster_recovery.md`).

- Historical rows are **not mutated**. No `FAILED` row is re-statused, deleted or rewritten; no
  missing fire is back-inserted into `client_schedule_run_history`.
- Recovery produces **new manual-run evidence**: new `runs` rows, new logs, new artifacts. That new
  evidence — not the old rows — is what the bundle cites.
- The alternative to recovering a gap is to *exclude* it by choosing a later `A` (§13.3). What is
  not permitted is declaring `READY` over an interval that still contains it.

**Gate 5 (evidence bundle).** A reviewable bundle is required before any coverage row is written. At
minimum it contains:

| Item | Notes |
|---|---|
| Selected coverage start and end | `A` and `W` as explicit UTC instants, plus the §13.3 rationale |
| Schedule IDs and client codes | `schedule_id`, `client_id`, `client_code`, `dataset_name` |
| Inventory of expected temporal segments | The §13.2 enumeration, including fires that produced no row |
| Successful production or recovery run IDs | Platform `runs.run_id` values backing each segment |
| Provider request/page evidence | Per sub-window request and page counts, abort codes (none), termination reason |
| Returned and unique identity counts | Provider rows returned and distinct provider trip identities per segment |
| Client-database committed row counts | Per segment, from the client business DB |
| Gap reconciliation | Each inventoried gap → the recovery run that closed it, or the `A` choice that excludes it |
| Final continuity proof | The §2.2 contiguity check applied across the ordered accepted segments |
| Environment and repository identity | Platform environment and UUID, repository HEAD, migration ceiling, wrapper hash |
| Reviewer and approval metadata | Who produced it, who reviewed it, when |
| Checksums | Content hashes of the bundle's constituent files |

**Never included:** raw trip payloads, credentials or secret references, and personal trip data
(driver names, tags, registrations beyond what an aggregate count requires) — consistent with
`docs/12_…` §16 and `CONVENTIONS.md` §7/§8. The bundle carries counts, identifiers and hashes, not
business content.

The stored `bootstrap_evidence_ref` points at this bundle; §5.5's `CHECK` makes a `READY` row
without it impossible.

### 13.6 Gate 6 — seed before the mode flip, in this order

```
1. compatibility runtime deployed but disabled (every client still 'strict_meta')
2. read-only inventory completed                                        (§13.2)
3. separately authorized recovery completed                             (§13.5 gate 4)
4. bootstrap evidence bundle reviewed and approved                      (§13.5 gate 5)
5. coverage state inserted with bootstrap_status = 'READY'              (§5.5)
6. client trips_pagination_mode changed to 'data_invariants_v1'
7. first scheduled compatibility run observed
```

- **The mode must not be enabled while the state is `UNINITIALIZED`** (or `GAP_DETECTED`, or
  `RESEED_REQUIRED`). Steps 5 and 6 are ordered, never reversed.
- **Prefer performing steps 5 and 6 as one reviewed platform-database transaction**, after all
  external evidence is complete. That removes the window in which a fire could observe a flipped
  mode with an unseeded row. If they are nevertheless separate statements, §5.2.1 is the backstop:
  the intervening fire fails closed rather than inventing a watermark.
- Step 7 observes; it does not authorize a second client. Each additional client repeats §13.2–13.6
  with its own inventory, its own interval and its own bundle.

### 13.6a Gate 6a — the zero-state cold start (a client that has never run)

§13.2–§13.6 are **evidence-based**: they inventory proven intervals and ask a human to choose
`A`/`W` inside that evidence. A newly onboarded client cannot enter them, and the refusal is
correct rather than a defect: its schedule is disabled, so it has never fired, so there is no
history to inventory and no proven interval to bound. `ECHO00001` is exactly this shape.

A **provably empty** client therefore has its own ordered sequence. It is a distinct path, not a
relaxation of the one above; a client with any historical execution — successful or failed — must
continue through §13.2–§13.6 and is refused by every tool on this path.

```
EMPTY_DISABLED_STRICT          zero coverage, zero recovery, zero history, zero platform runs,
                               zero client_trips rows, exactly one disabled authoritative
                               trips_sync schedule, mode 'strict_meta'
  -> COLD_START_EVIDENCE_READY   ops/audit_telematics_cold_start.py, classification
                                 COLD_START_ZERO_STATE_CONFIRMED (never UNRESOLVED_GAPS_PRESENT:
                                 a client that has never run has no gaps)
  -> BASELINE_COVERAGE_READY     ops/bootstrap_telematics_cold_start_coverage.py inserts exactly one
                                 zero-width baseline, A == W == the approved first managed instant
  -> COMPATIBILITY_MODE_READY    the narrow reviewed client_account mode transaction (§13.6 step 6)
  -> RECOVERY_CHAIN_IN_PROGRESS  ops/recover_telematics_trips_window.py
                                 --allow-disabled-schedule-for-cold-start, one separately approved
                                 and separately executed window per invocation, schedule still
                                 disabled throughout
  -> RECOVERY_CHAIN_COMPLETE     the last window has succeeded and W equals the approved final
                                 chain boundary
  -> SCHEDULE_ENABLED            ops/activate_telematics_trips_schedule.py, exactly one field
  -> NORMAL_SCHEDULED_OPERATION
```

Every transition is independently gated and either idempotent or safely refusing.

> **Historical defect, fixed `2026-08-04` — kept here because the corrected contract only makes
> sense against it.** The diagram above assumed that a recovery run against a disabled schedule
> performs the normal synchronization over its window. It did not.
> `jobs/api/telematics/sync_trips_and_speeding.py` carries its own guard —
> `if not schedule.enabled: ... return` — and this path *requires* a disabled schedule, so the job
> returned `0` having fetched nothing. `ops/recover_telematics_trips_window.py` read
> `returncode == 0` as business success and advanced `W` through the C6 CAS, producing a **verified
> coverage interval over zero fetched data**. Confirmed in production on `ECHO00001` (`2026-08-04`,
> run `2733f673-745c-46a9-8e51-5b513b0edc6b`, `0` provider requests, `0` business rows, `W` moved
> `2026-07-01T00:00:00Z → 2026-08-01T00:00:00Z`).
>
> **The `RECOVERY_CHAIN_IN_PROGRESS` transition is no longer blocked**; it is now gated. The
> corrected contract is stated in §13.6a.0 below, and every claim §13.6a makes about a "verified
> recovery" is true only under it. `ECHO00001` itself remains **quarantined**: its schedule stays
> disabled, its false coverage row remains present, documented as invalid, and must not be used for
> reporting. It is out of scope for the hardening and is not part of its completion criteria — see
> `docs/07_operations.md` §5.5.

#### 13.6a.0 What a cold-start recovery window must now prove

A process exit code of `0` is **not** evidence that business work happened, and is never again
sufficient to advance `W`. Three things were added, and all three are required together.

**1. An explicit manual-recovery authority.** The business job's disabled-schedule guard keeps its
default behavior: an ordinary invocation against a disabled schedule performs zero provider work and
skips. It may be passed only by an authority that is the conjunction of

* **job parameters** — `trigger = MANUAL_RECOVERY`, the exact client, the exact schedule
  (`expected_schedule_id`), dataset `trips_sync`, the exact recovery-run UUID, the exact window, and
  the explicit `allow_disabled_schedule_manual_recovery` flag;
* **a launch attestation** carried out-of-band in `TELEMATICS_MANUAL_RECOVERY_AUTHORITY`, naming the
  same identities. It travels through a channel the job parameters do not use, so a parameter set
  alone — however complete — never reaches this path;
* **the durable recovery row** in `workflow_a_control.client_dataset_recovery_run`, which must
  exist, be `RUNNING`, belong to the same client, schedule and dataset, and carry byte-identical
  window bounds, with the schedule still disabled.

**The trust level of the attestation, stated exactly.** It is an *operational attestation and
accidental-misuse guard*, not a security boundary and not authentication. It carries no secret, no
random capability and no launcher-only value: every field is a module constant or an identifier the
operator already supplies, so a local process running as the same OS user that knows the client,
schedule, dataset, recovery-run UUID and window can construct a byte-equivalent attestation. The
check that it names `ops/recover_telematics_trips_window.py` is a consistency check, **not** proof of
launcher provenance. It is consequently no defense against a malicious same-user process, an
operator deliberately constructing it, a compromised service account, or compromised platform
database credentials.

What remains genuinely meaningful, and is the reason the mechanism is worth having: the substantive
authorization gate is the **durable `RUNNING` recovery row**, against which every attested identity
and window bound is re-checked; and the **dispatcher can neither construct nor inherit** an
attestation. Between them, no ordinary accidental invocation — a stale command re-run, a copied
parameter set, a bare flag — can satisfy the complete conjunction. That is the accepted threat model.

**How the recovery row comes to exist — the actual sequence.** The row is *not* claimed by a
separate earlier operator step. `ops/recover_telematics_trips_window.py` claims it and launches the
child within the **same invocation**:

```
recovery-tool invocation (--execute)
  → all gates re-evaluated under the claim locks
  → ONE transaction: INSERT exactly one recovery row with status RUNNING   (the claim)
  → launch-attestation construction, from the claimed row's identities
  → child launch (ops/runner.py → jobs.api.telematics.sync_trips_and_speeding)
  → structured terminal outcome read back from the launcher-chosen path
  → recovery finalization, and coverage finalization only if the gate passes
```

What the operator supplies is the **approval** — the client, dataset, window, expected watermark,
reason and approval reference — which the tool validates before it claims anything. A dry run (the
default) stops before the claim and reports `would_claim_recovery`. No operator ever hand-writes a
`RUNNING` row, and no documented procedure asks them to; a row claimed out of band would still have
to match every identity and window bound, but it is not how this path works.

Any disagreement fails the run. A bare boolean, a direct job invocation, a stale recovery UUID, a
wrong client, a wrong schedule and a wrong window each refuse on their own. The dispatcher can never
hold one: it loads only `enabled = true` schedules, `_build_job_params` refuses by construction to
emit any authority parameter, and `_launch_job` strips the attestation from every child
environment. Enabled-schedule recovery behavior is unchanged; no schedule-history row is ever
synthesized.

**2. A structured terminal execution outcome.** The job writes exactly one strictly parsed JSON
record (`jobs/api/telematics/execution_outcome.py`, version `telematics-trips-execution-outcome/1`) to
the launcher-chosen path in `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE`. Its terminal values are

```
EXECUTED_COMMITTED  EXECUTED_ZERO_ROWS_COMMITTED  SKIPPED_DISABLED_SCHEDULE
SKIPPED_OTHER       FAILED
```

and it carries the client id/code, schedule id, dataset, recovery-run UUID, platform-run UUID, the
requested window bounds, whether provider execution was entered, whether business transaction logic
was entered, the transaction status, prepared/upserted/malformed counts, the skip flag and reason,
and the terminal timestamp. Free-form log matching is explicitly **not** the authoritative contract.

**3. A coverage-finalization gate.** `W` advances only when **all** hold:

```
subprocess return code = 0
AND terminal outcome is EXECUTED_COMMITTED or EXECUTED_ZERO_ROWS_COMMITTED
AND provider_execution_entered   = true
AND business_transaction_entered = true
AND transaction status           = COMMITTED
AND skipped                      = false
AND client / schedule / dataset / recovery / window identities match
AND platform-run identity is present on both sides and exactly equal
```

The three execution claims are **independent**, and none implies another: entering the provider,
reaching the business database, and committing. A record asserting a committed execution while
reporting `provider_execution_entered = false` describes rows it cannot have fetched, so it is
refused as internally contradictory at **parse** time (`EXECUTION_OUTCOME_MALFORMED`) rather than
merely being classified non-eligible — a self-contradictory record must not survive to reach any
gate that might read only part of it. The recorder agrees with the parser: reaching the terminal
statement without provider entry emits `FAILED`, never an executed outcome. An exact platform-run
identity does not rescue such a record; identity verification answers *whose* execution the record
describes, which is a different question from whether the record is coherent.

The last line is a presence requirement, not only an equality one. For an outcome that
may advance coverage, an absent, empty, blank or malformed platform-run id — on the
launcher's side or the record's — refuses (`EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING`,
`EXECUTION_OUTCOME_PLATFORM_RUN_ID_MALFORMED`), and a different valid id is an identity
mismatch. The comparison is never skipped because one side is falsy. Skip and failure
records are unaffected: `platform_run_id` stays legitimately optional there, because a job
that dies before loading its configuration never learns its platform run id and must still
be able to write a `FAILED` record.

It must not advance when the outcome is `SKIPPED_DISABLED_SCHEDULE`, any other skip, absent,
malformed, reports no committed transaction, or belongs to another or stale execution. **A zero-row
committed execution is valid work and does advance. A skipped zero-work execution never does**,
however clean its exit code. On failure the recovery evidence is preserved, the schedule stays
disabled, nothing is retried automatically, and coverage is left byte-identical.

Activation additionally requires this same structured proof for **every** window of the chain
(`ops/activate_telematics_trips_schedule.py`), so a `SUCCESS` recovery row is no longer accepted as
evidence on its own. Each accepted chain member must also carry a valid platform-run id that exactly
equals the one its own recovery row recorded, so a proof that names no platform run — or names a
different one — refuses with `ACTIVATION_REFUSED_EXECUTION_PROOF`. A consequence worth stating:
recovery rows written before `2026-08-04` carry no structured proof, so a chain built from them
cannot be activated and must be re-executed under the corrected path.

#### 13.6a.1 The recovery is a chain of one or more windows

A cold-start range longer than the client's `trips_max_recovery_span_seconds` (`R`) cannot be closed
by one recovery, and widening `R` to make it fit would defeat the cap that exists to bound provider
burn. The recovery step is therefore a **chain**:

```
baseline W → window 1 SUCCESS → W1 → window 2 SUCCESS → W2 → … → final approved W → activation
```

**A one-window cold start is a chain of length one** and stays fully supported; nothing about it
changes.

**Activation is not the end of the onboarding state machine.** The chain above covers states 5–7
only. The complete normative sequence — identical to `ONBOARDING_STATE_MACHINE` in
`scripts/onboard_workflow_a_client.py` and to the table in `docs/07_operations.md` §5.5 — is:

```
CREATED_DISABLED_STRICT → ZERO_STATE_VERIFIED → BASELINE_CREATED
→ COMPATIBILITY_MODE_SET → RECOVERY_EXECUTED_AND_COMMITTED → COVERAGE_VERIFIED
→ SCHEDULE_ACTIVATED → FIRST_NATURAL_FIRE_VERIFIED → PRODUCTION_READY
```

A client must not skip a state. `SCHEDULE_ACTIVATED` means the schedule may now fire, not that it
has: `FIRST_NATURAL_FIRE_VERIFIED` requires the first *natural* dispatcher fire to have completed
`SUCCESS` with `W` advanced from source `scheduled_run`, and only `PRODUCTION_READY` marks the client
production- and reporting-ready. Reading the chain diagram as ending the onboarding is the mistake
this paragraph exists to prevent.

**Deterministic split.** `ops/telematics_cold_start_chain.plan_recovery_windows` splits
`[start, final_end]` into consecutive windows of at most `R`: the first starts exactly at `start`,
every later one starts exactly where the previous ended (never `+ 1 s`, so no hole appears at any
internal boundary), none overlap, and the last ends exactly at `final_end`. A range that is not a
whole multiple of `R` ends with one short window rather than a widened boundary. Bounds must already
be whole-second UTC instants; nothing is rounded. `final_end <= start` and `R <= 0` are refused.
The recovery tool re-derives the split from the **current** watermark on every invocation, so an
approval computed against stale state cannot be executed.

**Chain identity without a migration.** Migration `058` has no chain column and none is added.
Identity is a structural composition of the existing `approval_ref`:

```
chain ref             TELEMATICS-COLD-START-ECHO00001-2026-08
window approval refs  TELEMATICS-COLD-START-ECHO00001-2026-08-W01
                      TELEMATICS-COLD-START-ECHO00001-2026-08-W02
```

A chain reference may not itself end in a `-W<NN>` segment, which makes the trailing segment
decidable without context, so a window reference can never be confused with a chain reference or
with a window of another chain. Every window keeps its own unique `approval_ref`, so
`uq_client_dataset_recovery_run_approved_window` keeps its full meaning and re-running an approved
window is still refused by the database. `reason` is deliberately not used to carry identity: it is
free prose with no structural contract.

**Which contract applies is decided by evidence, not by a flag.** A target carrying no row of the
declared chain is a *first window* and must satisfy every original zero-state gate unchanged. A
target already carrying chain rows is a *continuation* and must satisfy all of:

- the schedule is still disabled and the mode is still `data_invariants_v1`;
- exactly one `READY` coverage row, `covered_through_source = 'manual_recovery'`, `A` still the
  original cold-start baseline, `A < W`, and the expected current fingerprint matches;
- every recovery row attributable to the target belongs to this chain — an unrelated row refuses;
- every chain row is `SUCCESS`; a `FAILED`, `FINALIZATION_CONFLICT`, `PLANNED` or `RUNNING` row
  refuses, and is never skipped merely because a later window was requested;
- ordinals are exactly `1..N` with no gap or duplicate, each row matches the exact client, schedule
  and dataset, window 1 starts at `A`, each later window starts where the previous ended, and the
  last ends exactly at the current `W`;
- each chain window names exactly one existing `SUCCESS` platform business run, no two windows share
  a run, and no target business run belongs to no window;
- schedule history is still zero and there is no live target process;
- the requested window is the next ordinal, starts exactly at `W`, ends exactly at the deterministic
  next boundary, spans at most `R`, and carries its own unique approval reference;
- dry-run is still the default and there is still no automatic retry.

**Failure mid-chain.** A failed window preserves every prior successful recovery row and the failed
terminal row, leaves `W` at the last successfully finalized window, leaves the schedule disabled,
deletes no baseline or prior evidence, and is never retried automatically. Activation refuses while
the failed row exists. Continuation requires a new explicit operator decision and a new approval
reference.

**Activation after `N` windows.** Activation does **not** require exactly one recovery row. It
requires one named chain of `N` successful contiguous windows from `A` to the approved final
boundary, with `N` stated explicitly by the operator, the final boundary stated explicitly and equal
to the expected `W`, one `SUCCESS` business run per window, no unrelated recovery or business run,
and zero schedule-history rows. It still changes exactly one field of exactly one row.

**Why `A == W` is honest.** `[A, W]` is a closed interval of *verified* coverage. With `A == W` it
is degenerate: it spans zero elapsed time and therefore claims that no period of any duration has
been covered. It cannot overstate history, because a zero-width interval contains no period to
overstate. Migration `057`'s `ck_client_dataset_coverage_bounds_order` and
`ck_client_dataset_coverage_ready_complete` both admit `A <= W`, and `derive_effective_window`
requires only `A <= W`, so no schema change is needed.

**Why there is no boundary gap.** Every window must begin *exactly* at `W` (§12 and migration
`058`'s `ck_client_dataset_recovery_run_window_anchor`), never at `W + 1 s`. The union of the
baseline `[T, T]` and the recovered `[T, E₁] ∪ [E₁, E₂] ∪ …` is therefore `[T, Eₙ]`, contiguous
across the whole chain, and every instant from the approved managed start onwards is fetched. The CAS then advances `W` strictly (`E > T`),
`A` is never in the `SET` list, and each later scheduled fire derives
`E_start = min(base, W − O) <= W`, so `is_connected` holds by construction.

**`READY` is not "reporting-ready".** `bootstrap_status = 'READY'` is coverage-state vocabulary
meaning "this bounded claim is usable"; the schema requires it for a row to carry bounds at all.
The baseline row carries zero business data, the tooling reports `reporting_ready = false`, and the
schedule stays disabled until a recovery has succeeded.

**What this path never does.** It never synthesizes a schedule-history row, never creates a fake
successful run, never invents a historical interval, never uses `A − 1 s`, never enables the
schedule temporarily to satisfy a preflight, and never claims reporting readiness before recovery.

### 13.7 Gate 7 — bootstrap failure

If the evidence cannot establish a contiguous interval:

- set or retain `bootstrap_status = 'GAP_DETECTED'` (or leave `UNINITIALIZED` if nothing was ever
  seeded);
- **do not enable compatibility mode**;
- **do not advance `covered_through_ts`** — an unprovable interval is not made provable by moving
  its end;
- surface the unresolved intervals explicitly, as instants, in the bundle and in the ticket;
- require either additional recovery for those intervals, or a **narrower explicit
  `coverage_start_ts`** that excludes them (§13.3).

There is no fourth option. In particular there is no "enable and let the watermark sort it out".

### 13.8 Current incident bootstrap example (non-normative)

Illustrative only. **It invents no recovery result**: no recovery run has been authorized, executed
or evidenced by this task, and every value below that would come from such a run is shown as
outstanding.

Observed state, from the control plane and from §15.2:

| Date | `trips_sync` fire state |
|---|---|
| 2026-07-29 | `SUCCESS` for `DELTA00001`, `ALPHA00001`, `FOXTROT00001` — the most recent successful scheduled synchronization |
| 2026-07-30 | **no schedule-history rows at all** — the fire produced nothing and the dispatcher does not catch up |
| 2026-07-31 | **no schedule-history rows at all** |
| 2026-08-01 | terminal `FAILED` for all three (the `PAGINATION_MISMATCH` incident) |

**Why seeding directly from the latest `SUCCESS` is rejected here.** Take `ALPHA00001`
(`L = 1 d`, `F = 02:00:00Z`). The rejected procedure would set `W = 2026-07-29T02:00:00Z` from that
day's `SUCCESS` row, with no lower bound. The next compat fire would then compute
`E_start = min(base, W − O)`, cover from roughly `2026-07-29T01:00:00Z` forward, succeed, and
advance `W`. The 07-30 and 07-31 intervals sit *before* `W`, so the contiguity guard never sees
them: `E_start ≤ W + 1 s` holds trivially. The hole is never requested and never alerted — and,
because `W` carries no lower bound, the state now implicitly asserts contiguity for all history up
to that instant, including the two days that were never fetched. **The defect is not that the seed
value is wrong; it is that a single-bound state cannot express what was actually verified.**

**Why recovery evidence is required before a `READY` interval can be selected.** If the operator
wants the managed interval to *include* 2026-07-30 and 2026-07-31, those days must first be fetched
by separately authorized recovery runs, and the bundle must cite those runs' IDs, request/page
evidence and committed row counts (§13.4, §13.5). Until that happens the interval containing them
cannot be `READY`; the honest state is `GAP_DETECTED` with the two days listed as unresolved.

**How an explicit `coverage_start_ts` prevents silently claiming earlier history.** Suppose recovery
is later performed for 2026-07-30 → 2026-08-01 and reconciled. A defensible seed is then:

```
A = 2026-07-30T00:00:00Z      (start of the segment actually recovered and reconciled)
W = <end of the last reconciled recovery segment>
bootstrap_status = 'READY'
bootstrap_evidence_ref = <bundle reference>
```

with the interval before `A` left unclaimed rather than silently absorbed. If instead the operator
declines the recovery, the equally valid choice is a *later* `A` that starts after the hole — which
narrows the claim rather than falsifying it. Either way, subsequent scheduled runs move only `W`,
and `A` records exactly how far back the claim was ever proven.

Both example seeds are placeholders for a reviewed decision. **No value here is approved, and no
coverage row, mode flip or recovery run is authorized by this document.**

### 13.9 Mode transitions

| Transition | Behaviour |
|---|---|
| **`strict_meta` → `data_invariants_v1` (first enablement)** | Requires a `READY` coverage state **before** the mode change (§13.6). Takes effect on the next run start (`control_plane` is loaded fresh per run; no caches — `CONVENTIONS.md` §10). Without a `READY` state the fire aborts with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. |
| **First effective window** | `E_end = F − D`; `E_start = min(base, W − O)`, capped at `E_end − R`. There is no `W IS NULL` branch. |
| **The unresolved stabilization tail** | `(F − D, F]` is not covered by the first compat run. It is covered by the next fire, whose `E_start ≤ E_end` of this one. Deferred by one fire interval; never lost. This is a permanent, by-design property of the mode, not a one-off enablement artefact — at any moment the newest `D` seconds are deliberately not yet ingested. |
| **Is the previously failed fire retried?** | **No.** `UNIQUE (schedule_id, scheduled_fire_ts)` makes each fire single-shot, and the terminal `FAILED` rows of 2026-08-01 are immutable evidence (`docs/12_…` §14.2). Their intervals are addressed by recovery runs under §13.5, not by re-firing. |
| **`data_invariants_v1` → `strict_meta`** | `E_end` returns to `F`, moving coverage forward by `D`; no gap, since `F > F_prev − D`. **Strict mode ignores the coverage state entirely**: it is neither read nor written. The row remains stored, and it immediately becomes **stale** — it keeps describing an interval that scheduled runs are no longer maintaining. **No automatic advancement occurs while strict.** |
| **`strict_meta` → `data_invariants_v1` again** | Requires **revalidation or an explicit reseed**. A stale `W` must never be silently reused: strict-mode runs advanced coverage in reality without advancing the row, so the stored `W` understates coverage and, more importantly, was proven against a configuration that may no longer hold. Set `bootstrap_status = 'RESEED_REQUIRED'` at the moment of the switch to strict, so that the fail-closed check of §5.2.1 enforces the revalidation rather than relying on operator memory. Revalidation may reuse the existing `A` when the earlier proof is still sound and the strict interval is reconciled; otherwise both bounds are re-chosen. |
| **Disabling a schedule** | No fires; the interval freezes. Disabling does not delete the row (`ON DELETE CASCADE` is on row deletion, not on `enabled = false`), so `A`, `W` and the status survive. |
| **Re-enabling later** | If still `READY`, the next fire expands to `min(base, W − O)`, capped at `R`, self-healing up to 31 days of downtime. Beyond `R`, `W` stays pinned, the status becomes `GAP_DETECTED`, `TRIPS_COVERAGE_GAP_DETECTED` fires on every subsequent run, and recovery plus a reviewed reseed is required. A downtime long enough to invalidate the proof should be marked `RESEED_REQUIRED` explicitly. |
| **Deleting and recreating a schedule** | The coverage row is removed by `ON DELETE CASCADE`. The new `schedule_id` starts `UNINITIALIZED` and must be bootstrapped from scratch — a recreated schedule is a new coverage claim, never an inherited one. |

### 13.10 Configuration changes — `D`, `O` and `R`

The first version specified `D` and `O` and omitted `R`. All three are covered here.

| Change | Behaviour |
|---|---|
| **Stabilization delay `D`** | See §4.8. Increases are always safe for contiguity; decreases larger than `O` are closed by the coverage expansion of §5.2. **Either direction requires revalidation of the next effective window** before the change is applied: recompute `E_start`/`E_end` for the next fire under the new value and confirm `E_start ≤ W + 1 s` still holds. A change large enough that it does not is a material change (below). |
| **Overlap `O`** | Only widens or narrows the pre-roll. Narrowing below 3600 s removes the *analytic* DST guarantee for Warsaw schedules but not the state-based one, since `min(base, W − O)` still yields `E_start ≤ W`. Enforce `O ≥ 0`; recommend `O = 3600`. **Requires the same next-window revalidation as `D`.** |
| **Recovery span cap `R` — reduction** | A reduction **cannot permit a coverage jump**: the cap is applied as `E_start = max(E_start, E_end − R)`, so a smaller `R` can only move `E_start` *later*, never earlier, and the §5.3 guard then compares that later `E_start` against `W`. If the reduction pushes `E_start` past `W + 1 s`, the run does **not** advance `W`; it raises `TRIPS_COVERAGE_GAP_DETECTED` and sets `GAP_DETECTED`. A reduction can therefore **expose** a gap that a larger `R` was still healing — which is the intended, loud outcome, not a regression. |
| **Recovery span cap `R` — increase** | May allow a **wider recovery request**, up to the provider's 31-day lookup limit which remains the hard ceiling. It **must not advance coverage on its own**: `W` still moves only after a connected run (`E_start ≤ W + 1 s`) that exited `rc == 0` with its client-business transaction committed. Raising `R` never retroactively converts an unhealed interval into claimed coverage. |
| **All three** | **Changes do not rewrite historical evidence.** Past `client_schedule_run_history` rows keep the `stabilization_delay_seconds` / `overlap_seconds` values that were in force when they were claimed (§14.1); no terminal row is updated. |
| **Material changes** | A change that invalidates the proof behind the current interval — one that breaks next-window contiguity, a reduction of `R` that exposes an interval the previous `R` was covering, or a change made while the schedule's `lookback_days`, `frequency` or `timezone` also changes — sets `bootstrap_status = 'RESEED_REQUIRED'`. Compatibility then fails closed under §5.2.1 until a reviewed reseed lands. |
| **Auditability** | **Every change to `D`, `O` or `R` is auditable**: it is a control-plane `UPDATE` on `client_account`, it is recorded per-run in the history evidence columns and in `logs.context` (§14.1), and any resulting status transition is recorded with `seeded_at` / `seeded_by` / `bootstrap_evidence_ref` on the coverage row. |

---

## 14. Schedule-history and evidence semantics

### 14.1 What is stored where

| Value | Location | Rationale |
|---|---|---|
| `scheduled_fire_ts` | `client_schedule_run_history.scheduled_fire_ts` (existing) | Unchanged. Still the logical fire instant. The uniqueness key `(schedule_id, scheduled_fire_ts)` is preserved exactly. |
| **Effective** window | `client_schedule_run_history.window_start_ts` / `window_end_ts` (existing columns) | These columns already mean "the window the job was asked to fetch", and `_claim_fire` writes exactly what `_build_job_params` sends. Keeping that invariant is what makes the history usable for coverage reconstruction and recovery. Under `strict_meta` nominal ≡ effective, so **no existing row changes meaning**. |
| **Nominal** window | New nullable columns `nominal_window_start_ts` / `nominal_window_end_ts` | Not derivable after the fact, because `lookback_days`, `D` and `O` are mutable. `NULL` for every historical row and for every `strict_meta` row. |
| `stabilization_delay_seconds`, `overlap_seconds`, `trips_pagination_mode` | New nullable columns on the same row, **and** run parameters, **and** `logs.context` | The history row is the durable audit record; run params make the job self-describing; logs make incident triage possible without joining the control plane, per `docs/12_…` §3.5. |
| Coverage bounds and status before/after | `logs.context` (`coverage_start_ts`, `coverage_watermark_before`, `coverage_watermark_after`, `coverage_expanded_seconds`, `coverage_gap_detected`, `bootstrap_status`) plus `client_dataset_coverage` | The table holds current state; the logs hold the transition history. `coverage_start_ts` is logged on every compat run so an auditor can see the claimed lower bound at the time of the run, not only its current value. |
| Bootstrap abort | `client_schedule_run_history.error_summary = 'TRIPS_COVERAGE_BOOTSTRAP_REQUIRED'`, an `ERROR` log with `abort_code` and the offending `bootstrap_status`, and a `suspected_bug` report | §5.2.1. Written on a normally claimed and finalized `FAILED` row; no existing row is mutated. |

### 14.2 Constraints honoured

- `UNIQUE (schedule_id, scheduled_fire_ts)` is untouched.
- **Terminal history rows are never mutated.** The new columns are written once, at
  `_claim_fire` time, in the same `INSERT` that creates the `RUNNING` row. Existing strict/non-trips
  `_finalize_run` keeps writing only `status`, `finished_at`, `error_summary`. The two C6
  compatibility finalizers change only a still-`RUNNING` claimed row and never reopen or overwrite a
  `SUCCESS` or `FAILED` row.
- Run parameters must remain secret-free (`CONVENTIONS.md` §8); all new params are non-secret
  scalars.
- New job parameters for `trips_sync`: `scheduled_fire_ts`, `nominal_window_start_ts`,
  `nominal_window_end_ts`, `trips_stabilization_delay_seconds`, `trips_overlap_seconds`. Note the
  dispatcher currently passes `scheduled_fire_ts` **only** to Eco Driving datasets; `trips_sync`
  cannot today report its own fire timestamp, which is a concrete gap this design must close for
  the evidence in §14.1 to be complete.

---

## 15. Missed-fire and dispatcher semantics

The dispatcher considers only the **latest** fire per schedule and performs no multi-fire catch-up
(`dispatcher.latest_scheduled_fire_local` docstring, `CURRENT_TASK_CONTEXT.md` §5.6). This is a
deliberate, unchanged property.

### 15.1 Behaviour of the selected architecture

Let `k` be the number of consecutive missed or failed fires before the next successful one at
`F_s`. The next run's start is `min(base, W − O)` where `W` is the last successfully covered
effective end, i.e. `F_{s−k−1} − D`.

| Scenario | Architecture A alone | A + watermark (selected) |
|---|---|---|
| **One daily fire missed** (`ALPHA00001`, `L = 1 d`) | `E_start = F_s − 1 d − D − O`; the interval `(F_s − 2 d − D, F_s − 1 d − D − O]` is never requested. **Permanent gap of ≈ 1 day.** | `W = F_s − 2 d − D` ⇒ `E_start = W − O`. **Fully healed**, window ≈ 2 d + O. |
| **Two daily fires missed** | Permanent gap of ≈ 2 days. | `E_start = W − O`, window ≈ 3 d + O. **Fully healed.** |
| **Service down several days** (say 9 days) | Permanent gap of ≈ 9 days. | Window ≈ 10 d + O, under the `R = 31 d` cap. **Fully healed.** |
| **Service down > 31 days** | Permanent gap. | Window capped at `R`; the first disconnected fire is claimed and atomically persists `GAP_DETECTED` + history `FAILED` before launch; later fires stay loud and launch nothing until separately authorized recovery and reviewed reseed. `W` remains pinned. **Gap surfaced, not silent.** |
| **Terminal `FAILED` row blocks retry of that fire** | The fire is never retried (correct — the row is immutable evidence). Under A alone its window is lost forever. | The *next* fire absorbs it via the watermark. No history row is mutated. |
| **Next successful run has `L = 1 d`** | Covers 1 day only. | Covers `max(1 d, elapsed since W) + O`, capped at `R`. |
| **Next successful run has `L = 7 d`** | Covers 7 days, so up to 6 missed daily fires are masked by the lookback alone. | Same, plus watermark expansion beyond 7 days if the outage was longer. |

### 15.2 The live production case

The control plane currently shows, for `trips_sync`:

- `2026-07-29` fires: `SUCCESS` for `DELTA00001`, `ALPHA00001`, `FOXTROT00001`.
- `2026-07-30` and `2026-07-31` fires: **no history rows at all** — the dispatcher never fired or
  never claimed, and it does not catch up.
- `2026-08-01` fires: `FAILED` for all three (the `PAGINATION_MISMATCH` incident).

For `ALPHA00001` and `FOXTROT00001` (`L = 1 d`) this is a ≈48-hour hole that **no future scheduled run
will ever request** under any architecture without state. This is not a hypothetical: it is the
strongest available argument that a persisted coverage claim is justified even though normal window
derivation uses the full-window shift.

**It is also the case that proves the claim must be two-sided.** A hole *behind* the leading edge is
invisible to a single-bound watermark: seeding `W` from the 2026-07-29 `SUCCESS` row satisfies every
contiguity check forever after, because the missing days lie before `W` and nothing ever looks
there. Only an explicit `coverage_start_ts` — set by the reviewed §13 procedure, after the missing
fires are either recovered or deliberately excluded — distinguishes "verified from `A` onward" from
"assumed since the beginning of time". See §13.8 for this exact case worked through.

### 15.3 Verdict on the D1 sub-question

**A design that relies only on exact shifted windows does not self-heal missed fires.** The
selected architecture therefore does not rely only on them. Gaps within `R` are healed
automatically; gaps beyond `R` are surfaced explicitly and repeatedly. No configuration of this
design leaves a gap silently.

---

## 16. The selected architecture, stated completely

### 16.1 Exact formulas

All quantities are UTC instants or absolute second counts.

```
Inputs
  F   = scheduled_fire_ts                       (dispatcher.evaluate_schedule, unchanged)
  L   = lookback_days × 86400                   (client_dataset_schedule.lookback_days)
  M   = client_account.trips_pagination_mode    ('strict_meta' | 'data_invariants_v1')
  D   = client_account.trips_stabilization_delay_seconds   (default 10800)
  O   = client_account.trips_overlap_seconds                (default 3600)
  R   = client_account.trips_max_recovery_span_seconds      (default 2678400 = 31 d)
  A   = client_dataset_coverage.coverage_start_ts  for this schedule_id
  W   = client_dataset_coverage.covered_through_ts for this schedule_id
  S   = client_dataset_coverage.bootstrap_status   for this schedule_id

Nominal window (always computed, always recorded)
  N_end   = F
  N_start = F − L

Effective window
  if M == 'strict_meta':
      E_start = N_start
      E_end   = N_end                            # byte-identical to today
                                                 # A, W, S are neither read nor written
  else:                                          # 'data_invariants_v1'
      # Fail-closed gate precedence (§5.2.1), before provider/client side effects:
      require coverage row exists
      require schedule/client/client-code/dataset identity matches
      if S in {'READY', 'GAP_DETECTED'}:
          require complete aware whole-second A, W; A <= W <= now_utc
          require nonblank bootstrap_evidence_ref and seeded_by
          require aware seeded_at
      else:
          abort TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
      if foundational validation failed:
          abort TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
      if S == 'GAP_DETECTED':
          abort TRIPS_COVERAGE_GAP_DETECTED             # valid recorded gap only
      # C5 leaves coverage unchanged for every abort

      E_end   = F − D
      base    = N_start − D − O
      E_start = min(base, W − O)                 # no W IS NULL branch exists
      E_start = max(E_start, E_end − R)          # recovery span cap
      require E_start <= E_end                   # [t, t] is a valid closed interval

C6 branch placement — dispatcher only, trigger=SCHEDULED only
  if gate rejects newly disconnected READY and requires_gap_persistence=true:
      after RUNNING claim, before launch:
        _finalize_compat_gap
          S := 'GAP_DETECTED'                    # A/W/evidence/source unchanged
          history RUNNING := FAILED              # same transaction
      no subprocess
  elif gate allows:
      launch subprocess
      if rc != 0:
          history RUNNING := FAILED              # coverage unchanged
      if rc == 0:
          _finalize_compat_success
            validate complete claim-time snapshot
            W := max(W, E_end) only when ahead   # A/S unchanged
            history RUNNING := SUCCESS           # same transaction
  existing or malformed GAP_DETECTED:
      history RUNNING := FAILED; coverage unchanged
```

`A` is never written by this path. It is set only by the reviewed bootstrap or reseed of §13, which
may change both bounds together.

Intervals are **closed** `[E_start, E_end]` at one-second resolution; contiguity is
`next_start ≤ prev_end + 1 s` (§2). Overlap duration and duplicated representable timestamps are
distinct quantities (§2.4). Therefore `[t, t]` is a valid degenerate interval; equality can arise
from zero lookback or another valid arithmetic boundary and is not itself a bootstrap or gap error.
For valid inputs C4 guarantees (and its arithmetic proves) `E_start <= E_end`, so an inverted
interval `E_start > E_end` is impossible. C5 does not add a blanket strict-positive-duration
requirement: its operational coverage rejection is based on connectivity,
`E_start > W + 1 s`. There is no implemented or accepted C5 abort code for an empty window, and no
replacement code is introduced here.

### 16.2 Required state

Exactly one row per compatibility-mode schedule in
`workflow_a_control.client_dataset_coverage` (§5.5), keyed on `schedule_id`, holding the **bounded
closed interval** `[coverage_start_ts, covered_through_ts]` plus `bootstrap_status`,
`bootstrap_evidence_ref`, `seeded_at` and `seeded_by`. The row must be `READY` **before** initial
mode enablement (§13.6). At runtime, `UNINITIALIZED`, `RESEED_REQUIRED`, unknown and `NULL` statuses
are bootstrap-required. `GAP_DETECTED` re-emits the gap code only when the same identity, interval,
evidence and seed foundations remain valid; malformed recorded-gap state is bootstrap-required. No
state in the job, provider client or cross-run cache participates.

### 16.3 Required configuration

On `workflow_a_control.client_account`, all client-scoped, all with defaults that preserve current
behaviour:

| Column | Type | Default | Notes |
|---|---|---|---|
| `trips_pagination_mode` | `TEXT NOT NULL` | `'strict_meta'` | Already proposed by `docs/12_…` §3.2; `CHECK (… IN ('strict_meta','data_invariants_v1'))`. |
| `trips_stabilization_delay_seconds` | `INTEGER NOT NULL` | `10800` | `CHECK (>= 0)`. Inert while `strict_meta`. |
| `trips_overlap_seconds` | `INTEGER NOT NULL` | `3600` | `CHECK (>= 0)`. Inert while `strict_meta`. |
| `trips_max_recovery_span_seconds` | `INTEGER NOT NULL` | `2678400` | `CHECK (> 0 AND <= 2678400)` — the provider's 31-day lookup limit is the ceiling. Inert while `strict_meta`. |

Unknown, `NULL` or unreadable `trips_pagination_mode` resolves to `strict_meta`, never to the
permissive mode (`docs/12_…` §3.2). The compatibility numerics are read but not applied while the
mode is strict, so a misconfigured delay cannot change current behaviour.

### 16.4 Defaults

- **Delay: 180 minutes (10 800 s)** — carried over from `docs/12_…` §6.4. A reviewer may prefer
  24 h for the first client; the architecture is indifferent, since `D` cancels in §4.2.
- **Overlap: 3600 s** — justified in §10.2 and §11.3.
- **Recovery span cap: 31 days** — the provider lookup limit.

### 16.5 Behaviour after missed fires

§15. Healed automatically within `R`; surfaced explicitly and repeatedly beyond `R`; no history row
mutated in either case.

### 16.6 Behaviour after failed fires

Identical to missed fires. The `FAILED` row is terminal and immutable; coverage was never advanced
for it; the next fire absorbs its interval — provided the interval is inside `[A, W]`'s
continuation and within `R`. A failed or missing fire *older* than the current `A` is outside the
managed claim and is a recovery matter, not a self-healing one (§13.5).

### 16.6a Behaviour before bootstrap

The generic non-`READY` result is `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. It applies to
`UNINITIALIZED`, `RESEED_REQUIRED`, a `NULL` or unknown status, malformed or identity-inconsistent
state, and malformed `GAP_DETECTED`.

A pre-existing row whose `bootstrap_status = 'GAP_DETECTED'` is the narrow exception and returns
`TRIPS_COVERAGE_GAP_DETECTED` only after satisfying the same foundational integrity requirements
used to recognize a previously verified interval: matching schedule and client identities, the
accepted client-code identity, matching dataset, non-`NULL` `A` and `W`, valid timezone-aware
datetime values at accepted whole-second precision, `A <= W`, `W` not in the future under the
accepted deterministic contract, nonblank bootstrap evidence, valid `seeded_at`, and nonblank
`seeded_by`. The literal status value alone is not evidence. Failure of any foundational requirement
returns `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` even when the stored status string is `GAP_DETECTED`.

A structurally valid recorded gap remains loud on every later due fire with
`TRIPS_COVERAGE_GAP_DETECTED` until separately authorized recovery or reseed. Every rejection occurs
before any provider request and before any client-business write. C5 leaves `A`, `W` and
`bootstrap_status` untouched: it does not repair or rewrite the row. C6 under G-COV retains ownership
of every future durable coverage mutation (§5.2.1, §5.3, §13.1).

### 16.7 Behaviour for manual windows

§12. Literal, never shifted, never coverage-writing, still subject to the `docs/12_…` §6.4
eligibility guard in compatibility mode. Manual and recovery runs are also exempt from the §5.2.1
bootstrap gate in the sense that they do not consult coverage state — but they equally may not
advance it; only the reviewed §13 procedure may.

### 16.8 DST rule

§11. `D` and `O` are absolute UTC durations applied after the local→UTC fire conversion. Never
local wall-clock arithmetic, never before conversion.

### 16.9 Rollout requirements

Slots into `docs/12_…` §11 without reordering it:

1. Migration `055` (`trips_pagination_mode`, per `docs/12_…` §18 commit 1) — unchanged.
2. Migration `056` — the three numeric columns plus `client_dataset_coverage` plus the additive
   `client_schedule_run_history` evidence columns. Additive, defaulted, inert.
3. Dispatcher window derivation behind `M == 'data_invariants_v1'`; `strict_meta` byte-identical.
4. Full offline test matrix (§18) green before any live step.
5. `docs/12_…` §12 page-3 probe — already satisfied by
   `TELEMATICS_PAGE3_VALIDATION_SHORT_PAGE_CONFIRMED` (25 / 25 / 8, disjoint, `total` 58 stable);
   the probe gate for U1/U2 is met, U3–U8 remain open.
6. Deploy disabled; verify at least one scheduled cycle is byte-identical.
7. **Bootstrap gates for the first client (`DELTA00001`), in the §13.6 order**: read-only inventory
   (§13.2) → explicit interval selection (§13.3) → reconciliation against the §13.4 definition of
   operational completeness → separately authorized recovery of any in-interval missing or `FAILED`
   fire (§13.5 gate 4) → reviewed evidence bundle (§13.5 gate 5) → insert the coverage row as
   `READY` → flip `trips_pagination_mode` for `DELTA00001` only, preferably in the same reviewed
   platform-database transaction as the insert. **Seeding from the last `SUCCESS` row is not
   permitted** (§13.0).
8. Observe several consecutive fires; confirm `coverage_start_ts` is unchanged and
   `covered_through_ts` advanced monotonically.
9. Additional clients one at a time, each with its own inventory, interval, bundle and review.

### 16.10 Rollback procedure

C5 taxonomy evaluation is read-only and is not itself a rollback or recovery action. A valid recorded
`GAP_DETECTED` remains loud; a malformed row whose status merely says `GAP_DETECTED` is classified
`TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. Neither result changes coverage. All bootstrap, reseed, repair,
recovery and every durable coverage mutation remain separately reviewed actions; C6 under G-COV owns
future scheduled coverage writes.
1. `UPDATE workflow_a_control.client_account SET trips_pagination_mode = 'strict_meta' WHERE
   client_code = '<CODE>';` — effective on the next run start, no deploy, no restart, no timer
   change (`docs/12_…` §3.6).
2. Windows immediately revert to nominal; coverage state stops being read and written and becomes
   **stale**. **Do not delete the coverage row** — it is the record of what was covered and is
   needed if the mode is re-enabled. In the same reviewed change, set
   `bootstrap_status = 'RESEED_REQUIRED'`, so that re-enabling later cannot silently reuse a
   watermark that strict-mode operation left behind (§13.9).
3. New evidence columns become `NULL` again for subsequent rows; existing rows are untouched.
4. No migration rollback. Both migrations are additive and inert at their defaults
   (`CONVENTIONS.md` §12).
5. Terminal history rows and `runs` rows remain immutable — they are the incident record.

---

## 17. Open risks

**Blocking for enablement (must be resolved before rollout step 7):**

- **R1 — `docs/12_…` U3–U8 remain unproven.** Page-3 short-page behaviour is now confirmed for one
  window (`TELEMATICS_PAGE3_VALIDATION_SHORT_PAGE_CONFIRMED`), but ordering stability, disjointness
  for larger windows, `total` stability across clients, per-region behaviour, provider-side
  mutation during a read and fleet-wide enablement are still open. This decision does not close them
  and must not be read as closing them. The accepted D5 decision (R2) closes none of them either.
- **R2 — `docs/12_…` D5 (missing `total`) is resolved.** The accepted operator decision
  `docs/16_telematics_d5_total_policy_decision.md` (Option B — absent `total` permitted under data
  invariants, ACCEPTED 2026-08-03) fixes the failure taxonomy and no longer gates any rollout step.
  It unblocks C7 implementation and closes the `docs/14_…` §17 `total` unknown; it changes no window
  formula, no stabilization numeric, no coverage invariant and no bootstrap requirement in this
  document, and it closes none of the R1 unknowns.

**Non-blocking, but must be documented in the runbook:**

- **R3 — Provider deletes are not representable.** A trip deleted at the provider is never removed
  from `client_trips`. No overlap or delay changes this. A reconciliation strategy is out of scope
  and would need its own design.
- **R4 — Corrections older than `D + O` are invisible to `L ≈ Δ` schedules.** `ALPHA00001`,
  `FOXTROT00001` and `BRAVO00016` will not pick up an edit to a trip older than roughly 4 hours.
  Raising `lookback_days` is the available lever and is a separate, reviewed configuration change.
- **R5 — `BRAVO00016` has a pre-existing autumn DST gap** under the *current* production
  derivation (§11.3), independent of D1. `O = 3600 s` repairs it only once compatibility mode is
  enabled for that client. Until then the 2026-10-19 01:00Z hole will recur. **Recommend a separate
  ticket** to either raise `BRAVO00016`'s `lookback_days` to 8 or accept and document the gap.
- **R6 — `trips_sync` does not currently receive `scheduled_fire_ts`.** Until
  `_build_job_params` passes it, job-side logs cannot self-describe their fire, and the §14.1
  evidence set is incomplete.
- **R7 — Fixed `D`-second data latency.** A `D = 3 h` shift for an `02:00`-local schedule places
  `effective_end` in the previous local day. No consumer in this repository breaks, but any future
  same-day consumer would.
- **R8 — Coverage staleness after a strict round-trip.** Switching to `strict_meta` and back leaves
  a stale `[A, W]` that must be revalidated or reseeded. This is now an **enforced** control rather
  than a procedural one: the rollback procedure sets `bootstrap_status = 'RESEED_REQUIRED'`
  (§16.10 step 2), and §5.2.1 refuses `RESEED_REQUIRED` with the bootstrap-required classification. `covered_through_source`,
  `seeded_at`, `seeded_by` and `updated_at` remain as visibility aids. Residual risk: an operator
  who both skips the rollback step *and* manually forces the status back to `READY` without a fresh
  bundle. The §5.5 `CHECK` cannot detect a *stale* evidence reference, only a missing one.

**Blocking for enablement, added by the bootstrap correction:**

- **R9 — Bootstrap evidence quality depends on operator judgement.** The design enforces that both
  bounds exist, that the status is `READY` and that an evidence reference is present; it cannot
  enforce that the bundle actually proves what it claims. A reviewer accepting an interval whose
  segments were never reconciled under §13.4 would produce a `READY` state that is wrong in the
  same direction as the rejected design, only more slowly. Mitigations: the interval is explicit and
  narrow-able (§13.3), the bundle is reviewable and checksummed (§13.5), and choosing a later `A` is
  always available and always safe. **This is the reason `A` exists: the cost of an over-optimistic
  claim is now bounded by a value a human wrote down, rather than being unbounded backwards.**
- **R10 — The bootstrap procedure is implemented but unexecuted.** *(Updated when the bootstrap
  tooling landed; the original text stated that none of §13 was code.)* The coverage table
  (migration `057`), the `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` gate and the C6 mutation finalizers are
  implemented and deployed, and §13 now has two named execution surfaces:
  `ops/audit_telematics_coverage_bootstrap.py` (Gate 1, strictly read-only, §13.2) and
  `ops/bootstrap_telematics_trips_coverage.py` (Gate 6 step 5, dry-run-first, §13.6). **The writer has
  since been executed in production exactly once**, on `2026-08-03` for `BRAVO00016` / `trips_sync`,
  so the coverage table holds exactly one `READY` row; `BRAVO00016` is the sole `data_invariants_v1`
  canary and the other four clients remain `strict_meta`. What the tooling deliberately does *not*
  remove is R9 — the writer requires
  explicit operator-approved `A` and `W` and refuses any selection that intersects an inventoried
  unresolved interval, but it cannot judge whether a bundle proves what it claims. Selecting the
  interval remains a reviewed human decision.
- **R11 — Recovery for the current incident is unperformed.** The 2026-07-30 / 2026-07-31 missing
  fires and the 2026-08-01 terminal `FAILED` fires are inventoried in §13.8 and §15.2 but **not
  recovered**. No recovery run has been authorized, executed or evidenced. Any bootstrap interval
  that would contain them is `GAP_DETECTED` until that changes. The reviewed C11 surface
  (`ops/recover_telematics_trips_window.py`, §12) now exists, but it does not weaken this entry: it
  only closes an interval **anchored exactly at `W`** whose own business execution succeeded, it
  requires a `READY` row, and it never widens `A` backwards over inventoried history — that stays a
  reviewed reseed. Migration `058` is unapplied in production and no recovery has been executed.

---

## 18. Required future tests

Following `CONVENTIONS.md` §11: a focused manual script,
`ops/tests_manual/test_telematics_trips_stabilization_windows.py`, stdlib only, no network, no
database, no secrets. Pure functions (`evaluate_schedule` and a new window-derivation helper) are
the units under test; coverage-state transitions are tested against an in-memory fake.

| # | Case | Expected |
|---|---|---|
| W1 | Consecutive one-day windows, `L = 1 d`, `D = 3 h`, `O = 1 h` | `E_start(n+1) ≤ E_end(n) + 1 s` for 30 consecutive fires; union covers the whole span with no hole |
| W2 | Consecutive seven-day windows (daily and weekly cadence) | Same contiguity assertion; weekly case exercises `Δ = L` exactly |
| W3 | Overlap idempotency | A trip present in two consecutive effective windows produces one row and identical absolute speeding counts on the second pass |
| W4 | One missed fire | Next successful window's `E_start ≤ W`; union of covered intervals has no hole |
| W5 | Two missed fires | Same, with a wider window |
| W6 | Failed terminal run | Watermark unchanged after `rc != 0`; history row untouched; next fire absorbs the interval |
| W7 | Compatibility-mode fire with **no coverage row** | Aborts `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`; **zero provider requests**; no client-business write; state unchanged; history row `FAILED` with that `error_summary`; no existing row mutated |
| W8 | First compatibility-mode run against a `READY` bootstrapped interval | `E_start == min(base, W − O)`, capped at `R`; `W` advances to `E_end`; `A` byte-identical before and after; the `(F − D, F]` tail is covered by the *next* fire |
| W9 | Explicit historical manual window | Passed through literally; no shift; watermark untouched |
| W10 | Recent manual window in `data_invariants_v1` | Aborts with `PAGINATION_COMPAT_WINDOW_INELIGIBLE`; no fallback to `strict_meta`; no provider request |
| W11 | Delay change (increase and decrease) | Increase: contiguous. Decrease by `> O`: watermark expansion restores contiguity; watermark never regresses (`max`) |
| W12 | Overlap change | `O = 0` with a present coverage interval is still contiguous; `O = 0` with a coverage interval whose `W` is exactly the previous `E_end` on the `BRAVO00016` autumn case reproduces the 3600 s gap under architecture A alone (regression guard for §11.3) |
| W13 | Mode rollback | `strict_meta` derivation is byte-identical to today's `evaluate_schedule` output for all four production schedule shapes |
| W14 | Spring DST, `Europe/Warsaw` | `2026-03-29` 02:00 local resolves to `01:00Z` via `fold=0`; no fire skipped or duplicated; contiguous |
| W15 | Autumn DST, `Europe/Warsaw` | `2026-10-25` 02:00 local resolves to the first (CEST) occurrence; `Δ = 25 h` daily and `169 h` weekly both contiguous with `O = 3600` |
| W16 | UTC schedule | `Δ = 86 400 s` exactly across both transition dates; contiguous |
| W17 | `Europe/Warsaw` schedule | Covered by W14/W15 plus a full-year sweep asserting contiguity on every fire of 2026 |
| W18 | Provider late record inside `D` | Present in the first window that covers its `start_timestamp` |
| W19 | Provider corrected record | Re-fetched only if a later window covers its `start_timestamp`; asserts the documented limitation of R4 rather than a false guarantee |
| W20 | Duplicate provider trip ID across two runs | One row; `overwrite_existing = true` refreshes mutable fields; no duplicate in a single prepared batch |
| W21 | Backfill after a missed fire | Backfill window is literal, `insert_only = true`, coverage state untouched; only a subsequent reviewed reseed clears the alert |
| W22 | Nominal/effective window logging | Both present and different in `data_invariants_v1`; nominal `NULL` in `strict_meta`; delay, overlap and mode recorded |
| W23 | No mutation of terminal history rows | Simulated full lifecycle asserts `_finalize_run` writes only `status`, `finished_at`, `error_summary`; the new evidence columns are written once at claim time |
| W24 | Gap beyond `R` | Watermark pinned; first fire persists the gap atomically before launch; the next fire re-emits `TRIPS_COVERAGE_GAP_DETECTED` without refreshing timestamps; neither fire launches a subprocess |
| W25 | `W` monotonicity | A stale, forward-jumped or clock-skewed `W` never shrinks a window below `base` and never regresses |
| W26 | Every §5.2.1 precondition, one per case | Missing row; schedule/client/client-code/dataset identity mismatch; for `READY` and `GAP_DETECTED`: `A`/`W` missing, non-datetime, naive, sub-second, reversed, future `W`, blank evidence, missing/naive seed timestamp, or blank seed actor; plus `UNINITIALIZED`, `RESEED_REQUIRED`, unknown and `NULL` status. Each malformed case aborts `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` before any provider request and leaves coverage unchanged. A structurally valid recorded `GAP_DETECTED` is the narrow exception and re-emits `TRIPS_COVERAGE_GAP_DETECTED`. |
| W27 | `A` immutability under normal advancement | Over a simulated 60-fire sequence including missed fires, failures and a delay change, `coverage_start_ts` is never written; `covered_through_ts` is monotone |
| W28 | No automatic initialization | A `SUCCESS` scheduled run against a missing or `UNINITIALIZED` coverage row never creates a row and never sets a bound — the run does not happen at all (W26); asserts explicitly that no code path derives `A` or `W` from a run's own window, from the newest `SUCCESS` row, or from `max(synced_at)` |
| W29 | `GAP_DETECTED` transition and persistence | C5 returns the pure signal read-only. C6 claims the fire, then `_finalize_compat_gap` atomically persists `READY → GAP_DETECTED` + history `FAILED` before launch, with one shared UTC whole-second gap/update timestamp and unchanged `A`/`W`/evidence/seed/source. Later valid-gap fires re-emit with no coverage statement or timestamp refresh; malformed gap follows W26. |
| W30 | Strict round-trip | `strict_meta` neither reads nor writes coverage state; after a round-trip with the §16.10 procedure applied, the status is `RESEED_REQUIRED` and the next compat fire fails closed |
| W31 | `R` change semantics | Reducing `R` so that `E_start > W + 1 s` produces `GAP_DETECTED` rather than an advanced `W`; increasing `R` widens the request but advances `W` only on a connected `rc == 0` run |
| W32 | Bounded-claim regression guard for the incident | Reproduces §13.8: a single-bound watermark seeded at the 2026-07-29 `SUCCESS` end passes every contiguity check while 07-30/07-31 remain unfetched; the two-bound model with an honest `A` either excludes those days or reports them unresolved |
| W33 | Closed-interval terminology | For `O = 3600` the intersection is 3600 s of duration and 3601 duplicated representable timestamps; for a shared endpoint it is 0 s and 1 timestamp (§2.4) |

---

## 19. Implementation consequences by component

**None of this is performed by this task.**

| Component | Change |
|---|---|
| **Migration** | `055` — `trips_pagination_mode` (already specified by `docs/12_…` §18). `056` — `trips_stabilization_delay_seconds`, `trips_overlap_seconds`, `trips_max_recovery_span_seconds` on `client_account`; `workflow_a_control.client_dataset_coverage` **with both bounds, `bootstrap_status` defaulting to `UNINITIALIZED`, `bootstrap_evidence_ref`, `seeded_at`, `seeded_by` and the two `CHECK`s of §5.5**; additive nullable `nominal_window_start_ts`, `nominal_window_end_ts`, `stabilization_delay_seconds`, `overlap_seconds`, `trips_pagination_mode` on `client_schedule_run_history`. All additive, defaulted, inert — a new coverage row defaults to `UNINITIALIZED`, which is refused rather than assumed. Pattern: `018_workflow_a_schedule_event_enrichment_mode.sql`. **No migration is written by this task.** |
| **`ClientAccountConfig`** | Four new frozen fields; added to the `SELECT` in `load_client_account_config`; Python allowlist re-validation of the mode; numeric range validation; fail-closed on an unknown mode. |
| **Dispatcher** | `ScheduleRow` gains the client-scoped compatibility fields (join already reaches `client_account`). `evaluate_schedule` returns nominal **and** effective windows. A new pure helper `derive_effective_window(...)` holds §16.1 — including the §5.2.1 bootstrap gate as its first step — so both the gate and the arithmetic are unit-testable without a DB. `_claim_fire` writes the new evidence columns. Existing strict/non-trips `_finalize_run` stays unchanged. `_finalize_compat_gap` owns the post-claim/pre-launch atomic gap+`FAILED` transaction; `_finalize_compat_success` owns the post-`rc == 0` atomic advancement/no-op+`SUCCESS` transaction. `_build_job_params` adds `scheduled_fire_ts` (currently Eco-Driving-only), the nominal window and the numerics for `trips_sync`. |
| **Provider client** | **No change from this decision.** The compatibility state machine of `docs/12_…` §4 is unaffected; it simply now receives eligible windows. |
| **Sync job** | Reads the new params for logging only; **never re-derives or shifts a window**. Keeps the `docs/12_…` §6.4 eligibility guard as a guard. Adds the new keys to run-start and window-context logs. |
| **Schedule-history evidence** | §14. Written once at claim; terminal rows never mutated. |
| **Platform logs** | New flat scalar context keys: `trips_pagination_mode`, `nominal_window_start_ts`, `nominal_window_end_ts`, `effective_window_start_ts`, `effective_window_end_ts`, `stabilization_delay_seconds`, `overlap_seconds`, `coverage_start_ts`, `coverage_watermark_before`, `coverage_watermark_after`, `coverage_expanded_seconds`, `coverage_gap_detected`, `bootstrap_status`, `abort_code`. |
| **Tests** | `ops/tests_manual/test_telematics_trips_stabilization_windows.py` (§18). Existing `test_workflow_a_dispatcher.py` assertions on `window_end == scheduled_fire_ts` remain valid for `strict_meta` and must be extended, not replaced. |
| **Operations documentation** | `docs/07_operations.md` §5.5 — the §13 bootstrap gates as an ordered runbook, `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` and `TRIPS_COVERAGE_GAP_DETECTED` triage, reseed procedure, the "delay is not a correction mechanism" warning (§10.1), the R5 `BRAVO00016` note. `docs/05_jobs.md` — the new `trips_sync` params and the nominal/effective distinction. `docs/02_infrastructure.md` — no new ENV (everything is control-plane configuration). **Not written by this task**; §19 lists consequences, not deliverables. |
| **Recovery tooling** | A read-only coverage-inventory query (schedule × expected fires × history × coverage interval → uncovered intervals, including fires that produced **no** row) implementing §13.2 and replacing the manual reconstruction in `docs/12_…` §14.1 step 5. Advancing `covered_through_ts` after a verified recovery is owned by the reviewed C11 surface (`ops/recover_telematics_trips_window.py`) and happens only after that recovery's own business execution exits `rc == 0`; **exactly two reviewed surfaces may advance `W`**, and `covered_through_source = 'manual_recovery'` records which one did. Any change to `coverage_start_ts` remains a reviewed reseed bound to an evidence bundle — never automatic, and never performed by C11. |

---

## 20. Decisions stated explicitly

1. **D1 is resolved.** Scheduled windows are shifted in full by the stabilization delay, in the
   dispatcher, making `docs/12_…` §6.4 eligibility a theorem for every scheduled run.
2. **The delay cannot create a gap**, because it translates both endpoints equally and cancels from
   the contiguity condition (§4.2). This is the property that decides the architecture.
3. **The overlap defaults to 3600 s** and is required to be ≥ the maximum DST shift for the
   analytic guarantee to hold without state.
4. **A per-schedule coverage state is adopted as an explicitly bounded closed interval
   `[coverage_start_ts, covered_through_ts]` with a `bootstrap_status`, expansion-only and
   non-load-bearing.** It can only widen a window, never narrow one, never move the end, and never
   override the lookback. A single-bound watermark is **rejected**: it implicitly claims contiguity
   back to the beginning of client history and would have concealed the 2026-07-30 / 2026-07-31 hole
   (§13.0, §13.8).
5. **`covered_through_ts` advances only after a committed successful scheduled run**, monotonically,
   and never across a detected hole. **`coverage_start_ts` is immutable during ordinary
   advancement** — only a reviewed bootstrap or reseed may set or move it (§5.3).
5a. **Compatibility mode applies the §5.2.1 precedence.** Missing or identity-invalid rows;
   `UNINITIALIZED`, `RESEED_REQUIRED`, unknown/`NULL` status; and malformed foundational state under
   either `READY` or `GAP_DETECTED` fail with `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. A structurally
   valid recorded `GAP_DETECTED` is the narrow exception and re-emits `TRIPS_COVERAGE_GAP_DETECTED`.
   Both classifications occur before provider/client side effects and change no coverage in C5.
   **Coverage is never initialized automatically from a successful scheduled run**, from the newest
   `SUCCESS` history row, or from `max(synced_at)` (§5.2.1, §13.0, §13.1).
5b. **Enablement is gated by the ordered bootstrap procedure of §13**: read-only inventory →
   explicit interval selection → reconciliation against the §13.4 definition of operational
   completeness → separately authorized recovery of in-interval gaps → reviewed evidence bundle →
   seed as `READY` → mode flip (preferably in one reviewed platform-database transaction) → observe.
   A strict round-trip or a material `D`/`O`/`R` change requires revalidation or `RESEED_REQUIRED`
   (§13.9, §13.10).
6. **Missed and failed fires are healed automatically within 31 days and surfaced loudly beyond
   it.** No configuration leaves a gap silently.
7. **Shifting happens exactly once, in the dispatcher.** Jobs treat every window as literal, so
   operator-supplied historical windows can never be double-shifted.
8. **Nominal and effective windows are recorded separately**, with the effective window keeping the
   existing history columns and the nominal window in new additive ones.
9. **`(schedule_id, scheduled_fire_ts)` uniqueness and terminal-row immutability are preserved.**
   Nothing here updates a `SUCCESS` or `FAILED` row.
10. **Everything is client-scoped, defaults to today's behaviour, stays disabled until the
    compatibility implementation is deployed, and is reversible by one control-plane `UPDATE`.**
11. **Architectures B, C, D and E are rejected** for the reasons in §5.1, §6, §7 and §8
    respectively; B's persisted state is retained only in the reduced, two-bounded role of §5.2.
12. **Overlap duration and duplicated representable timestamps are distinct** (§2.4): `O = 3600`
    means 3600 s of overlap duration and 3601 duplicated representable second timestamps, while a
    shared endpoint means **zero** duration of overlap and **one** duplicated timestamp. This is a
    naming correction; no formula, default or runtime convention changes.
