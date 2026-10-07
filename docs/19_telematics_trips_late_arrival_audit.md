# Telematics `/trips` late-arrival audit — account ALPHA00001

**Prepared for Telematics support. Read-only forensic audit; no provider data was modified.**

Audit date: 2026-08-11
Account: `ALPHA00001` (fleet of 1,265 registrations active in the audited month)
Endpoint under audit: `GET /trips`

---

## 1. Summary

Records returned by `GET /trips` for account ALPHA00001 have repeatedly become available
from Telematics **materially later than the journeys they describe** — in confirmed cases,
more than a week later, and in the strongest inferred case more than two weeks later.

The audit identifies **1,031 individual trip records** for journeys between
2026-07-12 and 2026-07-31 that:

- were **not returned** by a `GET /trips` request whose interval contained the journey,
  issued after the journey had already ended, and which completed normally with every page
  the API advertised retrieved; and
- **were returned** by a later `GET /trips` request for an interval containing the same
  journey.

Independently, a day-by-day reconciliation over 28 consecutive daily request windows
(2026-07-01 to 2026-07-28) shows that **2,158 of 159,716 records — 1.35% — were not yet
available** when the day containing them was first requested, roughly 2 to 26 hours after
each journey started.

We are not reporting these as a defect claim. We are reporting them because our ingestion
schedule was designed on the assumption that a completed day is stable, and that assumption
does not hold. We need Telematics to tell us what the actual publication behaviour is so we
can size our polling correctly.

### Observed date range

| | |
|---|---|
| Journeys covered by individually confirmed cases | 2026-07-12 → 2026-07-31 |
| Day-level reconciliation window | 2026-07-01 → 2026-07-28 (28 daily windows) |
| Secondary confirmation window | 2026-06-13 → 2026-06-18 |
| Evidence horizon (request/response logs retained) | 2026-05-11 → 2026-08-11 |

---

## 2. What was examined, and how

### 2.1 Population

| Quantity | Value |
|---|---|
| Trip records stored for 2026-07 (Warsaw month) | 181,824 |
| Records individually audited and classified | **1,031** |
| Share of the month | 0.567% |
| Distinct registrations affected | 82 of 1,265 (6.5%) |

The 1,031 audited records are the complete set produced by a full month-wide
reconciliation: on 2026-08-10 we re-requested every 2-day sub-window of 2026-07 from
`GET /trips` and compared the response against what we already held. Every sub-window
response reconciled **exactly** against the provider's own advertised total
(`accumulated_count == advisory_total`, 16 of 16 sub-windows), so the comparison set is
complete on the provider's own terms.

### 2.2 Why absence is provable

Two independent properties make first-arrival provable for these records:

1. **The recovery job that wrote them is insert-only.** It writes with
   `ON CONFLICT … DO NOTHING`. A record it stamped is therefore a record that **did not
   exist locally before that moment**. This is a hard proof of local absence, not an
   inference from timestamps.
2. **The routine job that should have collected them is overwrite-on-conflict.** Any
   record the API had returned in an earlier request would have been written at that time.
   Absence therefore cannot be explained by us discarding a returned record.

### 2.3 Why the earlier request genuinely covered the journey

This is the point on which a late-arrival claim can most easily be wrong, so it was
checked per record rather than assumed.

Our routine schedule at the time requested a **one-day** interval per run. Most historical
requests therefore never addressed an older journey at all, and absence from such a request
is no evidence of anything. Only requests whose interval actually contained the journey's
start instant were counted.

For every one of the 1,031 records we reconstructed, from our stored request and response
logs, the exact interval sent on the wire and the exact time each page came back, and kept
a request as "covering" only if the journey's start instant fell inside the interval **as
the API interprets it** (§4).

**Result: all 1,031 records had at least one covering request that completed before the
record first appeared locally.** None fell into the "never actually requested" category.

### 2.4 Why local failure is excluded

For every covering request used in this report:

- the run completed with a success status and logged no error;
- pagination was complete — the number of pages retrieved equals the last page the API
  itself advertised for that interval, page numbers are contiguous from 1, and the
  terminating page was short (fewer rows than the requested page size);
- no parse rejections, no truncated responses, no budget or timeout stops;
- the count of records parsed from the response matches the count written to storage.

Runs that did fail (three in early August, all rejected by our own safety check for a
pagination inconsistency in the API response) were **excluded** from use as absence
evidence. They are not part of any claim here.

---

## 3. Classification and evidence standard

| Classification | Definition | Count |
|---|---|---|
| `CONFIRMED_PROVIDER_LATE_ARRIVAL` | A request whose interval contained the journey start, issued after the journey ended, completed normally with provably complete pagination, did **not** return the record; a later request for an interval containing the same journey did return it; the record is proven to have been locally absent in between by insert-only write semantics. | **1,031** |
| `PROBABLE_PROVIDER_LATE_ARRIVAL` | Same shape, one link inferential. | 0 |
| `LOCAL_INGESTION_FAILURE` | Absence explained on our side. | 0 |
| `AMBIGUOUS` | Competing explanations survive. | 0 |
| `INSUFFICIENT_HISTORICAL_TELEMETRY` | No covering request existed before first local appearance. | 0 |

The zero in the last row is a property of this particular sample, not of our telemetry in
general. The sample was drawn from a month that had been requested repeatedly, so a
covering request always existed. For the fleet-wide day-level figures in §5.2 the same is
**not** true, and that limitation is stated there.

---

## 4. `/trips` request-window semantics used in this audit

So that the intervals quoted in this report and in the accompanying CSV are unambiguous:

```
GET /trips  request  start_timestamp / end_timestamp  ->  interpreted as Europe/Warsaw local wall-clock
GET /trips  response row start_timestamp / end_timestamp  ->  UTC
```

The request and response sides do **not** agree. This is an empirically measured
behaviour, established by controlled GET-only probes on 2026-08-10 over two closed July
windows: the same intended interval expressed as a UTC projection returned rows offset by
exactly the Warsaw UTC offset, while the same interval expressed as Warsaw wall-clock
returned the six pre-registered reference journeys with exactly matching start timestamps,
end timestamps, floor-matching distances and floor-matching odometers, plus 1,487 control
rows matching already-stored records at 97–99%. **Telematics publishes no timezone contract
for these parameters**, so this is an observation, not a documented guarantee — and
confirming or correcting it is one of our questions in §8.

Two consequences applied throughout this audit:

- Every "requested interval" in this report and in the CSV is the interval **as the API
  interprets it**. In the CSV the `last_absent_requested_from` / `..._to` and
  `first_present_requested_from` / `..._to` columns carry the literal wire values with
  their interpretation timezone attached.
- Under that interpretation, each audited routine request addressed exactly a contiguous
  24-hour interval, and consecutive audited requests were adjacent with no gap between
  them. There is no window in the audited range that was skipped.

We also determined empirically that `GET /trips` matches records that **overlap** the
requested interval rather than only those starting inside it. All record counts compared in
§5.2 use that overlap definition, so no boundary journey is counted as missing merely
because it began before the interval.

**Reclassified 2026-08-13 — overlap is documented, not merely observed.** The retained provider
specification `docs/openapi.yaml` states it directly for `GET /trips`: *"Returns all trips that
overlap the specified time range. Any trip that starts before this timestamp but ends after it
will be included"*, and for `end_timestamp`, *"Any trip that starts or ends within the period
will be included, even if it extends outside the end_timestamp."* Our probes confirmed published
behaviour rather than establishing an undocumented one, so this is a **contract fact** and can
carry the weight of a design argument.

The same block documents two further contract facts, both load-bearing elsewhere: `/trips`
enforces a **maximum 31-day lookup period** between `start_timestamp` and `end_timestamp`, and
**no rate limit is documented for `/trips`** — notable only because the specification *does*
document rate limits on other endpoints (`/fuel/consumed` and `/fuel/level` at 10 requests per
minute), so the omission is informative, though it is not a guarantee that no server-side
throttling exists.

**This reclassification applies to the overlap rule alone.** The **timezone** behaviour above —
request parameters interpreted as Europe/Warsaw wall-clock while response rows are UTC — remains
**empirical and unpublished**, and confirming it is still one of our questions in §8. The two
claims must not be merged.

---

## 5. Delay measurements

### 5.1 Direct request evidence (no assumptions)

Two bounds are available per record. Both are derived from the timestamp of the page
response that carried — or failed to carry — the record.

- **`publication_lag_lower_bound`** = time of the last response that covered the journey
  and did **not** contain the record, minus journey start. *The record demonstrably did not
  exist at the provider at this age.*
- **`publication_lag_upper_bound`** = time of the first response that covered the journey
  and **did** contain the record, minus journey start. *The record demonstrably existed by
  this age.*

| Statistic | Lower bound (provable non-existence) | Upper bound (provable existence) |
|---|---|---|
| n | 1,031 | 1,031 |
| min | 3.29 h (`03:17:24`, 0.14 d) | 239.63 h (`239:37:48`, 9.98 d) |
| p50 | 14.37 h (`14:22:12`, 0.60 d) | 437.91 h (`437:54:36`, 18.25 d) |
| p90 | 100.23 h (`100:13:48`, 4.18 d) | 558.80 h (`558:48:00`, 23.28 d) |
| p95 | 123.47 h (`123:28:12`, 5.14 d) | 604.55 h (`604:33:00`, 25.19 d) |
| **max** | **206.60 h (`206:36:02`, 8.61 d)** | 709.29 h (`709:17:30`, 29.55 d) |

Distribution of the lower bound — the age at which the record was still provably absent:

| Still absent at | Records |
|---|---|
| ≤ 1 day | 761 |
| 1–3 days | 98 |
| 3–7 days | 166 |
| 7–14 days | 6 |
| > 14 days | 0 |

**The upper bounds are wide and are not a measure of Telematics's publication delay.** They
are wide because our schedule did not re-request those intervals again until the
2026-08-10 reconciliation. They bound our own detection, not the provider's publication.
They are reported for completeness and should not be read as a performance figure.

### 5.2 Day-level reconciliation (independent, aggregate)

An independent measurement that does not depend on which run wrote which record: for each
daily request window, compare the number of records the API returned at the time against
the number of records that exist for that same interval today.

| Period | Records returned at first request | Not yet available | Rate |
|---|---|---|---|
| 2026-07-01 → 2026-07-28 (28 daily windows) | 159,716 | **2,158** | **1.351%** |
| 2026-06-13 → 2026-06-18 (5 windows, re-requested 7 weeks later) | 27,889 | 246 | 0.882% |

For the nine windows 2026-07-20 → 2026-07-28, whose only later covering request was the
2026-08-10 reconciliation, this aggregate figure and the per-record count of §5.1 agree
almost exactly (for example 2026-07-21: 117 and 117; 2026-07-22: 93 and 93; 2026-07-28: 88
and 87). Two independent methods reproducing the same number is the strongest internal
check available in this dataset.

**This measurement is only meaningful where a later covering request exists.** Windows that
were requested exactly once can never show a difference, and are excluded rather than
reported as zero. That excludes 2026-06-19 → 2026-06-30 and 2026-08-02 → 2026-08-10 from
this table.

### 5.3 Settling curve

For the windows 2026-07-01 → 2026-07-19, a full re-request of the whole period was issued
on 2026-07-20 at 23:51 UTC. Comparing what was still missing after that re-request gives a
direct settling curve, measured from the end of the journey's day:

| Age of the day at re-request | Still missing after re-request | Share of that day's records |
|---|---|---|
| 1 day | 54 | 1.96% |
| 2 days | 45 | 0.94% |
| 3 days | 19 | 0.29% |
| 4 days | 33 | 0.49% |
| 5 days | 15 | 0.23% |
| 6 days | 17 | 0.25% |
| 7 days | 2 | 0.03% |
| 8 days | 4 | 0.15% |
| 9–20 days | 0 | 0.00% |

Read on its own this suggests publication is essentially complete by about 9 days. Section
5.4 shows at least one record that is not consistent with that, so the curve should be
treated as a lower estimate of the tail, not as an upper bound on it.

### 5.4 Record-identifier ordering (inferential, corroborating)

`provider_trip_id` appears to be allocated in monotonically increasing order at the moment
the provider creates the record, not at the moment the journey occurs. Across our whole
observation history the maximum identifier returned rises monotonically with wall-clock
time at roughly 215,000 identifiers per day, and for promptly published records the
identifiers for a given journey date form a tight contiguous band. **The late-arriving
records fall far outside that band and sit among identifiers whose promptly published
neighbours describe journeys one to two weeks later.**

If that reading is correct, it dates the creation of each record far more tightly than our
request cadence can:

| Statistic | Age of journey when the record's identifier was allocated |
|---|---|
| min | 1.14 d |
| p50 | 7.12 d |
| p90 | 14.25 d |
| p95 | 16.29 d |
| max | 20.31 d |

| Allocated at journey age | Records |
|---|---|
| ≤ 1 day | 0 |
| 1–3 days | 296 |
| 3–7 days | 212 |
| 7–14 days | 414 |
| > 14 days | 109 |

Notably **no** late record was created within one day of its journey. 564 of 1,031 records
were still not created three days after the journey; 316 were still not created seven days
after it.

This is an inference and is labelled as such. The direction we rely on most is sound: if
the API returned identifier *M* at instant *T*, then any smaller identifier had already
been allocated by *T*. The opposite direction — concluding an identifier did **not** yet
exist at *T* — is weaker, because we only ever observe identifiers inside the intervals we
request, so the true allocation frontier at *T* may be a few hours ahead of what we saw.
The strongest individual case below has a margin of roughly two to one against that
uncertainty, but it remains inferential and we would rather Telematics simply told us the
creation time (§8).

---

## 6. Strongest individual cases

Times are UTC. "Requested interval" is stated as the API interprets it (§4).

### Case 1 — record `439458815`, registration `WZ459JN` — strongest overall

| Event | Time | Evidence |
|---|---|---|
| Journey | 2026-07-23 06:41:00 → 17:59:30 | stored record |
| Covering request issued | 2026-07-24 02:00:01, interval `2026-07-23 02:00:00` → `2026-07-24 02:00:00` Europe/Warsaw | request log |
| Response complete, record **absent** | 2026-07-24 02:01:25, 7 of 7 advertised pages, terminating page short | response log |
| Identifier frontier observed below `439458815` | 2026-08-08 02:01 (max returned `439360456`) | response log |
| Identifier frontier observed above `439458815` | 2026-08-09 02:01 (max returned `439459108`) | response log |
| Record **present** in response | 2026-08-10 20:38 | response log |
| First stored locally | 2026-08-10 21:00:52 | insert-only write |

Provable by request evidence alone: absent 0.81 days after the journey.
Inferred from identifier ordering: **the record did not exist ~15.8 days after the journey
and was created within ~16.8 days of it.**

### Case 2 — record `436652605`, registration `XP06293` — strongest without any inference

| Event | Time | Evidence |
|---|---|---|
| Journey | 2026-07-12 07:15:02 → 10:22:34 | stored record |
| Covering request issued | 2026-07-20 21:50:44, interval `2026-07-11 22:00:00` → `2026-07-12 21:59:59` Europe/Warsaw | request log |
| Response complete, record **absent** | 2026-07-20 21:51:04, all advertised pages retrieved, terminating page short | response log |
| Record **present** in response | 2026-08-10 20:32:31 | response log |
| First stored locally | 2026-08-10 21:00:52 | insert-only write |

**Provable non-existence: 206.60 hours = `206:36:02` = 8.61 days after the journey**, with
no assumption about identifier allocation. This is the maximum directly provable delay in
the dataset.

### Case 3 — record `438298776`, registration `XE10645`

| Event | Time |
|---|---|
| Journey | 2026-07-21 14:30:19 → 14:30:38 |
| Covering response complete, record absent | 2026-07-22 02:01:17 (interval `2026-07-21 02:00:00` → `2026-07-22 02:00:00` Europe/Warsaw) |
| Identifier not yet allocated as of | 2026-07-29 02:02 |
| Identifier allocated by | 2026-08-03 19:15 |
| Record present in response | 2026-08-10 20:37:46 |

Provable non-existence 11.52 h; inferred creation between 7.5 and 13.2 days after the
journey.

### Case 4 — record `438145699`, registration `WE2CN07`

| Event | Time |
|---|---|
| Journey | 2026-07-23 07:29:35 → 07:45:04 |
| Covering response complete, record absent | 2026-07-24 02:01:25 |
| Identifier not yet allocated as of | 2026-07-29 02:02 |
| Identifier allocated by | 2026-08-03 19:15 |
| Record present in response | 2026-08-10 20:38:59 |

Provable non-existence 18.53 h; inferred creation between 5.8 and 11.5 days after the
journey.

---

## 7. Pattern: delay clusters per vehicle, not per record

The delay is **not** a uniform random tail. It concentrates in contiguous multi-day blocks
on individual registrations, with clean edges:

| Registration | Late records | Behaviour across 2026-07 |
|---|---|---|
| `XE10645` | 180 | Published normally through 07-13. From 07-15 to 07-28, **every** journey was late — zero prompt records for 14 consecutive days. Normal again from 07-29. |
| `WZ459JN` | 71 | Normal through 07-22. From 07-24 to 07-31, every journey late. |
| `WE2CN07` | 54 | Normal through 07-19. From 07-21 to 07-28, every journey late. |

Across the whole set: 82 registrations affected, but 31 of them contributed a single late
record while one contributed 180. The affected journeys are also longer than average (mean
duration 2,348 s vs 1,292 s fleet-wide; mean distance 37.7 km vs 17.9 km).

**None of the 1,031 late records overlaps in time with a record we already held for the
same vehicle.** They are genuinely absent journeys, not corrections, re-issues or
duplicates of records already delivered.

This pattern is what a per-unit backlog looks like: a device or an upstream stage stops
delivering for a vehicle, then the backlog is flushed in bulk days later. Confirming or
correcting that reading is question 2 in §8.

---

## 8. Questions for Telematics support

1. **Can a `/trips` record be created materially after the journey it describes?** Our
   evidence says yes, by days. Is that expected behaviour?

2. **What causes delayed publication?** Is it device buffering and later backhaul, a
   queue/replay stage, batch reprocessing, or something else? The per-vehicle,
   multi-day, all-or-nothing pattern in §7 suggests a per-unit backlog rather than a
   uniform processing tail — is that correct?

3. **Is there a documented maximum eventual-consistency delay?** We have directly proven
   8.61 days and inferred close to 17. What figure should we design to? If there is no
   guaranteed bound, please say so explicitly — that is itself an actionable answer.

4. **Can a completed historical date later gain trips?** Concretely: if we request
   `2026-07-21` today and again in 30 days, can the second response contain records the
   first did not? Our data says yes. Is there any point after which a date is final?

5. **Is there a provider-side creation or ingestion timestamp we can read?** A
   `created_at`, `received_at`, `published_at` or equivalent on the `/trips` record would
   let us measure this exactly instead of inferring it. Section 5.4 shows we are currently
   forced to infer creation time from `provider_trip_id` ordering — is that identifier in
   fact allocated monotonically at record creation? If yes, we would like that confirmed as
   a supported property. If no, please tell us, because our current inference depends on it.

6. **Is there a cursor, revision or change-feed mechanism we should be using instead of
   date-window polling?** Something of the form "give me every record created or modified
   since token *X*" would make late publication a non-issue for us. If `/trips` supports an
   ordering or filter on record creation rather than journey time, that would also solve it.
   Date-window polling cannot detect a record that did not exist when the window was polled,
   and widening the window is only a guess at the tail.

7. **Confirmation of the request/response timezone contract of §4.** We have measured
   `start_timestamp` / `end_timestamp` on the request to be interpreted as Europe/Warsaw
   local wall-clock while response timestamps are UTC. Please confirm, correct, or point us
   at documentation. In particular: how does the API resolve an ambiguous local time during
   the autumn DST fold, when the same wall-clock string denotes two distinct instants?

---

## 9. Limitations

Stated plainly, because several of them bound how strong a claim we can make.

1. **No provider-side creation timestamp exists in the data.** Every delay figure in §5.1
   is bounded by *our* request cadence, not measured at the source. Where our requests are
   sparse the bounds are correspondingly wide. This is question 5 in §8.

2. **The identifier-ordering evidence in §5.4 and Cases 1, 3 and 4 is inferential.** It
   assumes `provider_trip_id` is allocated monotonically at record creation. That assumption
   is well supported by our data but is not a documented provider guarantee. The
   "did not yet exist" direction is additionally limited by the fact that we only observe
   identifiers inside intervals we request, so the true allocation frontier may run a few
   hours ahead of the highest identifier we have seen.

3. **The upper bounds in §5.1 measure our detection, not Telematics's publication.** They are
   wide because we did not re-request those intervals for weeks. They should not be quoted
   as a provider figure.

4. **Delay is only measurable where an interval was requested more than once.** Intervals
   requested exactly once cannot exhibit a difference, so their zero is uninformative and
   they are excluded from §5.2 rather than reported as clean.

5. **The sample is not a uniform random sample of the fleet-month.** It is the complete set
   of records missing at one reconciliation instant (2026-08-10) for one month. Because
   intervals earlier in the month had already been re-requested on 2026-07-20 while later
   intervals had not, the per-record distribution in §5.1 is biased toward records from the
   later part of the month. The percentiles there are descriptive of this sample and should
   **not** be treated as a fleet-wide or steady-state distribution. The day-level figures in
   §5.2 and the settling curve in §5.3 are less exposed to this bias, which is why both are
   reported.

6. **Three requests in the audited range failed** (2026-08-01, 2026-08-02, 2026-08-03),
   rejected by our own safety check for an inconsistency between the requested page number
   and the page number reported in the response. Those runs were excluded from all evidence.
   They are not part of this report's claims, but we mention them because a page-number
   inconsistency in `/trips` responses may be of independent interest to Telematics.

7. **Two scheduled fires were missed** on 2026-07-30 and 2026-07-31 for reasons on our side.
   No claim in this report rests on those two dates: journeys in that period are covered by a
   later request on 2026-08-03 that completed normally and reconciled exactly against the
   API's own advertised total.

---

## 10. Machine-readable companion

`artifacts/telematics_late_arrival_audit.csv` — one row per audited record, 1,031 rows.

Columns: `provider_trip_id, registration, trip_start_ts_utc, trip_start_ts_warsaw,
trip_end_ts, last_absent_request_id, last_absent_request_started_at_utc,
last_absent_response_received_at_utc, last_absent_requested_from, last_absent_requested_to,
first_present_request_id, first_present_request_started_at_utc,
first_present_response_received_at_utc, first_present_requested_from,
first_present_requested_to, first_local_seen_at, first_local_inserted_at, evidence_source,
classification, confidence, publication_lag_lower_bound_hours,
publication_lag_upper_bound_hours, detection_interval_hours, notes`

Notes on reading it:

- `*_request_id` values are our internal request-batch identifiers, included so that any
  two rows citing the same batch can be cross-checked against each other.
- `*_requested_from` / `*_requested_to` carry the literal values sent on the wire together
  with the timezone in which the API interprets them (§4).
- `*_response_received_at_utc` is the timestamp of the last page response for that
  interval — a genuine per-response time, not a run-level approximation.
- `notes` carries the identifier-derived allocation bracket described in §5.4.
- The file contains vehicle registrations, which Telematics already holds. It contains **no
  driver names or other personal data**.
