# 35 — Eco Driving presentation modernization (S12)

Stage `ECO_DRIVING_PRESENTATION_NON_BLOCKED_SUBSET`. Screens `ECO-001`, `ECO-002`, `ECO-003` of the
approved Log Platform design, implemented on top of the shared shell delivered by S1–S11.

This document records the **durable rules** the stage established. It is a presentation contract:
no scoring rule, aggregation rule, schema object or production snapshot was changed, and none may be
changed to make a screen match a picture.

---

## 1. The repository is the authority on every number

The design package states plainly that its thresholds, point weights, maxima and rating bands are
placeholder data invented for layout. They are **not implemented**. Every business value on an Eco
screen originates from one of exactly two places:

| Value | Source |
|---|---|
| metric definitions, ladders, points, maxima | `api/eco_driving_explorer/eco_scoring.py` (a byte-for-byte mirror of `jobs/ecodriving/eco_scoring.py`) |
| coefficients, counters, points, losses, score, rating, position, qualification | the persisted `eco_driver_{weekly,monthly}_stats` row |

The presentation layer derives display formatting and severity classes. It derives no score.

`api/eco_driving_explorer/score_presentation.py` is the only new derivation module, and it reads
`SCORING_RULES` / `METRIC_MAX_POINTS` directly rather than restating any constant.

## 2. The production rating vocabulary is unchanged

Three bands, with the boundaries in `RATING_THRESHOLDS`:

| Band | Rule |
|---|---|
| `bezpieczny` | score ≥ 85 |
| `akceptowalny` | score ≥ 40 |
| `niebezpieczny` | below 40 |

The four-band vocabulary drawn in the design (`bardzo dobra` / `dobra` / `przeciętna` /
`wymaga uwagi`) is **deliberately not adopted**. The three bands are business-significant: they are
shared with Eco reporting and with the weekly and monthly driver e-mails, and a fourth band in the
portal would put the portal and the e-mail in disagreement about the same driver.

A ranking group never replaces a rating. `EXCLUDED` describes membership of the primary ranking
population; it says nothing about how the driver drove. An excluded driver keeps their band, keeps a
reachable detail page wherever the existing permissions allow it, and appears under their own filter
chip with a visible count. `UNKNOWN_DRIVER` remains a separate persisted group and is not merged
into either of the other two.

## 3. Semantic colour is coefficient-based — the load-bearing rule

**Every green/yellow/red state on an Eco surface is derived from the applicable normalized
`/ 100 km` coefficient and the repository's scoring ladder. None is derived from a raw event count.**

The mechanism is structural rather than conventional:

- `metric_severity(metric_key, *, points, loss)` accepts the persisted per-metric points and the
  persisted `*_maxpoints_subtract` loss. It has **no count parameter**, so a caller has nothing to
  pass that could reintroduce a count threshold.
- `metric_cell(...)` takes the string to print (`displayed_value`) as an argument separate from the
  values it classifies by. The printed value is never read on the classification path.

Severity is therefore a property of the driver's coefficient and stays fixed when the user switches
the displayed unit:

| Severity | Meaning, in repository terms |
|---|---|
| `ok` | the coefficient landed on a rung that loses nothing against the metric's maximum |
| `warn` | the rung lost points but still awards a non-negative amount |
| `bad` | the rung awards negative points |

No numeric traffic-light threshold is invented anywhere. A large count is not coloured because it is
large: a 10 000 km driver with 400 cornering events is at 4 per 100 km and is `ok`, while a 150 km
driver with 34 events is at 23 per 100 km and is `bad`. That exact pair is the adversarial fixture in
`ops/tests_manual/test_eco_driving_presentation_s12.py`, asserted in **both** display modes.

Each severity also carries a non-colour signal: a marker glyph plus a visually hidden word
(`bez straty punktów` / `strata punktów` / `punkty ujemne`). The rating band is a word badge, so it
reads with colour removed entirely.

## 4. `/ 100 km ⇄ Σ suma` is a display switch and nothing else

Both values are already persisted — the normalized coefficient in `*_events_per_100km` and the raw
counter in the event column — so the toggle selects between two stored facts and derives neither.

It **must not** and does not change the scoring algorithm, the ranking, the ranking position, the
qualification status or the rating band. It is two real links carrying `unit=` in the URL, so it
works with scripting disabled, is keyboard operable, exposes its state through `aria-current`, and a
filtered view stays shareable and survives Back.

On the driver detail page the toggle now also selects which of the two values is **primary**
(`docs/36` §12 A). Both stay visible — no business information is hidden to make a control work — and
the classified, coloured cell is the primary one. Because `metric_severity` has no count parameter,
`Σ` mode is still coloured by the `/ 100 km` coefficient.

## 5. Ranking groups are chips, not tabs

One ranking, filtered. `INCLUDED` / `EXCLUDED` / `UNKNOWN_DRIVER` render as filter chips with their
persisted counts from the period row; a non-default group additionally shows a removable active
filter chip. Three tab-like pseudo-pages would have implied three equal rankings, which is not what
the data is.

Rows outside every ranking population — the non-`QUALIFIED` ones — are reported as a separate count
(`poza rankingiem`) and are never folded into a group.

Free-text search over the driver name and the tag ID is a plain `GET` form. The term is bound, its
LIKE metacharacters are escaped so `100%` searches for the literal string, its length is bounded,
and the audit records only that a search was applied — never the term.

## 6. Lineage is stated once, in the context bar

`Linia danych: zrekonstruowana ze stanu bieżącego` is constant for a whole period, so it belongs in
the context bar rather than on every ranking row. The per-row lineage column is removed.

This is a presentation consolidation only. The underlying lineage, audit and data-quality
information is untouched, and `LineageQuality` still deliberately has no `EXACT_SNAPSHOT` member.

## 7. Driver detail: six sections, fixed order

`ECO-003` is one scrollable page. The order is part of the contract and is asserted directly against
rendered HTML through the `data-eco-section` markers:

1. `score` — **Wynik i pozycja**: score and position at equal weight, rating band, percentile, and
   the fleet histogram with this driver's bucket marked.
2. `trend` — **Trend wyniku**: the driver's own last eight comparable periods.
3. `identity` — **Tożsamość wpisu i okres**: the 16 persisted fields, with an exclusive period end
   and the assigned ID labelled opaque.
4. `composition` — **Z czego składa się wynik**: one row per metric, sorted by points lost.
5. `progression` — **Przebieg narastający w miesiącu**: the month's cumulative snapshots, as
   diagnosis. (Renamed from `Wkład tygodni`; see `docs/36` §12 F.)
6. `trips` — **Przejazdy w podstawie rankingu**: the evidence.

### 7.1 Composition, loss and share

| Column | Source |
|---|---|
| `Σ zdarzeń` | persisted event counter |
| `/ 100 km` | persisted normalized coefficient |
| `Próg` | the applicable rung of the metric's ladder — see §7.2 |
| `Punkty` | persisted per-metric points |
| `Maks` | `METRIC_MAX_POINTS`, per metric |
| `Utracone` | persisted `*_maxpoints_subtract` |
| `Udział w utraconych punktach` | that loss over the driver's own total loss magnitude |

`Utracone` keeps the repository's sign. `*_maxpoints_subtract` is a scoring delta and is always
`<= 0`; rendering it as a positive magnitude would invert its meaning against the persisted column,
the aggregation job and the e-mail report. Loss is read from the persisted column and is recomputed
as `min(points - max, 0)` only when a row has no persisted value — never inferred from a difference
between scores.

Share is `|loss| / Σ|loss|` for the same driver and period, in percent. When the driver lost nothing
at all the share is **undefined for every metric** and the table says so, rather than printing a
manufactured `0 %` of a zero denominator. A metric that lost nothing while others did is a genuine
`0 %` and is shown as such.

Rows sort by lost-point impact descending — the question is "what is hurting me", not "what did I
score". Equal losses fall back to the canonical `REQUIRED_METRICS` order, so the table is stable;
metrics with no loss value at all sort last.

### 7.2 `Próg` presents a ladder, not a threshold

The repository scores each metric with a **multi-step bucket ladder**, not one universal threshold.
Presenting the first bucket bound as though it were "the" threshold would misstate the business
rule, so the column shows the rung that applies to the current coefficient *together with its
position in the ladder* (`≤ 5 · krok 3/7`), with every rung in the cell's tooltip and an explicit
note that the scoring is multi-step.

This is a deliberate, recorded divergence from the single-value `Próg` drawn in the design: the
design's column cannot represent a ladder without misleading the reader, and truthfulness wins.

### 7.3 Trend

Sourced from the repository's existing `eco_driver_weekly_trends_view` /
`eco_driver_monthly_trends_view`. No new trend metric is defined. The approved window is the last
eight comparable periods, read newest-first by the view and reversed once for chronological display.

A period the driver has no persisted row for is **absent**, not zero-filled. A period whose
persisted row did not meet the distance threshold contributes its column but **not its score** —
otherwise the detail page would show through the trend exactly the number it refuses to show for the
current period.

### 7.4 Trip evidence

The permission boundary is unchanged. Without `can_view_eco_trip_details` the section stays on the
page and explains itself; it never disappears silently. `can_view_eco_trip_routes` remains a
separate grant and alone unlocks nothing here. No route, location, vehicle, personal or raw source
field is rendered.

Trip-level violation values are always Σ sums for that one trip, and the captions say so.

## 8. Fleet histogram

One client, one period, one ranking group — the same authorized universe as the ranking above it.
Its heading names that universe rather than calling a filtered subgroup the fleet (`docs/36` §12 B).
The query carries the trusted `client_id` from the resolved binding and the same period identity the
page is already authorized for, so the histogram cannot widen the disclosure surface. With no group
filter it still restricts to rows that belong to *some* ranking population.

Bins span the repository's own score domain, `MIN_POSSIBLE_SCORE` to `MAX_POSSIBLE_SCORE`
(`-100`…`100`), in 20 buckets of 10 points, with the top bucket closed on both sides so a perfect
`100` lands in a bucket that exists. The binning rule is implemented once and applied identically in
SQL and in Python (`score_bin_index`), so the two cannot drift.

**Recorded divergence:** the design specifies 24 buckets. That count belongs to a `0…100` axis; the
repository's score domain is `-100…100`, and a 24-bucket split of it has no clean width. Twenty
10-point buckets cover the real domain exactly, including negative scores, which a `0…100` histogram
would silently drop.

Median and mean are computed over the same scoped universe and stated numerically.

## 9. The 100 km rule is a reporting-period rule

`MIN_QUALIFYING_DISTANCE_METERS = 100_000`, evaluated once against the **total qualifying distance
of the whole selected reporting period**, exactly as `_qualification_and_calculation` in the
aggregation job does it:

| Period distance | `qualification_status` |
|---|---|
| `0` | `NO_DISTANCE` |
| `0 < d < 100 000 m` | `LOW_DISTANCE` |
| `d >= 100 000 m` | `QUALIFIED` |

The boundary is exact and is asserted in metres, never inferred from a rounded kilometre display.

A period that is not `QUALIFIED` renders the truthful insufficient-distance (or zero-distance) state
in place of the score, the position and the composition table. The states are distinct, because
"drove too little" and "drove nothing" are different facts.

**There is no daily gate and none may be added.** When the period qualifies, an individual short day
is still part of it: a 4 km trip inside a qualifying period stays listed in the trip evidence. The
threshold is evaluated once, for the period, and never again for a row.

## 10. Client-specific trip inclusion is unchanged

S12 changed no aggregation or input semantics. The current per-client contracts stand:

- **ALPHA00001** continues to exclude trips marked private, per its existing aggregation contract;
- **BRAVO00016** continues to include all trips for the driver, per its current approved contract.

The two clients are deliberately **not** standardized to one policy here. The presentation consumes
repository-authoritative persisted results and adds no filter of its own.

## 11. Deliberately removed panels

Removed by owner decision (`D-003`). Their absence is a requirement, and they must not reappear
under a different heading:

| Removed | Why | Returns when |
|---|---|---|
| Reconciliation panel | no discrepancy-tracking tooling exists yet | discrepancy tracking exists |
| Score-definition / "how the score was calculated" panel | superseded | — |
| The threshold/rating-band table attached to that panel | superseded by the in-table ladder representation | — |
| Per-row lineage badge | constant for a period; moved to the context bar | — |

**Only the panels were removed.** The reconciliation service keeps its MATCH / MISMATCH /
UNAVAILABLE contract, its sanitized-error path and its audit event; `ScoreDefinition` is still built
and still served by the JSON API. Both are asserted directly against the service so that deleting a
UI panel can never quietly delete a domain capability.

## 12. Shared platform

Eco Driving renders inside the shared shell: app bar, client context bar, section navigation,
`AUTO`/light/dark tokens, translation keys, focus-visible conventions. The module's inline
`<style>` block is retired in favour of the versioned page asset `api/static/css/eco-driving.css`,
which defines no literal colour — every value resolves to a `--lp-*` token — so there is no second
palette and no third shell. Eco-specific responsive behaviour is layered on the S10 contract; the
ranking and detail tables scroll horizontally and never become one card per row.

## 13. Explicit S12 exclusions

These are **not implemented**, deliberately, and must not be described as delivered.

### 13.1 Month + arbitrary multi-week basis widget (`EC-1`–`EC-7`)

**Delivered by the next stage. See `docs/36_eco_driving_arbitrary_week_basis.md`, which is now the
authoritative contract for the period model.** The rest of this section records why S12 did not do
it, and remains true of S12 itself.

The persisted weekly ranking periods are **cumulative month-to-date snapshots**, not isolated ISO
weeks. Therefore S12 does not sum `W1 + W2`, does not present a cumulative snapshot as an isolated
week, and does not offer arbitrary week selection. Two consecutive cumulative snapshots share a
prefix; adding them would double-count it.

The in-month diagnostic (section 5) presents the snapshots as a **running total with a per-step
delta**, marked cumulative in both the UI copy and the JSON envelope
(`is_cumulative_snapshot: true`), and states that the ranking is computed on one persisted
cumulative period rather than on summed weeks.

**Arbitrary week selection requires dynamic recomputation from the underlying Eco assignments.** It
cannot be obtained by summing the persisted cumulative snapshots, and it is a separate domain and
backend task. That work has since landed (`docs/36`): the persisted cumulative snapshots are still
never summed, and the in-month progression described above is still exactly what it says it is. The
progression heading changed from `Wkład tygodni` to `Przebieg narastający w miesiącu`, because once
isolated-week selection exists as well the old wording could be read as a per-week contribution.

### 13.2 Per-trip `Wkład w wynik` (`EC-21`–`EC-24`)

Not invented. The Eco score is a step function of aggregate normalized rates and is **not additive
across trips**, so a per-trip contribution is not derivable from the existing business logic.
Neither a formula nor a proportional estimate is provided, and no default trip filter is built on
such a value.

### 13.3 Trip → raw Database Explorer row link (`EC-25`)

Out of this commit. The hidden-row-identity architecture exists, but production dataset
row-identifier configuration is separately authorization- and deployment-gated. No raw `record_id`
is exposed. A later narrow integration task may use the S6 opaque references once the dataset
identity configuration path is ready.

### 13.4 Position-change `Δ` column

The approved ranking draws a `Δ` column against the previous comparable period. It is not
implemented: a ranking-wide delta requires re-basing the ranking read on the trend view, which
changes the ranking query path rather than its presentation. The per-driver equivalent is present on
the detail page as the trend's `Zmiana vs poprzedni okres` footnote.

## 14. Security invariants that did not change

Client/provider registry isolation, the trusted server-side `client_id`, `can_view_eco_ranking`,
`can_view_eco_trip_details`, `can_view_eco_trip_routes`, the no-admin-bypass rule and the audit
contract are all unchanged.

The histogram, the trend and the in-month progression are new **read** surfaces and are gated by the
same `can_view_eco_ranking` check, the same trusted binding and the same period identity as the
ranking. Each is scoped to one client; there is no cross-client aggregate anywhere. Each reuses an
existing audit event type with a `surface` discriminator rather than widening the audit vocabulary.

One defect was fixed in passing, because requirement "a below-threshold driver must reach a truthful
state" cannot hold without it: `get_ranking_entry` audited `entry.ranking_group.value`
unconditionally, so any row outside every ranking population (`ranking_group IS NULL`) raised and
returned a 500. It now records `None` for such rows.

## 15. Schema

`NO_SCHEMA_CHANGE`. Every column S12 reads — including the eight `*_maxpoints_subtract` values and
both trend views — already exists from migrations `027`–`037`.
