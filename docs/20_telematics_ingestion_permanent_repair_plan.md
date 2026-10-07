# Telematics / ALPHA00001 ingestion permanent repair plan

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the form
> `PYTHONPATH="$PWD" python3 ops/runner.py …` are development / local / debug only — they
> execute the mutable working tree. The supported production entrypoint is the installed
> wrapper `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See `docs/07_operations.md`
> → *Release boundary*.

**Status: `IN_PROGRESS`. M00, M0, M1, M2, M3 and M4 are complete. M2 is `COMPLETE` since 2026-08-13,
deployed and verified on its first natural production `L = 3` run. M3 is `COMPLETE` since
2026-08-14: release `fabaaa753f89` was activated 2026-08-13T10:59:21Z, and on 2026-08-14 three
natural compatibility `trips_sync` fires — DELTA00001, ALPHA00001 and FOXTROT00001 — each produced a
verified `EXECUTED_COMMITTED` outcome and advanced `covered_through_ts` to exactly its `E_end`,
with zero refusals, zero false successes and no fetch regression. The §20.6 evidence set is
satisfied in full; the closure record is §20.7. M4 is `COMPLETE` since 2026-08-15: release
`2782550f8efe` was activated 2026-08-14T15:27:58Z, and the first natural post-activation fires
for the same three clients proved the durable evidence contract end to end; the closure record
is §22.14. M5 is `COMPLETE` since 2026-08-17: migration 062 is applied, release `b682df90c958` was
activated 2026-08-15T17:30:46Z, and natural scheduled fires for DELTA00001, FOXTROT00001 and BRAVO00016
confirmed unchanged outcomes — including the weekly-frequency / `run_type = DAILY` role separation
that is M5's load-bearing behaviour; the closure record is §23.8. M5 created **zero** reconciliation
schedules. **M6 is `IMPLEMENTED_LOCALLY` since 2026-08-17**: the `WEEKLY_RECONCILIATION` lifecycle
exists in the repository — one registered deny-by-default mutation surface, creation and activation
separated, `L = 16` on Monday 00:30 Europe/Warsaw, no migration, no `R` change and no dispatcher
branch — but **no production schedule row exists, none is enabled and nothing is deployed**; the
record is §24. M7–M11 remain `PLANNED_NOT_IMPLEMENTED`.**

> **Active release ≠ repository HEAD ≠ the release a milestone shipped on.** Production currently
> executes release `ac13457a6a99`, built from commit `ac13457a6a993c07e9408482be1e1f3f1304f5c2`
> (previous `0e2ad513cff9`); both are strict descendants of M5's `b682df90c958` and carry a
> byte-identical Workflow A runtime surface (§23.8). The repository HEAD may legitimately be a
> different commit; documentation and control-context commits landing on `main` do not change what
> production runs, and production may advance past the release a milestone was verified on. Read the
> active release from `ops/manage_release.py status`, never from `git rev-parse HEAD`.

Per-milestone status is the table in §14, which is authoritative. §19 records the M2 delivery
in full; §19.12 records the production apply and the first natural `L = 3` run. §20 records the
M3 delivery; §20.7 records its production closure. §22 records the M4 delivery; §22.14
records its production closure.

`COVERAGE_CORRECTNESS_DEFERRED_TO_M3`. M2 proves the widened DAILY ingestion mechanics and
their safety behaviour. It proves nothing about historical or provider-local coverage
completeness, which remains M3's responsibility.

The investigation that produced this document changed nothing. Every "current" statement below
was verified against live code, live control-plane rows or live systemd state on
**2026-08-11**, and statements written in that pass are preserved as the observations they
were — where a later milestone superseded one, the correction is stated in place rather than
by silent overwrite. Every "target" statement not yet marked complete in §14 remains a
proposal awaiting approval.

**Supersession.** This plan supersedes the single rolling `lookback_days = 14` recommendation
in `docs/18_telematics_trips_request_time_contract.md` §3.3. The measured publication-lag
distribution in that section remains valid and load-bearing evidence and is *not* superseded —
only the chosen remedy is. §3.1's mechanism analysis ("the rolling refresh capability already
exists; July 2026 was a configuration state, not a missing capability") is confirmed by this
investigation and is carried forward unchanged.

Related: `docs/12` (pagination compatibility), `docs/13` (stabilization windows), `docs/14`
(compatibility implementation plan), `docs/15` (coverage mutation contract), `docs/18`
(request-time contract), `docs/19` (external late-arrival audit), `docs/05_jobs.md`.

---

## 1. Verified current architecture

Only the components in scope for this repair.

### 1.1 Execution path

```
log-job@dispatcher.timer   OnCalendar=*:0/5        (every 5 minutes, Persistent=true)
  └─ log-job@dispatcher.service
       └─ /usr/local/bin/log-job-runner.sh jobs.api.telematics.dispatcher {}
            └─ (wrapper-internal chain — see below)
                 └─ jobs.api.telematics.dispatcher
                      └─ subprocess → jobs.api.telematics.sync_trips_and_speeding
```

Inside the wrapper: it resolves `BASE_DIR`, then chains the project virtualenv through
`ops/run_with_environment_identity.py` into `ops/runner.py` with the module and JSON params.
That chain is written as prose rather than a copyable command line deliberately — the only
supported production entry point is the installed wrapper, and
`ops/tests_manual/test_release_boundary.py` rejects any documented block that spells out the
identity-wrapper-into-development-runner shape, because it keeps the identity guarantee while
silently dropping the release boundary.

**`BASE_DIR` when this section was written** was
`/opt/log-platform` — the mutable development working tree, which
is the defect §1.9 records and M1 fixed. **Since 2026-08-11** the installed wrapper is the
`release` variant and `BASE_DIR` resolves through `log-platform-release/current` to a pinned
release — `342c0b8e50ad` at M1, and `672adfb152f8` since the M2 promotion (§19.12).

The dispatcher is a **database-driven scheduler**. Cadence lives in
`workflow_a_control.client_dataset_schedule`, not in systemd. Adding a cadence is therefore
normally a control-plane row, not a unit file — but see §3.2 for why the required cadence is
*not* purely a config change.

### 1.2 Live schedule state (`client_dataset_schedule`, enabled rows only)

| client | dataset | freq | dow | run_time | tz | L | overwrite |
|---|---|---|---|---|---|---|---|
| BRAVO00016 | trips_sync | weekly | 0 (Mon) | 02:00 | Europe/Warsaw | 7 | true |
| **ALPHA00001** | **trips_sync** | **daily** | — | **02:00** | **UTC** | **3** | **true** |
| FOXTROT00001 | trips_sync | daily | — | 02:00 | UTC | 1 | true |
| FOXTROT00001 | fuel_daily_aggregation | daily | — | 02:00 | UTC | 1 | true |
| DELTA00001 | trips_sync | daily | — | 02:00 | Europe/Warsaw | 7 | true |

**Current, with one cell updated by M2.** The table was verified live on 2026-08-11 with
ALPHA00001 at `L = 1`. Migration `060_workflow_a_daily_trips_lookback_l3.sql` was applied to the
production control plane under a separately authorized promotion ahead of the 2026-08-13 fire
and moved that single cell `1 → 3`; every other cell is unchanged and no other client was
touched. **`L = 3` is the production configuration** — see §19.12.

ALPHA00001 `schedule_id = eb099f69-4876-4c7e-8f60-a2bad0c35b5b`. Because its timezone is `UTC`
and `run_time = 02:00`, the ALPHA fire lands at **04:00 Europe/Warsaw during CEST** and 03:00
during CET — a wall-clock drift no other trips client has. `day_of_week` is `0 = Monday`
(Python `weekday()` convention), confirmed in `dispatcher.py:26` and by BRAVO00016.

### 1.3 Window derivation

`dispatcher.evaluate_schedule` produces the *nominal* window:

```
fire_utc     = latest scheduled fire <= now, in the schedule's timezone
window_end   = fire_utc
window_start = fire_utc − timedelta(days = lookback_days)
```

`timedelta(days=N)` is **N × 86 400 absolute seconds**, not N calendar days. The dispatcher has
**no calendar-interval capability of any kind**.

`coverage_windows.derive_effective_window` then produces the window actually requested
(`docs/13` §16.1). With `F` = fire, `L` = lookback days, `D` = stabilization delay, `O` =
overlap, `R` = max recovery span, `W` = `covered_through_ts`:

```
N_end   = F
N_start = F − L·86400
E_end   = F − D
base    = (F − L·86400) − D − O
E_start = max( min(base, W − O), E_end − R )
is_connected = E_start <= W + 1s
```

ALPHA00001 tuning (`client_account`): `D = 10 800 s` (3 h), `O = 3 600 s` (1 h),
`R = 2 678 400 s` (31 d), `trips_pagination_mode = data_invariants_v1`.

`min(base, W − O)` can only move the start **earlier**, so the watermark never suppresses
already-covered time: `L` alone sets the historical re-request horizon. This is the mechanism
the new cadence exploits.

### 1.4 Request-time serialization

Verified against `provider_client.py` and `docs/18`:

```
request  /trips        start_timestamp / end_timestamp  → Europe/Warsaw wall-clock numerals
response /trips        row timestamps                   → UTC
request  /vehicles/events                               → Europe/Warsaw wall-clock numerals
response /vehicles/events  event_ts                     → UTC
```

Single boundary: `provider_client._wall_clock_wire_window` → `trips_wire_window`,
`vehicle_events_wire_window`. This is an empirically measured provider behaviour, not a
published Telematics contract. `TRIPS_MAX_SUB_WINDOW_DAYS = 30`.

~~**This code is currently uncommitted** — see §1.9.~~ **Committed by M0** and shipped in
release `342c0b8e50ad`; §1.9 records the resolution.

### 1.5 Provider safety budgets (`provider_safety.py`, env-overridable)

Code defaults are `MAX_REQUESTS_PER_RUN = 500`, `MAX_REQUESTS_PER_ENDPOINT = 300`,
`MAX_REQUESTS_PER_SUBWINDOW = 80`, `MAX_PAGES_PER_SUBWINDOW = 50` — but `.env` overrides all of
them, and **the overrides are what is in force**:

```
TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT = 3000
TELEMATICS_PROVIDER_MAX_PAGES_PER_ENDPOINT    = 3000
TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW   = 500
TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW= 500
TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN      = 5000
```

Any budget reasoning must use these values, not the code defaults. Budget exhaustion raises
`TelematicsProviderSafetyError` and **discards the whole sub-window** rather than returning a
partial result — failure is loud, not silently partial.

### 1.5a `/trips` provider contract, and why `L` does not move the per-sub-window budgets

Added 2026-08-13, from the retained provider specification `docs/openapi.yaml` — **documented
contract facts, not measurements.** Distinguishing these from the empirical observations in §1.4
matters, because the two carry very different weight in an argument.

| fact | source | consequence |
|---|---|---|
| `start_timestamp` / `end_timestamp` are **required** | `/trips` parameter block | no unbounded query exists |
| **"The lookup period is of maximum 31 days"** | `/trips` `end_timestamp` description | a hard provider ceiling on any single request |
| **"Returns all trips that overlap the specified time range"** — a trip starting before the window but ending inside it is returned | `/trips` `start_timestamp` description | the overlap rule used throughout `docs/19` and §3.7c is **documented**, not merely inferred |
| pagination is `page` / `limit`, both `*_without_validation` | `/trips` parameters | consistent with why `data_invariants_v1` exists |
| **no rate limit is documented for `/trips`** | absence in the `/trips` block | the specification *does* document limits elsewhere — `/fuel/consumed` and `/fuel/level` at 10 req/min, one endpoint at 1 per 5 min — so the absence is informative, though it is not a guarantee of no server-side throttling |

The **timezone** interpretation of those two parameters remains empirical and unpublished
(§1.4). Do not merge the two classes of claim.

**Per-sub-window budgets are invariant in `L`.** `TRIPS_DEFAULT_CHUNK_DAYS = 2` and
`TRIPS_MAX_CHUNK_DAYS = 5` (`sync_trips_and_speeding.py`), so a longer lookback adds *more*
sub-windows, never *larger* ones. Worst 2-day ALPHA chunk observed in production — 13 pages,
12 152 rows, 168.1 s, 14.71 MiB total, 1.25 MiB largest page — against the budgets in force:

| per-sub-window budget | in force | worst observed | headroom |
|---|---|---|---|
| `MAX_PAGES_PER_SUBWINDOW` | 500 | 13 | 38× |
| `MAX_REQUESTS_PER_SUBWINDOW` | 500 | ~14 | 36× |
| `compat max_rows_per_subwindow` (`page_limit × max_pages`) | 500 000 | 12 152 | 41× |
| `COMPAT_DEFAULT_MAX_ELAPSED_S` | 900 s | 168.1 s | 5.4× |
| `compat max_response_bytes` (per page) | 32 MiB | 1.25 MiB | 26× |

Only the **per-run** and **per-endpoint** budgets scale with `L`. The genuinely unbudgeted
dimension is memory: `docs/12` §9.2 requires every row to stay in memory until all chunks
complete, and no run-level row or byte cap exists. That is the one quantity a much larger `L`
changes in kind rather than degree, and M6 should measure it rather than assume it (§15).

### 1.6 Idempotency and persistence

`client_trips` upsert target is `ON CONFLICT (client_id, provider_trip_id)`
(`sync_trips_and_speeding.py:4841`), `DO UPDATE` when `overwrite_existing = true`,
`DO NOTHING` when false. Re-returning the same provider trip across overlapping daily / weekly
/ monthly windows is therefore already safe and cannot duplicate a row.

`Dysponent_ID` appears **nowhere** in `sync_trips_and_speeding.py` — verified by exhaustive
grep, not by reading the `DO UPDATE SET` list alone. Trip re-upsert cannot destroy dispatcher
enrichment. `docs/18` §3.2's claim is confirmed.

A deterministic UUID-v5 `record_id` (`jobs/api/telematics/record_id.py`,
namespace `331a59e5-a43c-4447-895a-ebc72cdd4eac`) exists as a parallel identity, but the trips
conflict target in force is the `(client_id, provider_trip_id)` pair.

### 1.7 Coverage and concurrency

- ~~`client_dataset_coverage` is keyed `PRIMARY KEY (schedule_id)` — **coverage is per-schedule,
  not per-(client, dataset)**.~~ **Superseded by M5 (migration 062), corrected in place 2026-08-17.**
  The primary key on `schedule_id` is retained as immutable provenance and as the pre-M5 rollback
  anchor, but the coverage **identity** is now `uq_client_dataset_coverage_dataset
  UNIQUE (client_id, dataset_name)`, and `_load_coverage_state` addresses the row by that pair. Every
  cadence over one dataset therefore reads and advances one watermark, which is precisely what makes
  M6 possible. See §23.1 and `docs/15` §4.0. The observation above is retained because it is the
  problem statement B2 in §3.2 was written against.
- Live rows: BRAVO00016, ALPHA00001, FOXTROT00001, DELTA00001 (`scheduled_run`), ECHO00001
  (`manual_recovery`). All `READY`. ALPHA `covered_through_ts = 2026-08-11 01:00+02`,
  `coverage_start_ts = 2026-07-01 02:00+02`.
- Advancement is a compare-and-swap in `coverage_finalization.advance_covered_through_cas`
  under a strict `covered_through_ts < new` monotonicity predicate; the only callers are
  `dispatcher._finalize_compat_success` and the C11 manual recovery.
- Concurrency: one **global** advisory lock, `DISPATCHER_ADVISORY_LOCK_KEY = 728503746327118001`,
  plus `if _count_running(conn) > 0: return None`. **Exactly one Workflow A job runs at a time,
  platform-wide.** Overlapping runs are structurally impossible.
- No catch-up: `latest_scheduled_fire_local` only ever considers the single most recent fire
  ≤ now. A fire missed for longer than one cadence period is lost, not queued. `UNIQUE
  (schedule_id, scheduled_fire_ts)` on `client_schedule_run_history` prevents re-firing.

### 1.8 Downstream

- **Report 207** — `jobs/reports/postprocess/job_report_207_speeding_migration.py`, run as a
  Workflow B postprocessor via `postprocessor_registry` from
  `jobs.reports.workflow_b.orchestrator`, on `log-workflow-b.timer` at **06:00 and 20:00
  Europe/Warsaw**. Its default selection already treats `NO_MATCHING_TRIP` as retryable
  (`error_filter`, line ~446). However the candidate query carries
  `raw_file_filter = AND r._raw_file_id = %s` when `raw_file_id` is supplied, so in the
  orchestrator path retries are **scoped to the raw file currently being processed**. A
  historical `NO_MATCHING_TRIP` row in an older raw file is not reconsidered merely because a
  matching trip later arrives.
- **Dysponent_ID** — `job_alpha00001_dysponent_id_enrichment.py` (+ `_exact_` variant), also a
  Workflow B postprocessor on the same 06:00 / 20:00 timer, plus
  `ops/backfill_alpha00001_dysponent_id.py` for manual backfill.
- **Eco** — `jobs/ecodriving/job_eco_driving_aggregate.py`. All ALPHA Eco schedules are
  currently `enabled = false`; the corrected July snapshot was produced out-of-band.

### 1.9 Runtime / deployment boundary — **resolved by M1 on 2026-08-11**

> **Superseded as a description of the present.** Everything in this subsection was true when
> it was written and is the reason M1 exists; it is retained as the problem statement, not as
> current state. Since **2026-08-11** the installed wrapper is the `release` variant (SHA-256
> `12ba9f936c9004b3d77ea406f4cec772d81ef7f2e00e570288438e14efa9ecac`), `BASE_DIR` resolves
> through `log-platform-release/current` → the pinned release, and the cutover fence reads
> `STATE=VERIFIED`. That release was `342c0b8e50ad` (`previous` `7518947f47ea`) at M1 and is
> `672adfb152f8` (`previous` `342c0b8e50ad`) since the M2 promotion. The uncommitted delta below
> was reviewed and committed by M0. Editing the development tree no longer changes what the
> dispatcher, Workflow B or retention execute. Both host prerequisites are complete.
> Current-state detail: `docs/07_operations.md` → *Release boundary*.

`log-job-runner.sh` hard-codes `BASE_DIR=/opt/log-platform`. That
is the **development working tree**. There is no `log-platform-release`; `git worktree list`
shows only feature worktrees. Editing a file under `jobs/` changes production behaviour at the
next 5-minute tick, with no build, no promotion and no rollback point.

At the time of writing the tree is **dirty**, and the modifications are in the executable
ingestion path:

```
jobs/api/telematics/provider_client.py         +383 lines   ← uncommitted, LIVE in production
jobs/api/telematics/sync_trips_and_speeding.py  +12/−…      ← uncommitted, LIVE in production
jobs/api/telematics/backfill_trips_insert_only.py +8        ← uncommitted, LIVE in production
```

Production is currently executing code that exists in no commit. The `/trips` Europe/Warsaw
wall-clock request contract — the fix that makes correct ingestion possible at all — is part of
that uncommitted delta.

---

## 2. Root causes

### 2.1 CONFIRMED

**C1 — `lookback_days = 1` cannot see a late-published trip.**
ALPHA's effective window re-requests roughly 24 h + D + O of history per fire. `docs/18` §3.3
measures the ALPHA July 2026 publication-lag distribution: 1.305 % of records still unpublished
at 1 day, 0.615 % at 2 days, 0.345 % at 5, 0.166 % at 7, 0.029 % at 10, 0.008 % at 12, none
beyond 13 — an ordering-proxy measurement; §3.7 carries the stronger request-evidence
measurement and the separate inferential one. Every record in that tail is structurally
unreachable at `L = 1`, on every reading. This is the
primary cause of the July 2026 ALPHA gap. It is a **configuration** state — the rolling refresh
capability already exists and needs no new subsystem (§1.3, `docs/18` §3.1).

**C2 — coverage advances on `rc == 0` alone; the trusted-outcome contract is unwired.**
`jobs/api/telematics/execution_outcome.py` (756 lines) defines the full contract the deferred
baseline asked for: `EXECUTED_COMMITTED` / `EXECUTED_ZERO_ROWS_COMMITTED` as the only
`COVERAGE_ELIGIBLE_OUTCOMES`, each additionally requiring `provider_execution_entered`,
`business_transaction_entered` and `transaction_status == COMMITTED`, with
`verify_outcome`, `is_coverage_eligible` and `require_platform_run_identity` as enforcement.

**None of it has a production consumer.** `verify_outcome`, `read_outcome`,
`is_coverage_eligible` and `COVERAGE_ELIGIBLE_OUTCOMES` are referenced only inside
`execution_outcome.py` itself and in `ops/tests_manual/`. `dispatcher.py` imports none of
them; `dispatcher.py:2455` reads `if rc == 0:` and calls `_finalize_compat_success` directly.
The module is built and tested but structurally inert. The deferred baseline's concern is
therefore **not** resolved — and the remedy is a wiring task with an already-designed contract,
not a design task.

**C3 — Eco treats technical incompleteness as a business zero.**
`job_eco_driving_aggregate._qualification_and_calculation(total_distance_meters)` decides
purely from the aggregate:

```python
if total_distance_meters <= 0:   return "NO_DISTANCE", "NO_DISTANCE"
if total_distance_meters < MIN_QUALIFYING_DISTANCE_METERS: return "LOW_DISTANCE", "OK"
return "QUALIFIED", "OK"
```

There is no readiness, completeness or freshness input anywhere in the signature or the body.
A driver whose trips have not yet been ingested is indistinguishable from a driver who did not
drive. Likewise `_ranking_group` derives `chart_exists = bool(row.get("driver_id"))`, so a
missing dispatcher/chart mapping — a technical state — is emitted as `UNKNOWN_DRIVER`, a
business-looking classification. The current corrected July slice contains 104
`UNKNOWN_DRIVER` and 66 `NULL` ranking-group rows.

**C4 — the production runtime has no release boundary.** §1.9. Confirmed by reading the
installed wrapper and `git worktree list`, and by the dirty executable tree.

### 2.2 STRONGLY SUPPORTED

**S1 — historical `NO_MATCHING_TRIP` rows are not reconsidered when a trip arrives late.**
The retry predicate exists and is on by default, but the orchestrator-path candidate query is
raw-file scoped (§1.8). A late trip arriving days after its report was processed has no
mechanism that revisits the older raw file. Supported by code reading; not yet demonstrated
against a specific production row.

**S2 — ALPHA's `UTC` schedule timezone is an unintended inconsistency.** Every other
Europe/Warsaw-operated trips client uses `Europe/Warsaw`; ALPHA's fire drifts an hour across
DST relative to them and relative to the Workflow B 06:00 dependency. No evidence of a
deliberate decision was found.

### 2.3 UNVERIFIED / HYPOTHETICAL

**U1 — provider-side late publication as the *mechanism* behind the July gap.** `docs/18` §3.3
infers the lag distribution from `provider_trip_id` creation ordering, which is an ordering
proxy, not a provider-published timestamp. See `docs/19` for the independent audit and its
evidence limits. Treat the *distribution shape* as strong and the *per-trip publication
instants* as bounded intervals only.

**U2 — Dysponent enrichment reach after a late trip insert.** *Partially resolved during this
investigation and promoted to STRONGLY_SUPPORTED — see S3.* What remains unverified is only the
concrete production frequency with which a late trip lands outside the enrichment window.

### 2.2 (cont.) STRONGLY SUPPORTED

**S3 — Dysponent enrichment is date-range scoped and can miss a late historical trip.**
`job_alpha00001_dysponent_id_enrichment` does **not** select by raw file — it selects trips with
`client_id = %(client_id)s AND start_timestamp >= %(start_date)s` (lines ~340, ~400, ~859), so
it re-evaluates every trip in its window on each run and a late trip inside that window *is*
picked up. However `date_from` is **required** and, per the module's own precondition message,
"automated callers derive it from the previous committed source load" (line ~277). A trip
inserted by a weekly or monthly reconciliation whose `start_timestamp` predates that derived
boundary therefore falls outside the window and is never enriched. This is the same blind-spot
shape as S1, with a date boundary instead of a raw-file boundary.

---

## 3. New scheduling model

### 3.1 Target semantics

| run type | cadence | fire (Europe/Warsaw) | horizon | status |
|---|---|---|---|---|
| `DAILY` | every day | existing ALPHA fire, normalized (§3.4) | `lookback_days = 3` | **DEPLOYED** (M2, §19.12) |
| `WEEKLY_RECONCILIATION` | Monday (`day_of_week = 0`) | **00:30** | `lookback_days = 16` | **APPROVED and implemented locally** — M6, §24. No production row exists |
| `MONTHLY_RECONCILIATION` | day 1 (`day_of_month = 1`) | **03:00** | **rolling `lookback_days`, value unresolved** | PLANNED — M7. The calendar-month window mode is **no longer the approved direction**; see §3.1a |

Only the `DAILY` row describes production. The weekly row is approved and implemented in the
repository, but **no `WEEKLY_RECONCILIATION` schedule row exists in production** and none is created
by this work — registering and enabling one are separately authorized operator actions (§24).

**The fire time moved from 02:30 to 00:30 Europe/Warsaw (M6).** §3.4 chose 02:30 to stay clear of
the 02:00 band, which is correct for ALPHA (base fires 04:00 CEST) but inverts §7's ordering property
for DELTA00001, whose base fires at 02:00 Warsaw: the *widest* window would then be the last to
advance `W` on a Monday. The 00:00–02:00 Warsaw band is entirely free on the live topology, so
00:30 is contention-free **and** keeps the base schedule the last advancer for every client. Nothing
in the guaranteed-horizon arithmetic depends on the fire time; this is an ordering choice, not a
coverage one.

### 3.1a MONTHLY does not need a calendar window mode

Recorded during the M6 adjudication; it supersedes the calendar framing in §3.2 B3 and the whole of
§3.3 as the *approved direction*, though both are retained as the analysis they were.

Two facts settle it:

1. **A previous-calendar-month window adds essentially nothing to the guaranteed horizon.** Under
   §3.7c's corrected formula a cadence of period `P` at lookback `L` guarantees
   `L + 0.1667 − P` days, and a calendar month is its own period: a trip on the 31st is re-requested
   about three hours after month end, a trip on the 1st about 31 days after it. Worst-phase
   guarantee ≈ **0.17 d**. Its value is a closed-period audit, not a horizon.
2. **A rolling `L ≥ 32` fired on day 1 at 03:00 strictly contains every calendar month**, including
   October's 31.0417 d across the autumn DST transition. So the calendar window buys no coverage a
   rolling one does not already have.

Dropping the calendar mode therefore removes the new `window_mode` code path, the DST calendar
arithmetic, §3.3's zero-slack fire windows and its *infeasible November* outright — all of which are
artifacts of the calendar framing rather than of the requirement.

**M7's exact `L` is deliberately left unresolved.** The July cohort's detection resolution was about
21 days, which cannot discriminate between candidate monthly lookbacks. Once M6 has run for several
weeks the delivery-lag telemetry measures the tail at 7-day resolution, and M7's value should be
derived from that. `R` must then be raised to at least `L·86400 + O` (§3.3's remedy, recomputed
against the correct clamp condition — see §3.7c).

Overlapping temporal windows between the three are **intentional**. §1.6 makes the overlap
safe at the persistence layer.

### 3.2 Why this is not a pure configuration change

Three structural blockers, all verified:

**B1 — one schedule row per (client, dataset).**
`uq_client_dataset_schedule UNIQUE (client_id, dataset_name)`. Three cadences for
`ALPHA00001 / trips_sync` are not representable. Options:

- *(a)* drop the unique constraint, add a `run_type` discriminator column
  (`DAILY | WEEKLY_RECONCILIATION | MONTHLY_RECONCILIATION`), and re-key to
  `UNIQUE (client_id, dataset_name, run_type)`;
- *(b)* register distinct dataset names (`trips_sync_weekly_reconciliation`, …) in the Python
  and DB registries.

**Recommended: (a).** Option (b) multiplies the dataset registry, the retention catalogue and
the table allowlist for what is one dataset, and it gives each pseudo-dataset its own coverage
row — see B2.

**B2 — coverage is keyed by `schedule_id`.**
`PRIMARY KEY (schedule_id)` on `client_dataset_coverage`. Three schedules would create three
independent watermarks over one dataset, which is semantically wrong: `covered_through_ts`
means "forward completeness of `client_trips` for this client", and there is exactly one such
truth. The reconciliation runs must **share the daily schedule's coverage row**, or coverage
must be re-keyed to `(client_id, dataset_name)` with `schedule_id` demoted to provenance.

**Recommended:** re-key coverage to `(client_id, dataset_name)`; have reconciliation runs
participate in the same CAS. A reconciliation run whose `E_end` is behind `W` then performs the
existing *validated no-op* (`docs/15` §5.2) rather than moving the watermark backwards — which
is already the correct behaviour and needs no new rule.

**B3 — the dispatcher has no calendar-interval capability.**
`window_start = fire − timedelta(days = L)` is absolute-duration arithmetic. A calendar month
is 28, 29, 30, 31 or — across the October DST boundary — **31 days + 1 hour**. `lookback_days`
is an integer day count and cannot express it. `MONTHLY_RECONCILIATION` requires a new window
mode, e.g. `window_mode = 'previous_calendar_month'`, computing in `Europe/Warsaw` and
converting to UTC:

```
N_start = previous_month_start 00:00 Europe/Warsaw  → UTC
N_end   = current_month_start  00:00 Europe/Warsaw  → UTC
```

and feeding those absolute instants into the existing `derive_effective_window` unchanged.

### 3.3 The `R` clamp makes the monthly window infeasible today — quantified

`E_start` is floored at `E_end − R`. With `D = 10 800 s` and `R = 2 678 400 s`, covering an
entire previous calendar month requires the fire time `T` (on day 1, Europe/Warsaw) to satisfy
both:

```
E_end  = T − D ≥ previous_month_end     ⇒  T ≥ 03:00 Europe/Warsaw
E_start = T − D − R ≤ previous_month_start ⇒  T ≤ previous_month_start + R + D
```

Computed for every month of a full year (leap year and both DST transitions included):

| run date | previous month | span | feasible fire window | slack |
|---|---|---|---|---|
| 01-01 | December | 31.0000 d | 03:00 only | **0 h** |
| 02-01 | January | 31.0000 d | 03:00 only | **0 h** |
| 03-01 | February | 28 d (29 d in a leap year) | 03:00–07:00 | 72 h |
| 04-01 | March (DST spring) | 30.9583 d | 03:00–04:00 | 1 h |
| 05-01 | April | 30.0000 d | 03:00–03:00 | 24 h |
| 06-01 | May | 31.0000 d | 03:00 only | **0 h** |
| 07-01 | June | 30.0000 d | 03:00–03:00 | 24 h |
| 08-01 | July | 31.0000 d | 03:00 only | **0 h** |
| 09-01 | August | 31.0000 d | 03:00 only | **0 h** |
| 10-01 | September | 30.0000 d | 03:00–03:00 | 24 h |
| **11-01** | **October (DST autumn)** | **31.0417 d** | **INFEASIBLE** | **−1 h** |
| 12-01 | November | 30.0000 d | 03:00–03:00 | 24 h |

Two conclusions:

1. **November is impossible at any fire time.** October spans 2 682 000 s across the autumn DST
   transition, exceeding `R = 2 678 400 s`. The window would be silently shortened by one hour
   — a silent under-fetch, the exact failure class this repair exists to remove.
2. Five other months are feasible only at a single instant with zero slack, leaving no room for
   the overlap `O` or any future adjustment.

**Required remedy:** raise ALPHA's `trips_max_recovery_span_seconds`. The worst case is
`span + D + O = 2 696 400 s`. Recommended value **`2 764 800 s` (32 days)**, which makes every
month feasible at 03:00 with ≥ 23 h of slack (recomputed and confirmed). This is a
`client_account` value, not a code constant.

`03:00 Europe/Warsaw is the earliest feasible monthly fire` regardless of `R`, because
`E_end = T − D` must still reach the end of the previous month. Firing at 03:00 therefore both
satisfies the constraint and minimises latency. **This is why 03:00 is chosen, not preference.**

### 3.4 Chosen fire times and contention

Observed contention in the 02:00–05:00 Europe/Warsaw band (last 4 days of run history):

```
02:00–02:09  DELTA00001 trips_sync      (~8–9 min)
02:00–02:01  BRAVO00016 trips_sync     (Mondays, ~1 min)
04:00–04:02  ALPHA00001 trips_sync      (~1–2 min at L=1)
04:10–04:20  FOXTROT00001 trips_sync      (~4–9 min)
```

Free: 02:15–03:55 and 04:25–05:00.

- **`WEEKLY_RECONCILIATION` → Monday 02:30 Europe/Warsaw.** Clear of DELTA/BRAVO, and finishes
  before the monthly slot on a Monday-the-1st.
- **`MONTHLY_RECONCILIATION` → day 1, 03:00 Europe/Warsaw.** Forced by §3.3; also contention-free.

Both land well before the Workflow B 06:00 fire that consumes trips for Report 207 — a property
the plan should preserve deliberately, not by accident.

Because exactly one Workflow A job runs platform-wide (§1.7), a Monday-the-1st serialises
weekly (02:30) → monthly (03:00, longest) → ALPHA daily (04:00) → FOXTROT (04:10). Runs queue on
5-minute ticks rather than overlapping; none is lost, because each remains the "latest fire ≤
now" until its next cadence period. The plan must nonetheless verify that the monthly run's
duration cannot push the chain past 06:00.

### 3.5 Provider budget check for the monthly run

At ~5 700 trips/day and ~12 pages per 2-day chunk (`docs/18` §3.3), a 31-day window is roughly
16 chunks ≈ 190 `/trips` requests. Against the budgets actually in force (§1.5) that is ~15×
headroom on requests-per-endpoint (3 000) and ~26× on requests-per-run (5 000) — comfortable.
Note this conclusion depends entirely on the `.env` overrides: against the **code defaults**
the same window would sit at only ~1.6× headroom, so a deployment that lost those overrides
would start failing monthly runs on budget exhaustion.

Two consequences for the plan: the monthly window is affordable, **and** the release boundary
(§5) must carry the provider-budget environment explicitly rather than relying on a `.env` that
happens to be present in the working tree. Re-measure actual request and page counts on the
first real weekly run before enabling monthly — an explicit milestone gate (§14, M6), not an
assumption.

### 3.6 DAILY normalization

Set `lookback_days = 3`. Recommend also correcting `timezone` from `UTC` to `Europe/Warsaw`
(S2) so ALPHA stops drifting across DST relative to its own reconciliation runs and to Workflow
B. If the existing 04:00-CEST wall-clock fire is to be preserved exactly, `run_time` must
become `04:00` when the timezone changes — changing the timezone alone silently moves the fire
two hours earlier. **Either change is safe; the pair must be made together or not at all.**

### 3.7 Residual coverage gap — stated honestly

`L = 8` weekly plus a month-boundary monthly run does **not** cover the full measured lag tail
for every trip date. Worst case: a trip late in a 31-day month with a ~12-day publication lag
becomes available after the monthly reconciliation has already run on the 1st, and outside every
subsequent weekly `L = 8` window (the nearest Monday reaches back only 8 days). Such a trip is
captured by **no** run in the proposed cadence.

**A weekly `L` is not a daily `L` — the phase penalty (added 2026-08-13).** This section
originally reasoned about `L = 8` using figures computed on a *daily* fire grid and then applied
them to a *weekly* cadence. That is not sound, and correcting it changes the M6 decision
completely. See §3.7c.

**Two distributions, and they must never be conflated.** The independent audit (`docs/19`)
examined 1 031 confirmed late arrivals against 181 824 stored July 2026 trips and reports the
tail two different ways, with two very different evidential standards. An earlier revision of
this section quoted a single table of "249 / 123 / 16 records still absent at 8 / 11 / 14 days".
**Those numbers appear in neither `docs/19` nor its retained companion CSV and are withdrawn.**
The values below are recomputed directly from `artifacts/telematics_late_arrival_audit.csv`, the
machine-readable evidence of record, and are asserted by
`ops/tests_manual/test_telematics_m2_release_contract.py` so they cannot drift again.

**(a) DIRECTLY PROVEN — `docs/19` §5.1, `publication_lag_lower_bound`.** A complete earlier
request covered the journey and did not return the record; a later covering request did. This
is the only standard that proves the provider had not published a trip at a given age.

| still provably absent at | records (of 1 031) | share of all July trips |
|---|---|---|
| ≥ 1 day | 270 | 0.148 % |
| ≥ 3 days | 172 | 0.095 % |
| ≥ 7 days | 6 | 0.0033 % |
| ≥ 8 days | 4 | 0.0022 % |
| ≥ 11 days | **0** | 0 % |
| ≥ 14 days | **0** | 0 % |

**Maximum directly proven absence: 206.60 h = 8.61 days.** Nothing in the retained evidence
proves a record absent beyond that.

**(b) INFERENTIAL — `docs/19` §5.4, `provider_trip_id` allocation ordering.** Infers *creation*
time from identifier ordering. `docs/19` §9.2 labels the assumption explicitly as unproven and
notes the "did not yet exist" direction is the weaker one.

| inferred allocation age | records (of 1 031) | share of all July trips |
|---|---|---|
| ≥ 3 days | 735 | 0.404 % |
| ≥ 7 days | 523 | 0.288 % |
| ≥ 8 days | 458 | 0.252 % |
| ≥ 11 days | 285 | 0.157 % |
| ≥ 14 days | 109 | 0.060 % |

Maximum inferred allocation age: 20.31 days (p90 14.25 d).

**What this does and does not overturn in `docs/18` §3.3.** §3.3's "13 days — none observed" is
*consistent* with the direct evidence — direct evidence also observes nothing beyond 8.61 days.
It is contradicted only by the **inferential** reading, which is the weaker instrument, not the
stronger one. The earlier claim in this plan that §3.3 was "decisively false" and that
"16 records were provably still unpublished at 14 days" was wrong on both counts and is
withdrawn. What `docs/19` genuinely adds is a *lower-bounded* per-record measurement where §3.3
had only an ordering proxy — a better instrument for the near tail, not proof of a longer one.

Consequence for **M2** (`L = 3` daily), which is what this plan has implemented: on direct
evidence a 3-day horizon reaches every record proven absent at under 3 days and leaves 172
records (0.095 % of July trips) beyond it; on the inferential reading it leaves 735 (0.404 %).
Either way `L = 3` is a very large improvement over `L = 1` and is not claimed to be complete —
the weekly and monthly milestones exist precisely because it is not.

Consequence for **M6**: see §3.7c, which supersedes the `L = 8` framing this section
originally carried.

`docs/18` §3.3 carries a correction note recording the above (§17). The correction is a reclassification, not a refutation: §3.3's ordering-proxy figures understate the near tail that §5.1 measures directly, and its "none observed beyond 13 days" line survives the direct evidence and fails only the inferential one.

### 3.7c The weekly phase penalty — why `L = 8` is rejected

Added 2026-08-13. **This subsection supersedes every earlier `L = 8` recommendation in this
document.** It changes an engineering recommendation only; it authorizes nothing and changes no
production value. `L = 3` DAILY remains the sole deployed cadence.

**The error being corrected.** The residual figures quoted for `L = 8` above (4 records direct,
458 inferential) were computed on a **daily** fire grid. M6 proposes a **weekly** cadence. A
daily grid at lookback `L` re-requests every trip date on every one of the next `L` days; a
weekly grid re-requests each trip date on a handful of Mondays, and in the worst phase on
exactly one. The two are not interchangeable, and applying a daily-grid residual to a weekly
cadence overstates that cadence's protection.

**Guaranteed capture horizon.** For a cadence of period `P` days at lookback `L`, using
`derive_effective_window` (§1.3) with `D = 10 800 s`, `O = 3 600 s`, the largest publication lag
guaranteed to be captured **for every trip-start phase** is

```
guaranteed_horizon = L + (D + O)/86400 − P   =   L + 0.1667 − P   days
```

**Arithmetic correction (M6, 2026-08-17).** This subsection originally carried the constant as
`0.208`. It is `(D + O)/86400 = 14400/86400 = 0.16667`; `0.208 d` would be five hours, not four.
Every horizon below is therefore **0.04 d (one hour) lower** than first published. The correction
was found by an independent recomputation during the M6 adjudication and is reproduced by
`ops/tests_manual/test_telematics_m6_weekly_reconciliation.py`. **It changes no conclusion** — the
`L = 16` recommendation survives it, and `L = 15` fails by slightly more than first stated.

Verified by exhaustive search over every 15-minute trip-start phase across a full week, using
the documented `/trips` overlap rule (§1.5a):

| cadence | guaranteed horizon |
|---|---|
| **DAILY `L = 3` — deployed** | **2.167 d** |
| + weekly Monday `L = 8` | **1.167 d — worse than the daily alone** |
| + weekly Monday `L = 12` | 5.167 d |
| + weekly Monday `L = 14` | 7.167 d |
| + weekly Monday `L = 15` | 8.167 d |
| + weekly Monday `L = 16` | **9.167 d** |
| directly proven maximum absence (§3.7a) | **8.610 d** |

**The horizon is a property of the cadence, and every client has one.** Computed for the live
schedule shapes, this is what M6 is actually fixing — it is not an ALPHA-specific repair:

| client | base shape | horizon today | with weekly `L = 16` |
|---|---|---|---|
| ALPHA00001 | daily, `L = 3` | 2.167 d | **9.167 d** |
| DELTA00001 | daily, `L = 7` | 6.167 d | **9.167 d** |
| FOXTROT00001 | daily, `L = 1` | **0.167 d** | **9.167 d** |
| BRAVO00016 | weekly frequency, `L = 7` | **0.167 d** | **9.167 d** |

FOXTROT00001 and BRAVO00016 are currently protected for about four hours. Neither figure was previously
stated anywhere in this plan.

**The `R` clamp does not bind at `L = 16`.** The clamp condition is
`E_start = max(F − L·86400 − D − O, F − D − R)`, so it binds exactly when `R < L·86400 + O`. At
`L = 16` that threshold is **1,386,000 s**, against the configured `R = 2,678,400 s`. **M6 therefore
requires no `trips_max_recovery_span_seconds` change.** (§3.3 derived its remedy from the calendar
framing and expressed the requirement as `span + D + O`; against a rolling window the correct
condition is the one above, and only M7 needs it.)

A weekly `L = 8` guarantees only **1.167 d** on its own — *less than the daily `L = 3` already in
production*. It cannot improve the worst case by construction, whatever a daily-grid figure
suggests.

**Exact fire-grid replay.** Replaying all 1 031 records of
`artifacts/telematics_late_arrival_audit.csv` against real fire grids, counting a record as
proven missed only when **every** covering fire provably ran while the record was still absent
(`fire ≤ last_absent_response_received_at` — the §3.7a standard, never the upper bound):

| cadence | proven missed (of 1 031) |
|---|---|
| DAILY `L = 3` alone (deployed) | 171 |
| + weekly Monday `L = 8` | **171 — unchanged** |
| + weekly Monday `L = 12` | 23 |
| + weekly Monday `L = 14` | 4 |
| + weekly Monday `L = 15` | **0** |

The same replay on a *daily-only* grid reproduces §3.7a (`L = 3` → 171 against §3.7a's 172,
`L = 8` → 4, `L = 15` → 0), which is what validates the method. The one-record difference at
`L = 3` is a boundary effect of the overlap rule at the window edge and is not material to any
conclusion here; the `L = 8` and `L = 15` figures agree to the record.

**Why `L = 16` and not `L = 15`.** `L = 15` takes this cohort's proven-missed count to zero, but
its *guarantee* is 8.167 d — **0.44 d short of the 8.610 d directly proven maximum**. `L = 15`
closes the historical replay; `L = 16` (9.167 d) closes the direct evidence under **every** fire
phase, including phases this cohort happens not to contain. That distinction is the whole
difference between "no counter-example in the sample" and "no counter-example is possible".

**Cost.** Measured from production, calibrated against the first natural `L = 3` run (2 chunks,
20 pages, 21 requests, 18 531 rows, 218 s — a model predicting 20.5 pages and 18 530 rows):

| weekly candidate | chunks | `/trips` requests | rows re-fetched | runtime |
|---|---|---|---|---|
| `L = 8` | 5 | ~55 | ~49 000 | ~8.8 min |
| `L = 15` | 8 | ~101 | ~91 600 | ~16.0 min |
| `L = 16` | 9 | ~107 | ~97 700 | ~17.0 min |

`L = 16` costs roughly **6 % more than `L = 15`** and buys the worst-phase guarantee. Against the
budgets actually in force (§1.5) `L = 16` sits at ~28× headroom on requests-per-endpoint and
~43× on requests-per-run; every **per-sub-window** budget is unaffected by `L` at all, because
chunking is fixed at `TRIPS_DEFAULT_CHUNK_DAYS = 2` regardless of window length (§1.5a).

**Weekly `L = 16` — APPROVED (2026-08-17) and implemented locally as M6 (§24).** The engineering
recommendation this subsection carried was accepted by the approver after the fire-grid replay was
independently reproduced. Still not scheduled and not deployed: the repository can now *represent
and register* the cadence, and no production row exists.

**What `L = 16` still does not close.** The inferential reading (max 20.31 d, 109 records
≥ 14 d) is not covered by any candidate under discussion; `L = 22` would be. Neither is the
month-boundary case in the first paragraph of §3.7, which the monthly reconciliation gives
exactly one look at, at an age fixed by the trip's day-of-month. The §4 telemetry is what will
measure whether either residual actually bites.

---

## 4. Late-arrival observability model

### 4.1 What exists — more than expected, but not durably

An earlier draft of this plan stated that no per-request telemetry existed. **That was wrong**,
and the late-arrival audit (`docs/19`) disproved it. Correcting it changes this section's
conclusion substantially, so the correction is recorded rather than quietly applied.

Per-request telemetry **does** exist, as structured log rows in `public.logs` (`type = 'SCRIPT'`,
`source = 'jobs.api.telematics.sync_trips_and_speeding'`), with the event name in `message` and
the payload in `context`:

| message | rows | carries |
|---|---|---|
| `telematics_provider_request` | 30 021 | `endpoint`, `params.start_timestamp` / `params.end_timestamp` (**the literal Warsaw wall-clock numerals on the wire**), `params.page`, `params.limit`, `sub_window`, `attempt`, `total_requests`, `endpoint_requests` |
| `telematics_trips_compat_page` / `telematics_provider_page` | 199 / 943 | `requested_page`, `returned_count`, `advisory_total`, `accumulated_count`, `meta_last_page`, `short_page`, `total_present`, `elapsed_seconds`, `response_bytes`, `unique_identity_count`, `page_identity_fingerprint` |

The log row's `ts` is the response-received instant. `run_id` joins to `runs` and thence to
`client_schedule_run_history.platform_run_id`. Together these already satisfy most of §4.2:
`docs/19` derived real per-response delay bounds from them, not run-level proxies.

**Three genuine gaps remain:**

1. **Retention: 60 days.** `log-platform-prune.timer` fires daily at 03:30 and
   `log-platform-prune.service` runs `--days 60`. The July 2026 evidence underpinning
   `docs/19` **expires around 2026-08-30**. Any evidence that must outlive that window has to
   be copied out or promoted to a retained structure. This is the single most time-sensitive
   finding in this plan.
2. **No per-trip linkage.** Nothing records which request first returned a given
   `provider_trip_id`. `client_trips.synced_at` / `sync_run_id` are **last-touched, not
   first-seen** — the scheduled path upserts with `overwrite_existing = true`, so every
   overlapping re-request rewrites them. An audit built naively on `synced_at` would be wrong;
   `docs/19` had to fall back to the insert-only backfill (`ON CONFLICT DO NOTHING`) for sound
   first-insert evidence. **This is the decisive gap**, and it gets worse under the new
   cadence, which deliberately re-requests the same trips three ways.
3. **Not queryable as evidence.** The data is JSONB inside a general log table with no index
   on endpoint, sub-window or page. Adequate for forensics; not adequate for a metric or an
   alert.

### 4.2 Required contract

For any trip, establish:

```
last known ABSENT   — the latest trustworthy, genuinely covering request that did not return it
first known PRESENT — the earliest request that did return it
```

with enough provenance to prove the absent observation was trustworthy.

### 4.3 Recommended model — one addition, plus a promotion

§4.1's correction changes the shape of this recommendation. The request-level facts are already
being captured; what is missing is **per-trip linkage** and **durability**. So the smallest
sufficient change is *one* new column plus a retained projection — not a whole new capture path.

**(1) `client_trips.first_seen_request_id`** — the decisive addition, and the one that cannot be
reconstructed after the fact. Set on INSERT only; **never** in the `ON CONFLICT DO UPDATE` list,
exactly as `Dysponent_ID` is protected today (§1.6). This is what makes `synced_at`'s
last-touched semantics harmless: first-seen provenance stops depending on a mutable column, and
a repeated observation cannot create a second first-seen event.

Without this, the new cadence actively destroys evidence — three overlapping run types rewrite
`synced_at` three times for the same trip.

**Correction (2026-08-13): the FK proposed below cannot exist.** `client_trips` lives in the
**per-client business database**; `workflow_a_control` lives in the **platform database
`logdb`** (`008_workflow_a_control_plane.sql`, `control_plane._platform_pg_conn`). PostgreSQL has
no cross-database foreign key, so `first_seen_request_id` is an **unenforced UUID reference** —
see §4.3a, which resolves this and the remaining M4 design choices.

**(2) `workflow_a_control.provider_request_log`** — a retained, queryable projection of what
§4.1 already logs. Justified by retention (60 d → the analysis
horizon must exceed the monthly reconciliation reach) and by needing a durable evidence
anchor for (1), not
by the data being unavailable:

| field | purpose |
|---|---|
| `request_id` (PK) | identity |
| `platform_run_id`, `run_history_id` | binds to the platform run and the fire |
| `endpoint` | `/trips`, `/vehicles/events` |
| `requested_from_utc`, `requested_to_utc` | the absolute interval intended |
| `wire_start_value`, `wire_end_value` | the literal Warsaw wall-clock numerals sent (§1.4) |
| `page`, `cursor` | pagination position |
| `request_started_at_utc`, `response_received_at_utc` | the two instants the lag bounds need |
| `http_status`, `row_count` | outcome |
| `subwindow_complete` | **whether this sub-window finished all pages without budget/provider error** |

`subwindow_complete` is the field that makes an absence *usable*. Absence from a request whose
sub-window did not complete proves nothing. It is derivable today from the page event's
`short_page` / `total_present` / `accumulated_count == advisory_total` reconciliation (§4.1) —
this promotes that derivation to a stored, trustworthy flag instead of re-deriving it per audit.

Most fields already exist in the log payload: `wire_start_value` / `wire_end_value` from
`params.start_timestamp` / `params.end_timestamp`, `page` from `params.page`,
`response_received_at_utc` from the page event's `ts`, `row_count` from `returned_count`. This
is a projection, not new instrumentation.

**`last known absent` is then derived, not stored**: the newest `provider_request_log` row for
the same client and endpoint with `subwindow_complete = true`, `requested_from ≤ trip_start <
requested_to`, and `response_received_at < ` the first-present response. A dedicated
late-arrival table would only cache this derivation, so it is **not** recommended.

### 4.3a Resolved M4 architecture decisions

Decided 2026-08-13, ahead of M4 implementation, and recorded here as the design decisions they
were. **All of them are now implemented and in production** — §22 records what was built
against them, §22.14 the production closure.

**D1 — placement: `workflow_a_control.provider_request_log`, in the platform database `logdb`.**
Coverage finalization already runs there, and §6 condition 6 is evaluated by the dispatcher, not
by the business job. Putting the evidence beside `client_dataset_coverage` lets the projection
and the coverage CAS share one transaction (D3, hinge 2). Placing it in the client business
database would buy an enforceable FK for `first_seen_request_id` at the cost of separating the
evidence from the watermark it must gate — the wrong trade.

**D2 — `client_trips.first_seen_request_id`: nullable UUID, no FK, no backfill.**

- lives in **each client business database**, beside the row it describes;
- `uuid NULL`, **no foreign key** — a cross-database FK is impossible (see the correction in
  §4.3), so referential integrity here is a convention the writer upholds, not a constraint the
  database enforces. State that plainly rather than implying an integrity guarantee that does
  not exist;
- **set on INSERT only**, and **never** present in the `ON CONFLICT DO UPDATE SET` list —
  the same protection `Dysponent_ID` already relies on (§1.6);
- **NULL means exactly one thing: first-seen provenance was never captured for this row.** It is
  not "unknown", not "pending", not a sentinel. Every pre-M4 row is NULL and stays NULL;
- **no backfill, ever.** First-seen provenance cannot be reconstructed after the fact; deriving
  it from `synced_at` would fabricate evidence, which is the precise defect M4 exists to remove
  (§4.1 gap 2). Derived metrics must **exclude** NULL rows, never impute them.

**D3 — atomicity: the two-hinge model.** There is no distributed transaction between the two
databases and none will be introduced; neither two-phase commit nor a transaction manager is
part of this design. Instead, two existing repository mechanisms each carry one hinge:

| hinge | pairing made atomic | mechanism | status |
|---|---|---|---|
| **1** | business commit ⇄ the claim that it committed | the write-once execution-outcome record, emitted at the job's single terminal point **after** `conn.commit()`, carrying `transaction_status`; identity-checked by `verify_outcome` | **already in force — M3**, extended by M4 with sub-window fields |
| **2** | durable completeness evidence ⇄ watermark advance | the `provider_request_log` projection is inserted **inside `_finalize_compat_success`'s existing platform transaction**, alongside the coverage CAS — one connection, one commit, one rollback | **M4** |

The invariant this produces: **the watermark cannot commit without the durable completeness
evidence that supports it, and that evidence cannot commit without the watermark.** The inverse
error — evidence claiming completeness for a business transaction that never committed — is
prevented by hinge 1's ordering: a rollback yields `NOT_COMMITTED` and `record_executed()`
already downgrades the outcome to `FAILED`.

**One residual state remains, and it is correctly fail-closed:** a crash between the business
commit and the outcome-record write leaves committed trips with no record. M3 already refuses to
advance. The gap self-heals through `min(base, W − O)` on a later fire, up to `R`. This needs
alerting (§13), not a stronger transaction model.

**D4 — ordering: `M4_BEFORE_M5_IS_SUPPORTED`.** M4 does not depend on M5, verified against
current schema and source. M4's evidence identity uses dimensions M5 does not touch —
`run_history_id`, `platform_run_id`, `client_id`, `schedule_id`, `dataset_name`, `endpoint`,
sub-window label and page. M5 re-keys `client_dataset_coverage` from `schedule_id` to
`(client_id, dataset_name)` and adds a `run_type` discriminator; neither changes what a request
record means. The only follow-through M5 owes M4 is that any per-schedule *query* over
`provider_request_log` must move with the coverage key — a read-path change, not an evidence-
semantics change. M4 may therefore be implemented first, and should be: §14 already orders it
before the wide runs so their absences are admissible evidence.

### 4.4 Derived metrics

```
publication_lag_lower_bound = last_absent.response_received_at_utc  − trip_start_ts
publication_lag_upper_bound = first_present.response_received_at_utc − trip_start_ts
detection_interval          = first_present.response_received_at_utc
                              − last_absent.response_received_at_utc
```

Report in hours, `HH:MM:SS` and decimal days. Never emit a point publication timestamp — the
evidence supports an interval only.

### 4.5 Retention of the telemetry

Platform logs are pruned at **60 days** (`log-platform-prune.service`, `--days 60`, daily 03:30
via `log-platform-prune.timer`). That is *less* than twice the monthly reconciliation reach, so
the existing log-based evidence cannot support year-over-year lag analysis and cannot outlive a
single reconciliation cycle by much.

`provider_request_log` therefore needs its own retention entry, independent of log pruning:
**180 days** — long enough to compare two consecutive monthly reconciliations and to
characterise seasonal provider behaviour, still small (roughly requests-per-run × runs-per-day
rows).

~~**Time-critical:** the July 2026 evidence behind `docs/19` lives only in `public.logs` and will
be pruned around **2026-08-30** … Recommend exporting the raw `telematics_provider_request` /
`*_page` rows to the backup area before 2026-08-30.~~ **Done — M00. See §4.5a for where the
evidence actually is and what is still exposed.**

### 4.5a Where the July evidence actually lives — verified 2026-08-13

Read-only verification of the M00 export and of the derived analysis, because §14 marked M00
`COMPLETE` without recording a location, and a milestone whose artifact nobody can point at is
not closed.

**Tier 1 — the analysis of record: SECURE.**
`artifacts/telematics_late_arrival_audit.csv` is **tracked in git** (committed in
`3a9743c`) and is present in the release manifest of `fabaaa753f89`. It is therefore in git
history, in every clone, and in every pinned release tree. The 1 031-row cohort, the §3.7a/b
distributions and the §3.7c fire-grid replay are **all** computed from this file alone and
remain fully reproducible after the `public.logs` prune. `ops/tests_manual/test_telematics_m2_release_contract.py`
asserts its contents, so it cannot silently drift.

**Tier 2 — the raw telemetry slice: PRESENT and integrity-verified, but single-copy.**
The M00 export is
`backups/forensics/provider_request_evidence/telematics_provider_request_evidence__20260811T101650Z.csv`
with a sibling `.manifest.json` (schema `log-platform-logical-slice-backup/v1`):

| property | value |
|---|---|
| rows | 31 939 — `telematics_provider_request` 30 415, `telematics_trips_compat_page` 581, `telematics_provider_page` 943 |
| span | 2026-05-11 → 2026-08-11 (**no date filter — the full retained horizon**) |
| sources | `jobs.api.telematics.sync_trips_and_speeding`, `jobs.api.telematics.backfill_trips_insert_only` |
| size | 22 012 769 bytes |
| SHA-256 | `fe5de0d4c80ddfdd477d3055fc5acb65e412e63a24e8526a1a5658fb3200cc43` — **re-verified against the manifest on 2026-08-13** |
| source rows deleted / modified | 0 / 0 — a pure read |

**Confirmed not at risk from the two sweeps that could plausibly reach it:** the file is not in
`public.logs`, so `log-platform-prune.service --days 60` cannot touch it; and
`ops/backup_retention.py` `discover_sets` iterates only *files* directly in `backups/`, matching
only the `postgres_*` / `minio_*` / manifest pattern, and documents that "unrecognised files are
ignored, never deleted" — the `forensics/` subdirectory is out of its scope entirely.

**Residual exposure, stated plainly.** `backups/` is in `.gitignore`, so this export is **not**
in git and **not** in any release tree. `ops/backup.sh` writes *into* `backups/`; it does not
back that directory up. The raw slice therefore exists in exactly **one copy, on one host, on
one filesystem**, with no off-host replication. Losing that filesystem would not cost the
analysis of record (tier 1), but it would permanently cost the ability to **re-derive** the lag
distribution from raw request/response rows, or to answer a new question about July 2026 that
the derived CSV does not already contain — including anything the Telematics support conversation
(§8 of `docs/19`) might raise.

**Recommended, not authorized, and not blocked by any milestone:** copy
`backups/forensics/provider_request_evidence/` to a second location outside this filesystem and
record the SHA-256 alongside it. That is a write, and it needs its own authorization; it was
deliberately not performed by the pass that verified the above.

---

## 5. Deployment boundary

**No implementation milestone may begin before this is resolved** (§1.9: production currently
executes an uncommitted working tree).

Target:

1. Create a pinned release worktree at `/opt/log-platform-release`,
   checked out to a **tag**, not a branch.
2. Repoint `log-job-runner.sh` `BASE_DIR` to the release path. The wrapper's SHA-256 is already
   tracked as an operational identity (`docs/15` §1) — update that record.
3. Promotion = commit → tag → `git -C <release> fetch && checkout <tag>` → verify clean
   worktree and expected HEAD → restart nothing (the timer picks up the next tick).
4. Rollback = check out the previous tag. Deterministic and fast.
5. The release worktree needs its own `.venv` **or** an explicitly shared one; the wrapper
   derives `VENV_PY` from `BASE_DIR`, so this must be decided, not defaulted.
6. Both advisory-lock domains must be considered during promotion: the Workflow A dispatcher
   lock (`728503746327118001`) and the Workflow B orchestrator lock. Promotion mid-run is safe
   for A (subprocess already launched from the old path completes), but the plan should require
   promotion outside the 02:00–06:00 band regardless.

**Immediate precondition:** the uncommitted `provider_client.py` / `sync_trips_and_speeding.py`
delta must be reviewed and committed *before* any boundary work, otherwise the first promotion
would either ship unreviewed code or silently revert the live `/trips` request-time fix.

---

## 6. Coverage correctness

`covered_through_ts` **may** advance when, and only when, all hold:

1. the dispatched job exited `rc == 0`; **and**
2. a verified `ExecutionOutcome` exists whose `outcome` ∈ `{EXECUTED_COMMITTED,
   EXECUTED_ZERO_ROWS_COMMITTED}`; **and**
3. that outcome asserts `provider_execution_entered`, `business_transaction_entered` and
   `transaction_status == COMMITTED`; **and**
4. its platform-run identity binds to the run actually dispatched
   (`require_platform_run_identity`); **and**
5. the claim-time coverage snapshot still matches under CAS, and the candidate is strictly
   greater than the stored watermark; **and**
6. every sub-window of the effective window completed without budget exhaustion or provider
   error.

A **valid zero-row run advances coverage** (condition 2 admits
`EXECUTED_ZERO_ROWS_COMMITTED`). A failed, skipped, rolled-back, partially-paginated or
identity-unbound run **must not**, regardless of exit code.

Conditions 2–4 are exactly what `execution_outcome.py` already implements and nothing consumes
(C2). Condition 6 is new and pairs with `subwindow_complete` (§4.3). Condition 5 is already in
force.

A reconciliation run whose `E_end` is behind `W` performs the existing validated no-op and is
**not** a failure.

---

## 7. Historical reconciliation interaction

Daily, weekly and monthly windows overlap by design. Interaction rules:

- **Persistence:** `ON CONFLICT (client_id, provider_trip_id)` absorbs repeated trips (§1.6).
  No duplicate is possible.
- **Concurrency:** structurally serialised (§1.7). No overlapping-run corruption is possible.
- **Coverage:** all three share one watermark (§3.2 B2). Only a run whose `E_end` exceeds `W`
  moves it; the reconciliation runs' value is the *historical* part of their window, which
  never touches `W`.
- **Ordering:** the widest window should not be the one that advances `W` on a boundary day.
  With weekly 02:30 → monthly 03:00 → daily 04:00, the daily run is last and is the natural
  watermark owner. This ordering is a deliberate property, and the plan should assert it.
- **`Dysponent_ID` and `first_seen_request_id`** are both excluded from `DO UPDATE`, so
  reconciliation cannot overwrite enrichment or restate first-seen provenance.

---

## 8. Report 207 follow-up

Target: when a reconciliation run inserts trips for a historical date, previously unmatched
Report 207 rows for that date become eligible for one bounded retry.

- Do **not** widen the orchestrator's per-raw-file scope — that would re-scan the entire report
  corpus twice a day.
- Add a bounded, explicitly-invoked retry pass keyed on **date ranges touched by a
  reconciliation run**, reusing the existing `NO_MATCHING_TRIP` retry predicate (§1.8) with
  `raw_file_id = None` and a `LIMIT`.
- `ops/recover_report_207_speed_violations.py` already contains the recovery shape; prefer
  extending it over writing a new path.
- Observable: retry backlog = count of rows still `migrated_to_client_db_error =
  'NO_MATCHING_TRIP'` older than N days.

Gate: S1 should be demonstrated against a real production row before the retry path is built.

---

## 9. Dysponent_ID follow-up

Enrichment cannot be destroyed by trip re-upsert (§1.6, confirmed). The real gap is S3: the
enrichment window starts at a `date_from` derived from the previous committed source load, so a
reconciliation-inserted trip older than that boundary is never enriched.

- **Extend the window, not the trigger.** After a reconciliation run, invoke enrichment with
  `date_from` set to the earliest `start_timestamp` that run actually inserted, rather than the
  source-derived boundary. The job already accepts an explicit `date_from` parameter
  (`_optional_date(params.get("date_from"))`), so this needs no new selection logic — only a
  caller that knows the reconciliation's insert range. §4.3's `first_seen_request_id` plus the
  request log give exactly that range.
- **Make the technical NULL observable:** `Dysponent_ID IS NULL` count and rate for trips newer
  than N days, so "not yet enriched" is never silently read as "no dispatcher". This is also
  the input Eco needs (§10).
- Do not backfill as part of this plan; that is a separate authorized operation.

---

## 10. Eco readiness

Eco must stop inferring business meaning from technical absence (C3).

Introduce an explicit readiness input to `job_eco_driving_aggregate`, evaluated per (client,
period) before materialization:

- trips coverage: `covered_through_ts` ≥ period end, and the period's dates were touched by a
  trusted run;
- owned event/speed metrics present for the period;
- driver/dispatcher identifiers resolved (`Dysponent_ID` null-rate below a threshold);
- assignments present.

If readiness fails, the run must **either** refuse to materialize **or** persist an explicit
incompleteness marker — never emit `NO_DISTANCE` / `UNKNOWN_DRIVER` as if it were an observed
business fact. `calculation_status` already exists as the natural carrier for a new
`INCOMPLETE_SOURCE_DATA` value; `qualification_status` should stay business-only.

Ranking must exclude incomplete rows from position assignment rather than ranking them at zero.

---

## 11. Retention

Required horizon for the new cadence, worst case:

| contributor | value |
|---|---|
| monthly reconciliation reach (October, DST) | 31.0417 d |
| stabilization delay `D` | 0.125 d |
| overlap `O` | 0.042 d |
| scheduler fire age (monthly fires once; a missed fire is lost, not deferred) | 0 d |
| DST widening | included above |
| operational safety margin | 7 d |
| **minimum required retention** | **≈ 38.2 d → adopt 45 d floor** |

The prior ~33-day figure was computed for a 14-day rolling lookback and does **not** transfer;
it is superseded.

Current actual configuration: `workflow_a_control.client_table_retention` sets
`retention_days = 365` for every ALPHA00001 table, and **`enabled = false` for all of them**.
Retention is therefore not a constraint today and **requires no change**. The operative
conclusion is a floor: `client_trips` retention for ALPHA00001 must never be set below **45
days**, and enabling retention at any value below that would silently break the monthly
reconciliation. Record this as a guard, and add it to the retention worker's validation if
cheap.

---

## 12. Failure semantics

| condition | required behaviour |
|---|---|
| provider failure | `TelematicsProviderSafetyError`, whole sub-window discarded (already in force). Run FAILED, coverage unchanged. |
| partial pagination | sub-window discarded; `subwindow_complete = false`; coverage must not advance; the run's absences are **inadmissible** as late-arrival evidence. |
| transaction failure | rollback; outcome `FAILED` or `transaction_status = NOT_COMMITTED`; coverage unchanged. |
| zero rows | valid work. `EXECUTED_ZERO_ROWS_COMMITTED`; coverage **advances**. |
| duplicate rows | absorbed by `ON CONFLICT (client_id, provider_trip_id)`; not an error; counted as `already_existing`. |
| late arrivals | inserted once; `first_seen_request_id` set on INSERT only; a first-seen event is emitted exactly once. |
| retry | no automatic retry of a failed fire — the next cadence fire re-requests the same history because `L` is re-derived. Weekly/monthly widen the recovery envelope. |
| missed schedule fire | **lost, not queued** (§1.7, no catch-up). A missed monthly fire means that month is never reconciled unless manually recovered. This is a real gap and must be alerted on (§13). |
| concurrent runs | structurally impossible (global advisory lock + `_count_running`). |

---

## 13. Monitoring and alerts

**Actionable alerts** (page or ticket):

| alert | threshold |
|---|---|
| trips_sync fire missed | no `SUCCESS` run for a schedule within 1.5 × its cadence |
| monthly reconciliation did not run | no `MONTHLY_RECONCILIATION` SUCCESS by 06:00 on day 1 |
| coverage stalled — **one** missed advancement | `covered_through_ts` not advanced within **1.5 × that schedule's own cadence** (see below) |
| coverage stalled — **repeated** | not advanced across **≥ 2 consecutive** cadence periods |
| outcome-gate refusal | any terminal `FAILED` carrying an M3 refusal code — first occurrence |
| stuck `RUNNING` | a history row `RUNNING` beyond `stale_running_timeout_minutes` (720) × 1.1 |
| **multi-client stall** | ≥ 2 clients stalled in the same window — one alert, not one per client |
| approaching the `R` cliff | no advancement for **> 21 d** (⅔ of `R`), i.e. self-healing is about to stop being possible |
| coverage advanced without eligible outcome | any occurrence — invariant breach |
| sub-window incomplete | any `subwindow_complete = false` in a run that advanced coverage |
| provider budget exhaustion | any occurrence |
| newly discovered late arrival > 13 days | any occurrence — outside the measured distribution |
| Report 207 `NO_MATCHING_TRIP` backlog | > N rows older than 7 days |
| Eco materialized while readiness failed | any occurrence |
| release worktree dirty or HEAD ≠ expected tag | any occurrence |

**Correction (2026-08-13): "26 h" was a daily-client number stated as a fleet-wide one.**
Thresholds must be expressed as a multiple of *each schedule's own cadence*, never in absolute
hours. BRAVO00016 `trips_sync` is **weekly**; a fixed 26 h would fire against it continuously and
train the operator to ignore the alert. Concretely: daily schedules → 36 h (one miss) / 60 h
(repeated); weekly schedules → 10.5 d / 17.5 d. Derive from
`client_dataset_schedule.frequency`, do not hard-code.

**Why the multi-client alert is a separate row.** One global advisory lock and one installed
wrapper mean a single platform fault stalls *every* client at once. Fanning that out as five
per-client alerts buries the actual signal, which is "the platform is broken", not "five clients
independently failed".

**Basis: a combination, not a single source.** Expected fire time comes from
`client_dataset_schedule` (the authority on cadence); what actually happened comes from
`client_schedule_run_history`; what durably advanced comes from
`client_dataset_coverage.covered_through_ts`. None alone suffices — run history cannot see a fire
that never fired (§1.7 has no catch-up), and the watermark alone cannot distinguish "did not
run" from "ran and correctly refused".

**Why M3 makes this urgent.** M3 widened the class of conditions that cost a day's watermark
progress from "the job failed" to "the job failed **or** its evidence did not survive" (§20.4).
That is deliberate and fail-closed, but until these alerts exist the only thing standing between
a silent multi-client coverage stall and the `R = 31 d` cliff is a runbook watch.

**Evidence payload every alert must carry:** `client_code`, `schedule_id`, `dataset_name`,
expected `scheduled_fire_ts`, `run_history_id` + `status` + `error_summary`, `platform_run_id`,
`covered_through_ts` before/after, `bootstrap_status`, active `release_id`, consecutive-miss
count, and **days remaining before the `R` clamp** — the last is the operator's actual urgency
signal and is not derivable from the others at a glance.

**Deduplication and clearing.** Fire-scoped alerts dedupe on
`(alert, schedule_id, scheduled_fire_ts)`; stall alerts on
`(alert, schedule_id, consecutive_miss_count)`, so escalation stays visible while a steady stall
does not re-page every 5-minute tick. Stall and missed-fire alerts clear automatically on the
next advancement from an eligible outcome. **The two invariant-breach rows — coverage advanced
without an eligible outcome, and `subwindow_complete = false` on an advancing run — must never
auto-clear**; they require a human to record what happened.

**Informational metrics** (dashboard only): run type; lookback/calendar window; requested
provider intervals; rows returned; pages; inserts; updates; already-existing; rejected;
provider errors; coverage eligibility; newly discovered late arrivals; delay bucket; max
observed delay; rolling p50/p95 **only when the sample is adequate**; `Dysponent_ID` missing
count/rate; Report 207 retry backlog; Eco readiness/exclusion counts.

---

## 14. Implementation milestones

Ordered by dependency, not by the contract's suggested order. Two deviations, both justified:
the uncommitted-delta review precedes everything because it currently *is* production; and
DAILY `L = 3` moves ahead of the telemetry work because it is a one-row change that closes the
majority of the measured lag distribution immediately, and delaying it has an ongoing data cost.

| # | milestone | status | why here |
|---|---|---|---|
| **M00** | **Export raw provider request/page log rows for 2026-06-01 → 2026-08-11 to the backup area** | **COMPLETE** | read-only, blocks nothing, and the evidence is destroyed by log pruning around **2026-08-30** (§4.5) |
| **M0** | Review and commit the uncommitted `provider_client.py` / `sync_trips_and_speeding.py` / `backfill_trips_insert_only.py` delta | **COMPLETE** | it is live production code that exists in no commit (§1.9) |
| **M1** | Deployment boundary: pinned release worktree, wrapper repoint, promotion + rollback runbook | **COMPLETE** — active since 2026-08-11 (release `342c0b8e50ad` then; `672adfb152f8` since M2) | nothing below is safely deployable without it (§5) |
| **M2** | **DAILY `lookback_days = 3`** (the timezone/run_time pair decision was **not** taken, §3.6 / §16 decision 3) | **COMPLETE** — deployed on release `672adfb152f8`; first natural `L = 3` run verified 2026-08-13; see §19, §19.12 | one control-plane row; closes the bulk of the measured lag distribution; no schema change |
| **M3** | Wire `execution_outcome` into the dispatcher; coverage advances only on verified outcome (§6 conditions 2–4) | **COMPLETE** — deployed on release `fabaaa753f89` (active since 2026-08-13T10:59:21Z); production-verified 2026-08-14 on three natural fires (DELTA00001, ALPHA00001, FOXTROT00001), all `EXECUTED_COMMITTED`, all advancing to exactly `E_end`, zero refusals. See §20, §20.7 | C2; contract already written and tested — this is wiring |
| **M4** | `provider_request_log` + `client_trips.first_seen_request_id` + `subwindow_complete` (§4.3), and §6 condition 6 | **COMPLETE** — deployed on release `2782550f8efe` (active since 2026-08-14T15:27:58Z); production-verified 2026-08-15 on three natural fires (DELTA00001, ALPHA00001, FOXTROT00001), each exactly tiling its effective window with complete `FINALIZED` evidence bound to that execution. See §22, §22.14 | must precede the wide runs so their absences are admissible evidence |
| **M5** | Schema: `run_type` discriminator, re-key `uq_client_dataset_schedule`, re-key coverage to `(client_id, dataset_name)` (§3.2 B1/B2) | **COMPLETE** — migration 062 applied; deployed on release `b682df90c958` (activated 2026-08-15T17:30:46Z); production-verified 2026-08-17 on natural scheduled fires for DELTA00001 (daily, `L = 7`), FOXTROT00001 (`fuel_daily_aggregation`, non-coverage-bearing) and BRAVO00016 (**weekly frequency, `run_type = DAILY`** — the role-vs-cadence proof), all SUCCESS with unchanged outcomes. Created **zero** reconciliation schedules. See §23, §23.8 | prerequisite for any second cadence. **M4 does not depend on it** (§4.3a D4) |
| **M6** | **WEEKLY, Monday 00:30 Europe/Warsaw, `L = 16`** (§3.7c; fire time revised from 02:30, §3.1). The reconciliation schedule lifecycle: one registered deny-by-default mutation surface, creation and activation separated, every inherited column projected from the base row | **ENABLED for ALPHA00001 and BRAVO00016** (2026-09-08, `M6-ENABLE-2026-09-08-OWNER-REQUESTED`). Registered disabled for all five on 2026-08-20. The first two real fires **failed** on a latent defect this milestone was the first thing ever to exercise — see §25 — fixed and deployed on release `2091baf1ef1d`. DELTA00001, FOXTROT00001, ECHO00001 remain registered-disabled | first consumer of M5. Requires **no** migration and **no** `R` change |
| **M7** | **MONTHLY day 1, 03:00 Europe/Warsaw, rolling `L = 32`**; `trips_max_recovery_span_seconds` raised to `32·86400 + O = 2 768 400` (migration 072). **The calendar window mode is no longer the approved direction** (§3.1a) | **COMPLETE and ENABLED for ALPHA00001 and BRAVO00016** (2026-09-08, `M7-MONTHLY-RECONCILIATION-2026-09-08-OWNER-REQUESTED`), on release `2091baf1ef1d`. `L = 32` chosen from the August 2026 D105.2 reconciliation, not the July cohort: a replay of the 907 confirmed lost trips over real fire grids leaves 113 uncaught by weekly `L = 16` alone and **zero** once the monthly runs (§25.2) | reuses M6's surface for the monthly role |
| **M8** | Report 207 bounded retry keyed on reconciliation-touched dates (§8) | PLANNED | needs M6/M7 to have something to react to |
| **M9** | Dysponent enrichment readiness + null-rate observability (§9) | PLANNED | independent of M8; may run in parallel |
| **M10** | Eco readiness gate (§10) | PLANNED | consumes M3, M4 and M9 signals |
| **M11** | Monitoring, alerts, retention floor guard, documentation sync (§11, §13) | PLANNED | last |

---

## 15. Verification matrix

| # | requirement | deterministic test | integration check | read-only production observation | abort / rollback condition |
|---|---|---|---|---|---|
| M0 | live delta is reviewed | existing `ops/tests_manual/test_telematics_trips_wire_time_contract.py` + `..._vehicle_events_wire_time_contract.py` pass | full manual suite for telematics | next daily run SUCCESS, row counts unchanged in shape | any test regression → do not commit |
| M1 | production runs from a pinned tag | wrapper `BASE_DIR` unit assertion | dry-run promotion + rollback in staging path | `git -C release status` clean, HEAD == tag, wrapper SHA-256 matches record | HEAD ≠ tag or dirty → roll back immediately |
| M2 | `L = 3` semantics | `derive_effective_window` unit: `E_start == F − 3·86400 − D − O` | dispatcher window assertion for the ALPHA row | one daily run: **`nominal_window_start_ts` exactly 3 d before fire** — *not* `window_start_ts`, which is the effective start at `F − 76 h` (§19.10); trips inserted ≥ prior baseline | nominal window not 3 d, or run FAILED → roll back per §19.9 |
| M2 | events cost | ALPHA gated off (`disabled` + `report_207_migration`) | — | first `L = 3` run issues **0** `/vehicles/events` requests | non-zero events count → configuration changed; stop and re-evaluate §19.4 |
| M2 | tz/run_time pair | fire-time unit across both DST transitions | — | fire lands at the intended Warsaw wall-clock | fire moved unintentionally → revert both fields together |
| M3 | coverage only on verified outcome | unit per outcome value: 2 eligible, 3 not; and each of the 3 missing assertions refused | dispatcher integration: `rc == 0` + `FAILED` outcome ⇒ no advancement | `covered_through_ts` advances only on runs with an eligible outcome | any advancement without an eligible outcome → disable schedule |
| M3 | valid zero-row run advances | unit: `EXECUTED_ZERO_ROWS_COMMITTED` ⇒ eligible | zero-row integration run | a genuine zero-row day still advances `W` | zero-row run stalls coverage → revert |
| M4 | request log completeness | unit: one row per request, both instants populated | multi-page run writes N rows with monotone `page` | `provider_request_log` row count per run matches the `telematics_provider_request` log-row count for the same run (the existing logs are the oracle) | mismatch → projection is lossy, halt M6 |
| M4 | first-seen linkage is sound | unit: `first_seen_request_id` set on INSERT, unchanged on conflict | re-run same window: `synced_at` changes, `first_seen_request_id` does not | no trip has a `first_seen_request_id` newer than its `synced_at` predecessor run | value tracks `synced_at` → last-touched bug reintroduced, revert |
| M4 | `subwindow_complete` correctness | unit: budget exhaustion ⇒ `false` | forced partial-pagination integration | no `true` on a run that raised a safety error | `true` on an incomplete sub-window → invariant breach, halt |
| M4 | first-seen is once-only | unit: `first_seen_request_id` absent from `DO UPDATE SET` | re-run same window twice, value unchanged | value stable across daily/weekly/monthly overlap | value mutates on re-upsert → revert |
| M5 | three cadences representable | `test_telematics_m5_multi_cadence_identity_postgres.py`: 3 roles coexist, duplicate `run_type` rejected, unknown role rejected, second base schedule rejected | same suite: two schedules resolve one coverage row; one advances, the other observes; behind-`W` no-op preserved; stale claim refused | exactly one coverage row per (client, dataset) | >1 watermark per dataset → roll back migration |
| M5 | the CAS did not narrow | same suite: all eleven claim fields perturbed **in the claim**, one at a time, asserted equal to `COVERAGE_CAS_FIELDS`; nine of them also perturbed **in the stored row**; `client_code` excluded and proven so | M3/M4 suites pass unchanged | watermark advances only under a matching claim | any claim field no longer refused → the re-key weakened the predicate, halt |
| M5 | pre-M5 rollback stays possible | same suite: the pre-M5 `WHERE schedule_id = …` statement is executed verbatim against the migrated schema | affects exactly one row while only base schedules exist; zero rows for a reconciliation schedule | a rolled-back release still advances the base watermark | the old shape resolves 0 or >1 rows → do not migrate |
| M5 | owner/provenance coherence | same suite: coverage or recovery anchored to another client's or dataset's schedule is rejected by the composite FK; re-anchoring by UPDATE rejected | 062 refuses pre-existing incoherent coverage and recovery rows, atomically | every coverage row's anchor belongs to its own owner | any incoherent row accepted → owner-keyed addressing is untrustworthy, halt |
| M5 | base/provenance lifecycle | same suite: deleting or re-owning the anchoring schedule is refused, with and without a reconciliation cadence present; watermark unchanged after each refusal | a reconciliation cadence cannot become a second anchor or a replacement one | no lifecycle action deletes or re-anchors shared coverage | a cascade or re-anchor succeeds → the shared watermark is destructible, halt |
| M5 | runtime provenance defence | same suite: a watermark anchored to a reconciliation cadence is refused at load, mutating nothing | — | the base-role half of the anchor holds where an FK cannot express it | a fire runs against a non-base anchor → halt |
| M5 | release gate verifies definitions | `test_release_schema_preflight_postgres.py`: eleven M5 cases — ledger missing, `run_type` missing, same-named wrong columns, same-named wrong CHECK, missing coverage uniqueness, reverted FK, wrong base-index predicate, wrong recovery predicates, wrong approval keys, `NOT VALID` constraint, correct shape passes | M4 061 checks unchanged and green | activation refuses any materially drifted 062 shape | a drifted shape activates → the gate is name-only again, halt |
| M5 | operator resolvers are base-scoped | same suite: with a base *and* a reconciliation schedule present, the four resolvers driven directly (coverage-bootstrap audit, bootstrap enabled-count, cold-start audit, trip-metrics diagnostic) pick the base and mutate neither; the child-job read is asserted on its resolved row; activation, C11 recovery and onboarding are asserted statically | — | M6 does not break an M5-owned operator surface | any resolver ambiguous or picking the reconciliation row → halt |
| M5 | no resolver escapes the audit | same suite: **recursive** walk of the live tree — every module naming `client_dataset_schedule` must fall into exactly one of base-intended, whole-inventory, transitive consumer or exact-schedule/helper (§23.2a), with no module in two categories and no stale entry | each category is asserted on its own semantic, not its label: whole-inventory schedule queries carry no `run_type` predicate; transitive consumers own no schedule SQL and inherit scope from `load_dataset_schedule`; the `schedule_id`-keyed surface carries its primary-key predicate; the helper issues no schedule SQL | a new resolver cannot be added silently, and a module cannot be parked in a category whose semantic it does not have | an unclassified live module, or a category whose semantic assertion fails → halt |
| M5 | the diagnostic keeps its LEFT JOIN | same suite: an enabled client with **no** `trips_sync` schedule is driven through the real `_load_clients` in both invocation modes | reported exactly once unfiltered and once by `--client-code`, with `trips_sync_enabled` and `event_enrichment_mode` NULL; the base-role predicate is asserted to sit in the JOIN condition | base scoping did not silently demote the LEFT JOIN to an inner join and erase schedule-less clients | the no-schedule client disappears from either path → halt |
| M5 | 062 refuses rather than repairs | same suite: two watermarks, absent uniqueness, concurrent active recoveries | each contradiction raises and installs no partial re-key | migration output is a visible stop, never a silent normalization | any repair statement in 062 → reject the migration |
| M6 | Monday 00:30 Warsaw firing, 16-day semantics | `test_telematics_m6_weekly_reconciliation.py`: `latest_scheduled_fire_local` swept hourly across both 2026 DST transitions (one fire per week, always Monday 00:30 local, 7-day spacing); window unit `E_start == F − 16·86400 − D − O` | M5 suite already proves weekly + daily share one watermark, that an eligible weekly advance moves `W`, and that a behind-`W` weekly run is a validated no-op | first Monday run: nominal window exactly 16 d, duplicates 0, `W` advanced to exactly `E_end` | duplicates > 0 or corruption → disable weekly |
| M6 | `R` does not clamp `L = 16` | same suite: `E_start` strictly above the `R` floor at `L = 16`, **and** `L = 40` proven to be clamped so the assertion is not vacuous | — | first Monday run's `window_start_ts` is `F − 16 d − D − O`, not `E_end − R` | `E_start == E_end − R` → the window was silently truncated, halt |
| M6 | inherited configuration is not defaulted | same suite: `event_enrichment_mode` and `overwrite_existing` copied from the base; a base row missing or NULLing either is refused; the physical column set is asserted exhaustively classified against `information_schema` | end-to-end register/enable against a disposable database: the stored weekly row carries `event_enrichment_mode = 'disabled'` | first ALPHA weekly run issues **0** `/vehicles/events` requests | non-zero events count → a default was applied; disable weekly immediately |
| M6 | the mutation guard stays fail-closed | same suite: unregistered surface, onboarding, base role, `enabled = true`, ineligible dataset, base disabled, non-`READY` coverage and `strict_meta` each refused with their own code; `test_telematics_schedule_activation_postgres.py`'s repository scan still green and its allowlist asserted equal to the registry | end-to-end: `--execute` without a matching `--confirm-client-code` writes nothing | no schedule row exists that no registered surface created | any unregistered inserter → halt |
| M6 | DAILY is unchanged | same suite: L=3/7/1 nominal windows and `E_end = F − D` unchanged; `ScheduleRow.run_type` still defaults to the base role; **AST assertion that no dispatcher control flow tests `run_type`** | full M2/M3/M4/M5 suites pass unchanged | base rows byte-unchanged; daily fires keep their windows and outcomes | any daily window or outcome moves → revert |
| M6 | provider budgets | unit asserting the in-force `.env` limits (§1.5), not code defaults | real weekly run request/page counts recorded | requests/endpoint and requests/run at ≥ 3× headroom against in-force limits; budget env present in the release environment | headroom < 3×, or budget env missing after promotion → do not enable M7 |
| M6 | **peak memory — NOT closed by any local test** | — | — | first real weekly run: peak RSS recorded against the L=3 baseline (~5.3× the rows resident, §1.5a) | RSS approaching the host limit → reduce `L` or introduce a run-level row cap before M7 |
| M7 | calendar window | unit for 28/29/30/31-day months, Jan→Dec boundary, both DST transitions | monthly run window equals `[prev_month_start, month_start)` in Warsaw | `window_start_ts`/`window_end_ts` match the calendar month exactly | any month off by ≥ 1 h → disable monthly |
| M7 | `R` no longer clamps | unit: `E_start > E_end − R` for all 12 months at 03:00 | November (October, DST) run covers 31.0417 d | November run's `E_start` == October start | clamp binds → raise `R` further, do not ship |
| M7 | no under-fetch across DST | unit: both transitions, bounded over-fetch only | — | trips at the month's first and last hour present | any missing boundary hour → abort |
| M4/M6 | late-arrival detection | simulate run A covering without trip → run B covering with trip: inserted once, classified late, both links correct, interval correct | integration with a synthetic provider | first real detection matches manual reconstruction | mislinked absent/present → telemetry unusable, halt M8 |
| M4 | local failure ≠ provider late | simulate provider returned trip, local insert failed ⇒ **not** classified as provider late | integration with forced transaction failure | no such misclassification in real data | misclassification → block external reporting |
| M4 | repeated observation | trip already present on later pulls creates no second first-seen event | daily+weekly+monthly overlap | first-seen count == distinct trips | second event emitted → revert |
| M8 | 207 retry | unit: `NO_MATCHING_TRIP` selected with `raw_file_id = None` + LIMIT | historical trip appears ⇒ bounded retry migrates it | backlog count falls, no full-corpus rescan | rescan cost or wrong rows → revert |
| M9 | Dysponent (S3) | unit: reconciliation-derived `date_from` covers the run's earliest inserted `start_timestamp` | late historical trip inserted ⇒ enrichment invoked with the widened window ⇒ trip enriched | `Dysponent_ID IS NULL` rate observable and falling; no trip left unenriched below the old source-derived boundary | rate not falling, or trips still unenriched outside the window → investigate before M10 |
| M10 | Eco readiness | unit: incomplete source ⇒ refuse or `INCOMPLETE_SOURCE_DATA`, never `NO_DISTANCE` | readiness-fail integration blocks materialization | no Eco snapshot written while readiness fails | a business zero emitted from technical absence → disable Eco schedule |
| M11 | retention floor | unit: retention < 45 d for ALPHA `client_trips` rejected | — | configured value ≥ 45 d or disabled | value below floor → block |

### Coverage test set (applies to M3)

Valid non-zero run → advances. Valid zero-row run → advances. Provider failure → does not.
Parser failure → does not. Transaction rollback → does not. Partial pagination → does not.
Skipped run → does not. Untrusted / identity-unbound outcome → does not.

---

## 16. Open decisions for the approver

1. ~~**§3.7c — the weekly lookback.**~~ **RESOLVED 2026-08-17: `L = 16`.** The approver accepted
   the engineering recommendation after the fire-grid replay was independently reproduced
   (L=3 → 171 missed; +weekly L=8 → 171; L=12 → 23; L=14 → 4; L=15 → 0; L=16 → 0) and after the
   horizon constant was corrected from `0.208` to `0.1667`. `L = 16` guarantees 9.167 d and clears
   the 8.610 d directly proven maximum in every fire phase; `L = 15` guarantees 8.167 d and does
   not. The decision is implemented locally as M6 (§24). **It remains true that no production
   schedule row exists and no lookback of any deployed schedule changed.**

   **Superseded framing:** an earlier revision presented this as "~0.137 %, not
   0.029 %" and cited "16 records still absent at 14 days" — figures that appear in no retained
   evidence and are withdrawn (§3.7, §19.11). A later revision quoted the horizons as
   2.208 / 8.208 / 9.208 d; those are one hour high and are corrected in §3.7c.
2. **§3.2 B1/B2** — `run_type` discriminator (recommended) vs. separate dataset names; and
   re-keying coverage to `(client_id, dataset_name)`. **Not a blocker for M4** (§4.3a D4).
3. **§3.6** — correct ALPHA's schedule timezone to `Europe/Warsaw` (with the paired `run_time`
   change), or leave it on `UTC`.
4. **§5** — release worktree `.venv`: dedicated or shared.
5. **§11** — adopt the 45-day retention floor as an enforced guard, or documentation only.

---

## 17. Cross-document status corrections

`DOCUMENTATION_CODE_MISMATCH` — `docs/15_telematics_coverage_mutation_contract.md`, status header
and §1.

- **Docs claim:** migrations `055`–`057` "are applied in production" but simultaneously that
  authorized inspection "confirmed migrations 055–057 absent, the coverage table absent […]
  zero compatibility clients and therefore zero production coverage rows"; migration `058`
  "has not been applied to production"; C11 "has never been executed in production"; "exactly
  one production coverage row exists (`BRAVO00016` / `trips_sync`)".
- **Code/production shows:** `schema_migrations` contains `055`, `056`, `057`, `058` and `059`.
  `client_dataset_coverage` exists with **five** rows (BRAVO00016, ALPHA00001, FOXTROT00001,
  DELTA00001, ECHO00001), all `READY`. ECHO00001 carries
  `covered_through_source = 'manual_recovery'`, which is written only by the C11 path —
  therefore C11 **has** executed in production.
- **Which represents intent:** the code and production state. The document's §1 paragraph is a
  stale snapshot from an earlier verification round and its status header was not updated when
  058/059 shipped.
- **Which must change:** `docs/15`. **Resolved 2026-08-17** in the M5 documentation closure: the
  status header now records the `062` ceiling, five `READY` coverage rows and C11 as executed, and
  the stale §1 verification paragraph is explicitly labelled a superseded historical snapshot rather
  than rewritten.

`DOCUMENTATION_CODE_MISMATCH` — `docs/18_telematics_trips_request_time_contract.md` §3.3, lag
distribution table.

- **Doc claims:** "13 days | none observed", with 0.029 % at 10 days and 0.008 % at 12 days.
- **Measured evidence shows:** the independent audit (`docs/19`) measures each record's
  publication lag against actual logged request/response evidence rather than an ordering
  proxy — a materially stronger instrument for the near tail. Its **directly proven** maximum
  is 8.61 days, with **zero** records proven absent beyond 11 or 14 days (§3.7a). Its
  **inferential** `provider_trip_id`-ordering analysis does reach further — 109 records at an
  inferred allocation age ≥ 14 days, max 20.31 days (§3.7b) — but `docs/19` §9.2 labels that
  assumption unproven and the "did not yet exist" direction the weaker one.
- **Why they differ:** §3.3 infers lag from `provider_trip_id` creation ordering. `docs/19`
  §5.1 measures it from request evidence and §5.4 re-derives it from ordering. The proxy
  understates the near tail — a trip is only observable once some request covering it is
  issued, and the July cadence (`L = 1`) rarely re-requested older dates.
- **Which represents intent:** neither is "intent" — all are measurements. The request-evidence
  measurement is the stronger instrument; the ordering analyses on both sides are the weaker one.
- **Which must change:** `docs/18` §3.3 needed a correction note, not a refutation. **Corrected
  while implementing M2**, then **re-corrected during M2 release preparation (§19.11)**: the
  first attempt propagated this plan's own withdrawn "16 records provably absent at 14 days"
  figure into `docs/18` and overstated §3.3 as "decisively false". Both are withdrawn. §3.3's
  "none observed beyond 13 days" is *consistent* with the direct evidence and is contradicted
  only by the inferential reading. §3.3 now carries a note stating exactly that, with its
  original table retained as the original measurement.

`docs/18` §3.3's `lookback_days = 14` recommendation is superseded by §3 of this plan, as
recorded at the top. Its measured lag distribution is retained as evidence **subject to the
correction above** — and note that the superseded `L = 14` was, on the corrected tail, closer to
adequate than the `L = 8` this plan is required to adopt.

---

## 18. Correction log for this document

Recorded so later readers can see what was revised and why, rather than trusting a clean
narrative.

1. **§4.1 — "no per-request telemetry exists" was wrong.** The original draft concluded this
   from the absence of a dedicated table. Per-request telemetry does exist as structured
   `public.logs` rows (`telematics_provider_request`, 30 021 rows, carrying wire timestamps, page
   and sub-window). Found by the `docs/19` audit and independently re-verified. §4.3's
   recommendation shrank from "two new tables' worth of capture" to "one column plus a retained
   projection" as a result.
2. **§1.5 / §3.5 — provider budgets.** The original draft used the code defaults
   (300/endpoint, 500/run). `.env` overrides them to 3 000/5 000 and the overrides are what is
   in force, changing monthly-window headroom from ~1.6× to ~15×.
3. **§3.7 / §17 — lag tail, corrected twice.** Originally sized the residual at 0.029 % from
   `docs/18` §3.3. A first correction re-sized it to "~0.137 %" and declared §3.3's "none
   beyond 13 days" false, citing "16 records provably absent at 14 days" — a figure now
   **withdrawn**. **That correction was itself wrong**: those counts appear in neither `docs/19` nor its companion CSV, and it
   conflated `docs/19`'s directly proven measurement with its inferential one. Independent
   review caught it during M2 release preparation. §3.7 now reports the two distributions
   separately, recomputed from `artifacts/telematics_late_arrival_audit.csv` — directly proven
   maximum 8.61 d with **zero** records beyond 11 or 14 days; inferential maximum 20.31 d with
   109 records ≥ 14 days — and `ops/tests_manual/test_telematics_m2_release_contract.py` asserts
   both against the CSV so neither can drift again.
4. **§2.3 → §2.2 — Dysponent (U2 → S3).** Promoted from unverified to strongly supported after
   reading the enrichment job's selection: date-range scoped with `date_from` derived from the
   previous committed source load, not raw-file scoped as first assumed.
5. **§1.9 — the deployment boundary is no longer the current state.** M1 cut over on
   2026-08-11; §1.9 is retained as the problem statement with a supersession note rather than
   deleted, because it is why M1 exists. §1.4's "currently uncommitted" note was resolved by
   M0. Corrected while starting M2, since M2 must not be planned against a stale picture of
   what production executes.
6. **§3.7c — `L = 8` weekly was justified with daily-grid evidence. Corrected 2026-08-13.**
   The residual figures this plan quoted for `L = 8` (4 records direct, 458 inferential) were
   computed on a *daily* fire grid and then applied to a *weekly* cadence, which overstates that
   cadence's protection by the full phase penalty. On the correct weekly grid, `L = 8` adds
   nothing at all over the deployed daily `L = 3`: identical 2.208 d guaranteed horizon,
   identical 171/1 031 fire-grid replay. The engineering recommendation moves to `L = 16`, and
   §16 decision 1 is reframed from "accept the bracket or pay 2×" to a narrow `L = 15` vs
   `L = 16` choice. Nothing in §3.7a/b changed — the underlying distributions were right; the
   cadence arithmetic applied to them was not.
7. **§13 — "coverage stalled: older than 26 h" was a daily-client threshold written as a
   fleet-wide one. Corrected 2026-08-13.** BRAVO00016 `trips_sync` is weekly, so that threshold
   would have fired against it continuously from the day it shipped. Thresholds are now
   expressed as multiples of each schedule's own cadence.
8. **§4.3 — the proposed FK cannot exist. Corrected 2026-08-13.** §4.3 justified
   `provider_request_log` partly by "needing an FK target" for
   `client_trips.first_seen_request_id`. The two tables are in **different PostgreSQL
   databases**, so no such FK is possible. §4.3a records the resolution: an unenforced UUID
   reference, stated as such rather than implying an integrity guarantee that does not exist.
9. **§4.5 / §14 M00 — a complete milestone with no recorded artifact.** M00 was marked
   `COMPLETE` without naming where the export landed, which is not a closed milestone in any
   useful sense. §4.5a records the verified location, row counts, SHA-256 and — more
   importantly — the residual single-copy exposure that "COMPLETE" was concealing.
10. **Header / §14 / §20 — M3 status advanced from `DEPLOYED_PRODUCTION_UNPROVEN` to `COMPLETE`.
    2026-08-14.** The 2026-08-13 status was correct when written: the release was active but no
    natural `trips_sync` fire had run under it, and this document deliberately refused to call
    that production-complete. Three natural fires on 2026-08-14 supplied the §20.6 evidence, and
    §20.7 records it — including which parts are direct and which are structural, so the upgrade
    does not quietly inflate what M3 proved. The 2026-08-13 wording is struck through rather than
    deleted.
11. **Active release vs repository HEAD — a conflation this document had left available.
    2026-08-14.** Nothing here previously stated that Git HEAD is not the production identity. It
    became load-bearing once documentation commits landed on `main` above the release commit
    (HEAD `47846e4`, release built from `fabaaa7`). A note under the header now fixes the
    distinction and points at `ops/manage_release.py status` as the only authority.

---

## 19. M2 delivery record — DAILY `L = 3`

**Status: `COMPLETE`.** The change is deterministically tested, applied to the production
control plane, and verified on its first natural production run. Production executes release
`672adfb152f8` and its control plane reads `lookback_days = 3` for ALPHA00001 / `trips_sync`.
The closure evidence is §19.12; §19.1–§19.11 are retained as the delivery record they were,
including §19.8's apply path, which was executed rather than proposed.

### 19.1 Where `L` lives, and why M2 is a migration rather than a code change

Traced end to end before implementing:

| layer | artifact | role for DAILY `L` |
|---|---|---|
| scheduler | `log-job@dispatcher.timer` → `dispatcher.py` | fires every 5 min; picks due schedules |
| window | `dispatcher.evaluate_schedule` | **the only production reader of `lookback_days`**: `window_start = fire − timedelta(days=L)` |
| stabilization | `coverage_windows.derive_effective_window` | turns `(F, L, D, O, R, W)` into the requested window (§1.3) |
| job | `sync_trips_and_speeding.run` | **requires** explicit `window_start_ts`/`window_end_ts`; derives no lookback and reads no `lookback_days` |
| provider | `provider_client.trips_wire_window` | serializes the window to Europe/Warsaw wall clock (§1.4) |

`L` therefore has exactly **one** production source: the
`workflow_a_control.client_dataset_schedule` row. This is what §14 meant by "one control-plane
row". There is no code constant to change, and the other `lookback_days` literals in the tree
are provably not on this path:

* `client_dataset_schedule.lookback_days DEFAULT 7` (migration `012`) — applies to newly seeded
  rows only; it never rewrites an existing row;
* `control_plane._default_schedule(lookback_days=1)` — the documented fallback returned when no
  schedule row exists. `sync_trips_and_speeding` reads `enabled`, `overwrite_existing` and
  `schedule_id` from that object and never its lookback.

### 19.2 Configuration precedence

Highest wins:

1. **Explicit `window_start_ts` / `window_end_ts` params** — manual recovery
   (`ops/recover_telematics_trips_window.py`), insert-only backfill and replay. Bypasses the
   lookback layer entirely; M2 does not touch it and cannot widen it.
2. **`client_dataset_schedule.lookback_days`** — the authoritative scheduled DAILY horizon.
   M2 sets it to `3`.
3. **Column `DEFAULT 7`** — new rows only.
4. **`_default_schedule` fallback** — no-row case, not on the scheduled trips path.

### 19.3 Exact window semantics at `L = 3`

Unchanged arithmetic (§1.3), new `L`. For a fire `F` with ALPHA's in-force
`D = 10 800`, `O = 3 600`, `R = 2 678 400`:

```
N_start = F − 3·86400                      nominal, exactly 259 200 absolute seconds
N_end   = F
E_end   = F − 10 800
E_start = F − 3·86400 − 10 800 − 3 600     (R does not clamp: 262 800 s << 2 678 400 s)
```

Worked example, `F = 2026-08-20T02:00:00Z`:

| bound | value |
|---|---|
| `N_start` | `2026-08-17T02:00:00Z` |
| `N_end` | `2026-08-20T02:00:00Z` |
| `E_start` | `2026-08-16T22:00:00Z` |
| `E_end` | `2026-08-19T23:00:00Z` |
| effective span | 3 d + `O` = 3 d 1 h |
| wire numerals (Europe/Warsaw) | `2026-08-17 00:00:00` → `2026-08-20 01:00:00` |

`D` cancels out of the span because it shifts both bounds equally. Consecutive daily fires
overlap by `2 d + O` — deliberately, and the window is never shortened on the basis of a
previous success.

**DST.** The effective window is absolute-duration arithmetic, so its span is exactly
`3·86400 + O` seconds across both Europe/Warsaw transitions. Only the *wire numerals* move,
through the single existing boundary `provider_client._wall_clock_wire_window`, which widens
outwards for an ambiguous endpoint and can therefore only over-fetch. Across the autumn
fallback the Warsaw wall-clock request span reads one hour shorter than the absolute span; that
is the provider's wall-clock addressing, not a lost hour, and M2 introduces no second DST
model. Verified for both 2026 transitions and a DST-quiet control.

### 19.4 Why the wider window stays safe

* **Idempotent.** `client_trips` upserts `ON CONFLICT (client_id, provider_trip_id)`; re-seeing
  a trip is a no-op update (`DO UPDATE`) or a no-op (`DO NOTHING`), never a second row (§1.6).
* **Enrichment-safe.** `Dysponent_ID` is written nowhere in the trips sync job, so re-upsert
  cannot clear it.
* **Within provider limits, `/trips`.** The dispatcher passes no `chunk_days`, so the job
  applies its own `TRIPS_DEFAULT_CHUNK_DAYS = 2`
  (`sync_trips_and_speeding.py:96`, `_trips_chunk_days` line 370). The 3 d + `O` window is
  therefore **2 chunks, not 1** — `TRIPS_MAX_SUB_WINDOW_DAYS = 30` is the provider client's
  ceiling on top of the applied chunking, not the chunk size. Either way this is a negligible
  share of the budgets in force (§1.5); no safeguard was bypassed or relaxed.
* **`/vehicles/events`: ALPHA issues ZERO requests, before and after M2.** Verified read-only
  against the live control plane on 2026-08-12: ALPHA00001 carries
  `event_enrichment_mode = disabled` **and** `trip_metrics_population_source =
  report_207_migration`. Either alone is sufficient — `sync_trips_and_speeding` gates the fetch
  on `if not api_owns_trip_metrics or event_enrichment_disabled: pass` and logs "Vehicle event
  enrichment disabled by event_enrichment_mode; skipping /vehicles/events fetch". So for ALPHA
  under current production configuration the events cost of M2 is:

  | | `/vehicles/events` requests |
  |---|---|
  | DAILY `L = 1` (today) | **0** |
  | DAILY `L = 3` (after M2) | **0** |

  An earlier revision of this section stated ALPHA runs enrichment *enabled* and presented a
  7 → 19 chunk growth as M2's headline operational risk. **That premise was wrong** and was
  caught by independent review (§19.11).

* **`CONDITIONAL / NOT CURRENT PRODUCTION BEHAVIOUR` — the enrichment analysis, retained
  because it is correct for the configuration it describes.** *If* API-owned event enrichment
  were enabled for this client under the current chunking rules
  (`VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS = 4`), the effective span would grow from ~25 h → **7
  chunks** at `L = 1` to ~73 h → **19 chunks** at `L = 3`, before pagination and before the
  per-registration adaptive fallback, which multiplies a failed chunk by fleet size. And in
  that mode the failure semantics are severe: incomplete enrichment calls
  `_raise_incomplete_event_enrichment` **before** the trips business transaction, so a failed
  enrichment run persists **no trips at all** ("DB upsert skipped due to incomplete event
  enrichment"). That is a real property of the code and is deliberately *not* weakened — it is
  simply not on ALPHA's path today.

  Two consequences. It applies today to **FOXTROT00001 and DELTA00001**, which do run
  `event_enrichment_mode = enabled` with `trip_metrics_population_source = api_migration` — so
  it must be re-evaluated before any future milestone widens *their* horizons. And it becomes
  ALPHA's risk the moment its enrichment mode changes, which is why §19.10 gates the first
  `L = 3` run on re-confirming the configuration rather than assuming it.

* **Coverage unchanged.** `E_end = F − D` does not move, so watermark advancement behaves
  exactly as before; a wider `E_start` only ever moves the request start earlier, which
  `min(base, W − O)` already permitted (§1.3).

### 19.5 What M2 deliberately did **not** do

* **The timezone / `run_time` pair (§3.6, §16 decision 3) was not taken.** ALPHA's schedule
  remains `timezone = UTC`, `run_time = 02:00`, so its fire still lands at 04:00 Europe/Warsaw
  during CEST. §3.6 requires the pair to move together or not at all, and only the lookback was
  approved. This remains open for the approver.
* **FOXTROT00001 remains at `L = 1`.** It is also a daily `trips_sync` client, but the
  late-arrival evidence (`docs/19`) is ALPHA-specific and no approved target exists for it.
  Left deliberately, not by oversight.
* **`COVERAGE_CORRECTNESS_DEFERRED_TO_M3`.** Fetching a wider window does not make
  `covered_through_ts` trustworthy. C2 (§2.1) is untouched: coverage still advances on
  `rc == 0` alone, and `execution_outcome` still has no production consumer. M2 makes no claim
  about coverage correctness.
* **No telemetry schema.** M4 owns first-seen/request linkage. The existing
  `telematics_provider_request` log rows already record the intended window, so `L = 3` becomes
  visible in existing evidence with no new structure (§4.1).
* **No weekly or monthly cadence, no second scheduler, no `run_type` discriminator.** M5–M7.
* **No historical recovery.** M2 changes future daily behaviour only.

### 19.6 Artifacts

| path | what |
|---|---|
| `db/migrations/060_workflow_a_daily_trips_lookback_l3.sql` | the control-plane change; guarded, idempotent, fail-closed, single row |
| `ops/tests_manual/test_telematics_daily_lookback_l3.py` | the M2 behavioural suite (see §19.7) |
| `ops/tests_manual/test_telematics_trips_stabilization_windows.py` | referencer allowlist extended for the new suite |
| `ops/tests_manual/test_cutover_transaction.py` | post-M1 repair of a check that encoded the pre-cutover host as an invariant |
| `ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql` | the guarded emergency rollback (§19.9); outside every migration runner's discovery path |
| `ops/tests_manual/test_telematics_m2_release_contract.py` | the release-preparation contract suite (§19.11) |
| `docs/19_telematics_trips_late_arrival_audit.md` | the late-arrival audit, now tracked — committed documents reference it |
| `artifacts/telematics_late_arrival_audit.csv` | its machine-readable companion, 1 031 rows; the evidence of record for §3.7 |

The migration refuses rather than repairs: an absent target, a duplicate target, a non-`daily`
target, or a current value that is neither `1` (pre-M2) nor `3` (post-M2) all raise. Re-applying
after convergence is a `NOTICE`, not a write. It issues exactly one `UPDATE`, against exactly
one table, with no DDL.

### 19.7 Deterministic evidence

`ops/tests_manual/test_telematics_daily_lookback_l3.py` — pure, no network, no database, no
production access. The provider pagination state machine, the safety budgets, the wire-time
boundary, `evaluate_schedule`, `derive_effective_window` and `evaluate_coverage_gate` all run
for real. It proves:

1. `L` has one production source, and the migration moves only it;
2. `E_start == F − 3·86400 − D − O` exactly, and the nominal window is exactly `3·86400 s`;
3. both 2026 Europe/Warsaw DST transitions plus a control — span invariant, widening
   outward-only, wire numerals from the single existing converter, and exactly one module in
   the tree defining that converter;
4. consecutive fires overlap by `2 d + O`, and the persistence identity makes that safe;
5. **late arrival:** a trip absent from run N's provider response and present in run N+1's is
   ingested by run N+1 under `L = 3`, and is provably unreachable under `L = 1`;
6. explicit manual/backfill windows are requested exactly as given, unwidened;
7. a multi-page response is consumed completely, with the advisory `last_page` not driving
   control flow;
8. the four-layer precedence above, including that the row value — not any default — decides
   the window;
9. the §19.4 cost model, behaviourally, by counting real HTTP requests through the provider
   client: `L = 3` is 2 trips chunks at the applied `chunk_days`; under ALPHA's ACTUAL
   configuration (`disabled` + `report_207_migration`) the `/vehicles/events` count is
   **0 → 0** across `L = 1 → L = 3`; under a separate, explicitly `CONDITIONAL` fixture
   (`enabled` + `api_migration`, which ALPHA does not run) it grows 7 → 19; and the fail-loud
   enrichment contract that makes that growth matter is still in force.

The migration was additionally applied against a **disposable** throwaway PostgreSQL 16
container (never the production database): applied cleanly `1 → 3`, was a no-op on re-apply,
left FOXTROT00001 and every non-`lookback_days` column untouched, and refused all four
fail-closed cases.

### 19.8 Production apply path — **performed**, see §19.12

Applying M2 mutates live DAILY ingestion behaviour: the running dispatcher reads
`lookback_days` from the control plane on every fire, so the migration takes effect at the next
tick regardless of which release is current. **Treat it as a production deployment, not as
release preparation.** Each step below is separately authorized.

**Step 0 — pre-apply read-only checks.**

* Confirm the enrichment premise §19.4 rests on still holds. If either value differs, STOP and
  re-evaluate the conditional 19-chunk risk before proceeding:

  ```sql
  BEGIN READ ONLY;
  SELECT ca.client_code, s.frequency, s.lookback_days, s.timezone, s.run_time,
         s.event_enrichment_mode, ca.trip_metrics_population_source
    FROM workflow_a_control.client_dataset_schedule s
    JOIN workflow_a_control.client_account ca USING (client_id)
   WHERE ca.client_code = 'ALPHA00001' AND s.dataset_name = 'trips_sync';
  ROLLBACK;
  ```

  Expected before the apply: `daily`, `lookback_days = 1`, `UTC`, `02:00`, `disabled`,
  `report_207_migration`.

  > **`BEGIN READ ONLY;` … `ROLLBACK;`, never a bare `SET TRANSACTION READ ONLY;`.** Under
  > `psql`'s default autocommit each statement is its own transaction, so a standalone
  > `SET TRANSACTION READ ONLY` applies to the transaction that ends on the same line and
  > protects **nothing** that follows — it is not a session-level guard. Only an explicit
  > `BEGIN READ ONLY` keeps every SELECT inside one genuinely read-only transaction, and
  > `ROLLBACK` (or `COMMIT`, which is equivalent here) closes it. This correction was made
  > during the M2 deployment, where the misleading form was first noticed.

* Confirm the watchdog has already observed this subject, because the migration bumps
  `updated_at` and that value is the eligibility epoch **only** for a never-observed subject
  (`ops/execution_watchdog.py` ~709). Verified present on 2026-08-12; re-check before applying:

  ```sql
  SELECT 1 FROM ops_control.watchdog_observation
   WHERE subject_key = 'workflow_a:ALPHA00001:trips_sync';
  ```

* Confirm no reviewed Telematics bootstrap or C11 recovery is mid-flight. `lookback_days` is in
  `BOUND_SCHEDULE_PARAMETERS`, so an inventory captured before the change correctly refuses
  after it — fail-closed, but do not strand an in-progress operation.

**Step 1 — prepare and verify the immutable release FIRST, before any data mutation.**

The current release `342c0b8e50ad` predates M2 and its `db/migrations/` stops at `059`. The
migration must be applied from a *verified, immutable* tree, not from the mutable development
checkout, so that what ran against production is exactly what was reviewed:

```bash
ops/manage_release.py prepare --commit <M2 commit sha> \
  --env-file /opt/log-platform/.env \
  --venv     /opt/log-platform/.venv
ops/manage_release.py verify --release <release_id>
```

Preparation and verification activate nothing (§5).

**Step 2 — apply migration 060 with the runner pinned to that release.**

```bash
/opt/log-platform-release/releases/<release_id>/ops/db_migrate.sh
```

`ops/db_migrate.sh` resolves `db/migrations` relative to its **own** location, so invoking the
release copy applies the release's reviewed SQL. Invoking the development copy applies whatever
is in the working tree at that instant — which is precisely the property the release boundary
exists to remove.

> **Direct `psql < 060_….sql` is UNSUPPORTED for the normal deployment path.** It applies the
> SQL without writing the `public.schema_migrations` ledger row, producing the
> "applied-but-unrecorded" state below.

**Step 3 — do not declare the migration complete until BOTH are verified.**

```sql
BEGIN READ ONLY;
SELECT s.lookback_days FROM workflow_a_control.client_dataset_schedule s
  JOIN workflow_a_control.client_account ca USING (client_id)
 WHERE ca.client_code = 'ALPHA00001' AND s.dataset_name = 'trips_sync';        -- must be 3

SELECT count(*) FROM public.schema_migrations
 WHERE filename = '060_workflow_a_daily_trips_lookback_l3.sql';               -- must be 1
ROLLBACK;
```

Both SELECTs sit inside the one explicit read-only transaction, for the reason given in step 0.

Either one alone is insufficient. The value without the ledger row is the unrecorded state; the
ledger row is what makes the change durable across later migration runs.

**If the value reads 3 but the ledger row is missing** — because the apply was done by hand, or
the process died between the apply and the ledger insert (they are two separate `psql`
invocations in `ops/db_migrate.sh`):

* **DO NOT** manually set `lookback_days` back to `1` in order to "let the migration run
  properly". That is not required and it briefly reverts production behaviour for no benefit.
* **DO** simply re-run the same pinned migration runner. Migration `060` is idempotent: it sees
  `lookback_days = 3`, emits `M2 already converged`, writes nothing, and exits 0 — after which
  `ops/db_migrate.sh` records the ledger row. Convergence is exactly what the no-op branch is
  for.

**Step 4 — observe the first `L = 3` run** against the gate in §19.10.

**Step 5 — optionally activate the prepared release.** M2's behaviour does not require it (the
migration is control-plane data), but activating keeps the running code and the applied schema
in agreement, which is the property the boundary exists to provide.

**Step 6 — update the documentation that records live state.** Done: §1.2's schedule table now
reads `L = 3`, the release-boundary records in `docs/07_operations.md` and
`docs/02_infrastructure.md` name release `672adfb152f8`, and migration `060` is recorded in
`docs/05_jobs.md` as `CONVENTIONS.md` §12 requires. The dated `2026-08-03` / `2026-08-04`
rollout records in `docs/07_operations.md` (§ `Produkcyjny rollout ALPHA00001`) still say
`lookback_days = 1` and are **deliberately left unchanged** — they are historical evidence of
what was true on those dates, not statements of live state.

---

### 19.9 Rollback — one contract, deliberately not a migration

**Artifact:** `ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql`.

**Why it is not `061_*.sql`.** `ops/db_migrate.sh` discovers migrations with
`find db/migrations -maxdepth 1 -type f -name '*.sql' | sort`, so anything placed there is
applied **automatically** on the next run. A rollback shipped as a sequential forward migration
would therefore not be dormant: it would execute on the next ordinary migration run and revert
a healthy `L = 3` deployment, and M2 could then only be re-applied via a `062`. An earlier
revision of this plan recommended exactly that; it was wrong and is withdrawn (§19.11).
`db/client_business/` is no better — `scripts/onboard_workflow_a_client.py` applies that
directory from its own list. `ops/sql/` is read by no runner in this repository. It executes
only when an operator names it.

**Guards, all fail-closed, all inside one transaction:**

1. migration `060` must be recorded in `public.schema_migrations` — otherwise the rollback
   would itself be undone by the next migration run, so it refuses and tells you to repair the
   ledger first;
2. exactly one `ALPHA00001` / `trips_sync` row must match (zero or several → refuse);
3. the schedule must still be `daily`;
4. `lookback_days` must currently be `3` (already `1` → `NOTICE`, no write; anything else →
   refuse rather than "repair");
5. one guarded `UPDATE`, `3 → 1`, with the value re-checked in the `WHERE` clause so a
   concurrent change loses the race;
6. exactly one affected row, or the transaction aborts;
7. **the migration ledger is never written** — no `INSERT`, `UPDATE` or `DELETE` touches
   `public.schema_migrations`;
8. read-back inside the same transaction: value is `1` **and** `060` is still ledgered, or
   nothing commits.

**Consequence, which is the whole point of guard 7:** because `060` stays ledgered, the next
ordinary `ops/db_migrate.sh` run **SKIPS** it, and `L = 1` persists. Deleting the ledger row
would turn this into a rollback the next deployment silently undoes.

Not executed against production. `ops/tests_manual/test_telematics_m2_release_contract.py`
asserts the artifact's location and guards statically **and executes the real file** against a
disposable PostgreSQL 16 database when `M2_ROLLBACK_TEST_DSN` names one: every branch above,
the ledger comparison before and after each, the subsequent migration-runner pass that must
SKIP `060`, and mutants proving each of those assertions is load-bearing. The suite refuses any
target it cannot prove disposable — see §19.11.

---

### 19.10 First production `L = 3` run — verification gate

> **Gate result: PASSED** on the first natural run, `2026-08-13 02:00:00Z`. The evidence is
> §19.12. The gate below is retained as the contract it is — it is what any future widening of
> a DAILY horizon must be measured against.

**Configuration**, re-confirmed immediately before the run: `event_enrichment_mode = disabled`
and `trip_metrics_population_source = report_207_migration`. If either differs, STOP — the
conditional 19-chunk enrichment risk in §19.4 applies and must be re-evaluated first.

**Window.** The dispatcher claims a compatibility fire with the **effective** window and records
the nominal bounds separately, so `client_schedule_run_history` carries two different things and
the gate must name which it is checking:

| field | steady-state expectation at `L = 3`, `D = 3 h`, `O = 1 h` |
|---|---|
| `nominal_window_start_ts` | `F − 72 h` |
| `nominal_window_end_ts` | `F` |
| `window_start_ts` (**effective**) | `F − 76 h` |
| `window_end_ts` (**effective**) | `F − 3 h` |
| effective span | `73 h` |

> **Do not gate on `window_start_ts == F − 72 h.`** An earlier revision of this section did, and
> it would have failed a perfectly correct `L = 3` run and triggered a false rollback — the
> effective start is four hours earlier than the nominal one because `E_start = N_start − D − O`
> (§1.3). Caught by independent review (§19.11).

`F − 76 h` is a **steady-state expectation, not an invariant.** `E_start = max(min(F − L·86400
− D − O, W − O), E_end − R)`, so a watermark left behind by a gap legitimately drags the
effective start earlier, and the `R` clamp could in principle bound it. A run whose effective
start is *earlier* than `F − 76 h` is catching up, not misbehaving. What must hold exactly is
`nominal_window_start_ts == F − 72 h`; that is the direct expression of `L = 3`.

**Trips requests.** Verify complete pagination and the expected chunking. The dispatcher passes
no `chunk_days`, so the job applies `TRIPS_DEFAULT_CHUNK_DAYS = 2` and a 73 h effective span
becomes 2 chunks — confirm against the run's actual `telematics_provider_request` rows rather than
assuming it.

**Events requests.** Expected count: **0**, per §19.4. A non-zero count means the configuration
changed under you; stop and re-evaluate.

**Persistence.** Verify parsed trips actually reached a committed upsert — not merely that the
process exited 0.

**Run status.** Require genuine success from both the platform run record and the schedule
history / dispatcher outcome. Process exit code is not the success contract; where authoritative
run evidence exposes a stricter one, that is what applies.

**Coverage.** `COVERAGE_CORRECTNESS_DEFERRED_TO_M3`. Do not read a successful wide run as
evidence that `covered_through_ts` is trustworthy. C2 remains unwired.

**Abort condition.** `nominal_window_start_ts` not exactly 3 days before the fire, or the run
FAILED → roll back per §19.9.

---

### 19.11 Release-preparation review — findings and corrections

An independent cross-model review of the M2 commit approved the implementation
(`M2_CODE_APPROVED`) and the event-enrichment release gate, but required changes to the
release-preparation material. All were verified against the code, the live control plane
(read-only) and the retained evidence before being accepted; all were correct.

| # | finding | correction |
|---|---|---|
| R1 | the apply path did not require a verified immutable release, and left "applied-but-unrecorded" recoverable only by manually reverting the value | §19.8 rewritten: release prepared and verified first, runner pinned to the release, both value **and** ledger verified, re-run the pinned runner rather than reverting, direct `psql` documented unsupported |
| R2 | rollback was described as "land a revert migration `061`" — which `ops/db_migrate.sh` would auto-apply, so not a dormant mechanism at all | §19.9 + `ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql`: guarded, transactional, outside every runner's discovery path, ledger deliberately untouched |
| R3 | the first-run gate checked `window_start_ts == F − 72 h`, conflating the effective window with the nominal one; a correct `L = 3` run would have failed it | §19.10 tabulates both, gates on `nominal_window_start_ts`, and states why `F − 76 h` is an expectation rather than an invariant |
| R4 | committed documents referenced the untracked `docs/19`, and propagated a "16 records provably absent at 14 days" figure that appears in no retained evidence and is hereby **withdrawn** | `docs/19` and `artifacts/telematics_late_arrival_audit.csv` committed; §3.7 rewritten as two separately labelled distributions recomputed from the CSV; §16, §17, §18 and `docs/18` §3.3 corrected |
| R5 | §19.4 asserted ALPHA runs `event_enrichment_mode = enabled` and headlined a 7 → 19 chunk growth | verified read-only: ALPHA is `disabled` + `report_207_migration`, so events are **0 → 0**. The 7 → 19 analysis is retained, labelled `CONDITIONAL / NOT CURRENT PRODUCTION BEHAVIOUR`, and noted as live today for FOXTROT00001 and DELTA00001 |

Deterministic coverage for these corrections lives in
`ops/tests_manual/test_telematics_m2_release_contract.py` — no `061` migration exists, the
rollback is outside every discovery path and never writes the ledger, nominal and effective
starts differ by `D + O`, ALPHA's events path is gated off, the evidence figures match the CSV,
and no committed document points at an untracked file.

The rollback artifact was exercised against a **disposable** throwaway PostgreSQL 16 container
(never the production database): valid `3 → 1` success with the ledger row intact and `060`
still recorded; refusal when `060` is unledgered; `NOTICE` no-op when already at `1`; refusal on
a missing row, a duplicate row, a wrong client/dataset and a non-`daily` schedule; and the
`public.schema_migrations` contents byte-identical before and after every case.

That exercise is no longer a session record — it is **committed regression coverage**. Section 6
of `ops/tests_manual/test_telematics_m2_release_contract.py` reruns the whole matrix on demand:

    M2_ROLLBACK_TEST_DSN=postgresql://user:pass@127.0.0.1:55432/disposable \
    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_telematics_m2_release_contract.py

It adds the property the manual exercise could only assert by inspection — after a valid
`3 → 1`, a faithful port of `ops/db_migrate.sh`'s discovery loop runs against the same database,
**SKIPS** `060` because it is still ledgered, and leaves `L = 1` standing; delete that ledger
row and the same loop re-applies `060` and raises `L` back to `3`. Four mutants (each ledger
guard, the value guard, the `L = 3` UPDATE predicate, and a ledger-deleting rollback) prove the
assertions are not vacuous.

Because that argument is only as good as the port, §5b of the same suite pins the runner's
**control flow**, not just its SQL strings: the loop is compared byte-for-byte against the
reviewed block and then analysed positionally — ledger query before the branch, the branch
testing `== "1"`, the skip path announcing `SKIP` and `continue`-ing, no migration applied or
ledgered on that path, and the apply strictly before the ledger INSERT under `set -euo
pipefail`. The pin is itself mutation-tested against a removed `continue`, an inverted
condition, a ledger-before-apply swap, an apply moved onto the ledgered path, and a dropped
`set -e` — each of which the previous fragment-only assertions survived. The port's own
apply-before-ledger ordering is then proven behaviourally: a migration that raises leaves no
ledger row.

`set -euo pipefail` being **present** is not the property that matters; errexit being **active
when the migration runs** is. A `set +e` inserted after the preamble keeps the fragment, the
loop text and every structural invariant above intact, and still lets a failed migration reach
the ledger INSERT. So the reviewed **active command sequence** — every command from the shebang
through the loop's `done`, blank lines and comments excluded — is pinned too, and read
separately for error state: errexit armed as the first command, no `set +e` / `set +o errexit`
anywhere between that and the ledger write, and the apply's status not consumed by `||`, `&&`,
`!` or a condition. Both layers reject `set +e` after the preamble, `set +o errexit` before the
loop, `set +e` inside the loop, a dropped `-e`, and `|| true` on the apply.

The suite is destructive, so it establishes disposability before it writes anything: loopback-only
DSN, then a refusal if the connection resolves to the production endpoint declared in `.env`, if
the connected database carries the production database name, if it attests the production platform
identity, or if it already holds Workflow A control-plane rows. All four refusals exit non-zero
without touching the target; production `logdb` is refused by the first of them.

At the time this review closed, M2 was still `IMPLEMENTED_NOT_DEPLOYED`. It was deployed and
verified afterwards — §19.12.

---

### 19.12 M2 closure — production apply and first natural `L = 3` run

**`M2 COMPLETE`.** Deployed, and verified on the first natural production run at `L = 3`.
Rollback was **not indicated** and was **not executed**;
`ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql` stays dormant.

The deployment validation ran a ten-gate set, `G1`–`G10`. **`G1`–`G9` passed** on the evidence
below — configuration, window, chunking, pagination, provider cost, persistence, run status in
both authoritative stores, release identity, and the rollback assessment. **`G10` is the
coverage gate and is bounded by design:** it confirms only that M2 claimed nothing about
coverage. Full provider/local historical coverage correctness is **not** established by this
run and remains M3's — `COVERAGE_CORRECTNESS_DEFERRED_TO_M3`.

| fact | value |
|---|---|
| active release | `672adfb152f8` (`672adfb152f8950927d7edaab2a0edcbfc60e055`), `previous` `342c0b8e50ad` |
| migration | `060_workflow_a_daily_trips_lookback_l3.sql` applied; `public.schema_migrations` — **1 row** |
| production configuration | ALPHA00001 `trips_sync` (`schedule_id = eb099f69-4876-4c7e-8f60-a2bad0c35b5b`): `daily`, `02:00 UTC`, **`lookback_days = 3`**, `overwrite_existing = true`, `event_enrichment_mode = disabled`, `trip_metrics_population_source = report_207_migration` |
| first natural `L = 3` fire | `2026-08-13 02:00:00+00` = `04:00 Europe/Warsaw`; executed `04:00:00.775+02` → `04:03:39+02` |
| run identity | `client_schedule_run_history` `ffeb1f65-215d-4319-826b-6b5132032eea`; platform run `97c392b5-4ac3-4ea4-807f-a3c6d2fb4ca9` |
| run status | **`SUCCESS` in both authoritative stores** — `client_schedule_run_history` and `public.runs`; executed from release `672adfb152f8` |

**What the run proved**, against §19.10:

* **Window.** Nominal `2026-08-10T02:00:00Z` → `2026-08-13T02:00:00Z` — exactly `3·86400 s`
  before the fire, which is the direct expression of `L = 3`. Effective
  `2026-08-09T22:00:00Z` → `2026-08-12T23:00:00Z`, span **73 h**, i.e. the steady-state
  `F − 76 h` → `F − 3 h`. No catch-up widening: the watermark did not drag `E_start` earlier.
* **Chunking.** Configured chunk size 2 days / 48 h; 2 chunks derived and **2 observed** — the
  §19.4 cost model, confirmed against the run's own request rows rather than assumed.
* **Pagination.** Sub-window 1: 13 pages, 12 152 rows; sub-window 2: 7 pages, 6 379 rows. Both
  terminated on `short_page` with `total_reconciliation = exact`. No truncation and no provider
  safety refusal. One transient `ReadTimeout` on a first attempt was retried successfully.
* **Provider.** `/vehicles/events` requests: **0**, as §19.4 requires for ALPHA's configuration.
  No quota or safety event.
* **Persistence.** `overwrite_existing = true`; prepared **18 531**, upserted **18 531** —
  equality holds, so parsed trips reached a committed upsert. Distinct persisted trip
  identities carrying the run id: **18 527**. The 4-row delta is duplicate `provider_trip_id`
  values inside the fetched batch, absorbed by the existing `(client_id, provider_trip_id)`
  conflict identity exactly as §19.4 says it must be. A diagnostic observation about provider
  data, **not** an M2 failure.

**What this does not prove — `COVERAGE_CORRECTNESS_DEFERRED_TO_M3`.** A successful wide run is
evidence about ingestion mechanics and safety behaviour, and about nothing else. C2 is still
unwired: `covered_through_ts` still advances on `rc == 0` alone and `execution_outcome` still
has no production consumer. M3 remains responsible for proving the intended provider/local
historical coverage properties, and M3 is **not** complete.

**Also unchanged by M2:** ALPHA's `timezone`/`run_time` pair (§3.6, §16 decision 3) is still
`UTC` / `02:00` and remains open for the approver; FOXTROT00001 is still at `L = 1`; no historical
recovery was performed.

---

## 20. M3 delivery record — dispatcher coverage-advance outcome gate

~~**Status: `IMPLEMENTED_LOCALLY_NOT_DEPLOYED` (2026-08-13).**~~ **Superseded the same day.**

~~**Status: `DEPLOYED_PRODUCTION_UNPROVEN` (2026-08-13).**~~ **Superseded 2026-08-14 by the first
natural post-activation fires.** Release `fabaaa753f89` (commit
`fabaaa753f89fc07eee7c89243ecbfe28734513d`) was activated at **2026-08-13T10:59:21Z**, replacing
`672adfb152f8`. Activation succeeded, the immediate release smoke passed, and natural dispatcher
ticks resolved `release=fabaaa753f89` — but for the rest of that day no natural `trips_sync` fire
had occurred, so the outcome gate had not yet run against a real business execution.

**Status: `COMPLETE` (2026-08-14).** Three natural compatibility `trips_sync` fires ran under
`fabaaa753f89` and satisfied every item of §20.6. The closure record, with identities and
evidence classification, is **§20.7**.

The subsections below (§20.1–§20.6) are preserved as written on 2026-08-13, before that evidence
existed. They are the delivery and pre-closure record; §20.7 is the outcome.

### 20.1 What M3 changes

`rc == 0` is demoted from sufficient to necessary. A compatibility `trips_sync` fire may reach
`_finalize_compat_success` only after `_require_coverage_eligible_outcome` has accepted the
child's terminal record — §6 conditions 2, 3 and 4:

* the child is given a fresh per-launch outcome destination inside `_launch_job`'s own temporary
  directory, so the record is created per launch, cannot be a leftover, and is gone when the
  launch returns. An inherited `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE` is stripped *before* the
  opt-in, so a stale path can never reach a child on any branch;
* the record must be present and strictly parse (`read_outcome`);
* it must verify against this exact claim (`verify_outcome`) — client, schedule, dataset,
  requested window, **exact platform-run identity**, and `recovery_run_id` absent, which is what
  refuses a manual-recovery record replayed against a scheduled claim;
* it must be `is_coverage_eligible(...)`.

Any refusal finalizes the fire `FAILED` with a deterministic `error_summary`, logs `ERROR`,
leaves the watermark untouched and performs **no coverage SQL at all** — the decision is taken
before the finalizer opens its transaction.

**Unchanged:** §6 condition 1; §6 condition 5 (the CAS and strict-monotonicity contract in
`coverage_finalization.advance_covered_through_cas`) — not one line, and its Postgres suites pass
unmodified; `rc != 0` semantics; window derivation; `L = 3`; schedule timing; persistence
identity. **§6 condition 6 (`subwindow_complete`) was out of M3's scope and deliberately left
unimplemented by it** — it landed in M4 (§22, closure §22.14).

### 20.2 Files

| file | change |
|---|---|
| `jobs/api/telematics/dispatcher.py` | `ScheduledExecutionOutcome`, `ScheduledOutcomeRefused`, `_read_execution_outcome`, `_require_coverage_eligible_outcome`; `_launch_job` gains `collect_execution_outcome` and returns a 5-tuple; the `rc == 0` compatibility branch is gated; two stale `strict_meta` comments corrected |
| `jobs/api/telematics/execution_outcome.py` | `verify_outcome(recovery_run_id=...)` widened to `Optional[str]` and asserted absent for a scheduled fire; `ExecutionOutcomeError.message` retained; stale docstring corrected |
| `jobs/api/telematics/sync_trips_and_speeding.py` | docstring only — the record is no longer recovery-exclusive |
| `ops/recover_telematics_trips_window.py` | `evaluate_execution_evidence` refuses an absent `recovery_run_id`, so the recovery gate cannot inherit the scheduled-fire reading |
| `ops/tests_manual/test_telematics_dispatcher_execution_outcome_gate.py` | new — 14 properties, the §15 M3 coverage test set |
| four existing suites | updated to the new `_launch_job` arity and allowlist |

### 20.3 Verification — deterministic, no provider call, no production write

The §15 M3 coverage test set, all passing: eligible committed advances; **valid zero-row
(`EXECUTED_ZERO_ROWS_COMMITTED`) advances**; `SKIPPED_DISABLED_SCHEDULE` refuses; absent record
refuses; platform-run identity mismatch refuses; absent platform-run identity refuses; recovery
record on a scheduled claim refuses; window mismatch refuses; `rc != 0` refuses without reaching
the gate; record never requested refuses; a gate defect still finalizes the fire; a refusal names
its client; a long reason keeps its leading code; an empty `client_code` is not a positive
assertion.

Regression, against a disposable PostgreSQL 16 on loopback: C6 finalization, C6
concurrency/reconciliation, coverage state schema, bootstrap writer, per-client bootstrap,
schedule activation, C11 recovery, recovery execution path, dispatcher silent no-op, Workflow A
dispatcher, stabilization windows, M2 `L = 3`, M2 release contract, cold-start chain, release
boundary — all pass.

**Was red, pre-existing and not caused by M3 — since repaired in a separate change:**
`test_telematics_coverage_bootstrap_gate.py` failed 2 checks ("allowed compat fire claims the
effective window", "the job receives the effective window exactly once") **identically with the
M3 change stashed at `672adfb152f8`**. Its fixture hard-coded `covered_through_ts = 2026-08-01`
while the fire is `now`-derived, so `W − O` had drifted below `base` and `min(base, W − O)`
selected the coverage-expansion branch — the assertions' `expected_start` describes the base
branch. A stale date-dependent *fixture*, not a production defect and not a wrong formula: the
expectations were correct for the case they name. The fixture now seeds `W` relative to the fire
(the effective end a daily-cadence predecessor run would leave), and a precondition check states
which branch of the clamp the block intends to exercise. Both assertions pass **unchanged**; the
clamp's own four branches remain owned by `test_telematics_trips_stabilization_windows.py`. The
suite is green.

### 20.4 Independent review

One `operations-reviewer` pass over the final diff returned `CHANGES_REQUIRED`. Confirmed and
fixed: an empty `client_code` (`ScheduleRow` carries `COALESCE(..., '')`, the record carries
`None`) became a positive identity assertion no child could satisfy — a permanent refusal loop
escalating to `GAP_DETECTED` after `R`; read-only production inspection confirmed **no client has
a NULL `client_code` today**, so the trap was latent, not live. Also fixed: the refusal now names
its client and schedule in the exception the terminal-failure incident is built from; a gate
defect can no longer escape and leave the row `RUNNING` (which would no-op every later tick for
every client until the 12-hour stale sweep); the refusal reason is bounded so truncation cannot
amputate its leading code. Accepted without code change: M3 enlarges the class of conditions
costing a day's watermark progress from "the job failed" to "the job failed *or* its evidence did
not survive" — bounded and self-healing via `min(base, W − O)` up to `R`, but it needs a runbook
watch until §13's coverage-stall alert lands in M11.

**Independent Codex review, 2026-08-13: `M3_CODEX_REVIEW_APPROVED`, `NO_BLOCKING_FINDINGS`.** A
cross-model pass over the final M3 diff followed the `operations-reviewer` pass above and the
fixes it required, and verified the invariant this milestone exists to establish —
`coverage advancement => rc == 0 AND verified eligible execution outcome`. That included the
fail-closed refusal behaviour, where an absent, unreadable, unverifiable or ineligible record
finalizes the fire `FAILED` and performs no coverage SQL at all, and the recovery-identity
semantics on both sides of the widening: `verify_outcome` asserts `recovery_run_id` **absent**
for a scheduled claim, and `evaluate_execution_evidence` refuses an absent one so the recovery
gate cannot inherit the scheduled-fire reading. It raised no additional findings.

The only change made after that review was the `test_telematics_coverage_bootstrap_gate.py`
fixture repair recorded in §20.3. It is test-only and **changed no production behaviour**. The
nine reviewed M3 code and test files — `jobs/api/telematics/dispatcher.py`,
`jobs/api/telematics/execution_outcome.py`, `jobs/api/telematics/sync_trips_and_speeding.py`,
`ops/recover_telematics_trips_window.py`, and the five `ops/tests_manual/` files other than the
bootstrap gate itself — remained **byte-identical** through that repair, so the approved diff
and the promoted diff are the same diff.

### 20.5 What M3 still does not prove

`provider_execution_entered` is set before the first request, so an eligible record proves
"entered the provider and committed", **not** "fetched the whole window". Provider safety stops
do re-raise, so the common truncation mode fails the run — but pagination completeness as an
admissible coverage precondition is §6 condition 6 and remains M4.

### 20.6 Production evidence required to close M3

Recorded 2026-08-13, when the activation had happened but no business run had. **Until every
item below is observed, M3 stays `DEPLOYED_PRODUCTION_UNPROVEN` and no later milestone may
begin.** Deterministic tests and an immediate release smoke prove the gate is wired; they cannot
prove it accepts a healthy production run, and a gate that refuses everything passes every test
M3 has.

Observe over the first natural post-activation compatibility `trips_sync` fires — ideally all
three of DELTA00001, ALPHA00001 and FOXTROT00001, since they differ in lookback, timezone and volume:

1. at least one natural post-activation compatibility `trips_sync` run exists;
2. the child wrote a terminal record at the fresh per-launch path, and it parsed;
3. the record **verified** — client, schedule, dataset, requested window, exact
   `platform_run_id`, `recovery_run_id` absent;
4. the record was coverage-eligible and history finalized `SUCCESS` with an empty
   `error_summary`;
5. `covered_through_ts` advanced to exactly the expected `E_end` under CAS;
6. **no false success** — nothing advanced on a run that should have been refused;
7. **no refusal of a healthy run** — in particular no `client_code` identity refusal (the latent
   trap fixed after the §20.4 review) and no window-mismatch refusal;
8. no history row left `RUNNING`, which would no-op every later tick for every client until the
   12-hour stale sweep;
9. no fetch or pagination regression — request, page and row counts in the shape §1.5a records;
10. no `/vehicles/events` regression — ALPHA still issues **0** events requests (§19.4).

**Branching, decided in advance so the result is not argued about after the fact:** if the above
is green, the next engineering task is **M4 implementation** (§21). If it exposes a defect, the
next task is **M3 remediation, not M4** — do not layer an unvalidated milestone onto an
unvalidated one. A pre-activation run on `672adfb152f8` is **not** admissible as a substitute for
any item above.

### 20.7 M3 closure — the first natural post-activation fires

**`M3_PRODUCTION_COMPLETE`, 2026-08-14.** Every §20.6 item is satisfied. Read-only production
verification only: no database write, no provider call, no release, schedule, coverage, systemd
or job mutation was performed to obtain this evidence.

**Runtime identity at fire time.** `ops/manage_release.py status` reported `current =
fabaaa753f89`, `previous = 672adfb152f8`, wrapper variant `release`,
`production_executes_release_root: true`; `verify --release fabaaa753f89` recomputed
`verified: true` against 654 files and digest
`sha256:b023907f0386d93260b564e1d2e1975063d390c247ac5b53ad56b201eac6b204`. `activations.log` shows
no activation after 2026-08-13T10:59:21Z. Every dispatcher tick spanning the three fires logged
`log-job-runner release=fabaaa753f89 base_dir=…/releases/fabaaa753f89`, so each fire demonstrably
executed the M3 release rather than the working tree.

**The three fires.** All `trips_pagination_mode = data_invariants_v1`, all
`trigger = SCHEDULED`, all `source = jobs.api.telematics.sync_trips_and_speeding`. Chosen by the
schedule, not selected after the fact — they are simply the fires that occurred, and they differ
in lookback, timezone, enrichment mode and volume as §20.6 wanted:

| client | `L` | tz | scheduled fire | `run_history_id` | `platform_run_id` | outcome | upserted | `W` before → after | history ↔ run |
|---|---|---|---|---|---|---|---|---|---|
| DELTA00001 | 7 | Europe/Warsaw | `2026-08-14 00:00:00Z` | `fc05ebf7-2d41-45c7-8ff6-4605fbf0de4a` | `01c66710-0af8-4053-837e-322a1773e356` | `EXECUTED_COMMITTED` | 2 893 | `2026-08-12 21:00Z → 2026-08-13 21:00Z` | `SUCCESS ↔ SUCCESS` |
| ALPHA00001 | 3 | UTC | `2026-08-14 02:00:00Z` | `6776097b-5efb-44a0-8f9b-1b3911c4d8a8` | `dcb02141-a562-48e5-8805-0ce569dff0f8` | `EXECUTED_COMMITTED` | 19 065 | `2026-08-12 23:00Z → 2026-08-13 23:00Z` | `SUCCESS ↔ SUCCESS` |
| FOXTROT00001 | 1 | UTC | `2026-08-14 02:00:00Z` | `faaabf11-8d33-4fde-8d99-556459af2244` | `9f94b142-7fe5-48c9-85ab-f51565d3449f` | `EXECUTED_COMMITTED` | 2 409 | `2026-08-12 23:00Z → 2026-08-13 23:00Z` | `SUCCESS ↔ SUCCESS` |

FOXTROT was dispatched on the 04:10 CEST tick rather than 04:00, because the dispatcher handles one
due dataset per tick and ALPHA held that tick. Expected behaviour, not a delay.

For each fire `covered_through_ts` equals its run's `window_end_ts` **to the second**, and
coverage `updated_at` equals its run's `finished_at` **to the second**. `covered_through_source`
stayed `scheduled_run`, `bootstrap_status` stayed `READY`. Each watermark moved exactly +24 h.

**Where the evidence lives.** The dispatcher writes the accepted outcome into the tail context of
its `Job finished SUCCESS (rc=0)` log line — `public.logs`, `source =
jobs.api.telematics.dispatcher`, rows `487205`, `487318`, `487785`. Each context carries
`execution_outcome`, `execution_outcome_upserted_count`, `coverage_advanced: true`, and the full
dispatched identity (`client_id`, `client_code`, `schedule_id`, `dataset_name`, `run_history_id`,
`platform_run_id`, `window_start_ts`, `window_end_ts`, `scheduled_fire_ts`), each matching the
authoritative history and `public.runs` rows.

**Evidence classification — stated precisely, because these are not all the same strength:**

| §20.6 item | classification |
|---|---|
| subprocess succeeded | **DIRECT** — `rc: 0`; `public.runs.status = SUCCESS` |
| history finalized `SUCCESS`, empty `error_summary` | **DIRECT** |
| a terminal record was collected | **DIRECT** — `execution_outcome` and `execution_outcome_upserted_count` are set on the tail context *only* on the post-gate branch |
| the record **verified** against this claim | **STRUCTURAL** — `verify_outcome` raises `ScheduledOutcomeRefused` on any mismatch, so the success log line is unreachable unless it passed, and zero refusals were observed. The compared field values are **not** themselves durable; that is M4 |
| the record was coverage-eligible | **DIRECT** for the value (`EXECUTED_COMMITTED` ∈ `COVERAGE_ELIGIBLE_OUTCOMES`); **STRUCTURAL** for the remaining conjuncts of `is_coverage_eligible` (`provider_execution_entered`, `business_transaction_entered`, `transaction_status = COMMITTED`, `¬skipped`), which are enforced before the line is written |
| finalization was reached and `W` moved | **DIRECT** — `coverage_advanced: true` is the return of `_finalize_compat_success`, corroborated by the coverage table itself |

**Not observable today, and not claimed:** the raw terminal-record payload, the per-field identity
comparison values, `provider_request_log`, `subwindow_complete`. Those are M4 (§21). The record is
written to a per-launch temporary path and is gone when the launch returns, by design (§20.1) —
M3 never promised to persist it.

**Negative checks, all clean.** A sweep of `public.logs` since activation across `message`,
`error` and `context` for `TRIPS_OUTCOME_NOT_COLLECTED`, `TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE`,
`TRIPS_OUTCOME_GATE_FAILED`, `EXECUTION_OUTCOME_ABSENT`, `EXECUTION_OUTCOME_UNREADABLE`,
`EXECUTION_OUTCOME_IDENTITY_MISMATCH`, `outcome_refusal_code`, any `EXECUTION_OUTCOME_*` and any
`MISMATCH` returned **0 rows** — so §20.6 item 7 holds and in particular the `client_code`
identity trap fixed after the §20.4 review did not fire. There are **0** `ERROR`/`CRITICAL` rows
platform-wide since activation, **0** history rows in `RUNNING` (item 8), and all 10 platform runs
since activation are `SUCCESS`.

Three inverse queries were run for §20.6 item 6, each returning **0 rows**: coverage rows updated
since activation with no SUCCESS `trips_sync` row matching on `(client_code, dataset_name,
finished_at = updated_at, window_end_ts = covered_through_ts)`; post-activation SUCCESS
`trips_sync` runs where `covered_through_ts ≠ window_end_ts`; and history rows left `RUNNING`. So
every advancement since activation is bound one-to-one to a successful executed run, and no
successful run failed to advance. No false-success path exists in the observed set.

**Fetch and pagination (§20.6 item 9) — no regression.** All 7 subwindows across the three runs
terminated `short_page` with `total_reconciliation = exact` and `total_present = true`, and
`accumulated_count == accumulated_unique_rows == advisory_total` throughout:

| client | subwindows | pages | rows |
|---|---|---|---|
| DELTA00001 | 4 | 1 / 1 / 2 / 1 | 682 + 655 + 1 004 + 552 = **2 893** |
| ALPHA00001 | 2 | 13 / 7 | 12 544 + 6 521 = **19 065** |
| FOXTROT00001 | 1 | 3 | **2 409** |

Rows are conserved end to end: subwindow sum = `Fetched trips: N` = `execution_outcome_upserted_count`
for each client. No safety limit was approached — peak 12 544 rows against a 500 000 cap, 229 s
against `compat_max_elapsed_s = 900`, 15.9 MB against 33.5 MB. No pagination safety incident. ALPHA
against the §19.12 `L = 3` reference: 2 chunks / 13 + 7 pages then and now, 18 531 → 19 065 rows —
the same structural shape under ordinary daily volume drift. Two `telematics_provider_request_retryable`
`ReadTimeout` warnings on ALPHA `/trips` page 1 (attempts 1 and 2, succeeding on 3, which is why
`total_requests` is 15 for 13 pages) are the pre-existing retry envelope working, not an M3 effect.

**Event enrichment (§20.6 item 10).** ALPHA00001 issued **0** `/vehicles/events` requests, as item
10 requires. The other two are `event_enrichment_mode = enabled` and issued 254 (DELTA00001) and
180 (FOXTROT00001) — matching their own pre-M3 baselines of 272 / 277 / 253 and 154 / 167 / 170 over
2026-08-11…13. Item 10 is ALPHA-scoped for a reason: enrichment is **not** globally disabled across
compatibility clients, and §19.11 R5 already records that correction. Per-endpoint totals for the
three fires: ALPHA `/trips` 24, `/drivers` 4, `/vehicles` 2; FOXTROT `/trips` 3, `/drivers` 1,
`/vehicles` 1, `/vehicles/events` 180; DELTA `/trips` 5, `/drivers` 1, `/vehicles` 1,
`/vehicles/events` 254.

**Blast radius.** Three distinct clients, three schedules, two timezones, three lookbacks, two
enrichment modes, volumes spanning 2 409–19 065 rows, dispatched across two ticks — one shared
gate, no refusal anywhere. Disabled ECHO00001 did **not** advance (still `2026-08-01 00:00Z`,
`manual_recovery`, `updated_at 2026-08-04`). No fleet-wide stall.

**BRAVO00016 is a watch item, not an M3 blocker.** Its first natural M3 fire is due Monday
**2026-08-17 00:00Z** (weekly, `day_of_week = 0` = Monday per `dispatcher.py`). It exercises the
identical dispatcher path — the gate keys on `_is_compatibility_trips_fire` and
`data_invariants_v1`, both of which it satisfies — and its `L = 7` is already proven by DELTA00001.
Confirming that fire is routine operations, and nothing in M3 or M4 waits on it.

**Branching resolved:** the §20.6 result is green, so per the rule fixed in advance the next
engineering task is **M4 implementation (§21)**, not M3 remediation.

---

## 21. M4 implementation contract

Written 2026-08-13, **before** M4 begins and **before** the M3 production result exists, so that
the session which eventually implements M4 does not have to re-derive any of it. This section
states **what must be true**, not how to write it. It authorized nothing when written: M4 was
then `PLANNED`, no migration existed, and §20.6 gated the start. M4 has since been built (§22)
and closed in production (§22.14); the contract below is what it was built against, and it
held.

### 21.0 Entry condition

M4 may not begin until §20.6 is green. If it is not, the next task is M3 remediation.

**Satisfied 2026-08-14.** §20.6 is green on three natural production fires — see §20.7. This entry
condition is met and M4 may begin.

### 21.1 What M4 owns — and what it must not touch

M4 owns **§6 condition 6** and the durable evidence that makes it checkable. Conditions 1–5 are
already in force and are **not** in scope: §6 condition 5 in particular — the CAS and strict
monotonicity contract in `coverage_finalization.advance_covered_through_cas` — must come through
M4 unchanged, exactly as it came through M3.

| §6 condition | owner | state entering M4 |
|---|---|---|
| 1 — `rc == 0` | pre-M3 | in force |
| 2 — outcome ∈ `{EXECUTED_COMMITTED, EXECUTED_ZERO_ROWS_COMMITTED}` | M3 | in force |
| 3 — provider / business / `COMMITTED` assertions | M3 | in force |
| 4 — platform-run identity binding | M3 | in force |
| 5 — CAS + strict monotonicity | pre-M3 | in force, **do not modify** |
| **6 — sub-window completeness** | **M4** | not implemented |

Out of scope and explicitly *not* M4: the `run_type` discriminator and coverage re-key (M5), the
weekly and monthly cadences (M6/M7), alerting (M11).

### 21.2 The advancement invariant after M4

`covered_through_ts` may advance when, and only when, **all** hold:

```
rc == 0
AND a terminal execution-outcome record exists, parses strictly, and verifies against
    this exact claim — client, schedule, dataset, requested window, exact platform-run
    identity, recovery_run_id absent
AND outcome ∈ { EXECUTED_COMMITTED, EXECUTED_ZERO_ROWS_COMMITTED }
AND provider_execution_entered AND business_transaction_entered
    AND transaction_status == COMMITTED
AND the effective window was EXACTLY TILED by the sub-windows attempted —
    contiguous, gap-free, spanning [E_start, E_end)                        ← M4
AND every one of those sub-windows is durably recorded COMPLETE            ← M4
AND that durable evidence belongs to THIS execution, not another           ← M4
AND the claim-time coverage snapshot still matches under CAS, and the
    candidate is strictly greater than the stored watermark
```

The three marked terms are the entire M4 delta.

### 21.3 The fact M4 must prove, and why it is not already proven

The compatibility fetch contract already *enforces* almost everything and aborts loudly:
transport and HTTP failure, page repeat, page overlap, unstable advisory total, total
reconciliation failure, row/byte/elapsed/page budget exhaustion — every one raises
`TelematicsProviderSafetyError` and **discards the whole sub-window**, which fails the run. A
sub-window that returns at all in compatibility mode has already passed its invariants.

So M4's genuinely new fact is narrower and is about the **set**, not the members:

> A sub-window that was **never attempted** — a chunk-iteration defect, a truncated effective
> window, a silently short tiling — is invisible to every mechanism now in force, M3 included.
> Nothing today records that the sub-windows attempted exactly tile the window claimed.

That, plus **durability**: the evidence exists only as `public.logs` rows pruned at 60 days and
unindexed, which cannot support a coverage precondition or an admissibility judgement made
months later.

**A sub-window is COMPLETE only when** the compatibility fetch reached its valid terminal state
— a short page, with the advisory total reconciled where one was present — with no provider
error, no budget exhaustion, no pagination invariant failure. **An effective window is COMPLETE
only when** every sub-window in its tiling is complete *and* the tiling is exact.

**Two semantics that must survive M4 unchanged, because both are easy to regress:**

- **an absent advisory `meta.total` is a PERMITTED state**, not a failure. Only a *present*
  total that fails to reconcile is a failure. This is the accepted D5 Option B rule
  (`docs/16` §5); M4 must not quietly promote absence into a refusal.
- **`EXECUTED_ZERO_ROWS_COMMITTED` remains valid work and must still advance** once completeness
  is proven. A genuine zero-row day is not an incomplete window.

### 21.4 Persistence — minimum required direction

Schema direction only. **Do not create migration files before M4 is authorized to begin.**

**Platform database `logdb`** — `workflow_a_control.provider_request_log`, insert-only,
immutable, retention **180 days** independent of log pruning. Fields per §4.3, with
`subwindow_complete` promoted from an in-memory derivation to a stored flag. Identity should
rest on dimensions M5 does not disturb: `run_history_id`, `platform_run_id`, `client_id`,
`schedule_id`, `dataset_name`, `endpoint`, sub-window label, page. Natural uniqueness on
`(run_history_id, endpoint, sub_window_label, page)` makes a duplicate request record
structurally impossible rather than a failure class to detect at read time.

> **Correction (2026-08-14, implementation).** The key above is superseded. Independent review
> found the design text and migration 061 disagreeing on it, and resolving that deliberately —
> rather than by making one match the other — showed **both** were wrong once the request/evidence
> lifecycle split landed. The authoritative key is now
> **`(platform_run_id, endpoint, sub_window_index, page)`**; §22.3 states the two reasons, which
> are that `run_history_id` is NULL when the row is first written and `sub_window_label` is
> nullable. Nothing else in this section changes.

**Each client business database** — `client_trips.first_seen_request_id uuid NULL`, per §4.3a
D2: no FK (impossible across databases), set on INSERT only, never in `DO UPDATE SET`.

Rollback asymmetry, deliberate: the platform table drops cleanly, because nothing reads it until
condition 6 is enabled. The `client_trips` column is additive and nullable and should be **left
in place** on rollback rather than dropped from a live business table.

Client-business rollout must go through **both** paths — `scripts/apply_client_business_migrations.py`
for existing clients and the new-client baseline DDL — or the two diverge silently.

### 21.5 Atomicity

Implement the two-hinge model in §4.3a D3. No distributed transaction, no two-phase commit.
Hinge 2 — the `provider_request_log` projection inserted inside the existing
`_finalize_compat_success` transaction, alongside the coverage CAS — is the new work.

### 21.6 Historical behaviour

**M4 applies prospectively. There is no backfill of first-seen provenance, and there must not
be one.** Every pre-M4 row keeps `first_seen_request_id IS NULL`, meaning *provenance was never
captured*. Derived metrics exclude NULL rows; they never impute from `synced_at`, which is
last-touched and would fabricate exactly the evidence M4 exists to make trustworthy.

### 21.7 Failure direction

**Fail closed, always.** Missing, unreadable, unverifiable, or incomplete M4 evidence refuses
advancement and leaves the watermark untouched — the same shape M3 established: decide before
the finalizer opens its transaction, perform no coverage SQL at all on refusal, and still
finalize the fire so no row is left `RUNNING`.

The cost of that choice is already understood and accepted (§20.4): M4 further widens the class
of conditions that cost a day's watermark progress. It is bounded and self-healing through
`min(base, W − O)` up to `R`, and §13 is what makes a stall visible before the `R` cliff.

### 21.8 Acceptance

§15's M4 rows are the acceptance criteria and already exist: request-log completeness against the
existing logs as oracle; first-seen linkage soundness; `subwindow_complete` correctness under
forced partial pagination; first-seen is once-only across overlapping re-requests; late-arrival
detection (shared with M6); local failure must not be misclassified as a provider late arrival;
repeated observation creates no second first-seen event.

Add one row M4's own design implies and none of those cover — **an effective window whose tiling
is short by one sub-window must refuse advancement.** That is the failure mode §21.3 exists to
catch, and it is invisible to every existing check, because each sub-window that *was* attempted
passes its invariants perfectly.

Independent **Codex review is strongly recommended** for the M4 diff: it changes the live
coverage predicate, spans two databases with no distributed transaction, and its failure mode is
a silent fleet-wide coverage stall.

---

## 22. M4 delivery record — durable request/sub-window completeness evidence

**Status: `PRODUCTION_COMPLETE` since 2026-08-15.** Written 2026-08-14, **revised the same day**
after an independent Codex review returned two blocking findings and one schema/documentation
mismatch. All three are closed; §22.4 and §22.12 record what changed and why. The candidate was
committed as `2782550f8efef79509ec59ddec6ff08861453cd5`, released as `2782550f8efe` and
activated 2026-08-14T15:27:58Z behind the schema gate of §22.6b; §22.14 is the production
closure record. §21's contract remains the specification — this section records what was built
against it, and production confirmed it unchanged.

### 22.1 What M4 changes

§6 condition 6 becomes real. `covered_through_ts` may now advance only when, in addition to
every M3 condition unchanged, the child proves that the sub-windows it attempted **exactly
tiled** `[E_start, E_end)` and that every one of them reached a valid complete terminal state —
and that proof is durably committed in the same platform transaction as the watermark.

Conditions 1–5 are untouched. `advance_covered_through_cas` has no M4 edit of any kind: the CAS,
the eleven claim-time values, the strict monotonicity guard and the post-write verification came
through M4 byte-identical, which is what §21.1 requires.

### 22.2 The tiling unit, and the one assumption made explicit

The unit that tiles the effective window is the **job-level chunk**
(`_build_trip_fetch_chunks`), whose `(request_start_ts, exclusive_end_ts)` pairs are contiguous,
half-open and exactly spanning. Each chunk is then handed to `fetch_trips`, which splits it again
through `iter_31d_windows`.

That second split is 1:1 for every reachable configuration — `chunk_days ≤ TRIPS_MAX_CHUNK_DAYS
= 5`, provider sub-window `≤ TRIPS_MAX_SUB_WINDOW_DAYS = 30` — and the implementation
**requires** it rather than assuming it. A chunk that produced anything other than exactly one
provider sub-window covering exactly its requested interval is recorded INCOMPLETE. A future
constant change that made the split real would therefore stall coverage loudly instead of
advancing it on a tiling nobody verified.

The half-open `covers_*` interval and the inclusive-ended `requested_*` interval are both stored.
They differ by one boundary step on every non-final unit, and conflating them is how a tiling
proof degenerates into a rounding argument.

### 22.3 Files

| file | change |
|---|---|
| `jobs/api/telematics/request_evidence.py` | **new.** The completeness contract: immutable records, strict parsing, `verify_window_completeness` (the tiling and completeness checks), and the collector the job and provider client drive |
| `jobs/api/telematics/execution_outcome.py` | record version `/2`, adding `window_completeness`. Reader accepts `/1` and `/2`; writer emits `/2` |
| `jobs/api/telematics/provider_client.py` | observational evidence sink only. Records one page record per fully validated page, binds first-seen identities, and marks the sub-window complete at RECONCILE. No fetch/pagination branch reads it |
| `jobs/api/telematics/sync_trips_and_speeding.py` | declares each tiling unit before the provider is asked anything; attaches the proof to the terminal record; adds `first_seen_request_id` to the trip INSERT list only |
| `jobs/api/telematics/dispatcher.py` | condition 6 gate (`_require_complete_window_evidence`) and the durable projection (`_project_provider_request_log`) inside `_finalize_compat_success`'s existing transaction |
| `db/migrations/061_workflow_a_provider_request_log.sql` | **new.** The platform evidence relation |
| `db/client_business/047_client_trips_first_seen_request_id.sql` | **new.** Additive nullable column |
| `scripts/onboard_workflow_a_client.py` | new-client baseline applies and records 047 |
| `api/platform_prune.py` | 180-day retention for the evidence, on its own fixed horizon — for **both** lifecycle states, so a request fact never expires before the evidence it stands in for would have |
| `db/schema_requirements.json` | **new.** Release-pinned declaration of the physical schema a release requires before it may become current |
| `ops/release_schema_preflight.py` | **new.** The activation gate: ledger **and** physical verification, platform and **every** enabled client, read-only; plus `activation_fence`, the `SHARE` table lock held across the pointer swap |
| `ops/release_boundary.py` | activation runs the gate inside the management lock, then swaps the pointer **inside** the fleet fence |
| `ops/manage_release.py` | the activation dry run reports the same gate, and exits non-zero when activation would be refused |
| `ops/activate_telematics_trips_schedule.py` | the advisory lock an earlier candidate added here was **removed** — under the corrected enumeration, enabling a schedule cannot change fleet membership, so it protected nothing (§22.6b) |

### 22.4 The two-database model, as built

**This section was rewritten after independent review.** The first candidate's description of
hinge 2 was accurate about the transaction boundary and wrong about what that boundary
guaranteed, so the earlier claim is replaced rather than annotated.

**The defect.** `client_trips.first_seen_request_id` was committed in the client business
transaction, and the platform evidence was written afterwards, at coverage finalization. A
platform failure between the two — a projection error, a CAS conflict, a crash — left a committed
trip row referencing a request that never became durable anywhere. The reference is correctly
*immutable*, so later overlapping upserts preserved it faithfully: the row was permanently
unresolvable. Review reproduced it deterministically.

**The correction: order plus a two-state row.** No distributed transaction, no two-phase commit,
no new client-side table. `provider_request_log` now carries a lifecycle:

| state | written by | when | means |
|---|---|---|---|
| `PENDING` | the business job | **before** its business transaction commits | *this provider request happened* |
| `FINALIZED` | the dispatcher | inside the coverage CAS transaction | *this request belongs to a fire whose window was verified exactly tiled and complete* |

The execution order is now:

```
fetch completes, tiling final
  -> request facts committed PENDING            (platform DB, own transaction)
    -> business transaction commits             (client DB; trips carry first_seen_request_id)
      -> M3 verification -> M4 completeness verification
        -> promote PENDING -> FINALIZED + coverage CAS   (platform DB, one transaction)
```

**What each failure now costs.**

* platform write fails → the run fails before the business commit; no trip row exists to
  reference anything. Nothing is stranded.
* business transaction rolls back → unreferenced `PENDING` rows remain. They are inert forensic
  records: no trip points at them and no coverage read accepts them.
* promotion or CAS fails, or the process dies → the trip row keeps its immutable identity **and
  that identity still resolves**, as a `PENDING` request fact. The watermark does not move.

**What happens to those `PENDING` rows afterwards — stated precisely, because an earlier draft of
this section got it wrong.** They stay `PENDING` until retention expires them, and that is the
intended end state:

* they remain valid **first-seen provenance**: the trip row that references one resolves, forever
  as far as any audit is concerned;
* they are **never coverage evidence**. Nothing promotes them implicitly, and no coverage read
  accepts a `PENDING` row;
* a **later fire does not promote them**. Promotion matches on `platform_run_id`, and every launch
  mints a fresh platform run and fresh request identities, so a later fire over the same window
  creates and finalizes *its own* evidence and leaves the failed fire's rows untouched;
* only a **retry of finalization for the same execution** — same `platform_run_id`, same minted
  request identities — can promote them, which is what makes the promotion idempotent rather than
  a second chance for a different fire.

The earlier text claimed a later fire would promote the same identities. It does not, and it must
not: that would let one execution's coverage verdict be satisfied by another execution's requests.
The coverage gap left by the failed fire is closed the way M3 already closes it — a later fire
re-requests the window under `min(base, W − O)` and proves it with evidence of its own.

**Hinge 1** (M3) is unchanged: the terminal record is written after `conn.commit()` and may claim
`COMMITTED` only then.

**Hinge 2** is now a promotion, not an insert, and still shares one transaction with the coverage
CAS — one connection, one commit, one rollback. Every identity column (`run_history_id`,
`client_id`, `client_code`, `schedule_id`, `dataset_name`) is written *at promotion*, bound from
the dispatcher's own claim and never from the child's payload. The promotion matches
`status = 'PENDING' AND platform_run_id = <this run>` and requires the affected row count to equal
the proof exactly, so a proof naming requests this run never recorded — or already-finalized rows
— refuses instead of advancing.

**What is NOT atomic, stated plainly.** The client business commit and the platform commits are
separate transactions and always will be. What the design provides is not atomicity but an
ordering in which every reachable interleaving is safe, plus a durable record that makes the
cross-database reference recoverable. `provider_request_log` is no longer insert-only: it has
exactly one permitted update, the `PENDING -> FINALIZED` promotion.

**`platform_run_id` is deliberately not a foreign key** to `public.runs`, matching
`client_schedule_run_history` (migration 008). `api/platform_prune.py` deletes `runs` rows on its
own horizon; an enforced FK would either abort that prune or cascade-delete evidence this table
must retain for 180 days.

### 22.5 Refusal vocabulary

All evaluated in the pure phase, before the finalizer opens its transaction, so a refusal
performs no coverage SQL and still finalizes the fire (nothing is left `RUNNING`):

| code | meaning |
|---|---|
| `TRIPS_WINDOW_EVIDENCE_ABSENT` | no completeness carrier — including a pre-M4 `/1` record |
| `TRIPS_WINDOW_EVIDENCE_MALFORMED` | unparseable or self-contradictory carrier |
| `TRIPS_WINDOW_EVIDENCE_IDENTITY_MISMATCH` | evidence describes another window or endpoint |
| `TRIPS_WINDOW_TILING_INCOMPLETE` | gap, overlap, duplicate, short tiling or overhang |
| `TRIPS_SUBWINDOW_INCOMPLETE` | a unit in an exact tiling never reached a valid terminal state |
| `TRIPS_WINDOW_EVIDENCE_PROJECTION_FAILED` | the promotion failed mechanically inside the transaction; the watermark did not move |
| `TRIPS_WINDOW_EVIDENCE_PROJECTION_INCOMPLETE` | the proof names request identities this platform run never recorded as PENDING, or that another fire already finalized |

### 22.6 Two semantics deliberately preserved

* **an absent advisory `meta.total` remains PERMITTED.** `total_reconciliation` records `absent`
  or `exact`, and `absent` is a valid COMPLETE state. Only a *present* total that fails to
  reconcile is a failure, and that still raises inside the fetch contract, unchanged.
* **`EXECUTED_ZERO_ROWS_COMMITTED` still advances.** A genuine zero-row window terminates on a
  short — empty — page, so it is complete, and it advances exactly as before.

Provider fetch behaviour is otherwise byte-unchanged. The evidence sink is observational: no
branch of `data_invariants_v1` reads it, and the only edit inside `_request_json` captures the
response status that was already there. Event enrichment is untouched.

### 22.6a Natural uniqueness — one authoritative definition

Independent review found §21.4 and migration 061 disagreeing: the design text said
`(run_history_id, endpoint, sub_window_label, page)`, the schema implemented
`(run_history_id, endpoint, sub_window_index, page)`. Resolving it deliberately showed **both**
were wrong once the request/evidence lifecycle landed. The authoritative key is now:

```
UNIQUE (platform_run_id, endpoint, sub_window_index, page)
```

Two independent reasons, either of which is sufficient:

* **`platform_run_id`, not `run_history_id`.** The row is written before any fire has claimed it,
  so `run_history_id` is NULL at insert time — and NULLs are distinct in a UNIQUE index, so a key
  containing it would constrain nothing at exactly the moment the constraint is needed. The
  platform run is the identity the child actually owns when it writes.
* **`sub_window_index`, not `sub_window_label`.** The index is the tiling position and is NOT NULL
  on every row. The label is NULL for a unit that never reached the provider — precisely the rows
  a duplicate-detection constraint most needs to cover. A key on the nullable column would
  silently stop constraining them.

Schema, implementation, tests and this document now state that one definition.

### 22.6b Activation is gated on schema prerequisites

Independent review's second blocking finding: `manage_release.py activate` verified release
*bytes* and nothing else. Because the trip upsert names `first_seen_request_id` unconditionally,
activating M4 while one client business database still lacked migration 047 would have pointed
production at code that client cannot run — failing every ingest for it, not degrading one
feature.

**Where the requirements come from.** `db/schema_requirements.json`, read **from the release tree
being activated**, never from the working copy. A release therefore declares its own
prerequisites: an older release that predates a migration does not carry the requirement and is
never incorrectly blocked, a release that needs one cannot activate without it, and adding a
future prerequisite is a data edit rather than a code change. A release with no requirements file
predates the mechanism and passes with that reason recorded — which is what keeps rollback to any
historical release possible.

**What is verified, for the release being activated:**

* **platform** — migration 061 recorded in `public.schema_migrations`, *and* the relation, the
  columns with their types and nullability, and the named constraints physically present;
* **fleet** — for **every** enabled `client_account`, full stop: the database is reachable, 047 is
  recorded in that database's ledger, and `client_trips.first_seen_request_id` physically exists
  as a nullable `uuid` (the type is checked, not just the name).

Ledger **and** physical, always. A ledger records intent, and intent is exactly what a broken
rollout leaves looking correct; disagreement between the two is itself a refusal.

**Correction (second review).** An earlier version of this gate additionally required the client
to already own a `trips_sync` schedule. Independent review reproduced the consequence: four
enabled accounts, three with schedules and healthy, the fourth's database absent — the gate
examined three and passed, and the empty-fleet guard never fired because the set was partial, not
empty.

The requirement was never "clients that can currently fire"; it is that the enabled fleet's
databases carry the schema the release needs. A client with no schedule today is one onboarding
step from having one. **The eligibility filter is gone: enabled is the whole rule.** The
enumerated set is now identical to the enabled-account set by construction — one table, one
predicate, one query — so a partial fleet is unrepresentable rather than merely detected, and a
runtime assertion still refuses if the two counts ever disagree.

**Fail-closed, without exceptions.** The gate takes no filter parameter, so no client can be
excluded for convenience. An empty affected set while enabled accounts exist is a refusal, not a
vacuous pass — a gate that examined nothing must never report success. An unreachable client is a
refusal, never a skip.

**The check-to-use window — corrected after the second review.** The first attempt validated,
released an advisory lock, re-checked a fingerprint on a fresh connection, and *then* swapped the
pointer. Review committed a mutation in that gap. Two things were wrong: the protected interval
ended before the thing it protected, and an advisory lock constrains only code that volunteers to
take it, so direct SQL ignored it entirely.

The fence is now a real database lock held continuously across the swap:

```
BEGIN
LOCK TABLE workflow_a_control.client_account IN SHARE MODE
  re-enumerate the fleet, compare to the validated fingerprint
  swap the release pointer            <-- happens HERE, still holding the lock
ROLLBACK                              <-- read-only; releases the lock
```

`SHARE` is the minimum mode that works. It conflicts with `ROW EXCLUSIVE`, which every
`INSERT`/`UPDATE`/`DELETE` must acquire, so **any** session — a tool, a migration, a psql
prompt — blocks on its write until the transaction ends. It does not conflict with `ACCESS SHARE`,
so ordinary reads of the control plane are untouched.

`client_account` is the sole authoritative definition of both fleet membership (`enabled`) and
each client's database coordinates, so locking it fences the property. Client *business* databases
are deliberately not locked: that would be distributed locking, and it is unnecessary — the
fingerprint plus this fence prove the validated fleet is still the authoritative fleet, and the
per-client schema facts were established during validation.

A fleet mutation that commits *before* the fence is still detected by the fingerprint and refuses
the activation. A mutation attempted *during* it cannot commit until after the pointer has moved.
There is no interval in between.

The advisory lock previously added to `ops/activate_telematics_trips_schedule.py` has been
**removed**. Under the corrected enumeration, enabling a schedule cannot change fleet membership,
so that lock protected nothing; leaving it would imply a synchronization protocol that is no
longer the mechanism.

**Read-only.** The gate issues only `SELECT`s, creates no ledger table, and is asserted
mutation-free by running it twice against a real fleet and diffing every table.

### 22.7 Rollback asymmetry

Deliberate, and it is not symmetric because the two sides are not symmetric.

* **Platform:** `DROP TABLE workflow_a_control.provider_request_log;` is safe while condition 6
  is not enabled, because nothing else reads it.
* **Client business:** the `first_seen_request_id` column should be **left in place**. It is
  additive and nullable, so it costs a rolled-back release nothing, while dropping it would
  destroy provenance that by construction cannot be recreated.

### 22.8 Deployment ordering — load-bearing

**The client-business migration must be applied to every enabled client BEFORE the M4 release is
activated.** The job's trip INSERT names `first_seen_request_id` unconditionally, exactly as it
names `trip_mode` and `Driver_Restrictions`, so a client database without the column cannot
ingest at all. This is fail-closed and consistent with every previous additive client column, but
it makes the order mandatory rather than advisory. It was found by
`ops/tests_manual/test_telematics_recovery_execution_path_postgres.py`, which now applies 047 for
exactly that reason.

Second, smaller ordering note, recorded in the 061 header: once 061 exists, migration 014's
`TRUNCATE client_schedule_run_history` can no longer run, because 061 references that table. A
fresh in-order apply is unaffected (014 runs first); replaying the whole historical set onto an
already-migrated database is not, and `ops/db_migrate.sh` never does that.

### 22.9 Retention

180 days (§4.5), applied by `api/platform_prune.py` against its own fixed
`PROVIDER_REQUEST_LOG_RETENTION_DAYS` constant rather than the worker's `--days` value — so the
60-day log prune cannot shorten it and an operator cannot shorten it by passing `--days`. It
rides the existing prune timer; no new timer was created. An environment without migration 061
records `provider_request_log_absent` as a plan exclusion and prunes normally.

### 22.10 Verification performed

All local and deterministic. No provider call, no production database, no production mutation.

| suite | result |
|---|---|
| `test_telematics_m4_window_completeness.py` (**new**) | ALL PASS — tiling, completeness, malformed/mismatched evidence, the collector against the real chunk builder, first-seen once-only, vocabulary drift |
| `test_telematics_m4_provider_request_log_postgres.py` (**new**) | ALL PASS — both migrations apply and replay, constraints enforced by PostgreSQL, hinge 2 in both directions, first-seen insert/upsert semantics, and the full A1–A7 cross-database provenance lifecycle |
| `test_release_schema_preflight_postgres.py` (**new**) | ALL PASS — against a real **four**-client fleet whose fourth client owns no schedule: E1–E7 enumeration (exact checked set asserted, not just a verdict), ledger missing/lying, column absent, wrong column type, constraint absent, unreachable client, no-filter, and mutation-freedom. R1–R3 prove the fence blocks a cooperative writer *and* raw SQL while allowing reads |
| `test_release_activation_fence_postgres.py` (**new**) | ALL PASS — drives the real `activate_release` against temporary release trees: R2/R4 raw SQL cannot commit until after the pointer swap, R3 a pre-fence fleet change refuses, R5 a failing swap claims nothing and releases the lock, R6 a refused prerequisite touches no pointer, R7 read-only paths leave no lock or open transaction |
| `test_telematics_dispatcher_execution_outcome_gate.py` (**M3 regression**) | ALL PASS |
| `test_telematics_coverage_finalization_postgres.py` | PASS, extended with evidence/watermark co-commit and co-rollback assertions |
| `test_telematics_coverage_concurrency_postgres.py` | PASS |
| `test_telematics_coverage_bootstrap_gate.py` | PASS, end-to-end tick including an advancing compatibility fire |
| `test_telematics_trips_pagination_compat.py` | PASS — 300-case matrix, confirming fetch behaviour is unchanged |
| `test_telematics_daily_lookback_l3.py` (M2) | PASS |
| full Telematics + `test_platform_prune` set | 30/30 PASS |
| whole `ops/tests_manual` suite | **zero new failures** against a pristine-HEAD baseline |

The sharpest case, and the reason M4 exists (§21.8): an effective window whose tiling is short by
exactly one **otherwise-valid** sub-window is refused. Every unit present passes every
per-sub-window invariant perfectly; only the set is wrong.

### 22.11 What M4 still does not prove

Production behaviour was the open item here, and §22.14 closes it. Everything else this section
disclaimed still stands, and closure must not be read wider than it is:

* **Coverage completeness over history.** M4 proves that whatever effective window a fire
  declared was exactly tiled and completely fetched. It says nothing about whether that window
  is *wide enough*. The late-arrival horizon remains M6/M7, and ALPHA's `L = 3` is still not
  claimed to cover the full tail (§3.7c).
* **Stall detection.** A fail-closed refusal on evidence grounds is now reachable, and nothing
  alerts on one. That is M11; until it lands, a refused fire is caught by runbook watch.
* **The later cadences.** Only the compatibility `trips_sync` path is gated by condition 6.
  M5–M7 introduce the second and third cadences, and each owes its own production evidence.
* **BRAVO00016's weekly fire**, expected 2026-08-17T00:00Z. Routine confirmation, not a gate —
  see §22.14 for why the three daily clients already settle the shared path.

### 22.13 What the second correction pass changed

A second independent review confirmed the provenance blocker closed and found two further
blocking defects in the activation gate, plus a documentation mismatch. All three are closed:

| finding | resolution |
|---|---|
| **enabled clients were under-enumerated** — the gate filtered on owning a `trips_sync` schedule, so an enabled client without one was skipped and a partial fleet passed | the filter is removed; enabled is the whole rule, the enumerated set equals the enabled-account set by construction (§22.6b) |
| **check-to-pointer-swap race** — the fence released before the swap, and was an advisory lock that raw SQL ignored | a `SHARE` lock on `workflow_a_control.client_account` held continuously across the pointer swap, proven against raw concurrent SQL (§22.6b) |
| **documentation said a later fire promotes the failed fire's PENDING rows** | it does not and must not; §22.4 now states the actual semantics, and `test_P1_a_later_fire_cannot_promote_them` proves a different fire finalizes only its own evidence |

### 22.12 What the correction pass changed

Independent Codex review of the first candidate returned two blocking findings and one
schema/documentation mismatch. All three are closed, and none of the areas review approved were
redesigned:

| finding | resolution |
|---|---|
| **orphaned first-seen provenance** — an immutable `first_seen_request_id` could permanently reference evidence that never became durable | the request/evidence lifecycle split (§22.4): request facts are committed `PENDING` before the business transaction, promoted to `FINALIZED` inside the coverage transaction |
| **no activation migration gate** — `activate` verified release bytes only | release-pinned schema prerequisites, ledger **and** physical, platform and whole fleet (§22.6b) |
| **natural uniqueness mismatch** — docs and schema disagreed | resolved to `(platform_run_id, endpoint, sub_window_index, page)`, for reasons that apply to both of the previous candidates (§22.6a) |

Unchanged, deliberately: execution identity verification, the eligible-outcome contract, the
fresh per-launch outcome path, non-zero rc behaviour, recovery identity handling, CAS and strict
monotonicity, FAILED/SUCCESS semantics, the gate ordering, the tiling checks, zero-row semantics
and provider fetch behaviour. The M3 suite and the pagination compatibility matrix are green.

### 22.14 M4 closure — the first natural post-activation fires

**Status: `M4_PRODUCTION_COMPLETE`, 2026-08-15.** Verified read-only against production: no run
was triggered, no provider was called, nothing was mutated. This is the M4 counterpart of §20.7,
and it is deliberately short — the contract is §21 and §22.1–§22.9, and this records only what
production proved about it.

**Runtime identity.** `current = 2782550f8efe` (commit
`2782550f8efef79509ec59ddec6ff08861453cd5`), `previous = fabaaa753f89`, activated
2026-08-14T15:27:58Z, `verify` recomputed byte-identical against the source repository, installed
wrapper variant `release`. Every dispatcher tick since activation resolved
`release=2782550f8efe`, with no restart, no rollback and no further activation.

**Schema gate, as designed in §22.6b.** The activation record carries the preflight it passed:
platform `061_workflow_a_provider_request_log.sql` and client-business
`047_client_trips_first_seen_request_id.sql`, ledger **and** physical, across all five enabled
accounts — ECHO00001, DELTA00001, FOXTROT00001, BRAVO00016, ALPHA00001 — with zero physical defects.
Re-running the same read-only preflight after the fires returns the identical
`fleet_fingerprint`, so the fleet the gate covered is the fleet that ran.

**The three fires.** Each is the first natural post-activation `trips_sync` for its schedule
(`uq_run_history_schedule_fire` makes that exact, not inferred), dispatched by the scheduled
dispatcher, `trigger = SCHEDULED`, `rc = 0`, `SUCCESS ↔ SUCCESS`, `EXECUTED_COMMITTED`.

| client | fire | run_history_id | platform_run_id | `[E_start, E_end)` | sub-windows × requests | coverage |
|---|---|---|---|---|---|---|
| DELTA00001 | 2026-08-15T00:00:00Z | `23cb94a5-14d4-41f7-848f-86cd77a346c7` | `e1b30e99-bd8c-4b37-82bc-33ce54118c11` | `[2026-08-07T20:00Z, 2026-08-14T21:00Z)` | 4 × 6 | `2026-08-13T21:00Z → 2026-08-14T21:00Z` |
| ALPHA00001 | 2026-08-15T02:00:00Z | `6baeafdb-4a49-4cb8-8608-295c450bf703` | `ff5fe90d-7bca-42ca-8a8e-bb85719b6fa4` | `[2026-08-11T22:00Z, 2026-08-14T23:00Z)` | 2 × 19 | `2026-08-13T23:00Z → 2026-08-14T23:00Z` |
| FOXTROT00001 | 2026-08-15T02:00:00Z | `c3a96591-7623-47af-81ba-11f7eba805fa` | `c9a4832a-8420-47ba-8adf-e6736e271371` | `[2026-08-13T22:00Z, 2026-08-14T23:00Z)` | 1 × 2 | `2026-08-13T23:00Z → 2026-08-14T23:00Z` |

**Exact tiling (§22.2).** The sub-window counts are the ones `_build_trip_fetch_chunks` must
produce at `chunk_days = 2` for those windows — 4, 2 and 1 — not a number anyone asserted. For
each fire the durable `covers_*` intervals satisfy: first `covers_from_ts = E_start`, last
`covers_to_ts = E_end`, every adjacent boundary joining exactly, indices contiguous from 1, no
gap, no overlap, nothing outside the window. `requested_to_ts` sits one boundary step inside
`covers_to_ts` on every non-final unit and coincides on the final one, exactly as §22.2 requires.

**Completeness.** All 27 rows: `http_status = 200`, `termination_reason = short_page`,
`total_reconciliation = exact`, `subwindow_complete = true`, page sequences contiguous `1..N` with
no duplicate. The `absent` reconciliation branch (§22.6) was permitted but not exercised — every
window carried a present, reconciling advisory total.

**The load-bearing invariant.** Every coverage advance is matched by complete `FINALIZED`
evidence belonging to *that* execution: each row carries the fire's own `platform_run_id` and
`run_history_id`, and its `client_id`, `client_code`, `schedule_id`, `dataset_name` and effective
window all equal the dispatcher's claim rather than the child's payload. Within a fire every
`finalized_at` is a single instant, equal to that schedule's `client_dataset_coverage.updated_at`
and to the run-history `finished_at` — the promotion and the coverage CAS committed together, as
hinge 2 requires. The ordering of §22.4 was observed directly: request facts were logged durable
`PENDING` (6/19/2, `coverage_eligible: false`) minutes before each business commit.

**Zero `PENDING` residue.** The relation holds 27 rows, all `FINALIZED`, none written before
these three fires. There was therefore no older evidence available to adopt, and none was.

**First-seen provenance (§22.4, migration 047).** New inserts carried provenance and nothing else
did: 367 / 5 462 / 1 859 rows with a non-NULL `first_seen_request_id`, against 2 818 / 18 366 /
1 865 upserted — the remainder were overlapping re-upserts that correctly kept their existing
(NULL) value, confirming the insert-only contract under `overwrite_existing = true`. Every
distinct value resolves to a `FINALIZED` `provider_request_log.request_id` of the right client and
the right run; zero unresolved. Per-request new-row counts never exceed that page's `row_count`.

**Historical NULL preserved.** 58 232 / 792 258 / 203 018 pre-M4 rows remain NULL. The two enabled
clients with no M4 fire are the control: BRAVO00016 has 50 860 rows and **zero** non-NULL, ECHO00001
zero rows. No backfill occurred, and none may ever be added.

**No false-success, no false-stall.** Three fires, three eligible outcomes, three advances, zero
`TRIPS_WINDOW_*` / `TRIPS_SUBWINDOW_*` / `EXECUTION_OUTCOME_*` refusals fleet-wide, zero rows left
`RUNNING`, zero `ERROR` log rows. No pagination regression: 6 / 19 / 2 `/trips` requests against a
pre-M4 baseline of 5–6 / 19–20 / 2–3 for the same clients and window shapes. Two transient
provider read timeouts retried and succeeded; the ALPHA one hit `/trips` page 1 and still produced
exactly one evidence row, confirming the sink records validated pages, not HTTP attempts.

**Why BRAVO00016 does not gate this.** It is weekly (Monday 02:00 Europe/Warsaw, next fire
2026-08-17T00:00Z) and runs the identical path — same dispatcher finalization, same chunker, same
`data_invariants_v1` pagination, same `chunk_days` default — and shares DELTA's `L = 7`, which the
DELTA fire exercised at four sub-windows including both single-page and multi-page units. Its fire
is operational confirmation, not evidence M4 lacks. **ECHO00001** has an enabled account with a
disabled `trips_sync` schedule: correctly inside the schema gate's fleet, correctly absent from
the fire evidence.

---

## 23. M5 delivery record — multi-cadence schedule identity, one shared watermark

**Status: `PRODUCTION_COMPLETE` as of 2026-08-17.** Migration 062 is applied to
production and physically verified; release `b682df90c958` (commit
`b682df90c95853f6e04ad567f8e016e11f1e5047`) was activated 2026-08-15T17:30:46Z
behind the release-pinned schema gate. Natural scheduled verification is complete
across all three representative classes — see §23.8. Everything below describes a
delivered rollout; the sections retained in their original tense (§23.2a, §23.3a)
are the local-candidate review history and are labelled as such.

M5 remains **structural enablement only**: it created no weekly or monthly
schedule, and no reconciliation cadence exists in production (§23.7). M6 is the
first consumer of the M5 structure, not part of M5 completion.

### 23.1 What M5 changes

M5 is structural enablement, and its success condition is that nothing observable
moves. With one base schedule per dataset — which is the whole fleet today — the
scheduled fire, the effective window, `W`, the lookback, the overlap, the
chunking, the provider requests and the M4 evidence are all identical to M4.

What changes is what the schema *permits*:

| # | before | after |
|---|---|---|
| 1 | `uq_client_dataset_schedule UNIQUE (client_id, dataset_name)` — one schedule per dataset | `UNIQUE (client_id, dataset_name, run_type)`, plus a partial unique index enforcing **at most one** `DAILY` base schedule per `(client_id, dataset_name)` |
| 2 | no role discriminator | `run_type ∈ {DAILY, WEEKLY_RECONCILIATION, MONTHLY_RECONCILIATION}`, NOT NULL, default `DAILY` |
| 3 | coverage addressed by `schedule_id` | coverage addressed by `(client_id, dataset_name)`, enforced by `uq_client_dataset_coverage_dataset` |
| 4 | recovery exclusivity keyed on `schedule_id` | keyed on `(client_id, dataset_name)` — the shared watermark |

Row 1 states the index guarantee exactly, and the distinction is load-bearing
enough to separate here rather than leave to inference:

| guarantee | what actually proves it | scope |
|---|---|---|
| **at most one** `DAILY` schedule per `(client_id, dataset_name)` | `uq_client_dataset_schedule_base` alone | every `(client, dataset)` pair, including pairs with no schedule at all |
| **exactly one** valid anchor, carrying the base role | the complete schema — base partial index, `uq_client_dataset_coverage_dataset`, the composite FK with `RESTRICT` — together with the supported runtime and operator writers | **coverage-bearing datasets only** |

A partial unique index cannot prove existence, and 062 deliberately does not add
a constraint that would. An enabled client may legitimately own no schedule for a
dataset; the release gate's fleet fixture contains exactly such a client on
purpose. Existence and permanence of the anchor come from the coverage row's
foreign key, and base-role validity of that anchor is enforced one layer up by
the writers and runtime rather than by a single index. §23.3a states the
resulting system invariant and why it is expressed in that domain.

### 23.2 `run_type` is a role, not a cadence

The single most important thing to understand about the vocabulary, because
getting it wrong would have broken production data: **`run_type` and `frequency`
answer different questions.** `frequency` says how often a row fires; `run_type`
says what part it plays for its dataset.

`DAILY` therefore means *base ingestion schedule* — the row that carries forward
coverage — and it does **not** imply a daily cadence. Migrations 032 and 046
already seed base schedules whose `frequency` is `weekly` for the Eco datasets.
A migration that had forced role and cadence to agree would have rejected them.

The database enforces coherence only where it is genuinely required:
`ck_client_dataset_schedule_run_type_cadence` constrains the two reconciliation
roles to their matching cadence and leaves the base role unconstrained.

### 23.2a Independent review — five findings, all corrected

Codex classified the first candidate `M5_CODEX_REVIEW_BLOCKED`: four blockers and
one material finding. All five were reproduced from source before being fixed;
none was disproven. The approved parts of the candidate — the owner-keyed
coverage runtime, the CAS semantics, the M4 transactional invariant and the
zero-behaviour-change property — are unchanged.

| # | finding | correction |
|---|---|---|
| 1 | coverage provenance could contradict the shared owner: 057's single-column FK proved only that *some* schedule existed | composite FK `(schedule_id, client_id, dataset_name)` against a new superset unique key; 062 refuses pre-existing mismatches; the dispatcher re-checks fail-closed at load |
| 2 | 057's `ON DELETE CASCADE` would let a base schedule's deletion destroy the *shared* watermark; docs overclaimed "exactly one base schedule" | FK is `ON DELETE RESTRICT` / `ON UPDATE RESTRICT`; cardinality restated in its true domain (§23.3a) |
| 3 | recovery owner columns were not structurally tied to the schedule they name, so owner-keyed exclusivity could serialize the wrong owner | same composite FK on `client_dataset_recovery_run`; 062 refuses incoherent historical rows |
| 4 | the release gate checked constraint *names* only, so a drifted 062 shape could activate | requirements gained `definition`/`validated` and an `indexes` list, compared against `pg_get_constraintdef` / `pg_indexes.indexdef`; all load-bearing 062 shapes declared |
| 5 | four operator surfaces still read "the schedule for this client/dataset" as all cadences | each scoped to the base role, proven against a two-cadence dataset |

A **second focused review pass** found one further live resolver that the first
audit had missed for a mechanical reason worth recording: the scan enumerated
`ops/*.py` and never descended into `ops/checks/`, so
`check_trip_metrics_population_source._load_clients` — whose `LEFT JOIN` matched
every cadence of `trips_sync` — survived a full review. It is corrected, and the
audit is now a **recursive classification** of every live module naming the
schedule relation, with an unclassified module failing the suite. Directory depth
can no longer hide a resolver, and a whole-inventory surface is checked
statement-by-statement so the dispatcher's coverage-anchor join is not mistaken
for a cadence filter.

The claim the audit supports is *not* "every live resolver is base-scoped" — that
would be false, and forcing it would break the dispatcher. It is: **every live
module naming the schedule relation is recursively discovered and classified into
exactly one of four categories according to its actual semantics, and every
base-intended resolver is verified to select the `DAILY` role.**

| category | modules | semantic |
|---|---|---|
| **base-intended direct resolvers** | `ops/activate_telematics_trips_schedule.py`, `ops/recover_telematics_trips_window.py`, `ops/audit_telematics_coverage_bootstrap.py`, `ops/bootstrap_telematics_trips_coverage.py`, `ops/audit_telematics_cold_start.py`, `ops/checks/check_trip_metrics_population_source.py`, `scripts/onboard_workflow_a_client.py`, `jobs/api/telematics/control_plane.py` | resolve "the schedule for this client/dataset" through their own SQL, so each must name the base role in its own predicate — asserted |
| **whole-inventory** | `jobs/api/telematics/dispatcher.py`, `ops/execution_watchdog.py`, `ops/environment_identity_promotion.py` | intentionally enumerate **every** cadence; their schedule enumeration is asserted to carry no `run_type` predicate, because narrowing it would be the defect |
| **transitive consumers** | `jobs/api/telematics/aggregate_trip_fuel_daily.py`, `jobs/api/telematics/sync_trips_and_speeding.py`, `jobs/api/telematics/manual_recovery_authority.py` | issue **no schedule SQL at all**; they consume a `DatasetSchedule` that the already base-scoped `control_plane.load_dataset_schedule` resolved. Scope is inherited, not declared — asserted by proving they own no schedule query, and that the first two call the loader while the third only re-verifies a schedule its caller loaded |
| **exact-schedule / helper** | `ops/bootstrap_telematics_cold_start_coverage.py`; `jobs/api/telematics/schedule_mutation_surfaces.py` | the first addresses one exact row by operator-supplied `schedule_id` (`WHERE schedule_id = %s FOR UPDATE`), which no number of roles can make ambiguous; the second is validation vocabulary that names the relation only in prose and issues no SQL against it |

The transitive consumers are deliberately **not** described as `schedule_id`-keyed:
none of them keys on a `schedule_id`, and that label concealed the fact that their
correctness rests entirely on `load_dataset_schedule` remaining base-scoped. The
audit therefore asserts that dependency directly, and `control_plane.py` stays in
the base-intended set that proves it.

A sixth, smaller defect was found in the candidate's own proof: the CAS test
claimed to perturb all eleven claim fields but perturbed nine plus an excluded
one. It now perturbs all eleven, and asserts its own set equals
`COVERAGE_CAS_FIELDS` so the two cannot drift apart again.

Correcting finding 1 also exposed a replay defect the first candidate did not
have: the composite FKs depend on the new unique key, so a drop-and-recreate of
that key fails on re-apply with `DependentObjectsStillExist`. It is created
only when absent — forcing it with `CASCADE` would have silently dropped the
very integrity the migration installs.

### 23.3 Decision A — pre-M5 rollback compatibility is preserved

`pk_client_dataset_coverage PRIMARY KEY (schedule_id)` is **retained**, and the
new `UNIQUE (client_id, dataset_name)` is added beside it. This is not a
half-measure; the two constraints are consistent by construction, because a
coverage row names exactly one owning schedule and no two rows can name the same
one, so the conjunction is simply the stronger of the two.

It works because of a fact established from source rather than assumed:
**`client_dataset_coverage.schedule_id` is never mutated by any runtime writer.**
The only two UPDATE statements in the repository that touch this relation set
`covered_through_ts`/`covered_through_source`/`updated_at` and
`bootstrap_status`/`last_gap_detected_ts`/`updated_at`. `schedule_id` is written
once, by the bootstrap writer, and never again.

**The guarantee, stated at exactly the strength that was proven.** Under a valid
062 schema, and for as long as that schema is in force:

* every coverage row stays anchored to a schedule of its own owner (composite FK);
* that anchor cannot be deleted or re-owned while coverage references it
  (`ON DELETE RESTRICT` / `ON UPDATE RESTRICT`), so the row a pre-M5 release
  addresses cannot disappear or change identity underneath it;
* a pre-M5 release running the **base** schedule therefore resolves exactly one
  coverage row and advances the watermark exactly as it did before;
* a pre-M5 release meeting a **reconciliation** schedule finds no coverage row
  for that `schedule_id` and **fails closed** — a refusal, never a wrong
  watermark.

This is bounded by that schema and lifecycle contract. It is **not** a claim
about arbitrary future schema evolution: a later migration that changes coverage
identity again supersedes every statement in this section, and nothing here
should be read as guaranteeing compatibility past it.

Had `schedule_id` instead been redefined as "last advancing schedule", rollback
would have become time-dependent and therefore worthless — the pre-M5 release
would find its row or not depending on which cadence advanced last. That
alternative was rejected for exactly this reason.

### 23.3a Base-schedule cardinality — corrected

The first candidate said "exactly one base schedule per (client, dataset)". That
is false in the global domain and the schema never enforced it: an enabled client
may legitimately own **no** schedule for a dataset, and the release gate's fleet
fixture contains exactly such a client on purpose (`E1`). Rather than weaken the
documentation to match the index, the invariant is restated in the domain where
it is both true and load-bearing:

> **Every coverage-bearing dataset has exactly one anchoring schedule, of the
> same owner, which cannot be deleted or re-owned while it anchors.**

`uq_client_dataset_schedule_base` supplies *at most one* base schedule per
`(client, dataset)`; `uq_client_dataset_coverage_dataset` supplies *at most one*
watermark; the composite FK with `RESTRICT` supplies existence and permanence of
the anchor. Together those are the property M6 actually needs. That the anchor
carries the base role specifically is enforced one layer up — a partial unique
index cannot be a foreign-key target, and copying `run_type` onto the coverage
row to make one possible would invent duplicated business state.

**The boundary, stated rather than implied:**

| state | pre-M5 release behaviour |
|---|---|
| after 062, only base schedules exist (all of M5) | operates completely unchanged; every enabled schedule owns the coverage row whose `schedule_id` equals its own |
| after a future M6/M7 cadence row exists | still correct for the **base** schedule; **refuses fail-closed** for a reconciliation schedule, because no coverage row carries that id and the gate returns `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` |

That second row is a refusal, never a wrong watermark. Owning it is M6's job.

### 23.4 Decision B — recovery exclusivity follows the watermark

Migration 058 keyed "at most one non-terminal recovery" on `schedule_id` because,
at the time, one schedule *was* one watermark. M5 decouples those, so a
schedule-keyed index would silently stop meaning what it was written to mean.

The coverage lock plus the full-fingerprint CAS would keep concurrent recoveries
*safe* — the loser gets `TRIPS_COVERAGE_ADVANCE_CONFLICT` and overwrites nothing
— but 058's index exists to make concurrency **impossible**, not merely
survivable, and downgrading that silently is precisely the kind of quiet
weakening this plan exists to prevent. Both recovery uniqueness indexes were
therefore re-keyed to `(client_id, dataset_name)`. Today that is a
zero-behaviour-change re-key; after M6 it preserves the original guarantee.

### 23.5 The CAS changed address, not predicate

`schedule_id` moved from being the addressing key to being one of the eleven
compared claim values. The safety fingerprint is exactly as wide as it was:
eleven fields, null-safe, `covered_through_ts < new`, rowcount exactly one,
post-write verification, unchanged conflict classification and unchanged
transaction ownership. `telematics-coverage-fingerprint/1` is untouched — its field
list and order are part of the hashed payload, and every
`initial_coverage_fingerprint` already stored in `client_dataset_recovery_run`
would have been invalidated by changing it.

One consequence had to be accepted deliberately: the pure gate no longer compares
the coverage row's `schedule_id` against the firing schedule. For a reconciliation
fire those legitimately differ, and the old check would have refused exactly the
fires M5 exists to enable — reporting a well-formed row as bootstrap-required,
which is also the wrong diagnosis. Identity is now `client_id` and
`dataset_name`; `docs/15` §4.0 records the amended contract.

### 23.6 Files

| path | change |
|---|---|
| `db/migrations/062_workflow_a_multi_cadence_schedule_identity.sql` | new; one explicit transaction, four fail-closed guards before any DDL |
| `jobs/api/telematics/coverage_finalization.py` | coverage addressed by owner; `CoverageOwner`; conflicts carry the contended row |
| `jobs/api/telematics/coverage_windows.py` | gate identity is the owner; `schedule_id` documented as provenance |
| `jobs/api/telematics/dispatcher.py` | owner-keyed load/lock/gap paths; `run_type` projection; reconciliation join follows the owner |
| `jobs/api/telematics/schedule_mutation_surfaces.py` | the closed `run_type` vocabulary and its two validators |
| `ops/recover_telematics_trips_window.py` | owner-keyed coverage; resolves the base schedule |
| `ops/activate_telematics_trips_schedule.py` | owner-keyed coverage read; resolves the base schedule |
| `ops/bootstrap_telematics_trips_coverage.py`, `ops/bootstrap_telematics_cold_start_coverage.py`, `ops/audit_telematics_coverage_bootstrap.py` | owner-keyed reads and existence checks |
| `scripts/onboard_workflow_a_client.py` | re-keyed `ON CONFLICT`; seeds the base role only |
| `db/schema_requirements.json` | 062 declared as a platform prerequisite, by physical DEFINITION for every load-bearing constraint and index |
| `ops/release_schema_preflight.py` | requirements gained `definition`/`validated` and an `indexes` list; verification compares canonical `pg_get_constraintdef` / `indexdef` |
| `jobs/api/telematics/control_plane.py` | the child job's schedule read is base-scoped (it was `LIMIT 1` with no `ORDER BY`) |
| `ops/audit_telematics_coverage_bootstrap.py`, `ops/audit_telematics_cold_start.py` | base-role resolvers |
| `ops/checks/check_trip_metrics_population_source.py` | base-role resolver; the predicate stays in the `LEFT JOIN` condition so schedule-less clients are still reported (found by the second review pass) |
| `ops/tests_manual/test_telematics_m5_multi_cadence_identity_postgres.py` | new; the M5 proof |

### 23.7 What M5 deliberately did **not** do

* **No weekly or monthly schedule row exists.** M5 makes them representable; M6
  and M7 create them, each with its own review and its own production evidence.
* **No lookback, overlap, recovery-horizon, cadence, chunking, pagination or
  provider change.** The `L = 16` weekly recommendation remains M6's decision and
  remains unapproved.
* **No backfill.** `run_type = 'DAILY'` is a classification of rows the schema
  already constrained to one per dataset, proven from the catalog *and* the data
  before anything is labelled. No coverage value was read, written or derived.
* **No client-business migration.** Both relations live in `workflow_a_control`.
* **No alerting.** A refused fire is still caught by runbook watch until M11.

### 23.8 M5 closure — natural post-activation verification

Migration 062 was applied under authorization and release `b682df90c958` was
activated 2026-08-15T17:30:46Z behind the release-pinned schema gate (previous
`2782550f8efe`). Because M5 introduces no new production behaviour, closure is a
non-regression check rather than a new-evidence gate of the M3/M4 kind — the first
milestone that genuinely changes what production does is still M6.

**Release attribution — amended contract.** The original Stage F contract required
the qualifying natural fires to execute under exact release `b682df90c958`. No
scheduled fire occurred during its ≈5-hour currency window (2026-08-15 17:30:46Z →
22:33:42Z), while production advanced to the strict descendants `0e2ad513cff9` and
then `ac13457a6a99` — both Workflow B / alerting / systemd work. The contract was
amended by explicit authorization to accept a strict descendant **provided**
deterministic evidence establishes strict Git ancestry, a byte-identical relevant
M5 / Workflow A runtime surface, and no descendant-only semantic impact on
schedule resolution, `run_type`, coverage, the M3 outcome gate or M4 evidence
finalization. This corrected release *attribution*; it waived no behavioural
verification.

That evidence was produced: `M5_DESCENDANT_ANCESTRY = PASS` (linear chain
`b682df9 → 1bec254 → 0e2ad51 → ac13457`, zero merge commits) and
`M5_RUNTIME_SURFACE_IDENTITY = BYTE_IDENTICAL`. A dependency/import-closure audit
of the production entry points examined 41 files: 39 behaviour-bearing files were
byte-identical across all three releases, and the 2 that differed —
`api/suspected_bug.py` and `ops/operational_alert.py` — were individually cleared.
`SuspectedBugEvent`, the only symbol the M5 path imports, is byte-identical;
neither file references any `workflow_a_control` relation; and
`ops/operational_alert.py` is reachable only from `ops/runner.py`'s terminal-failure
handler, so a successful run never imports it. Result:
`DESCENDANT_CHANGE_IMPACT_ON_M5 = NONE`.

**Natural scheduled evidence.** Every qualifying fire was `trigger = SCHEDULED`
with `SUCCESS` in both `client_schedule_run_history` and `public.runs`; zero manual
and zero recovery runs participated.

| class | schedule | natural fire | release | evidence | verdict |
|---|---|---|---|---|---|
| DELTA00001 `trips_sync` | daily, `L = 7`, `run_type=DAILY` | `2026-08-17 00:00Z` (`cb13b8dd…`) | `ac13457a6a99` | 4 sub-windows × 6 requests, all `FINALIZED`, exact tiling; `W` advanced 2026-08-15 21:00Z → **2026-08-16 21:00Z = `E_end`**, `covered_through_source = scheduled_run` | `DELTA_TRIPS_NATURAL_RUN = PASS` |
| FOXTROT00001 `fuel_daily_aggregation` | daily, `run_type=DAILY` | `2026-08-17 02:05Z` (`b6605a02…`) | `ac13457a6a99` | base role resolved uniquely; non-coverage-bearing, so no coverage CAS is required by contract | `FOXTROT_FUEL_NATURAL_RUN = PASS` |
| BRAVO00016 `trips_sync` | **weekly**, `day_of_week = 0`, `L = 7`, **`run_type=DAILY`** | `2026-08-17 00:00Z` (`38e36563…`) | `ac13457a6a99` | 4 sub-windows × 4 requests, all `FINALIZED`; shared `W` advanced 2026-08-09 21:00Z → **2026-08-16 21:00Z**; anchor and provenance valid | `BRAVO_WEEKLY_TRIPS_NATURAL_RUN = PASS` |

BRAVO00016 is the load-bearing proof of the §23.2 model: **`run_type` is a role,
not a cadence.** A weekly-frequency schedule advanced the shared watermark
correctly while carrying the base role `DAILY`, and the coverage anchor query
resolves on `(client_id, dataset_name)` joined to `run_type = 'DAILY'` without ever
reading `frequency`. Hence `ROLE_FREQUENCY_SEPARATION = PASS`.

The M3 gate's satisfaction is a deterministic runtime inference, not a stored
field: the terminal outcome record is a per-launch temporary file the schema does
not persist, so coverage standing at exactly `E_end` with
`covered_through_source = scheduled_run` is itself the durable projection of the
fail-closed chain (`verify_outcome` → `is_coverage_eligible` →
`_require_complete_window_evidence`) having passed before `_finalize_compat_success`
opened its transaction.

**Invariants at closure.** 49 schedules, all `run_type = DAILY`; **0**
`WEEKLY_RECONCILIATION`, **0** `MONTHLY_RECONCILIATION`; 0 duplicate
`(client_id, dataset_name, run_type)` groups; 0 duplicate base owners; 5 coverage
rows with 0 duplicate owners, 0 orphaned anchors and 0 owner/provenance
mismatches; `provider_request_log` 100 % `FINALIZED` with global `PENDING = 0` and
no run-attributable residue; no M5-attributable error in any representative run
window. Natural execution created no schedule, and no M6/M7 behaviour appeared.

**Separate operational issue, not an M5 blocker.** `log-workflow-b.service` holds a
`FAILED_NON_RETRYABLE` failure at `workflow_b.stage3` (2026-08-17 06:09Z). It lies
outside the M5 runtime surface, outside every representative run window, and
touches no schedule, coverage or provider-evidence code. It is recorded here only
so its status is not mistaken for M5 health; it is owned by a separate task.

---

## 24. M6 delivery record — the WEEKLY_RECONCILIATION schedule lifecycle

**Status: `IMPLEMENTED_LOCALLY`, 2026-08-17.** The repository can represent, derive, register and
enable a `WEEKLY_RECONCILIATION` cadence under the existing deny-by-default mutation policy.
**Nothing is deployed, no production schedule row was created, none was enabled, no provider request
was made and no release was promoted.** Creating and enabling the first ALPHA00001 row are separately
authorized operator actions that this section does not perform and does not authorize.

### 24.1 What M6 changes

| | |
|---|---|
| platform migration | **none** — M5's 062 already carries `run_type`, the re-keyed uniqueness and the shared coverage key |
| client-business migration | none |
| `trips_max_recovery_span_seconds` | **unchanged**; `R = 2,678,400` exceeds the `L·86400 + O = 1,386,000` the window needs (§3.7c) |
| dispatcher | **unchanged** — it still carries `run_type` as evidence and branches on it nowhere. Asserted by AST, not by reading |
| coverage model | unchanged. One watermark per `(client, dataset)`, one CAS, monotone, behind-`W` is a validated no-op |
| M3 / M4 | reused verbatim. No role-specific outcome, evidence or completeness semantics exist |
| DAILY schedules | untouched. No row, lookback, cadence, window or chunk size moved |

### 24.2 The approved cadence

```
run_type       = WEEKLY_RECONCILIATION
frequency      = weekly            (forced by ck_client_dataset_schedule_run_type_cadence)
day_of_week    = 0                 (Monday, Python weekday() convention)
run_time       = 00:30
timezone       = Europe/Warsaw
lookback_days  = 16
enabled        = false             (always, at creation)
```

Fire time revised from §3.4's 02:30 — see §3.1. Both 2026 DST transitions fall on a **Sunday**, so
Monday 00:30 always exists and is never ambiguous; that is asserted rather than assumed.

### 24.3 The lifecycle, and why it is three steps and not one

`jobs/api/telematics/schedule_mutation_surfaces.py` is deny-by-default and previously registered two
surfaces: onboarding (creates base rows, always disabled) and
`ops/activate_telematics_trips_schedule.py` (enables a client's *first* schedule at the end of the
cold-start state machine). **Neither could create a reconciliation cadence**, and forcing one
through the cold-start activation gates — which demand a completed recovery chain, a coverage
fingerprint and *no* schedule history — would have meant weakening them. That was not done.

M6 registers one new surface, `ops/manage_telematics_reconciliation_schedule.py`, in **all three**
mutation classes — `CREATION_SURFACES`, `ACTIVATION_SURFACES` and `DEACTIVATION_SURFACES` — with
three separate subcommands:

1. **`register`** — resolves the base row, projects it, and INSERTs the cadence **disabled**.
   Refuses: an unregistered caller, onboarding, the base role, an ineligible dataset, an
   already-registered role, an absent base schedule, a cadence the dispatcher could never fire
   (weekly without `day_of_week`; monthly without `day_of_month`/`day_of_month_last`), an unknown
   IANA zone, and any request to create the row enabled.
2. **`enable`** — flips exactly one field. Additionally requires: the base schedule **exists and is
   enabled**, the client is `data_invariants_v1`, the client account is enabled, and the shared
   coverage row is **`READY`**. A reconciliation cadence must never advance a watermark that nothing
   else maintains.
3. **`disable`** — the reversal of step 2, and the reason `enable` is not a one-way door. It flips
   the same one field back and **preserves everything else**: the schedule row survives, every
   `client_schedule_run_history` row it owns survives, and the base `DAILY` row is re-read after the
   write and required to be field-for-field identical, `updated_at` included. There is deliberately
   **no delete-based rollback**: removing the row would take its history with it.

   It is asymmetric with `enable` in three deliberate ways.

   * **It asks nothing about the base schedule, the coverage status, the pagination mode or the
     client account.** Those gates exist to stop a cadence from *starting*; disabling removes
     execution capability. Requiring them would make the reversal unavailable in exactly the
     degraded states an operator most needs it — ECHO00001's shape, base `trips_sync` disabled, is
     the live example, and `disable` is permitted there while `enable` is still refused.
   * **An already-disabled row is an idempotent no-change, not an error** — `enable` refuses
     `ALREADY_ENABLED`, because re-granting capability on an ambiguous request is worth refusing;
     telling an operator mid-incident that a reversal "failed" when the desired state already holds
     is not. The no-op writes nothing at all, not even `updated_at`.
   * **It is registered in its own mutation class.** `DEACTIVATION_SURFACES` holds this tool alone.
     Reusing `ACTIVATION_SURFACES` would have silently granted
     `ops/activate_telematics_trips_schedule.py` the authority to switch a base `trips_sync` row off —
     the one mutation the coverage model cannot detect. **No surface is registered to disable a base
     schedule**, so deny-by-default makes it impossible through any reviewed path.

   Refuses: an unregistered caller, a registered-but-not-permitted caller, the base role, an unknown
   role, an ineligible dataset, an absent reconciliation row, more than one matching row, a row that
   moved between the plan and the write, and a row another transaction is holding
   (`FOR UPDATE NOWAIT` — a contended row is a write conflict, never an indefinite block).

   **The idempotent answer comes from a locked re-read, never from the plan.** The plan is a
   snapshot taken earlier in the same READ COMMITTED transaction, so a concurrent `enable` could
   otherwise make `disable` report `ALREADY_DISABLED` and exit 0 while leaving the schedule enabled —
   a false success on the one operation whose purpose is to stop future execution. `execute_disable`
   therefore locks the target row, re-checks every immutable field against the plan, and only then
   decides between the write and the no-op.

   **It covers `MONTHLY_RECONCILIATION` as well as `WEEKLY_RECONCILIATION`, deliberately.** The gate
   accepts any reconciliation role, exactly as `enable` already does. Restricting the reversal to the
   weekly role would hand M7 the identical one-way door this step exists to close, and would make the
   creation, activation and deactivation authorities disagree about which roles they govern. Production
   holds zero `MONTHLY_RECONCILIATION` rows today, so the wider scope grants no capability over any row
   that currently exists.

4. **dispatch** — unchanged and unowned by this tool. An enabled row is picked up by the existing
   dispatcher through the same claim, coverage gate, M3 verification and M4 evidence as any other
   schedule.

Dry-run is the default for all three. A write needs `--execute`, `--approval-ref`, and a
`--confirm-client-code` equal to `--client-code`, plus the matching `--expected-environment` and
`--expected-platform-uuid`, and re-reads the row on the writing cursor before commit.

**Disabling while a reconciliation run is already `RUNNING` — semantics A, future fires only.** The
live run finishes, finalizes and writes its coverage decision exactly as it would have; the next
dispatcher tick simply does not enumerate the row. This is the platform's existing behaviour, not a
new mechanism: `_load_enabled_schedules` evaluates `cds.enabled = true` once, at the top of a tick,
and no post-claim statement re-reads it — asserted by AST in the M6 suite. Refusing a disable while
a run is active was considered and rejected: a `RUNNING` row can persist for up to
`stale_running_timeout_minutes` (720 by default) after a killed dispatcher, which is precisely when
the cadence most needs switching off. The plan output reports `active_runs` and the contract name so
the operator sees the state rather than inferring it. The tool adds **no** cancellation or
termination mechanism; it prevents future executions, it does not kill active work.

### 24.4 Inherited configuration — the defect this prevents

`client_dataset_schedule` column defaults are correct for onboarding a new client and **wrong** for
deriving a second cadence over a live one. Concretely: `event_enrichment_mode` defaults to
`'enabled'`, while ALPHA00001's base schedule carries `'disabled'`. A weekly row built from the
cadence columns alone would have silently issued sixteen days of `/vehicles/events` requests that
M2 explicitly verified must be **zero** (§19.4, §15).

So every column is classified exactly once — identity, inherited, declared, generated — and the
partition is asserted **exhaustive against `information_schema`**. A column added to the table
without being classified fails the test rather than quietly acquiring its default. `timezone` is
deliberately *declared*, not inherited: ALPHA's base is `UTC` and the approved cadence is
`Europe/Warsaw`.

### 24.5 Files

| file | change |
|---|---|
| `jobs/api/telematics/schedule_mutation_surfaces.py` | the reconciliation vocabulary, the four-way column partition, `derive_reconciliation_schedule`, and the three deny-by-default guards (creation, activation, deactivation) |
| `ops/manage_telematics_reconciliation_schedule.py` | **new** — the one registered lifecycle surface: `register`, `enable`, `disable` |
| `ops/tests_manual/test_telematics_m6_weekly_reconciliation.py` | **new** — the M6 suite |
| `ops/tests_manual/test_telematics_schedule_activation_postgres.py` | the repository scan's allowlist, now asserted equal to `REGISTERED_SURFACES` |
| `ops/tests_manual/test_telematics_m5_multi_cadence_identity_postgres.py` | a fifth resolver category, `role-explicit`, with its own assertions; the "no reconciliation row anywhere" test amended to its real invariant |
| `ops/tests_manual/test_telematics_trips_stabilization_windows.py` | the single-production-importer set, plus a pre-existing omission corrected (§24.7) |

### 24.6 One design decision worth recording

The activation guard needs the `READY` constant. Importing it from the pure coverage-window helper
would have made the policy oracle a **second production importer** of a module whose importer count
is exactly one and is asserted — `test_runtime_non_activation`. The constant is therefore restated,
exactly as `coverage_finalization.py` already restates it, with a drift test in the M6 suite. The
guard caught this during implementation; it was not noticed by reading.

### 24.7 A pre-existing test failure, found and fixed

`test_runtime_non_activation` has been **failing since `b682df9`**: M5 added a `coverage_windows`
reference to its own suite without extending that test's expected set. It was red on a pristine
`745b3eb` checkout, verified in a detached worktree before any M6 edit. The entry is added and the
invariant it guards — exactly one production importer — is unchanged and still holds.

### 24.8 What M6 does not prove

- **Peak memory.** An `L = 16` window holds roughly 5.3× the rows resident that the deployed `L = 3`
  run does (§1.5a names memory as the one genuinely unbudgeted dimension). No local test can
  establish this; it must be measured on the first real weekly run, and it is the gate on M7.
- **Real provider cost.** Projected at ~9 sub-windows, ~85–107 `/trips` requests and ~15–18 min for
  ALPHA, from the production `L = 3` calibration. Projection, not measurement.
- **Anything about production.** No fire has happened. `ROLE_FREQUENCY_SEPARATION` was proven by M5
  on a weekly-frequency **base** schedule; a genuine `WEEKLY_RECONCILIATION` row has never run.
- **First-seen provenance for weekly-discovered trips** is captured by M4's existing mechanism, but
  the delivery-lag metric that would *read* it is not built. If that metric matters before the first
  weekly fire, it is separate work and is not part of this diff.

---

## 25. M6/M7 activation record — the first reconciliation fires, and the defect they found

Written 2026-09-08, after enabling both reconciliation cadences for ALPHA00001 and BRAVO00016 under
`M6-ENABLE-2026-09-08-OWNER-REQUESTED` and `M7-MONTHLY-RECONCILIATION-2026-09-08-OWNER-REQUESTED`.
Everything below is observed production behaviour, not a plan.

### 25.1 A reconciliation fire could never have succeeded

§24 closed M6 as `IMPLEMENTED LOCALLY` with the honest caveat that *"a genuine
`WEEKLY_RECONCILIATION` row has never run"*. The first two that ran both failed:

| fire | window | result |
|---|---|---|
| BRAVO00016 weekly, `2026-09-07 00:30` (backdated) | `2026-08-21 20:30` → `2026-09-06 21:30` | FAILED — `EXECUTION_OUTCOME_IDENTITY_MISMATCH` after 1 min |
| ALPHA00001 weekly, `2026-09-07 00:30` (backdated) | `2026-08-21 20:30` → `2026-09-06 21:30` | FAILED — same, after 16 min 34 s |

> `EXECUTION_OUTCOME_IDENTITY_MISMATCH: terminal record schedule_id is
> '7ed68a89-…' , expected '3fbbc64c-…'`

**The cause.** `_require_coverage_eligible_outcome` verifies the child's terminal record against the
schedule that *claimed* the fire (`schedule.schedule_id`). The child stamped that record with the
schedule `control_plane.load_dataset_schedule` returns — which M5 deliberately scoped to the **base**
row (§23), because the base row is the child's *configuration*. Both statements are correct; they
were simply never reconciled with each other. For a `DAILY` fire the two ids are the same row, so
the gate passed. For a reconciliation fire they never are.

M5 introduced this the moment a second cadence became expressible; nothing before M6 could observe
it, because nothing before M6 ever fired a non-base role.

**The fix** (commit `9b79507`, release `2091baf1ef1d`) separates *which row configures me* from
*which fire am I*. `dispatcher._build_job_params` passes the firing `schedule_id` alongside the
`schedule_run_type` it already sent; `sync_trips_and_speeding` binds it for `trigger=SCHEDULED`
only, so the manual-recovery path keeps binding the authoritative base row that
`recover_telematics_trips_window` resolves and verifies against. The dispatcher still verifies the
returned record against its own claim, so this tells the child which fire it is executing rather
than asking it.

**The fail-closed design held.** Both failed fires had already committed their business
transactions — BRAVO00016 upserted 5 689 trips (10 newly first-seen), ALPHA00001 upserted 89 967 (6
newly first-seen) — and `covered_through_ts` was left byte-identical for both clients, exactly as
§6 requires. The cost of the two failures is 103 orphan `PENDING` `provider_request_log` rows,
referenced by 16 `client_trips` rows whose provenance therefore stays pending. They are inert and
age out with the 180-day prune.

### 25.2 Why `L = 32` monthly, decided on August evidence

§3.1a left the monthly `L` deliberately unresolved and asked for post-M6 telemetry. The August 2026
D105.2 reconciliation supplied better evidence than telemetry would have: 907 trips confirmed
missing against the provider's own report, every one of them recovered and field-verified. Replaying
those 907 against real fire grids, taking each trip's provider-arrival instant as the `first_seen`
of the next present trip of the same vehicle (an upper bound):

| cadence | caught | lost | lost km |
|---|---:|---:|---:|
| DAILY `L = 3` alone (deployed) | 0 | 907 | 37 327 |
| + weekly `L = 8` (the superseded §3.7 proposal) | 225 | 682 | 32 813 |
| + weekly `L = 16` (M6) | 794 | 113 | 5 315 |
| + weekly `L = 16` + monthly `L = 32` | **907** | **0** | **0** |

Weekly `L = 16` alone leaves 113 trips uncaught, which is what makes M7 load-bearing rather than
belt-and-braces.

### 25.3 The `R` clamp, and why the old ceiling was not a provider limit

`derive_effective_window` floors `E_start` at `E_end − R`, so the clamp binds exactly when
`R < L·86400 + O`. §3.3 derived the monthly infeasibility from the calendar framing; against a
rolling window the condition above is the correct one, and at `L = 32`, `O = 3 600` it demands
`R ≥ 2 768 400`. The old ceiling was 2 678 400 in both `jobs/trips_stabilization_config.py` and
`ck_client_account_trips_max_recovery_span_seconds`.

That ceiling adopted the provider's documented 31-day `/trips` lookup limit, which is a limit on a
single **request**. `_build_trip_fetch_chunks` already caps every request at `chunk_days` (default
2), so the derived window never reaches the provider as one lookup. Migration 072 raises the ceiling
to exactly 2 768 400 — the threshold at which the clamp goes inert for `L = 32` and no further — and
adopts it for ALPHA00001 and BRAVO00016 only. The column DEFAULT and the other three clients stay at
31 days. The migration header carries the load-bearing ordering constraint: it must not be applied
before a release carrying the new code ceiling is active, because
`validate_trips_stabilization_config` would otherwise reject the stored value on every dispatcher
tick.

### 25.4 First successful reconciliation fires — measured

Both monthly fires were backdated to `2026-09-01 03:00`, so they ran immediately on enable.

| fire | window | runtime | peak RSS | `/trips` requests | rows |
|---|---|---|---|---|---|
| BRAVO00016 monthly `L = 32` | `2026-07-30 23:00` → `2026-09-01 00:00` | 1 min 56 s | 104 MB | 17 | 11 480 |
| ALPHA00001 monthly `L = 32` | `2026-07-30 23:00` → `2026-09-01 00:00` | 29 min 55 s | 882 MB | 185 | 177 141 |

Both windows are exactly 32 d + 1 h, confirming the clamp is inert. Every sub-window reported
`subwindow_complete`, every request finalized `exact`, and `covered_through_ts` correctly did **not**
move for either client — `E_end = 2026-09-01 00:00` precedes both watermarks, so the monotonic
advance is a true no-op. ALPHA00001's run produced **zero** newly first-seen trips, which is the
expected result for a month already reconciled against D105.2 that same day.

**§14's open memory risk is closed by measurement.** The failed ALPHA weekly `L = 16` peaked at
477 MB in 16 min 34 s (against §3.7c's predicted ~17 min), and the monthly `L = 32` at 882 MB —
close to the linear extrapolation. Against 15.6 GB of host RAM with ~10 GB available at fire time,
neither is a constraint, but `L = 32` is no longer a negligible footprint and should be re-measured
if a materially larger fleet adopts the monthly cadence.

Provider budget on the largest run: 185 requests against the deployed `.env` limits of 3 000 per
endpoint and 5 000 per run — ~16× headroom, consistent with §3.5's estimate of ~190 requests.

### 25.5 What this activation deliberately did not do

- **DELTA00001, FOXTROT00001 and ECHO00001 were not enabled** for either cadence. Their weekly rows stay
  registered-disabled and no monthly row was registered for them. Both remain protected for only
  0.167 d (FOXTROT00001) and 6.167 d (DELTA00001) — see §3.7c — which is now a known, quantified and
  *unaddressed* exposure rather than an unexamined one.
- **The two release-bound services were not restarted.** `log-platform-api.service` and
  `database-export-worker.service` still execute `770712e244bf`; neither imports any module this
  change touches, so they may adopt `2091baf1ef1d` at their next ordinary restart.
- **M8 is still PLANNED, and the gap is now demonstrated.** The 2026-09-08 manual backfill of the
  907 trips reached `client_trips` and nothing downstream: `Dysponent_ID` was enriched for 0 of 907
  rows, both eco assignment tables saw 0 of 907, and `eco_driver_monthly_stats` for 2026-08 had been
  recomputed six hours *before* the backfill committed. Now that two reconciliation cadences write
  historical rows on a schedule, the absence of downstream invalidation is a recurring defect rather
  than a one-off.
- **No orphan `PENDING` evidence was deleted** (§25.1).
