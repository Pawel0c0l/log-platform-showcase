# CLIENT_TRIPS_DATA_RELIABILITY_AUDIT

Read-only audit of the trip records ingested from the Telematics provider `/trips`
endpoint and persisted in the per-client `public.client_trips` tables.

- **Executed:** 2026-08-30
- **Method:** read-only SQL against the four production client-business databases
  (`SET default_transaction_read_only = on` on every session) plus static reading
  of the ingestion path. No production row was written, deleted, corrected or
  re-imported; no provider request was issued; no filtering logic was added.
- **Scope note:** this is an audit only. Section 12 proposes a candidate policy;
  nothing in it is implemented.

---

## 1. Executive summary

**Verdict: mostly reliable, with a small, highly concentrated class of severely
corrupted distance values that materially distorts per-vehicle and per-driver
metrics while barely moving fleet-level totals.**

The dataset is internally consistent to an unusual degree. Timestamps are
correct, there are no duplicates at all, no negative durations, no negative
distances, and no impossible coordinate values. 95.34% of trips raise no quality
flag of any kind under the rule set in §6.

The single dominating fact of this audit is:

> `end_odometer_value − start_odometer_value = trip_distance_meters` holds
> **exactly, for all 1,224,652 trips where both are present — 100.0000%, zero
> exceptions.**

Distance is therefore **not an independent measurement**. It is the delta of the
device odometer register. Consequently the odometer is worthless as a
cross-check of distance (they are the same number), and every odometer register
fault becomes, one-for-one, a corrupted trip distance. Every severe distance
anomaly found in this dataset is an odometer-register fault.

Materiality:

| Level | Effect |
|---|---|
| Fleet total distance | **−1.21%** if all high-confidence invalid records are excluded (241,610 km of 20,015,959 km) |
| Mean distance/trip | 16.344 km → 16.177 km (**−1.02%**) |
| Concentration | 60.4% of all invalid distance sits in **5 vehicles**; 4 vehicles alone carry 141,720 km |
| Per-driver Eco metrics (ALPHA00001) | 315 of 2,491 driver-months (12.6%) contain ≥1 invalid trip; **9 driver-months have their event-per-100 km rate understated by 10.6% to 3,300%** |

So: cosmetic at fleet level, **materially wrong at driver/vehicle level**, which
is exactly where the Eco Driving product makes its decisions. A single bogus
39,948 km trip silently makes one driver look 288% safer for a whole month.

There is also a **second, opposite error** the dataset hides: 172,508 trips
(14.08%) record distance `0`, and 22,295 of them have measurable GPS
displacement. Distance is under-reported at least as often as it is
over-reported. Any policy that only trims the upper tail will bias totals down.

---

## 2. Dataset scope

**The audit covers the entire available dataset. Nothing was sampled.**

| | |
|---|---|
| Trip records examined | **1,225,535** |
| Distinct clients | **4** (a 5th client database, `echogallery_main`, has the table but 0 rows) |
| Distinct vehicles | 1,895 registrations / 1,837 `(client, vehicle_id)` streams |
| Earliest trip start | 2026-03-31 22:01:42 UTC |
| Latest trip start | 2026-08-29 00:56:43 UTC |
| Span | 150 days, 151 distinct calendar days, 5 calendar months |
| Total recorded distance | **20,015,958.96 km** |

Per client:

| Client | DB | Trips | Vehicles | First trip | Last trip | Recorded km |
|---|---|---:|---:|---|---|---:|
| ALPHA00001 | `alpha_main` | 873,924 | 1,401 | 2026-04-01 | 2026-08-29 | 15,191,536 |
| FOXTROT00001 | `foxtrot_main` | 232,493 | 318 | 2026-04-20 | 2026-08-29 | 2,796,043 |
| DELTA00001 | `delta_main` | 64,545 | 80 | 2026-04-20 | 2026-08-28 | 917,366 |
| BRAVO00016 | `telematics_main` | 54,573 | 96 | 2026-04-01 | 2026-08-23 | 1,111,014 |

By month (all clients, trip start in Europe/Warsaw):

| Month | Trips | km | ALPHA | FOXTROT | DELTA | BRAVO |
|---|---:|---:|---:|---:|---:|---:|
| 2026-04 | 209,838 | 3,333,323 | 2,871,079 | 184,472 | 59,999 | 217,774 |
| 2026-05 | 240,539 | 3,892,461 | 3,089,966 | 347,113 | 224,429 | 230,953 |
| 2026-06 | 268,609 | 4,284,713 | 3,069,953 | 749,077 | 232,227 | 233,455 |
| 2026-07 | 277,049 | 4,580,463 | 3,268,323 | 826,972 | 222,845 | 262,323 |
| 2026-08 (partial) | 229,500 | 3,924,999 | 2,892,216 | 688,409 | 177,866 | 166,508 |

Trip mode: 1,185,589 `business` (19,532,693 km) / 39,946 `private` (483,266 km).
No NULLs.

Per-vehicle volume: median 625 trips and 10,037 km per vehicle over the period
(p25 413 / 6,623 km; p95 1,263 / 21,737 km; max 2,353 / 107,390 km).

**Authoritative table:** `public.client_trips` in each client-business database,
primary key `(client_id, provider_trip_id)`. `public.client_trips_legacy_backup_021`
exists in `telematics_main` as a migration rollback artifact and is **not**
authoritative. `eco_trip_assignments` / `eco_person_trip_assignments` are
downstream derivations, not sources.

---

## 3. Data provenance

### 3.1 Field origin

Established by reading `jobs/api/telematics/sync_trips_and_speeding.py`, not from
field names.

| Column | Origin | Transformation |
|---|---|---|
| `provider_trip_id` | API `trip_id` | integer parse |
| `trip_distance_meters` | API `trip_distance` | **none — verbatim copy** (`sync_trips_and_speeding.py:4350`, `:5447`) |
| `trip_duration_seconds` | API `trip_duration_seconds` | **none — verbatim copy** (`:5493`) |
| `start_timestamp` / `end_timestamp` | API `start_timestamp` / `end_timestamp` | ISO parse; naive strings treated as UTC (`_parse_provider_dt`, `:595`) |
| `start_latitude/longitude` | API `start_coordinates.{latitude,longitude}` | float parse; NULL if the nested object is absent (`_extract_coords`, `:1143`) |
| `end_latitude/longitude` | API `end_coordinates.{…}` | as above |
| `start_odometer_value` / `end_odometer_value` | API `start_odometer_value` (fallbacks `start_odometer`, `odometer_start`) | integer parse, negatives rejected (`_extract_odometer_value`, `:703`) |
| `vehicle_id`, `registration`, `chassis_number`, `*_location`, `*_geofence_name` | API verbatim | — |
| `trip_mode` | API `is_private` | mapped to `private`/`business` |
| `Driver_Restrictions` | bulk `GET /drivers` → `license_driver_restrictions` | **locally joined**, not a trip field |
| `high_rpm_events_count`, `overrev_events_count` | `GET /vehicles/events` | **locally aggregated per trip** |
| `speeding_*_count` | speeding notifications | **locally aggregated per trip** |
| `record_id`, `synced_at`, `sync_run_id`, `first_seen_*` | local | ingestion provenance |

**No unit conversion, scaling, rounding or arithmetic is applied to distance,
duration or odometer anywhere in the ingestion path.** A grep of the whole
repository confirms `trip_distance` is read in exactly two places, both plain
`dict.get`.

### 3.2 Units

- `trip_distance_meters` — **metres**. Confirmed by magnitude (median 4,000, i.e.
  4 km per trip) and by the exact identity with the odometer delta below.
- `start/end_odometer_value` — **metres**. Median start odometer 49,022,000
  (49,022 km); p99 191,066,470 (191,066 km). These are plausible vehicle
  lifetime odometers only in metres.
- `trip_duration_seconds` — seconds.

### 3.3 The critical provenance finding: distance IS the odometer delta

```
rows with both trip_distance_meters and (end_odometer − start_odometer): 1,224,652
rows where they are exactly equal:                                       1,224,652  (100.0000%)
maximum absolute discrepancy:                                                    0
```

Since the ingestion code copies all three fields independently and never
computes any of them, this identity can only exist in the provider payload. It
means:

1. **Distance is odometer-derived, not GPS-derived.** There is no separate
   "GPS distance", "driven distance" or "business/private distance" field —
   §D of the brief has no data to test, because the odometer is the distance.
2. **Odometer cannot validate distance.** The brief asks whether odometer data
   is a usable validation signal. It is not; it is the same number.
3. What *is* usable is **odometer continuity across consecutive trips of the
   same vehicle** — a genuinely independent signal, exploited in §5.2.

Timestamp semantics: the provider request takes Europe/Warsaw wall-clock while
response timestamps are UTC (`docs/18`). The parser treats naive strings as UTC,
which matches. Empirically confirmed: trip-start hour-of-day in Europe/Warsaw
peaks at 12:00 with a normal 07:00–17:00 working profile, and 0 trips have
`end < start`. **No timezone defect.**

### 3.4 Records are mutable after ingestion, and revisions leave no trace

The upsert is `ON CONFLICT (client_id, provider_trip_id) DO UPDATE`, and
`trip_distance_meters` is in the `DO UPDATE SET` list
(`sync_trips_and_speeding.py:5629`). Measured:

- 298 distinct `sync_run_id` values; `synced_at` spans 2026-04-29 → 2026-08-29.
- Of the 121,167 rows (9.89%) carrying first-seen provenance,
  **74,993 (6.12% of all rows) were re-upserted at least 60 s after first sight**,
  typically ~48 h later (p50 47.9 h, p90 48.0 h) — the rolling refetch window.

So the provider *can* revise a trip and the platform *will* overwrite it. **No
before/after history is kept**, so a provider-side distance revision is
undetectable after the fact. This is a provenance gap, not a defect observed to
have fired.

### 3.5 Documentation vs implementation

No `DOCUMENTATION_CODE_MISMATCH` found for the audited fields. `docs/05_jobs.md`
§ "Finalna logiczna kolejność kolumn" matches the live schema in all four
databases (identical column sets and order). One **undocumented semantic**:
nothing in the repository documentation states that `trip_distance_meters` is an
odometer delta. That is a material omission — every downstream consumer treats it
as a measured trip distance.

---

## 4. Baseline distribution

### 4.1 Distance (metres, n = 1,224,652 non-null)

| Statistic | Value |
|---|---:|
| null | 883 (0.0721%) |
| zero | 172,508 (14.076%) |
| negative | **0** |
| positive | 1,052,144 (85.85%) |
| min | 0 |
| p25 | 1,000 m |
| **median** | **4,000 m** |
| p75 | 15,000 m |
| p90 | 44,000 m |
| p95 | 73,000 m |
| p99 | 175,595 m |
| p99.9 | 338,270 m |
| p99.99 | 499,686 m |
| **max** | **89,472,993 m (89,473 km)** |
| mean | 16,344 m |
| total | 20,015,958.96 km |

Robust statistics: median 4,000 m, **MAD 4,000 m**, IQR 14,000 m (Q1 1,000,
Q3 15,000). Tukey fences: Q3+1.5·IQR = 36 km, Q3+3·IQR = 57 km. **88,132 trips
(7.196%) sit above Q3+3·IQR** carrying 10,174,340 km — i.e. **half the fleet's
total distance is "statistically extreme"**. This is precisely why raw Tukey/IQR
rules are useless here: the distribution is strongly log-shaped, and long
motorway trips are the business.

On log10 scale (non-zero distances): mean 3.783, sd 0.701. mean+5σ = 19,381 km —
only **2 trips** exceed it. The log-scale tail is far better behaved than the
linear one and already isolates the two worst records.

### 4.2 Duration (seconds, n = 1,225,035 non-null)

| Statistic | Value |
|---|---:|
| null | 500 (0.0408%) |
| zero | 384 (0.0313%) |
| negative | **0** |
| p25 | 243 s |
| **median** | **644 s (10.7 min)** |
| p75 | 1,549 s |
| p90 | 3,043 s |
| p95 | 4,318 s |
| p99 | 7,871 s |
| p99.9 | 14,043 s |
| max | 86,037 s (23.9 h) |

Median 644 s, MAD 497 s, IQR 1,306 s.

`end_timestamp − start_timestamp` equals `trip_duration_seconds` for
**1,225,021 rows (99.9581%)**. All 14 exceptions differ by *exactly* 86,400 s
(see §5.1).

### 4.3 Whole-trip average speed (n = 1,224,490 computable)

| p50 | p75 | p90 | p95 | p99 | p99.9 | p99.99 | max |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25.5 | 41.4 | 59.7 | 73.0 | 102.4 | 148.4 | **1,689.6** | **108,855** |

The p99.9→p99.99 jump from 148 km/h to 1,690 km/h is the sharpest discontinuity
in the whole dataset and is the natural empirical boundary between "fast trip"
and "corrupt record".

### 4.4 Field availability

| Field | NULL | % |
|---|---:|---:|
| `start_latitude`/`longitude` | 235,930 | 19.251% |
| `end_latitude`/`longitude` | 236,155 | 19.270% |
| either endpoint missing | 236,826 | 19.324% |
| `start`/`end_odometer_value` | 383 | 0.031% |
| `trip_duration_seconds` | 500 | 0.041% |
| `trip_distance_meters` | 883 | 0.072% |

Missing coordinates are **stable across months** (40.5k–52.1k per month) and
**concentrated by vehicle**: 300 of 1,837 vehicle streams have >50% of their
trips with no coordinates. This is a device/fleet property, not a time-based
regression.

### 4.5 Distance quantization — a structural property, not an anomaly

| Divisible by | Share of non-zero distances |
|---|---:|
| 10 m | 94.74% |
| 100 m | 79.59% |
| **1,000 m** | **59.01%** |

The 15 most frequent non-zero distance values are exactly 1 km, 2 km, … 15 km
(90,648 / 66,564 / 51,727 / … occurrences). This is **not** "suspicious
constants" — it is the reporting resolution of the odometer register, and it
varies per vehicle:

| Odometer step | Vehicle streams | Trips | km |
|---|---:|---:|---:|
| 1,000 m | 1,100 | 591,384 | 11,260,990 |
| 100 m | 130 | 97,624 | 1,768,487 |
| 10 m | 255 | 157,239 | 3,101,518 |
| 1 m | 348 | 205,897 | 3,884,965 |

Distribution by client is very uneven — DELTA00001 has **no** 1 km-step vehicles
(53 of 80 at 100 m), FOXTROT00001 has 276 of 318 at 1 km step. **531 of 1,789
vehicles change their quantization step between months**, so resolution is not
even stable per device.

Two consequences, both quantified below:
- 1 km-step vehicles have a **16.78% zero-distance rate** vs 6.88–7.15% for
  10 m/100 m vehicles (§5.7);
- 278 trips are flagged as >130 km/h purely because a ≤2 km quantized distance
  is divided by a ~15 s duration (§5.3).

Crucially, **quantization does not bias aggregate totals**. Because distance is
an odometer delta, consecutive trips telescope: for any vehicle,
Σ(trip distances) = last_odometer − first_odometer − Σ(gaps). Rounding error is
redistributed between adjacent trips, never accumulated.

---

## 5. Anomaly taxonomy

Discovered classes, in descending order of evidential strength.

### 5.1 STRUCTURAL — internally contradictory records

| Class | Count | Evidence |
|---|---:|---|
| `trip_distance_meters` NULL | 883 | 15 vehicles, 2 clients |
| `trip_duration_seconds` NULL | 500 | 6 vehicles, BRAVO00016 only |
| duration ≠ (end − start) | **14** | difference is **exactly +86,400 s in all 14** |
| `end == start` (zero elapsed) | 384 | all have distance 0 — benign |
| overlapping trips, same vehicle | **8** | 0.0007% |
| end before start | **0** | — |
| negative distance / duration | **0** | — |

The 14 timestamp cases are a clean signature: `end_timestamp` is one calendar day
later than the duration implies. Because `trip_duration_seconds` is a separate
provider field and agrees with everything else, `end_timestamp` is the wrong
one. Example: trip `432210151` (BRAVO00016) — start 2026-06-27 09:04:56Z, end
2026-06-28 12:13:15Z (97,699 s elapsed) but `trip_duration_seconds = 11,299`.
Distance 12,000 m, odometer continuous on both sides.

The 883 NULL-distance rows split into two distinct cohorts:

- **Cohort A — 500 rows / 6 BRAVO00016 vehicles:** distance *and* duration NULL,
  **but odometer present**. Odometer delta yields **6,955 km of real distance
  silently missing from totals** (p50 5 km, max 208 km per trip).
- **Cohort B — 383 rows / 9 vehicles (8 BRAVO00016 + 1 ALPHA00001):** distance and
  odometer both NULL, duration present. Nothing recoverable.

Vehicle 39816049 (BRAVO00016) alone accounts for 454 of the 500 Cohort-A rows —
52.9% of that vehicle's 859 trips are unusable. This is a **vehicle-specific
systemic failure**, not random noise.

### 5.2 ODOMETER-REGISTER FAULT — the root cause of every severe distance error

Because distance ≡ odometer delta, an odometer glitch *is* a distance error.
Continuity of the register across consecutive trips of the same vehicle is the
only genuinely independent signal available:

```
consecutive same-vehicle trip pairs:                     1,223,216
start_odo(n) == end_odo(n−1) exactly:                    1,193,877  (97.601%)
positive gap (odometer advanced between trips):             27,314  ( 2.233%)
negative gap (odometer went backwards):                      2,025  ( 0.166%)
1,531 of 1,895 vehicles show at least one discontinuity
```

Two failure modes were isolated and confirmed by inspecting the raw trip
sequences:

**Mode 1 — transient spike (self-correcting).** The end odometer jumps, the next
trip inherits the inflated value, then the register reverts to its true baseline.

> Vehicle `39685202`, trip `426236330`: odometer 56,510,570 → 56,942,766 in
> **15 seconds** ⇒ recorded distance 432,196 m (432 km), implied average speed
> **103,727 km/h**. Start and end coordinates are 5 m apart. The next trip
> (`426242277`) starts at the inflated 56,942,766; the one after
> (`426245902`) starts at **56,510,570** — the correct pre-glitch value.
> The entire 432 km is fabricated.

**Mode 2 — persistent step (permanent offset).** The register jumps and never
returns; all subsequent trips are correct *relative to each other* but the
vehicle's lifetime odometer is permanently wrong.

> Vehicle `39804957`, trip `422993873`: odometer 3,607,233 → 43,555,460 in
> **55 minutes** ⇒ 39,948,227 m (39,948 km) at **43,265 km/h**. Every subsequent
> trip continues from 43.5 M. Geodesic displacement for that trip is 52.6 km —
> consistent with a real ~60 km journey mis-measured by a factor of ~660.

Of the 827 trips averaging >200 km/h, **753 (91.1%) are Mode 2** (odometer never
reverts) and 26 are Mode 1; 39 are intermediate and 9 not classifiable.

**Failure-mode counts by gap magnitude:**

| Positive gap size | n | km |
|---|---:|---:|
| 0–100 m | 8,764 | 251 |
| 100 m–1 km | 10,239 | 2,654 |
| 1–10 km | 5,607 | 16,558 |
| 10–100 km | 1,526 | 47,184 |
| 100–1,000 km | 870 | 367,612 |
| >1,000 km | 308 | 5,729,246 |

| Negative gap size | n | km |
|---|---:|---:|
| 0–100 m | 214 | 7 |
| 100 m–1 km | 349 | 153 |
| 1–10 km | 1,015 | 3,227 |
| 10–100 km | 285 | 10,089 |
| 100–1,000 km | 151 | 35,871 |
| >1,000 km | **11** | **5,402,706** |

Positive and negative extremes very nearly cancel (6,163,504 km vs 5,452,053 km),
confirming that the huge gaps are spikes that revert rather than real travel.

**Register saturation confirmed:** the value **2,684,354,550** appears as both a
start and an end odometer on 2 trips (2 ALPHA00001 vehicles). `2,684,354,550 =
(2²⁸ − 1) × 10` — a saturated 28-bit counter in decimetres. This is a hardware
register limit, not a data error in the platform.

### 5.3 PHYSICALLY IMPLAUSIBLE AVERAGE SPEED

Whole-trip average = `trip_distance_meters / (end − start)`. Note this is an
average over the *entire* trip including stops, so any value above ~130 km/h is
already extraordinary for a Polish fleet.

| Threshold | Trips | % of trips | km | % of total km | Vehicles | Clients |
|---|---:|---:|---:|---:|---:|---:|
| >110 km/h | 7,145 | 0.5830% | 1,201,910 | 6.005% | 1,273 | 4 |
| >130 km/h | 1,760 | 0.1436% | 302,594 | 1.512% | 646 | 4 |
| >160 km/h | 1,093 | 0.0892% | 228,870 | 1.143% | 422 | 4 |
| **>200 km/h** | **827** | **0.0675%** | **215,948** | **1.079%** | **325** | **4** |
| >300 km/h | 542 | 0.0442% | 204,502 | 1.022% | 233 | 4 |
| >500 km/h | 339 | 0.0284% | 189,062 | 0.945% | 174 | 4 |
| >1,000 km/h | 197 | 0.0161% | 173,734 | 0.868% | 115 | 4 |

Short-duration / large-distance combinations:

| Condition | Trips | km | Vehicles |
|---|---:|---:|---:|
| <5 min & >20 km | 157 | 15,989 | 90 |
| <5 min & >50 km | 86 | 13,655 | 55 |
| <10 min & >50 km | 144 | 24,689 | 74 |
| <10 min & >100 km | 85 | 20,604 | 57 |
| <30 min & >200 km | 64 | 25,133 | 47 |
| <30 min & >300 km | 26 | 15,639 | 23 |

**The speed>200 set must be split by recorded distance**, because quantization
manufactures spurious speed on tiny trips:

| Recorded distance | Trips | km | % of total km |
|---|---:|---:|---:|
| ≤1 km | 62 | 61.5 | 0.0003% |
| 1–5 km | 113 | 313.6 | 0.0016% |
| 5–20 km | 179 | 2,141 | 0.0107% |
| 20–100 km | 242 | 12,471 | 0.0623% |
| 100–500 km | 209 | 43,519 | 0.2174% |
| >500 km | **22** | **157,442** | **0.7866%** |

Of the 62 sub-1 km cases, **59 have distance exactly 1,000 m** and a median
duration of 14.5 s — pure 1 km-quantization artifacts with a maximum possible
error of 1 km each. They are *flags*, not *distance corruption*. Meanwhile
**22 trips carry 73% of all the anomalous distance.**

Temporal distribution of >160 km/h trips is flat: 190 / 198 / 256 / 266 / 183
across April–August. **No API-version or ingestion-batch effect.**

### 5.4 DISTANCE vs GPS DISPLACEMENT — mostly a false lead

Haversine displacement between start and end coordinates was computed for the
988,709 trips (80.68%) that have both endpoints.

`recorded_distance / straight_line_distance` percentiles:

| p05 | p25 | p50 | p75 | p90 | p95 | p99 | p99.9 | max |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.06 | 1.23 | 1.41 | 1.77 | 2.75 | 4.98 | **992.6** | 13,301 | 339,997 |

A median route/straight-line ratio of 1.41 is exactly what real road networks
produce — strong evidence the bulk of the data is genuine.

**The ratio must not be used as a validity test, and this dataset proves why.**
Of the 9,905 trips with displacement ≤50 m and distance >5 km:

- **9,703 (98.0%) have an average speed ≤90 km/h** — ordinary **round trips**
  that return to their origin, e.g. trip `423091569`: 602 km over 12 h 16 min at
  49.1 km/h, displacement 7 m. Perfectly legitimate.
- only 149 exceed 130 km/h and 134 exceed 200 km/h.

So the geodesic signal is only useful **in combination with duration**.

**The inverse test is far more informative.** 141,047 trips (11.5%) record a
distance *shorter* than their own straight-line displacement — physically
impossible. Median deficit is only 0.15 km (quantization + GPS noise), but
restricting to `geo > 5 km` and `distance > 0` leaves **1,484 genuinely
impossible records (22,703 km)**, median deficit 5.7 km, max 465 km.

Inspection shows this class has two causes:
- **stale start coordinate after a coverage gap** — e.g. FOXTROT00001 trip
  `429833804`: 104 km recorded, 569 km geodesic, and `odo_gap = +2,316,000`
  (2,316 km of movement occurred before this trip was recorded, so the start
  position is 2,316 km out of date);
- **genuinely truncated trip records** (7.0% of the class carries an odometer
  discontinuity).

### 5.5 COORDINATE ANOMALIES — device cold-start, distance unaffected

- Latitude/longitude out of valid range: **0**
- Null-island (0,0): **0**
- Either coordinate exactly 0: **0**
- **Outside the plausible operating region** (lat 35–72, lon −12–45): **59 trips,
  25 vehicles, 2 clients** — total recorded distance **7 km**.

The pattern is unmistakable. All affected vehicles have high `vehicle_id`s
(459,xxx,xxx–460,xxx,xxx = recently provisioned devices) and each shows a short
run of trips at one fixed garbage position, followed by a "trip" that appears to
travel thousands of kilometres to the real Polish fleet location as soon as the
GPS gets its first fix:

| Vehicle | Garbage fix | Trips there | First real fix |
|---|---|---:|---|
| 459958839 | −78.2573, 86.0426 (Antarctica) | 2 | 51.968, 20.578 |
| 459958833 | 1.1104, 123.7047 (Indonesia) | 3 | 51.968, 20.580 |
| 460327065 | 23.0126, 114.3407 (China) | 5 | 52.151, 21.026 |
| 460161150 | −19.5016, 81.1920 (Indian Ocean) | 3 | 52.150, 21.025 |

Maximum apparent geodesic displacement: 15,120 km (trip `418183430`) against a
recorded distance of **1 km**. **Distance is correct; the coordinates are not.**
This class costs nothing in kilometres but poisons any purely geodesic rule.

Missing coordinates show **no** correlation with distance corruption: the
>200 km/h rate is 0.0587% among coordinate-less trips vs 0.0696% among trips with
coordinates.

### 5.6 DUPLICATES — none

Every duplication hypothesis in the brief was tested and returned **zero**:

| Test | Result |
|---|---|
| duplicate `(client_id, provider_trip_id)` | 0 (enforced by PK) |
| `provider_trip_id` reused across clients | 0 |
| same `(client, vehicle_id, start_ts, end_ts)` | **0** |
| same `(client, vehicle_id, start_ts)` | **0** |
| same `(client, registration, start_ts, end_ts, distance)` | **0** |
| overlapping trips, same vehicle | 8 (0.0007%), 45 km |
| one long trip alongside its constituents | not observed |

Repeated ingestion is structurally harmless: the upsert is keyed on the provider
trip id, so pagination retries and the 48 h rolling refetch produce idempotent
updates, not new rows. **Duplicate accumulation contributes 0 km of inflation.**

### 5.7 ZERO, TINY AND MISSING DISTANCE — the under-count nobody is looking at

| Band | Trips | % of trips | km | % of total km |
|---|---:|---:|---:|---:|
| NULL | 883 | 0.072% | — | — |
| **exactly 0** | **172,508** | **14.076%** | 0 | 0% |
| 0 < d ≤ 10 m | 3,297 | 0.269% | 30.6 | 0.0002% |
| 10–50 m | 6,706 | 0.547% | 205.4 | 0.0010% |
| 50–100 m | 9,593 | 0.783% | 877.4 | 0.0044% |
| 100–500 m | 26,091 | 2.129% | 8,405.1 | 0.0420% |
| 500–1,000 m | 119,149 | 9.722% | 112,064.2 | 0.5599% |
| negative | **0** | — | — | — |

Zero-distance trips are **not** all ignition-noise. Median duration is 60 s, but:

- **22,295** have GPS displacement > 200 m,
- **5,507** have GPS displacement > 1 km,
- **2,240** have GPS displacement > 5 km, totalling **≥141,444 km of movement
  recorded as zero distance**,
- **10,810** last longer than 10 minutes.

Zero-distance rate by odometer resolution proves the mechanism:

| Odometer step | Trips | Zero-distance rate | Zero + displacement >200 m |
|---|---:|---:|---:|
| 1,000 m | 711,298 | **16.78%** | 14,866 |
| 100 m | 105,482 | 7.15% | 532 |
| 10 m | 168,855 | 6.88% | 985 |
| 1 m | 239,891 | 14.17% | 5,912 |

A vehicle whose odometer reports whole kilometres records `0` for every trip
that does not cross a kilometre boundary. **Short trips are systematically
under-measured, and the effect is device-dependent.** Do not treat tiny/zero
distance as invalid — it is the same measurement instrument working as designed
at its resolution limit.

### 5.8 COVERAGE GAPS — distance the dataset never saw

Positive odometer gaps with a physically plausible implied speed over the idle
interval (≤100 km/h) — i.e. real movement between recorded trips:

```
26,696 gap events, 869,706 km  (≈4.3% of recorded total)
```

Median gap 150 m, p90 9.7 km; median idle interval between the trips 44 min.
Per client: ALPHA00001 dominates. This is movement that happened (the odometer
proves it) but for which no trip row exists — towing, ignition-off roll, trips
the provider did not return, or ingestion windows that missed them.

### 5.9 Systematic-vs-random classification

| Question | Answer |
|---|---|
| Random? | **No.** 60.4% of invalid distance sits in 5 vehicles; 73.4% in 20. |
| Vehicle-specific? | **Yes, strongly.** 513 of 1,837 vehicle streams carry ≥1 high-confidence anomaly; only 91 of 1,821 vehicles with ≥50 trips exceed a 1% anomaly rate, and 12 exceed 5%. Worst: BRAVO00016 `39816049` at 52.9% (NULL-distance cohort). |
| Client-specific? | **Partly.** All 4 clients are affected. Per-client invalid-distance share: ALPHA00001 1.40%, FOXTROT00001 0.65%, BRAVO00016 0.62%, DELTA00001 0.36%. |
| Device/firmware-specific? | **Suggestive.** Anomaly rate by `vehicle_id` band: <38 M 0.157%, 38–40 M 0.337%, 40–42 M 0.145%, 100–459 M 0.230%, 459 M+ 0.478%. The newest devices are the worst, and the cold-start GPS garbage (§5.5) is exclusively theirs. The dataset carries no device-model or firmware column, so this cannot be closed. |
| Time-period-specific? | **No.** Monthly high-confidence anomaly rate: 0.30 / 0.25 / 0.26 / 0.28 / 0.21%. Stable. |
| API-version / ingestion-specific? | **No evidence.** Anomalies are spread across 298 sync runs with no clustering; the pagination-contract change of 2026-07-29–08-01 produced no step in the anomaly rate. |
| Systemic? | **Yes, in mechanism** — every client, every month, driven by one shared cause (odometer-derived distance). |

---

## 6. Quantitative anomaly table

Detection rules are evaluated over all 1,225,535 trips.
`avg_speed = trip_distance_meters / (end_timestamp − start_timestamp)`;
`geo` = haversine start→end.

| # | Anomaly class | Detection rule | Count | % trips | Vehicles | Clients | Distance affected | % of total km | Confidence |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| R1 | Missing distance | `trip_distance_meters IS NULL` | 883 | 0.0721% | 15 | 2 | — | — | **Structural** |
| R2 | Timestamp/duration contradiction | `abs((end−start) − trip_duration_seconds) > 0` | 14 | 0.0011% | 12 | 3 | 598.8 km | 0.0030% | **Structural** |
| R3 | Impossible speed, material distance | `avg_speed > 200 km/h AND dist > 5 km` | 646 | 0.0527% | 229 | 4 | 215,543 km | 1.0769% | **Strong** |
| R4 | Impossible speed, any distance | `avg_speed > 300 km/h` | 542 | 0.0442% | 233 | 4 | 204,502 km | 1.0217% | **Strong** |
| R5 | Extreme distance at impossible speed | `dist > 500 km AND avg_speed > 130 km/h` | 26 | 0.0021% | 26 | 4 | 159,966 km | 0.7992% | **Strong** |
| R6 | Distance below own displacement | `0 < dist < 0.9 × geo AND geo > 5 km` | 1,484 | 0.1211% | 374 | 4 | 22,703 km | 0.1134% | **Strong** |
| R7 | Suspicious speed | `130 < avg_speed ≤ 200 km/h` | 933 | 0.0761% | 481 | 4 | 86,646 km | 0.4329% | Medium |
| R8 | Speed artifact of quantization | `avg_speed > 200 km/h AND dist ≤ 5 km` | 181 | 0.0148% | 140 | 4 | 405 km | 0.0020% | Medium |
| R9 | Odometer discontinuity at trip start | `start_odo ≠ prev_end_odo` (same vehicle) | 29,339 | 2.3940% | 1,531 | 4 | 605,235 km | 3.0238% | Weak |
| R10 | Large but plausible distance | `dist > 300 km AND avg_speed ≤ 130 km/h` | 2,012 | 0.1642% | 719 | 4 | 735,392 km | 3.6740% | Weak (statistical only) |
| R11 | Coordinates outside operating region | lat∉[35,72] or lon∉[−12,45] | 59 | 0.0048% | 25 | 2 | 7 km | 0.0000% | **Strong** (coordinate only) |
| R12 | Zero distance despite movement | `dist = 0 AND geo > 200 m` | 22,295 | 1.8192% | 1,534 | 4 | 0 km (**under-count**) | — | Medium (under-count) |
| I1 | No coordinates | `geo IS NULL` | 236,826 | 19.3243% | 751 | 4 | 3,419,381 km | 17.0833% | Informational |
| I2 | Zero distance | `dist = 0` | 172,508 | 14.0761% | 1,893 | 4 | 0 km | — | Informational |

### Flags vs unique trips

| | |
|---|---:|
| Total rule firings (R1–R12) | **58,414** |
| Unique flagged trips | **57,108 (4.6598%)** |
| Flags per flagged trip | 1.023 |
| Rule overlaps | R3∩R4 = 461, R3∩R5 = 22, R4∩R5 = 21 |

### Deduplicated severity summary

| Tier | Trips | % of trips | Distance | % of total km | Vehicles |
|---|---:|---:|---:|---:|---:|
| **INVALID** (R1–R6) | **3,111** | **0.2538%** | 241,610 km | **1.2071%** | 492 |
| **WARN only** (R7–R12, not already invalid) | 53,997 | 4.4060% | 1,301,097 km | 6.5003% | 1,859 |
| **CLEAN** (no flag) | **1,168,427** | **95.3402%** | 18,473,251 km | 92.2926% | 1,895 |

Trips carrying only weak signals (R9/R10/R12 and nothing else) account for
52,921 of the 57,108 flagged trips — i.e. **93% of flagged trips carry only a
statistical or under-count warning, not a demonstrable defect**.

---

## 7. Cross-signal / high-confidence anomalies

Three signals are genuinely independent: **duration** (provider field, agrees
with timestamps 99.96% of the time), **GPS displacement** (independent sensor),
and **odometer continuity across trips** (independent of any single trip's
distance).

Cross-tabulation of the 827 trips averaging >200 km/h:

| Independent corroboration | Trips |
|---|---:|
| coordinates available | 688 |
| … of which displacement <1 km | 435 |
| … of which displacement <50 m | 273 |
| odometer discontinuity on the prior side | 30 |
| odometer discontinuity on the next side | 121 |
| discontinuity on either side | 144 |
| **both sides continuous (persistent register step)** | **679** |

The large "both sides continuous" number is not a weakness of the test — it is
the *finding*: 91% of these faults are Mode-2 permanent register steps, where the
corrupted value is baked into the vehicle's odometer chain and only the speed and
geodesic tests can see it.

**Severity model derived from this dataset:**

| Severity | Definition | Trips | km | % of total km |
|---|---|---:|---:|---:|
| **Strong** | ≥2 independent signals, or a physically impossible single signal (R3/R4/R5/R6/R11), or structural contradiction (R1/R2) | 3,158 | 241,615.5 | 1.2071% |
| **Medium** | 1 suspicious signal, corroboration unavailable (R7/R8, NULL duration) | 1,029 | 84,285.2 | 0.4211% |
| **Weak** | statistically unusual or under-counted, but internally consistent (R9/R10/R12 only) | 52,921 | 1,216,806.9 | 6.0792% |
| **Clean** | no flag | 1,168,427 | 18,473,251.5 | 92.2926% |

Concentration of Strong-tier distance:

| Client | Vehicle | Strong trips | Strong km |
|---|---|---:|---:|
| ALPHA00001 | 40359275 | 6 | 89,590.6 |
| ALPHA00001 | 39804957 | 3 | 40,042.1 |
| ALPHA00001 | 39804987 | 1 | 8,995.1 |
| BRAVO00016 | 459946970 | 61 | 4,190.2 |
| ALPHA00001 | 40358225 | 1 | 3,202.2 |
| DELTA00001 | 39013149 | 1 | 3,014.8 |
| FOXTROT00001 | 41266332 | 16 | 2,886.7 |
| FOXTROT00001 | 41266284 | 18 | 2,834.1 |

**Top 5 vehicles = 60.4% of all Strong-tier distance. Top 20 = 73.4%.**
513 of 1,837 vehicle streams carry at least one Strong-tier record.

---

## 8. Root-cause evidence

Taxonomy: `UPSTREAM_CONFIRMED` / `LOCAL_INGESTION_CONFIRMED` /
`DOWNSTREAM_DERIVATION_CONFIRMED` / `LIKELY_UPSTREAM` / `UNKNOWN`.

| Anomaly class | Classification | Evidence |
|---|---|---|
| Odometer-register faults (R3/R4/R5, 646/542/26 trips) | **LIKELY_UPSTREAM (very high)** | `trip_distance` is copied verbatim (`:4350`, `:5447`); `start/end_odometer_value` are copied verbatim from *separate* payload keys (`:4333–4337`); the exact identity `end−start = distance` across 1,224,652 rows cannot arise from three independent verbatim copies unless the provider itself produced it. Mode-2 persistence across subsequent trips requires the provider's own odometer stream to carry the fault. |
| Timestamp +86,400 s (R2, 14 trips) | **LIKELY_UPSTREAM (high)** | `_parse_provider_dt` performs no date arithmetic; `trip_duration_seconds` and `end_timestamp` are independent payload fields that disagree by exactly one day. |
| Coordinate cold-start garbage (R11, 59 trips) | **LIKELY_UPSTREAM (high)** | `_extract_coords` only reads `latitude`/`longitude` out of the nested object and float-parses. Values are self-consistent (identical repeated fix) and confined to newly provisioned devices. |
| NULL distance Cohort A (500 trips) | **LIKELY_UPSTREAM (high)** | odometer present and coherent while `trip_distance` is absent — a payload-shape gap, not a parse failure (`_safe_int` would also have nulled the odometer). |
| 1 km quantization / zero-distance under-count (R12, I2) | **UPSTREAM_CONFIRMED (structural)** | odometer register resolution; visible directly in the stored odometer values, which move in 1,000 m steps for 1,100 vehicle streams. |
| Duplicate/multiplied distance | **NOT PRESENT** | zero duplicates on every key tested; upsert is idempotent. |
| Unit conversion / decimal-placement error | **EXCLUDED** | no arithmetic in the ingestion path; consistent metre semantics across all 1.22 M rows and all 4 clients. |
| Eco Driving per-100 km rate distortion | **DOWNSTREAM_DERIVATION_CONFIRMED (propagated)** | §10 — the derivation is correct; its input is not. |
| Coverage gaps (§5.8, 869,706 km) | **UNKNOWN** | consistent with unrecorded movement, missed ingestion windows or provider omission. Cannot be separated with stored data alone. |

**Explicitly not claimed.** No live provider request was issued during this audit,
so nothing is marked `UPSTREAM_CONFIRMED` for the distance classes. The internal
evidence is strong enough to exclude local ingestion as the cause, but only a
GET against `/trips` for a named window can promote `LIKELY_UPSTREAM` to
`UPSTREAM_CONFIRMED`. See §14.

---

## 9. Representative records

All values as stored. Vehicles referenced by provider `vehicle_id` only.

### 9.1 Clearly valid normal trip

| Field | Value |
|---|---|
| trip / vehicle | `422646821` / BRAVO00016 `33233004` |
| start → end | 2026-05-04 15:30:35Z → 15:39:58Z |
| duration | 563 s (matches elapsed exactly) |
| distance | 4,000 m |
| avg speed | 25.6 km/h |
| displacement | 2.52 km (ratio 1.59) |
| odometer | 418,131,000 → 418,135,000; continuous both sides |
| flags | none | 
| **Verdict** | **valid** |

### 9.2 Unusually long but almost certainly legitimate

| Field | Value |
|---|---|
| trip / vehicle | `423091569` / ALPHA00001 `39803817` |
| start → end | 2026-05-06 05:59:51Z → 18:15:57Z |
| duration | 44,166 s (12 h 16 min), matches elapsed |
| distance | 602,000 m (602 km) |
| avg speed | **49.1 km/h** |
| displacement | 7 m — a **round trip** |
| odometer | 68,458,000 → 69,060,000; continuous both sides |
| driving events | 5 harsh-braking, 10 idle — a real working day |
| flags | R10 (statistical only) |
| **Verdict** | **valid.** A naive `distance/geodesic > 50` rule would have destroyed this record. |

### 9.3 Suspicious — insufficient evidence to reject (AMBIGUOUS)

| Field | Value |
|---|---|
| trip / vehicle | `432170505` / ALPHA00001 `39806382` |
| start → end | 2026-06-27 14:41:42Z → 17:53:23Z |
| duration | 11,501 s |
| distance | 417,000 m |
| avg speed | **130.5 km/h** |
| displacement | **unavailable** (no coordinates) |
| odometer | 74,436,000 → 74,853,000; continuous both sides |
| flags | R7, R10 |
| **Verdict** | **ambiguous.** 130 km/h whole-trip average is implausible for a Polish fleet but not physically impossible on a long motorway leg; with no GPS and a clean odometer chain there is no second signal. Confidence: medium. |

### 9.4 Almost certainly invalid — transient odometer spike

| Field | Value |
|---|---|
| trip / vehicle | `426236330` / ALPHA00001 `39685202` |
| start → end | 2026-05-25 08:53:18Z → 08:53:33Z |
| duration | **15 s** |
| distance | **432,196 m (432 km)** |
| avg speed | **103,727 km/h** |
| displacement | **5 m** |
| odometer | 56,510,570 → 56,942,766 |
| odometer, next trip | starts 56,942,766 → then reverts to **56,510,570** two trips later |
| flags | R3, R4 |
| **Verdict** | **invalid, deterministically.** The register reverted to its pre-glitch value, so the true trip distance is ~0 m. Confidence: very high. |

### 9.5 Almost certainly invalid — persistent odometer step

| Field | Value |
|---|---|
| trip / vehicle | `422993873` / ALPHA00001 `39804957` |
| start → end | 2026-05-06 09:38:46Z → 10:34:10Z |
| duration | 3,324 s |
| distance | **39,948,227 m (39,948 km)** |
| avg speed | **43,265 km/h** |
| displacement | 52.6 km |
| odometer | 3,607,233 → 43,555,460, and **every later trip continues from 43.5 M** |
| neighbours | preceding trips 88 km / 9.8 km, following 34.9 km / 9.9 km — all normal |
| flags | R3, R4, R5 |
| **Verdict** | **invalid.** A single trip larger than the entire annual mileage of the fleet's busiest vehicle. Real distance is ≥52.6 km (geodesic lower bound). Confidence: very high. |

### 9.6 The worst record in the dataset — recoverable start-odometer glitch

| Field | Value |
|---|---|
| trip / vehicle | `442974919` / ALPHA00001 `40359275` |
| start → end | 2026-08-28 15:15:30Z → 16:04:49Z |
| duration | 2,959 s |
| distance | **89,472,993 m (89,473 km — 0.447% of the whole fleet's 5-month total)** |
| avg speed | **108,855 km/h** |
| displacement | unavailable |
| odometer | **1,037** → 89,474,030 |
| previous trip end odometer | **89,409,580** |
| next trip start odometer | 89,474,030 (continuous) |
| flags | R3, R4, R5 |
| **Verdict** | **invalid, and deterministically correctable.** Only the *start* odometer glitched (1,037 m ≈ a reset register). Reconstructing from the previous trip's end gives 89,474,030 − 89,409,580 = **64,450 m over 49 min = 78.4 km/h** — an entirely ordinary trip. Confidence: very high. |

### 9.7 Structural — timestamp one day out

| Field | Value |
|---|---|
| trip / vehicle | `432210151` / BRAVO00016 `459779234` |
| start → end | 2026-06-27 09:04:56Z → **2026-06-28** 12:13:15Z |
| elapsed | 97,699 s |
| `trip_duration_seconds` | **11,299 s** (difference exactly 86,400 s) |
| distance | 12,000 m |
| odometer | continuous both sides |
| **Verdict** | **structurally invalid timestamp; distance is fine.** Any speed-based rule must use `trip_duration_seconds`, not the timestamp delta, or this record is misclassified. Confidence: high. |

### 9.8 Coordinate garbage, distance correct

| Field | Value |
|---|---|
| trip / vehicle | `418183430` / ALPHA00001 `459958839` |
| start → end | 2026-04-07 11:06:12Z → 11:11:12Z (300 s) |
| distance | 1,000 m; odometer 6,000 → 7,000, continuous |
| start coordinates | **−78.2573, 86.0426** (Antarctica) |
| end coordinates | 51.9679, 20.5784 (Poland) |
| apparent displacement | **15,120 km** |
| **Verdict** | **coordinate invalid, distance valid.** Cold-start GPS before first fix. Confidence: high. |

### 9.9 Under-count — real movement recorded as zero distance

| Field | Value |
|---|---|
| trip / vehicle | `418663732` / ALPHA00001 `459958860` |
| duration | 54 s |
| distance | **0 m**; odometer 12,000 → 12,000 (1 km resolution) |
| displacement | 11,774 km (also a cold-start coordinate) |
| **Verdict** | representative of the 172,508 zero-distance rows: at 1 km odometer resolution any sub-kilometre movement is recorded as 0. Confidence: high (mechanism), the individual row's true distance is unknowable. |

---

## 10. Aggregate KPI distortion

Analytical simulation only. **No filtered value was persisted.**

### 10.1 Raw baseline

| Metric | Value |
|---|---:|
| Total recorded distance | 20,015,959.0 km |
| Mean distance per trip | 16.3442 km |
| Median distance per trip | 4.000 km |
| Trips | 1,225,535 |

### 10.2 Sensitivity scenarios

| Scenario | Records excluded | % excluded | km removed | % of total km | New total km | New mean km/trip | Δ mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Raw** | 0 | 0% | 0 | 0% | 20,015,959.0 | 16.3442 | — |
| **A** structurally impossible only (R1, R2, R11) | 945 | 0.0771% | 605.8 | 0.0030% | 20,015,353.1 | 16.3445 | +0.002% |
| **B** + high-confidence (R3–R6) | 3,158 | 0.2577% | 241,615.5 | **1.2071%** | 19,774,343.5 | 16.1770 | −1.023% |
| **C** + medium-confidence (R7, R8) | 4,187 | 0.3416% | 325,900.6 | 1.6282% | 19,690,058.3 | 16.1216 | −1.362% |
| **D** + all warnings (R9, R10, R12) | 57,108 | 4.6598% | 1,542,707.5 | 7.7074% | 18,473,251.5 | 15.8104 | −3.266% |
| *E* blunt: distance > 1,000 km | 9 | 0.0007% | 150,064.0 | 0.7497% | 19,865,895.0 | 16.2218 | −0.749% |
| *F* blunt: distance > 500 km | 122 | 0.0100% | 217,246.7 | 1.0854% | 19,798,712.3 | 16.1684 | −1.076% |
| *G* blunt: avg speed > 130 km/h | 1,760 | 0.1436% | 302,594.1 | 1.5118% | 19,713,364.8 | 16.1203 | −1.370% |

Scenario B is the recommended reference point: it removes 1.21% of distance while
touching 0.26% of records. Scenario D is over-broad — R9 and R10 are weak
statistical signals and D discards 6.5% of distance that has no independent
evidence of being wrong.

Note that the blunt rules perform *worse* than B on the same budget: F removes
1.09% of distance but keeps every one of the 620 invalid trips between 5 km and
500 km, while E catches only 9 records.

### 10.3 Per-client distortion under Scenario B

| Client | Trips | Invalid | Raw km | Invalid km | % km removed | Raw mean km | Clean mean km |
|---|---:|---:|---:|---:|---:|---:|---:|
| ALPHA00001 | 873,924 | 1,849 | 15,191,536 | 213,319 | **1.4042%** | 17.383 | 17.175 |
| FOXTROT00001 | 232,493 | 271 | 2,796,043 | 18,087 | 0.6469% | 12.026 | 11.962 |
| BRAVO00016 | 54,573 | 974 | 1,111,014 | 6,908 | 0.6217% | 20.693 | 20.599 |
| DELTA00001 | 64,545 | 17 | 917,366 | 3,296 | 0.3593% | 14.213 | 14.165 |

### 10.4 The distortion that actually matters — Eco Driving

Eco Driving scores drivers on **events per 100 km**. Inflated distance *lowers*
the rate, so a corrupted trip makes a driver look **safer**, improves their
score, and moves them up the ranking.

Measured on ALPHA00001 `eco_trip_assignments` (`aggregation_included = true`):

| | |
|---|---:|
| Included trips | 234,971 |
| … containing a high-confidence anomaly | 568 |
| Included distance | 4,395,942 km |
| … contributed by anomalous trips | 33,247 km (**0.756%**) |
| Driver-months | 2,491 |
| **Driver-months containing ≥1 anomalous trip** | **315 (12.6%)** |
| Driver-months with distance inflated >5% | 56 (2.2%) |
| Driver-months with distance inflated >25% | **9** |
| Driver-months with distance inflated >100% | **4** |

For those 9 driver-months, the understatement of the event rate:

| Month | Bad trips | Raw km | Clean km | Raw ev/100 km | Clean ev/100 km | Rate understated by |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08 | 1 | 1,782.3 | **0.0** | 36.69 | — | month is *entirely* one bogus trip |
| 2026-08 | 1 | 306.0 | 9.0 | 22.88 | 777.78 | **+3,300%** |
| 2026-07 | 1 | 12,059.7 | 3,064.6 | 2.40 | 9.30 | **+288%** |
| 2026-08 | 3 | 1,997.0 | 1,383.0 | 6.51 | 9.18 | +41.1% |
| 2026-07 | 1 | 4,445.8 | 3,189.1 | 8.52 | 11.35 | +33.2% |
| 2026-07 | 10 | 4,807.1 | 3,634.5 | 13.31 | 17.00 | +27.7% |
| 2026-08 | 1 | 667.0 | 530.0 | 6.75 | 8.30 | +23.1% |
| 2026-08 | 5 | 3,434.4 | 2,666.3 | 14.03 | 15.53 | +10.6% |
| 2026-07 | 2 | 190.2 | 63.4 | 25.23 | 25.22 | 0.0% |

**Answer to the brief's question:** at fleet level the anomalies are cosmetic
(−1.2% of total distance). At driver level they are **not** cosmetic — they
change ranking-relevant scores by up to three orders of magnitude for individual
driver-months, and the affected drivers are the ones who look *best*.

Counterweight: the under-count in §5.7/§5.8 is at least the same order of
magnitude (≥141,444 km of movement recorded as 0 distance, plus ~869,706 km of
plausible coverage gaps). **Excluding only the upper tail biases totals downward.**

---

## 11. Data-retention / filtering options

### Option A — raw only (status quo)

- Provenance: perfect. Auditability: perfect. Reproducibility: perfect.
- Risk of deleting legitimate journeys: zero.
- Effect on dashboards: **the measured distortion in §10.4 persists**, including
  driver rankings computed from a 39,948 km trip.
- Complexity: none. Rules improvable later: n/a (nothing to improve).
- Transparency: high but misleading — users cannot tell a 602 km round trip from
  a 432 km 15-second glitch.
- **Assessment: unacceptable given §10.4, and only because of §10.4.**

### Option B — hard filtering (delete/exclude at ingest)

- Provenance: **destroyed**. Auditability: destroyed. Historical recomputation:
  impossible.
- Risk of deleting legitimate extremes: real. Scenario D would discard 30,937
  trips (1.2 M km) whose only sin is being unusual, including the 602 km round
  trip in §9.2.
- Rules improvable later: **no** — a rule change cannot be applied retroactively
  because the evidence is gone.
- **Assessment: reject.** Contradicts the repository invariant that applied
  migrations are immutable and database evolution is additive, and the anomaly
  set is provably concentrated enough that deletion buys nothing.

### Option C — raw + quality flags

- Provenance and auditability: preserved.
- Effect on dashboards: **none unless consumers opt in**. Flags do not fix the
  Eco Driving distortion by themselves.
- Complexity: one additive migration (nullable flag columns or a side table) plus
  a recomputable classifier.
- Rules improvable later: **yes**, with full backfill.
- Transparency: high — a UI can show "this trip is flagged and why".
- **Assessment: necessary but not sufficient.**

### Option D — immutable raw source + cleaned analytical projection

- Provenance: perfect (raw untouched). Auditability: perfect (the projection is
  a pure function of raw + rule version).
- Effect on dashboards: **the actual fix** — Eco Driving, totals and rankings read
  the projection; the Database/Report Explorer keeps reading raw.
- Reproducibility: high, provided the rule version is recorded with each
  materialization.
- Historical recomputation: trivially supported.
- Complexity: highest — a view or materialized table per client, plus rule
  versioning, plus a decision per downstream consumer.
- **Assessment: the correct target architecture.** C is its first half.

### Option E — selective deterministic correction

Only defensible where the intended value is *provably* reconstructible. This
dataset contains exactly two such populations, both measured:

| Correction | Rows | Raw | Reconstructed | Basis |
|---|---:|---:|---:|---|
| NULL distance where both odometers exist | **500** | NULL | **+6,955 km** | `end_odo − start_odo`, the identity that holds for all 1,224,652 other rows |
| Start-odometer glitch with prior-trip baseline (`odo_gap < 0` and speed > 200 km/h) | **17** | 90,077.5 km | **588.7 km** | `end_odo − prev_end_odo`; removes **89,489 km = 0.447% of fleet total** |

Both corrections are deterministic, individually auditable and reversible if
stored as a derived column rather than an overwrite. Everything else — the 753
persistent register steps, the 22,295 zero-distance-with-movement rows — has **no
deterministic target value** and must not be "corrected".

- **Assessment: adopt narrowly, inside Option D's projection, never as a mutation
  of raw.**

### Recommended combination

**D (raw source + cleaned analytical projection), with C as its flag layer and E
applied only to the two provable populations above.** Raw stays exactly as the
API delivered it.

---

## 12. Candidate quality policy (proposal only — not implemented)

Four tiers. Every threshold below is stated with its measured impact, as required.

### ACCEPT
Everything not matched by another tier.
> **1,168,427 trips (95.34%), 18,473,251 km (92.29% of total).**

### INVALID — exclude from every derived metric, retain and display in raw views

| Rule | Rationale | Impact if adopted |
|---|---|---|
| `trip_distance_meters IS NULL` | nothing to aggregate | 883 trips (0.0721%), 0 km |
| `abs((end−start) − trip_duration_seconds) > 0` | self-contradictory record | 14 trips (0.0011%), 598.8 km (0.0030%) |
| `avg_speed > 200 km/h AND distance > 5 km` | no road vehicle averages 200 km/h over a whole trip; the >5 km guard removes quantization artifacts | 646 trips (0.0527%), 215,543 km (**1.0769%**) |
| `avg_speed > 300 km/h` | impossible at any distance | 542 trips (0.0442%), 204,502 km (1.0217%) |
| `distance > 500 km AND avg_speed > 130 km/h` | catches long-distance corruption that survives the speed rules | 26 trips (0.0021%), 159,966 km (0.7992%) |
| `0 < distance < 0.9 × geodesic AND geodesic > 5 km` | a route cannot be shorter than its own displacement | 1,484 trips (0.1211%), 22,703 km (0.1134%) |

> **Combined (deduplicated): if this INVALID tier were adopted, 3,111 trips
> (0.2538%) would be affected and 241,610 km (1.2071% of total recorded
> distance) would be removed from analytical aggregates.** Fleet mean drops
> 16.3442 → 16.1770 km/trip (−1.02%). Per client: ALPHA00001 −1.40%,
> FOXTROT00001 −0.65%, BRAVO00016 −0.62%, DELTA00001 −0.36%.

Use `trip_duration_seconds`, not the timestamp delta, in every speed rule — see
§9.7 — otherwise 14 records are misclassified.

### WARN — retain in all metrics, surface in the UI, monitor the trend

| Rule | Impact if adopted |
|---|---|
| `130 < avg_speed ≤ 200 km/h` | 933 trips (0.0761%), 86,646 km (0.4329%) |
| `avg_speed > 200 km/h AND distance ≤ 5 km` (quantization artifact) | 181 trips (0.0148%), 405 km (0.0020%) |
| `start_odo ≠ prev_end_odo` for the same vehicle | 29,339 trips (2.3940%), 605,235 km (3.0238%) |
| `distance > 300 km AND avg_speed ≤ 130 km/h` (statistically unusual, internally consistent) | 2,012 trips (0.1642%), 735,392 km (3.6740%) |
| coordinates outside the operating region | 59 trips (0.0048%), 7 km — **flag the coordinates, not the distance** |
| `distance = 0 AND geodesic > 200 m` (under-count) | 22,295 trips (1.8192%), 0 km removed |

> **If the whole WARN tier were instead excluded, a further 53,997 trips (4.41%)
> and 1,301,097 km (6.50%) would leave the aggregates — which is why it must not
> be.** These are warnings, not defects.

### EXCLUDE_FROM_DERIVED_METRICS (distinct from INVALID)

Records that are individually plausible but whose *inclusion* biases a specific
derived metric:

- Trips from a vehicle-month where the vehicle's INVALID share exceeds 25% of
  its distance — 9 ALPHA00001 driver-months today (§10.4). Excluding whole
  driver-months rather than single trips prevents a partially-cleaned month from
  producing an equally wrong rate.
- Trips whose Eco Driving assignment period contains an INVALID trip: **315 of
  2,491 driver-months (12.6%)** would be marked as reduced-confidence rather than
  silently rescored.

### INFORMATIONAL (never affects any metric)

- no coordinates — 236,826 trips (19.32%), 3,419,381 km;
- distance = 0 — 172,508 trips (14.08%);
- odometer quantization step per vehicle — the resolution metadata every rule
  above should read.

### Thresholds deliberately NOT proposed

- **`distance > 1000 km = invalid`** — catches 9 trips / 150,064 km (0.75%) and
  misses 617 invalid trips below 1,000 km. Empirically inferior to the speed
  rules at the same distance budget.
- **`distance/geodesic > N`** as a standalone rule — 98.0% of the extreme-ratio
  population is legitimate round trips (§5.4).
- **Tukey/IQR outlier removal** — Q3+3·IQR flags 7.196% of trips holding
  10,174,340 km (50.8% of total distance). Statistically defensible, operationally
  absurd.
- **Any rule keyed on odometer-vs-distance disagreement** — the two are the same
  number in 100.0000% of rows.

---

## 13. Unknowns and limitations

1. **No live provider payload was captured.** `jobs/api/telematics/request_evidence.py`
   is explicitly "not a raw-response archive" — it records identity, position,
   timing and counts, never payloads. Root causes are therefore
   `LIKELY_UPSTREAM`, not `UPSTREAM_CONFIRMED`.
2. **Provider revisions are invisible.** 6.12% of provenance-bearing rows were
   re-upserted, `trip_distance_meters` is overwritten on conflict, and no history
   is retained. A provider correction cannot be distinguished from the original
   value after the fact.
3. **No GPS trace.** Only start/end coordinates are stored, so recorded distance
   cannot be compared against a reconstructed route. Geodesic displacement is a
   lower bound only.
4. **19.32% of trips have no coordinates at all**, so the geodesic signal is
   unavailable for 236,826 records — including 139 of the 827 speed anomalies.
   Whether the provider omitted the `start_coordinates` object or sent it in a
   shape `_extract_coords` does not recognise (it has no synonym fallbacks, unlike
   `_extract_odometer_value`) cannot be settled without a payload sample.
5. **No device model or firmware column exists** in `client_trips`, so the
   device-generation correlation in §5.9 rests on `vehicle_id` magnitude as a
   proxy. It cannot be confirmed.
6. **Coverage gaps (869,706 km) are unattributable.** Real unrecorded movement,
   provider omission and ingestion-window misses are indistinguishable from the
   odometer alone.
7. **True distance for the 753 persistent-step faults is unrecoverable.** The
   register never returns to a correct baseline, so no reconstruction exists —
   only the geodesic lower bound.
8. **True distance for zero-distance trips is unknowable per record.** The
   mechanism (1 km resolution) is certain; the individual values are not.
9. **The 2026-08 window is partial** (to 2026-08-29), so August month-over-month
   comparisons understate volume.
10. **`echogallery_main` has the `client_trips` table but zero rows**; that
    client is out of scope by absence of data, not by exclusion.
11. **Thresholds are calibrated to a Polish/Central-European road fleet.** The
    200 km/h rule would need revisiting for a fleet operating where sustained
    higher averages are achievable.

---

## 14. Recommended next step

In priority order, before any filtering is implemented:

1. **Confirm upstream provenance with one bounded read-only probe.** Issue a
   GET-only `/trips` request for a narrow window covering a named anomaly — the
   cheapest decisive case is DELTA00001 trip `422448100` (2026-05-03 13:02–13:18,
   3,014.8 km in 885 s, smallest fleet) — and record whether `trip_distance`,
   `start_odometer_value` and `end_odometer_value` arrive corrupted. This
   promotes the entire distance taxonomy from `LIKELY_UPSTREAM` to
   `UPSTREAM_CONFIRMED` or redirects the whole investigation to the ingestion
   path. **This requires explicit authorization for a live provider request and
   was deliberately not performed during this audit.**

2. **Raise the odometer-register faults with the provider.** The evidence package
   is ready: 5 vehicles carry 60.4% of the corrupt distance, the failure has two
   reproducible signatures (transient spike / persistent step), and one device
   hit the `(2²⁸−1)×10` register ceiling. This is a provider hardware/firmware
   problem, and no amount of downstream filtering fixes the underlying telemetry.

3. **Decide the architecture question first, the thresholds second.** The
   thresholds in §12 are calibrated and their impact is measured; the open
   decision is Option D — does a cleaned analytical projection exist alongside
   immutable raw, and which consumers read which. That decision determines
   whether §12 is a flag schema or a filter.

4. **Quantify the under-count before publishing any corrected total.** Removing
   1.21% from the top while ignoring ≥141,444 km recorded as zero distance and
   ~869,706 km of coverage gaps produces a total that is *differently* wrong.
   Establishing whether the provider offers a per-vehicle odometer or daily
   distance endpoint would let this be closed properly.

5. **Document that `trip_distance_meters` is an odometer delta** in `docs/05_jobs.md`.
   Every downstream consumer currently treats it as a measured trip distance, and
   the two failure profiles are not the same.

6. **Do not implement Option E's corrections as mutations of `client_trips`.**
   The 500 NULL-distance recoveries (+6,955 km) and 17 start-glitch
   reconstructions (−89,489 km) belong in the derived projection, where they stay
   auditable and reversible.

---

### Appendix — reproducibility

All figures in this document were produced by read-only SQL and pandas analysis
over a full extract of `public.client_trips` from `telematics_main`, `alpha_main`,
`foxtrot_main` and `delta_main` on 2026-08-30. Every psql session was opened with
`SET default_transaction_read_only = on`. Derived quantities:

- `elapsed_s = end_timestamp − start_timestamp`
- `avg_speed = trip_distance_meters / elapsed_s` (converted to km/h)
- `geo_km` = haversine(start_lat/lon, end_lat/lon), R = 6371.0088 km
- `odo_gap = start_odometer_value − end_odometer_value(previous trip, same vehicle, ordered by start_timestamp)`

No production row was modified. No provider request was issued. No filtering
logic was added to the repository.
