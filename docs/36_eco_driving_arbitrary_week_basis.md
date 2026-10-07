# 36 — Eco Driving month + arbitrary-week ranking basis

Stage `ECO_DRIVING_ARBITRARY_WEEK_SELECTION_DYNAMIC_RECOMPUTATION`. Closes the criteria `EC-1`–`EC-7`
that S12 deliberately left out (`docs/35` §13.1), and carries the six accepted S12 review findings to
completion.

This document records **durable rules**. It is a domain contract as well as a presentation one,
because the feature changes what a number on the Eco screens means.

---

## 1. Two period models now coexist, and they must never be confused

| Model | What it is | Where it comes from |
|---|---|---|
| **Persisted cumulative snapshot** | `eco_{driver,person}_weekly_stats`: a month-to-date running total, `period_start_date` is always the month start | written by each family's aggregation job |
| **Isolated week bucket** | the *incremental* segment ending at the same boundary, `[start, end)` | derived, never stored |

They **share a boundary and a label and nothing else**. `2026-07-W3` as a persisted row covers
1–20 July; `W3` as a bucket covers 13–20 July.

**Cumulative snapshots are never summed, subtracted, averaged or rescored to answer a week
selection.** Two consecutive snapshots share a prefix, so adding them double-counts it. The
regression that proves this is `test_arbitrary_selection_is_not_a_snapshot_sum`: underlying isolated
weeks of 10 / 8 / 7 events produce snapshots of 10 / 18 / 25, and a `W1 + W3` selection must return
**17**, never the snapshot sum of **35**. The test asserts a second metric and the distance too, so a
snapshot-derived implementation cannot pass by coincidence.

## 2. Week boundaries are the aggregation job's own, not ISO weeks

`api/eco_driving_explorer/period_domain.month_week_buckets` is the **single** definition, and
`job_eco_driving_aggregate._month_bounded_weekly_periods` now builds its persisted periods from it.
The person job uses the same boundary rule for its own periods.

- the first bucket runs from the first of the month to the next Monday;
- full buckets are Monday-to-Monday;
- the last bucket is truncated at the month end;
- a bucket shorter than seven days is `is_partial`;
- boundaries are **business-timezone local midnights** (`Europe/Warsaw`), lower bound inclusive,
  upper bound exclusive;
- labels are `W1`…`W6`, and week identity is **month-relative**. A month yields **four to six**
  buckets: one starting on a Sunday produces a one-day `W1` plus a trailing `W6` (March 2026 and
  August 2021 are real examples), while a 28-day February starting on a Monday produces exactly
  four. A week id is valid only when `month_week_buckets(month)` defines it, so `W6` is accepted in
  a six-bucket month and refused in every other — a week id from another month is never
  reinterpreted, and no timestamp is derived from one the month does not define.

Adjacent selected buckets are merged into one interval before the query is built, so a trip at a
shared boundary instant cannot satisfy two predicates.

## 3. Whole month is the persisted monthly snapshot; a subset is recomputed

The server resolves exactly one mode from the canonical selection. There is no state in which the
whole month and a week subset are both active.

| Mode | Trigger | Data path |
|---|---|---|
| `MONTH` | `weeks` absent, or every week listed | see the source resolution below |
| `WEEKS` | a proper, non-empty subset | dynamic recomputation from the family's assignments |
| `EMPTY` | `weeks=none` | a prompt to select a week (`EC-5`); **no query is issued** |

`MONTH` is one logical user selection with one URL. Its **execution source** is resolved by the
server and reported as `basis_source`:

| `basis_source` | When | Behaviour |
|---|---|---|
| `MONTH_PERSISTED` | a canonical monthly snapshot exists for that month | that row is served |
| `MONTH_DYNAMIC` | no monthly snapshot exists yet | the full canonical month range is aggregated dynamically |
| `WEEKS_DYNAMIC` | a proper subset | dynamic over the selected union |
| `EMPTY` | zero weeks | nothing is read |

Whole month **prefers** the persisted monthly result, because that row is the official monthly
reporting truth shared with the monthly e-mail and report. It stays preferred even when the
underlying assignments have since changed: history is not silently rewritten, and a proper subset of
that same month still recomputes dynamically.

The fallback exists because a month can be selectable before its snapshot is materialised — a
current month typically is. Reading only the stats table there would report an empty ranking for a
month that plainly has data. Selecting every week of such a month canonicalizes to the same logical
whole month and returns the same dynamic result, not zero. When a month has neither a snapshot nor
any source assignments, the page states that truthfully instead of fabricating a result.

Equivalence is established by test, not by substitution:
`test_full_month_dynamic_parity_with_the_canonical_generator` runs the dynamic aggregation and
`_stats_row_from_aggregate` over the identical trip set and asserts equality of qualifying distance,
every event total, every normalized rate, every metric point value, every loss, the total score, the
qualification status, the calculation status, the rating and the ranking group.

## 4. Two ranking families, two real data contracts

There are two production Eco pipelines, and they are **not** one contract with different table names.
Each has an executable provider, selected only from the registry allowlist by the trusted server-side
`(client_code, ranking_family)` binding.

| | driver family | person family |
|---|---|---|
| client | `ALPHA00001` | `BRAVO00016` |
| provider | `AlphaDriverEcoDrivingProvider` | `BravoPersonEcoDrivingProvider` |
| aggregation job | `jobs/ecodriving/job_eco_driving_aggregate.py` | `jobs/ecodriving_person/job_eco_driving_person_aggregate.py` |
| assignments | `public.eco_trip_assignments` | `public.eco_person_trip_assignments` |
| identity | `assigned_id` (opaque driver tag) | `person_name_group_key` (one physical person) |
| display name | roster `eco_drivers_id_chart.driver_name` | canonical `person_name` resolved while assigning |
| roster | `public.eco_drivers_id_chart` | `public.eco_person_people_email_view` |
| stats | `eco_driver_{weekly,monthly}_stats` | `eco_person_{weekly,monthly}_stats` |
| trends | `eco_driver_*_trends_view` | `eco_person_*_trends_view` |

Both share the shared canonical domain for week geometry, quantization, the 100 km gate, the ranking
group rule, scoring and ranking order. Everything family-specific is a `queries.FamilySources`
descriptor of fixed repository-controlled names — never caller input.

The descriptor governs **every** executable surface, not just the dynamic one:

| Surface | Family-resolved |
|---|---|
| dynamic aggregation, trips, month list | assignments table, identity column, inclusion predicate, roster join |
| persisted ranking list / entry / distribution | stats table, stats identity column, roster join and name |
| persisted ordering, tie-break, sort allowlist | stats identity column |
| trend, in-month progression | trend view, stats identity column |
| reconstruction totals, window diagnostics | assignments table, identity column, inclusion predicate |

Migration `043` renamed the person family's identity from `assigned_id` to `person_name_group_key`,
so a driver-shaped `ORDER BY`, tie-break or reconciliation query would name a column that does not
exist on person stats. Sort keys stay a **semantic vocabulary** in URLs — `assigned_id` means "the
entry's identity" — and resolve to the physical column through the descriptor, so no physical table
or column name is ever accepted from or exposed to a browser.

Reconciliation deliberately reads the same source, identity and inclusion contract as ordinary
aggregation: comparing a persisted row against a *different* trip universe would not be a
reconciliation. The window diagnostics query is the one deliberate exception to the inclusion
filter — it is unfiltered so excluded trips can be counted — but it still scopes to the family's own
table and identity.

The dynamic basis reads the family's assignments table: `client_id` (trusted binding) +
`trip_start_ts` inside the selected union + the family's identity column `IS NOT NULL`, with totals
filtered by that family's inclusion predicate and a `HAVING` requiring at least one included trip —
field for field its own job's `_fetch_aggregate_rows`.

## 5. Client-specific trip inclusion is the family's own predicate

The inclusion decision is **read back** from the column the family's job wrote. It is never
re-derived, and there is no generic private-trip rule anywhere in the portal.

**ALPHA00001 — driver family.** The job writes
`aggregation_included = (assigned_id IS NOT NULL AND is_private_trip IS FALSE)`, and migration `028`
adds `chk_eco_trip_assignments_private_trip_exclusion`, which makes an included private trip
unrepresentable. The dynamic predicate is
`a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE` — verbatim from that job, including
the clause the job itself keeps as belt and braces. **Private trips remain excluded.**

**BRAVO00016 — person family.** The job writes `aggregation_included = (match_count = 1)`: inclusion
depends solely on resolving exactly one physical person for the trip's driver name. The private
driver-tag flag is recorded on the row but has **no bearing on inclusion**, and migration `043`'s
`chk_eco_person_trip_assignments_identity_outcome` ties inclusion to a resolved identity alone. The
dynamic predicate is `a.aggregation_included IS TRUE`, verbatim from that job, with **no private-trip
clause**. An applicable trip with `is_private_trip = TRUE` and `aggregation_included = TRUE`
therefore **does** contribute to score, ranking and trip evidence.

Applying the driver family's exclusion to the person family would silently drop real production
data; applying the person family's rule to the driver family would admit trips its own pipeline
excludes. Neither can happen: each predicate is carried by that family's own descriptor, no
executable line of the query layer or the shared domain names a client, and the registry refuses any
`(client, family)` pair outside its allowlist.

Trip evidence uses the **same** family predicate as scoring, so the rows shown are exactly the rows
that produced the number.

## 6. The 100 km rule applies once, to the whole selected union

`MIN_QUALIFYING_DISTANCE_METERS = 100_000`, evaluated once against the total qualifying distance of
the **entire** selected union, exactly as `_qualification_and_calculation` does it.

- `W1` 60 km alone → not qualified;
- `W1` 60 km + `W3` 55 km → 115 km → qualified;
- exactly `100 000 m` → qualified; `99 999 m` → not.

There is **no per-week, per-day or per-trip gate and none may be added**. A 2 km day inside a
qualifying selection remains legitimate trip evidence.

## 7. Scoring, bands and ranking are the production ones

`period_domain.score_aggregate` is the one scoring entry point for the job and the portal. It restates
no threshold, weight or maximum: every one is read from `eco_scoring`. The three production bands —
`bezpieczny` / `akceptowalny` / `niebezpieczny` — are unchanged, and no design-only band appears.

Ranking is **recomputed** for the selected basis over the whole authorized population, then filtered,
searched and paged. It is never a filtered persisted ranking and never reuses a position from another
period. Canonical order and ties: score descending as ordinary numeric ordering, then total
kilometres descending, then the family's identity column ascending; rows without a score sort last.
Positions are assigned per ranking group (`INCLUDED`, `EXCLUDED`), so a group filter, a search term
or a page boundary cannot change anybody's rank.

**A score of exactly `0` is a real score and ranks above every negative score** — `1 > 0 > -1 > NULL`.
It is not a missing value, not an unqualified row and not NULL, and the states stay distinct. The
ordering is tested with an explicit `is None`; falsy-value fallbacks sorted a legitimate `0` below
`-1`. One key, `period_domain.score_order_key`, now serves the scheduled generators **and** the
portal's dynamic pagination, and the persisted path expresses the same rule as
`eco_driving_score_total DESC NULLS LAST`. Dynamic entries are ordered **before** page slicing, so a
page boundary, a group filter or a search term cannot renumber anybody. See §13.

`INCLUDED` / `EXCLUDED` / `UNKNOWN_DRIVER` keep their meanings and are derived per basis by
`period_domain.ranking_group` — never by voting across historical snapshots. A row that is not
`QUALIFIED` belongs to no group and is reported separately. An `EXCLUDED` driver keeps a normal
rating and a reachable detail page.

## 8. What the driver page shows for a dynamic basis

The detail page resolves its execution source exactly as the ranking does, so the two can never
disagree about which data path produced the number on screen.

The selected basis drives the **current-period** surfaces: score, position, participants, rating,
composition, the fleet histogram and the trip evidence. The breadcrumb and a context line state the
basis, and every link on the page carries it, so the page cannot quietly fall back to another period.

The histogram is derived from the same recomputed population in memory — same client, same union,
same group — so a persisted monthly distribution cannot leak into a `W1 + W3` view, and no separate
distribution query is issued.

**Trend and progression stay persisted history.** No synthetic period is invented for an ad-hoc
selection and nothing is written anywhere. For a dynamic basis the "current period" marker is
stripped from both, because none of those persisted periods is the selection on screen.

## 9. URL and state model

Canonical form: `month=YYYY-MM` plus `weeks=1,3`.

- ascending, deduplicated, month-relative;
- whole month **omits** `weeks`, so "all weeks listed" and "no weeks parameter" produce one URL;
- the empty state is the explicit token `weeks=none`, because an empty query value disappears when a
  URL is built and would silently become whole month;
- an unknown, out-of-month or malformed week identifier is **refused**, as is a malformed month;
- the week list is length-bounded and the interval predicate is capped at `MAX_SELECTION_INTERVALS`;
- no timestamp ever comes from the browser;
- when `month` is present, `period_key` is **ignored entirely** — one page resolves one period model;
- nothing is persisted to the database, to preferences or to `localStorage`.

Browser-supplied month and week state is selection input. It is never authorization.

## 10. Security

Unchanged. `can_view_eco_ranking` gates the ranking, the histogram and the detail;
`can_view_eco_trip_details` gates trip evidence; `can_view_eco_trip_routes` remains separate and
unlocks nothing here; administrators do not bypass client grants. Every dynamic query is bound to the
trusted server-side `client_id`; there is no cross-client aggregate and no raw identifier is exposed.

Audit reuses the existing event types and records only safe basis facts: mode, month, selected week
identifiers and their count, group, result counts, and whether a search was applied — never the
search term, never a trip row, never SQL. A surface that was not rendered is not audited as rendered.

## 11. Performance

One statement per basis. A dynamic ranking issues a single set-wise aggregation over the client's
assignments for the selected union and derives the ranking, the group counts and the histogram from
it in memory; the empty basis issues none. There is deliberately no per-driver query.
`test_query_shape_is_bounded_and_has_no_n_plus_one` asserts one call for a 40-driver, 200-trip month.

The read is bounded by one client, one month and at most that month's week buckets — six intervals
in the worst case, enforced by `MAX_SELECTION_INTERVALS`. Each family uses its own existing
`trip_start_ts` index (`idx_eco_trip_assignments_trip_start_ts`,
`idx_eco_person_trip_assignments_trip_start_ts`); no index, cache or schema object was added, and the
whole-month dynamic fallback is a single merged interval, not one query per week.

## 12. S12 review carry-forward, resolved

| # | Finding | Resolution |
|---|---|---|
| A | `/ 100 km ⇄ Σ` inert on `ECO-003` | Both values stay visible; the toggle selects which is **primary**. The primary cell is the classified one, and `metric_cell` classifies from persisted points/loss — never from the printed number — so `Σ` is coloured by the `/ 100 km` coefficient. The active unit is exposed as `data-eco-unit`. |
| B | Histogram labelled as the fleet | `distribution_heading` names the actual universe: the whole ranked population, or the approved group label for a filtered one. A universe note travels with the plot. |
| C | Invisible histogram work | A non-qualified detail page issues no distribution read and records `distribution_rendered: false`. |
| D | Dead `unit` parameter | Removed from `render_progression`. |
| E | Context client identity | The context **client** field is the client code on every Eco surface. The provider/family description lives in the module name, the family badge and the detail identity grid — it is no longer rendered as the client's name. |
| F | `Wkład tygodni` | Renamed to `Przebieg narastający w miesiącu`, with a caption and note that state the rows are cumulative, overlap, must not be summed, and are **not** the isolated-week selection. |

## 13. The zero-score ranking correction

The shared ordering rule previously used a falsy-value fallback for the score, so a driver who scored
exactly `0` was treated as unscored and ranked **below** a driver who scored `-1`. That contradicts
canonical descending-score ranking.

The fix is in the shared domain, so every ranking path uses it:

- `jobs/ecodriving/job_eco_driving_aggregate.py` (driver family);
- `jobs/ecodriving_person/job_eco_driving_person_aggregate.py` (person family);
- the portal's dynamic recomputation;
- the portal's dynamic **pagination**, which carried a second copy of the same falsy fallback and
  affected recomputed entries that have no assigned position — the state an `UNKNOWN_DRIVER`
  population is always in.

**Blast radius, stated precisely.** Only ranking **order and position** change, and only for the
zero-versus-negative case within one ranking group. Unchanged: every scored value, the metric points,
the losses, the total score, the qualification status, the rating band, the ranking group, the
rating-band share, the group counts and every other persisted business field. `NULL`/unscored rows
still sort last and stay distinct from a zero.

**No historical data is touched.** This task performs no backfill, no snapshot recalculation and no
row mutation; persisted historical snapshots remain historical truth. A future scheduled execution
after deployment would write the corrected order. Nothing is deployed here.

## 14. Still out of scope

- **Per-trip `Wkład w wynik` (`EC-21`–`EC-24`)** — the score is a step function of aggregate
  normalized rates and is not additive across trips. No proportional, counterfactual or marginal
  attribution is invented. **Unresolved.**
- **Trip → raw Database Explorer row (`EC-25`)** — no `record_id` is exposed and no production
  dataset row-identifier configuration is performed. **Unresolved.**
- **Position-change `Δ` column** — unchanged from S12.

## 15. Schema

`NO_SCHEMA_CHANGE`. No migration, no new table, no new column, no new index.

**No ad-hoc selection is ever persisted.** Nothing is written to the ranking, trend, snapshot or job
tables, and no historical period is recalculated or replaced. The whole feature is a read.
