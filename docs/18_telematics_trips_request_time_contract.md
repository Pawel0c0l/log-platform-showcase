# Telematics request-time contract and rolling historical refresh

**Status: verified against the live provider and implemented.**

This document records three things that are load-bearing for Telematics trip and
Eco Driving completeness and were previously undocumented:

1. the timezone contract of the `/trips` **request** parameters, which is not
   the same as the contract of the **response** rows (§1);
2. the same contract for `/vehicles/events`, measured separately and
   independently (§2);
3. how the scheduled `trips_sync` window re-reads already-covered history, and
   why that is the permanent remedy for provider late publication (§3).

**Both measured endpoints behave the same way**, and neither behaviour is
assumed for any endpoint that has not been measured:

    request  start_timestamp / end_timestamp  ->  Europe/Warsaw local wall-clock
    response row / event timestamps           ->  UTC

This is an empirically observed provider behaviour recorded from production
diagnostics, **not a contractual provider SLA**. Telematics publishes no timezone
contract for these parameters. The file name is kept for stable cross-reference
even though the scope is now wider than `/trips`.

Related canonical documents:

- `docs/12_telematics_trips_pagination_compatibility.md` — pagination compatibility mode.
- `docs/13_telematics_trips_stabilization_windows.md` — stabilized window derivation (§16.1).
- `docs/15_telematics_coverage_mutation_contract.md` — the coverage compare-and-swap.
- `docs/05_jobs.md` — `jobs.api.telematics.sync_trips_and_speeding`.

Code of record:

- `jobs/api/telematics/provider_client.py` — `_wall_clock_wire_window` (the shared
  primitive), `trips_wire_window`, `vehicle_events_wire_window`,
  `PROVIDER_WALL_CLOCK_TIMEZONE_NAME`, `TRIPS_MAX_SUB_WINDOW_DAYS`,
  `VEHICLE_EVENTS_MAX_REQUEST_WINDOW`, `VEHICLE_EVENTS_MAX_INTENDED_WINDOW`,
  `fetch_trips`, `fetch_vehicle_events_fleet`, `fetch_vehicle_events_registration`.
- `jobs/api/telematics/sync_trips_and_speeding.py` — `_vehicle_events_chunk_delta`.
- `jobs/api/telematics/coverage_windows.py` — `derive_effective_window`.
- `ops/tests_manual/test_telematics_trips_wire_time_contract.py`
- `ops/tests_manual/test_telematics_vehicle_events_wire_time_contract.py`
- `ops/tests_manual/test_telematics_trips_rolling_refetch.py`

---

## 1. The request/response timezone asymmetry

    GET /trips request  start_timestamp / end_timestamp  ->  Europe/Warsaw local wall-clock
    GET /trips response row start_timestamp / end_timestamp  ->  UTC

The two directions do **not** agree. This is a provider behaviour, not a
platform convention, and it is not assumed for any Telematics endpoint that has
not been measured in the same way. `/vehicles/events` has since been measured
independently and behaves identically — §2.

### 1.1 Evidence

Established by four GET-only probes against ALPHA00001 on 2026-08-10, over two
closed July 2026 windows.

| round | wire `start_timestamp` | intended Warsaw window | rows returned (Warsaw) |
|---|---|---|---|
| 1 | `2026-07-21 14:30:00` (UTC projection) | 16:30–17:30 | 14:14–14:43 — offset by the UTC offset |
| 2 | `2026-07-21 16:30:00` (Warsaw wall-clock) | 16:30–17:30 | 16:30:19–17:21:38 — as intended |

Round 2 returned all six pre-registered reference trips with exactly matching
start timestamps, end timestamps, floor-matching distances and floor-matching
odometers. An independent control of 1,487 other-vehicle rows in the same two
responses matched already-stored `client_trips` rows at 97–99%, confirming the
query construction and the matching methodology.

The response side needs no change: `_parse_provider_dt` already reads a naive
row timestamp as UTC, and 180,780 stored July trips reconcile to the second
against the independent D105.2 report under that reading.

### 1.2 Implementation

`provider_client.trips_wire_window(start, end)` is the single boundary. It
takes absolute instants (naive input is read as UTC, as everywhere else in the
module), converts to `Europe/Warsaw`, and returns plain strings — so the
conversion cannot pick up the host's local timezone and cannot be applied
twice.

`fetch_trips` is its only caller. Since §2 it is a thin wrapper over the
endpoint-neutral `_wall_clock_wire_window`, which `vehicle_events_wire_window`
also wraps; the two wrappers stay separate because their window limits differ.
`_provider_dt_str` is unchanged and every endpoint outside §1 and §2 keeps its
previous serialization until its own contract is independently proven.

### 1.3 DST

Because the provider addresses trips by wall-clock only, a window that spans a
Europe/Warsaw DST transition cannot be expressed unambiguously.

- **Autumn fallback.** Local 02:00–02:59:59 occurs twice. Two distinct instants
  serialize to the same string, and a window contained inside the fold
  serializes to an inverted or empty interval. Which occurrence the provider
  resolves is outside our control.
- **Spring forward.** Local 02:00–02:59:59 does not exist. `astimezone` never
  emits a time inside the gap, and local time advances monotonically across it,
  so a window whose endpoints are both unambiguous is already exact and is not
  widened at all.

**Rule.** The invariant is stated on the *worst-case reading* of each emitted
wire string, not on the offsets of the two endpoints:

    max(readings(start_str)) <= start    and    min(readings(end_str)) >= end

where `readings(s)` is every UTC instant whose Warsaw wall-clock is `s` — two
instants inside the autumn fold, one otherwise. Each endpoint is independently
moved outward until its own worst-case reading satisfies the invariant, which
costs at most one Warsaw offset change (1 hour) per side.

**Why not the offset delta.** Widening only when the two endpoints carry
different UTC offsets is *not* sufficient. A window lying wholly inside one
occurrence of the repeated hour has the same offset at both endpoints and is
ambiguous regardless; left unwidened it serializes to an inverted interval and
the provider may resolve both endpoints to the other occurrence, omitting the
intended interval entirely. Such windows are reachable in production:
`_build_trip_fetch_chunks` emits a short remainder chunk whenever the effective
window is not a whole multiple of `chunk_days`, and
`jobs.api.telematics.backfill_trips_insert_only` accepts arbitrary operator
windows. An exhaustive sweep of 7,942 DST-adjacent window shapes found 470
(5.9%) that the offset-delta rule would have under-fetched.

Under the rule above the requested wall-clock interval contains every instant
of the intended UTC interval under *either* resolution of an ambiguous local
time. This can only over-fetch, never under-fetch. Over-fetching is free —
`client_trips` upserts on `(client_id, provider_trip_id)`, so a duplicate row
is a no-op update — whereas a skipped UTC interval is silent data loss. The
asymmetry is deliberate.

`ops/tests_manual/test_telematics_trips_wire_time_contract.py` proves the
invariant for the worst-case provider resolution of every ambiguous endpoint,
including windows contained inside a single occurrence of the fold, and sweeps
every window shape on a one-minute grid ±3 hours around both 2026 transitions
for eleven window lengths from 1 second to 30 days.

**Observed behaviour, not a contractual guarantee.** Everything in §1 is
inferred from the 2026-08-10 probes and from reconciliation against stored
data. Telematics publishes no timezone contract for these parameters, and how it
resolves an ambiguous local time is unknown — which is precisely why the
implementation is required to be correct under *every* resolution rather than
under an assumed one. If the provider changes this behaviour, the probe in §1.1
is the test that detects it.

### 1.4 Sub-window size

`fetch_trips` splits at `TRIPS_MAX_SUB_WINDOW_DAYS = 30`, not at the provider's
documented 31-day maximum, so that a DST-widened sub-window (30 days + at most
2 hours) still fits inside the provider limit. Measured worst case over a
75-day range straddling both transitions: 30 days + 1 hour of wall-clock span,
30 days of absolute span. In the scheduled path this is
defensive only: `jobs.api.telematics.sync_trips_and_speeding` already chunks the
execution window at `chunk_days` (default 2, maximum 5) before calling
`fetch_trips`.

---

## 2. The same contract for `/vehicles/events`

    GET /vehicles/events request  start_timestamp / end_timestamp  ->  Europe/Warsaw local wall-clock
    GET /vehicles/events response event_ts                         ->  UTC

This was measured **separately** from §1 and was not assumed from it. Until it
was measured, §4 listed it as an open hazard: with `/trips` corrected and
`/vehicles/events` left on the UTC projection, the two endpoints would have
been misaligned by the Warsaw UTC offset. That hazard is now closed.

### 2.1 Evidence

One host-executed GET-only diagnostic against `DELTA00001` on 2026-08-10, with
the expected result pre-registered before the requests were issued.

Reference: registration `EL5JV96`, provider trip `432316079`, running
2026-06-29 08:00:32–08:43:57 UTC (10:00:32–10:43:57 CEST), whose stored
provider-derived evidence is `speeding_140_160_count = 105`. Probe interval
07:58:32–08:45:57 UTC = 09:58:32–10:45:57 CEST.

| variant | wire window | rows returned | classification |
|---|---|---|---|
| A — current UTC serialization | `07:58:32` … `08:45:57` | 14, all at 06:43:54–06:45:57 UTC; **zero** inside the intended interval | `minus_offset` |
| B — Warsaw wall-clock | `09:58:32` … `10:45:57` | first page of 100, **all** inside the intended interval; 19 of the pre-registered expected evidence type; registration filter honored | `intended` |

Variant B's response was truncated at the first page — provider metadata
reported 289 rows over 3 pages. That limits only the *completeness*
measurement. It does not weaken the time-contract discrimination: every
observed Variant B row fell inside the intended absolute interval, and Variant
A fell exactly one Warsaw offset earlier.

As in §1, the response side needs no change. `_parse_provider_dt_optional`
already reads a naive `event_ts` as UTC, event→trip matching is
`trip.start_ts <= event_ts <= trip.end_ts` on absolute instants, and no
compensating offset exists anywhere in event parsing or enrichment — so
correcting the request window changes *which* events are fetched and nothing
else.

### 2.2 Implementation

`provider_client.vehicle_events_wire_window(start, end)` is the single
boundary, and both request paths use it:

- `fetch_vehicle_events_fleet` — the fleet-wide path used by normal enrichment;
- `fetch_vehicle_events_registration` — the registration-filtered fallback.

The fallback is deliberately **not** left on the old serialization: it runs
precisely when the fleet path is failing, so a divergence there would silently
change which events a degraded run collects. Both emit the same wire window for
the same intended absolute interval, and
`ops/tests_manual/test_telematics_vehicle_events_wire_time_contract.py` asserts
that directly.

`sub_window_label` stays the UTC label, so sub-window identity, budget
accounting and log correlation are unaffected. Each request logs
`telematics_vehicle_events_request_window` with the intended UTC window, the wire
window and the DST widening.

### 2.3 DST

The DST rule of §1.3 is endpoint-neutral: it depends only on the fact that the
provider addresses records by ambiguous local wall-clock, not on anything
specific to trips. `vehicle_events_wire_window` therefore reuses the same
`_wall_clock_wire_window` primitive and the same worst-case-reading invariant:

    max(readings(start_str)) <= start    and    min(readings(end_str)) >= end

Over-fetching is safe here for a different reason than for `/trips`. There the
argument is the `(client_id, provider_trip_id)` upsert; here it is that
`event_ts` is absolute and matching is absolute, so an event fetched outside
the intended interval simply matches no trip inside it, and duplicates are
dropped by the existing per-event dedupe keys.

`test_telematics_vehicle_events_wire_time_contract.py` proves the invariant for
summer, winter, the exact live-probe June window, both request paths, the
spring gap, the autumn fold spanned, both individual occurrences of the
repeated hour, either endpoint inside the fold, a short remainder chunk, and a
one-minute-grid sweep of 5,776 DST-adjacent window shapes at eight lengths from
1 second to the maximum accepted intended window.

### 2.4 Window limits

`/vehicles/events` rejects a request window of 24 hours or more — a limit
`/trips` does not share, and the reason the two wrappers stay separate.

A nominal window safely under the limit can grow on the wire:

- DST widening adds up to one Warsaw offset change (1 hour) per endpoint;
- a **spring-forward** crossing inflates the wall-clock numeral difference by a
  further hour relative to the absolute span.

`vehicle_events_wire_window` therefore checks all three quantities: the
intended absolute span against `VEHICLE_EVENTS_MAX_INTENDED_WINDOW` (21 h), and
both the post-widening absolute span and the wire wall-clock span against
`VEHICLE_EVENTS_MAX_REQUEST_WINDOW` (24 h). A violation raises `ValueError`,
not `TelematicsProviderSafetyError`, so it can never be absorbed by the adaptive
chunk-reduction paths that retry genuine provider failures.

So that this is never reached in practice, `_vehicle_events_chunk_delta` clamps
the configured chunk to the same 21-hour bound: an oversized
`TELEMATICS_EVENTS_CHUNK_HOURS` is split *before* conversion rather than rejected
by the provider mid-run. Production is nowhere near either bound — the default
chunk is 4 hours and the adaptive minimum is 30 minutes — so this is defensive
only. Production event windows were **not** enlarged by this change.

Chunk adjacency is unchanged and remains gap-free: a non-final chunk asks up to
one second before the next chunk starts, provider windows are inclusive at both
ends, and timestamps are second-granular. DST widening only extends chunks
outward, so it can add overlap but cannot open a hole.

### 2.5 Historical effect

Before this change, `/trips` and `/vehicles/events` were serialized by the
*same* `_provider_dt_str`, from the *same* `window_start_ts` / `window_end_ts`,
and were therefore shifted by the *same* Warsaw offset. Trips and their events
were consistently misaligned in the same direction, so events were not
misassigned to the wrong trips and stored counters were not systematically
undercounted; what the runs actually addressed was a window shifted an hour or
two earlier, which the next overlapping run re-covered.

See §4 for the remaining bounded exposure and the operational conclusion.

---

## 3. Rolling historical refresh

### 3.1 The mechanism already exists

`coverage_windows.derive_effective_window` computes

    E_start = max( min(F − L·86400 − D − O,  W − O),  E_end − R )

`min(..., W − O)` can only pull the start **earlier**; the coverage watermark
`W` never suppresses already-covered time. So `lookback_days` (`L`) already
produces a genuine historical re-request on every ordinary scheduled fire. No
new persistence, no new subsystem and no manual recovery authority is involved.

This is why the July 2026 ALPHA00001 gap was a configuration state, not a
missing capability: `L = 1` requested each trip date exactly once, about a day
after the fact, while Telematics publishes a tail of records days later.

### 3.2 What stays invariant

- **Forward completeness** is `covered_through_ts`. It advances under a strict
  `covered_through_ts < new` predicate (`docs/15`), so a rolling window can
  never move it backwards; a candidate at or behind it is a validated no-op.
- **Historical refresh horizon** is `L`, re-derived on every fire. It is not
  persisted and does not need to be.
- **Manual recovery authority** stays reserved for recovery outside the rolling
  envelope. Routine overlap is decided by the ordinary coverage gate, which
  returns `ALLOWED` for a window starting behind `W`.
- **`Dysponent_ID` survives re-upsert.** It is absent from the `ON CONFLICT
  … DO UPDATE SET` list and is never written by the trips sync job at all.
- **Failure is loud.** A budget exhaustion or a provider-safety violation
  raises `TelematicsProviderSafetyError` and discards the whole sub-window rather
  than returning a partial result that could be mistaken for a complete refresh.

### 3.3 Choosing the horizon

> **Reclassified — this table is an ordering proxy, and it understates the NEAR tail.**
> `docs/19_telematics_trips_late_arrival_audit.md` re-measured the same July 2026 cohort against
> actual logged request/response evidence. Read `docs/20` §3.7 for both distributions; the
> short version:
>
> * **Directly proven** (`docs/19` §5.1 — a complete earlier request covered the journey and
>   did not return the record): maximum absence **8.61 days**, and **zero** records proven
>   absent at 11 or 14 days. The near tail is denser than this table shows — 270 records still
>   provably absent at ≥ 1 day — but the ceiling below is **not** contradicted by direct
>   evidence.
> * **Inferential** (`docs/19` §5.4 — `provider_trip_id` allocation ordering, the same class of
>   instrument as this table): reaches 20.31 days, with 109 records at an inferred allocation
>   age ≥ 14 days. `docs/19` §9.2 labels the assumption unproven.
>
> So "13 days — none observed" survives the direct evidence and fails only the inferential
> reading. An earlier revision of this note asserted the line was outright false and cited
> "16 records provably still unpublished at 14 days"; **that figure appears in no retained
> evidence and is withdrawn** (`docs/20` §19.11). The table below is retained as the original
> measurement.
>
> The `lookback_days = 14` remedy below is **superseded** by `docs/20` §3, which splits the
> horizon across daily / weekly / monthly cadences. `docs/20` §16 decision 1 records the
> residual that choice leaves, bracketed across both instruments.

Measured Telematics publication lag for ALPHA00001, July 2026, from
`provider_trip_id` creation ordering:

| lag ≥ | share of records still unpublished |
|---|---|
| 1 day | 1.305% |
| 2 days | 0.615% |
| 5 days | 0.345% |
| 7 days | 0.166% |
| 10 days | 0.029% |
| 12 days | 0.008% |
| 13 days | none observed |

Two confirmed individual cases published at ≈8 and ≈12 days.

`lookback_days = 14` covers the entire observed distribution with two days of
margin. The effective window spans `L + O` = 14 d + 1 h, which `chunk_days = 2`
splits into 8 chunks — seven of two days plus a one-hour remainder — and each
chunk is one provider sub-window (well under `TRIPS_MAX_SUB_WINDOW_DAYS`). At
~5,700 trips/day that is ~12 pages per two-day chunk, so ~92 `/trips` requests
and ~98 requests per run in total, against the budgets in force of 500
pages/sub-window, 3,000 requests/endpoint and 5,000 requests/run. The tightest
budget that scales with the lookback is requests-per-endpoint (~33× headroom in
force, ~3.3× against the shipped default of 300); the tightest budget overall
is the compatibility per-sub-window elapsed limit of 900 s at ~9× headroom,
which does not scale with the lookback. Independently, the `R` clamp in
`derive_effective_window` binds at `L·86400 + O > R`, so **30 days is the
hard maximum lookback** under `R = 2,678,400 s`; beyond it the window is
silently shortened. A tiered short-window-plus-deep-catch-up design was considered
and rejected: at this request cost it would add orchestration complexity for no
measurable benefit.

---

## 4. Operational failure semantics

A run that cannot complete its rolling window fails; it does not report a
shorter window as complete. Run evidence in `logs` carries the requested UTC
window, the effective provider wire window and the DST widening
(`telematics_trips_request_window` for `/trips`,
`telematics_vehicle_events_request_window` for `/vehicles/events`), plus
per-sub-window pages, rows and budget accounting, and the job summary's
`trips_fetched` / `trips_rows_upserted`.

Three known operational hazards are **not** addressed by this change and remain
open follow-ups:

- **Bounded residual event exposure around a DST chunk boundary.** The
  `/vehicles/events` contract is no longer unproven (§2) and both endpoints now
  address the same absolute interval, so the hazard previously recorded here —
  `/trips` corrected while `/vehicles/events` stayed on the UTC projection — is
  closed. One narrow historical case survives it. Before the fix each event
  chunk was serialized independently, and the provider read each as Warsaw
  wall-clock; if a chunk boundary happened to fall exactly on a DST transition
  in the interpreted domain, adjacent chunks addressed intervals an hour apart,
  leaving up to a one-hour hole in event coverage. Trips in that hour would
  carry undercounted `speeding_*_count`, `high_rpm_events_count` and
  `overrev_events_count`. This is possible at most twice a year, is bounded to
  one hour, and does **not** apply to `ALPHA00001`, whose
  `event_enrichment_mode` is `disabled`. Where the schedule runs
  `overwrite_existing = true` and `trip_metrics_source` is API-owned, the
  on-conflict clause recomputes all five event-derived counters, so any
  subsequent run whose window re-covers the affected trips repairs them; under
  `DO NOTHING` it does not, and a targeted re-run would be needed. This is the
  only known event-history exposure and it is bounded — see the classification
  in the remediation record.

- **Missed scheduler fires are never caught up.** `latest_scheduled_fire_local`
  deliberately considers only the most recent fire. A host outage silently
  drops every fire it spans — this is what removed the 2026-07-30 and
  2026-07-31 `trips_sync` fires, during a dispatcher outage from 2026-07-29
  04:10 to 2026-08-01 15:02. A wider `lookback_days` repairs the *data* on the
  next successful run but does not restore the missed fires themselves.
- **Code that reads a column may be deployed before the migration that adds
  it.** On 2026-08-02 every dispatcher run failed with
  `UndefinedColumn: column "trips_pagination_mode" does not exist`. Migration
  ordering is a deployment-gating concern, not a code defect.
