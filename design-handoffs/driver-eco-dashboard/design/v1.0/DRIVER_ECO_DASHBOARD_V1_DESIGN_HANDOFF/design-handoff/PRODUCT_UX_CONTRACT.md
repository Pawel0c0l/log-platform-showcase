# PRODUCT_UX_CONTRACT

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`
**Canonical visual source:** `../Eco Driving Dashboard.dc.html`

Information architecture and behaviour of the four experiences. One product, one component system, two axes of variation: `period_type = weekly | monthly` and `view = summary | detailed`.

---

## 1. The single mental model

The dashboard answers four questions in a fixed order, and every screen keeps that order:

```
1. WHERE AM I ?      score · classification · position on the score axis
2. AM I MOVING ?     delta vs an explicitly dated comparison basis · rank movement · trend
3. WHY ?             categories: count → coefficient → band → points lost
4. WHAT NEXT ?       deterministic coaching · next-better-band opportunity
                     ( Detailed view = the "which days" drill-down of 3 )
```

`VISUAL DESIGN DECISION`: this order is never re-arranged between weekly and monthly, so a driver who learns one screen knows all four.

## 2. What differs between weekly and monthly

Only three things differ. Everything else is identical.

| Aspect | Weekly | Monthly |
|---|---|---|
| **What the headline number is** | the closed **cumulative month-to-date** snapshot | the **closed calendar month** |
| **Comparison basis** | the previous weekly mailing's closed MTD snapshot, e.g. `01.07 – 12.07` vs `01.07 – 19.07` | the previous closed calendar month, e.g. `01.06 – 30.06` vs `01.07 – 31.07` |
| **Trend series** | the closed MTD snapshots of the current month (each bar = "from the 1st to this closing date") | the same MTD progression, read as the shape of the finished month, with the previous month's final score as a reference line |

`BUSINESS/DATA CONTRACT` — weekly semantics (AS-IS `SCORING_RANKING_CONTRACT` §5.1, owner decision `D-03`):

- `period_start_date` is **always the 1st of the month**. Only `period_end_date` advances.
- The comparison is snapshot-to-snapshot, i.e. 19 cumulative days vs 12 cumulative days. It is **not** 13–19 vs 06–12.
- Displayed end date = `period_end_date − 1 day` (the stored boundary is exclusive).
- A period never spans a month boundary. When a new month starts, the series restarts and the comparison basis changes; label both ranges explicitly and never imply equal period lengths.
- The words "ostatni tydzień", "w tym tygodniu", "last week", "this week" are **banned** from the UI. Reason: snapshots can be skipped (AS-IS `G-08`: `2026-08-W1` was never produced), so "previous" is only ever defined by its dates.

Required weekly framing devices (all three must be present):

1. The period header states the range plus the chip **"Narastająco od 01.07 — nie »ostatni tydzień«"**.
2. The sequence chip states which closed snapshot this is: **"3. z 5 zamkniętych okresów lipca · zamknięty 20.07.2026"**.
3. The comparison block states the previous range in full plus one sentence: **"Porównujemy 19 dni z 12 dniami — nie dwa osobne tygodnie."**

## 3. SUMMARY view — required blocks, in order

| # | Block | Component | Required | Notes |
|---|---|---|---|---|
| 1 | Period header | `PeriodHeader` | always | range, cumulative/closed chip, sequence chip, comparison basis with dates and one explanatory line |
| 2 | Eco score | `ScoreCard` + `TotalScoreAxis` + `DeltaChip` | always | score, classification pill with its numeric range, delta vs basis, previous-period ghost marker, distance-to-next-band |
| 3 | Ranking | `RankingCard` **or** `RankingNotice` | conditional | `18 / 158` + movement; never a bare `#18`; never a fabricated delta |
| 4 | Rating group | `GroupShareCard` | when ranked and scored | share % of the ranked population in the driver's group + the full three-group distribution bar |
| 5 | Distance | `DistanceKpi` | qualified dashboard only | qualifying distance, delta vs basis, trip count, and the standing reminder that every coefficient is per 100 km |
| 6 | Coaching | `CoachingGrid` (4 × `CoachingCard`) | when a score exists | largest loss · largest improvement · largest deterioration · highest-value opportunity; each card is explainable through its value line + factual sentence (no literal "Dlaczego" field) |
| 7 | Categories | `CategoryTable` (8 × `CategoryRow` + expandable `ScoringAxis`) | always | count · coefficient · band ladder · points/points lost · expandable full axis |
| 8 | Trends | `SnapshotTrend` + `CategoryTrendList` | when ≥ 2 comparable periods | total score across the month's closed snapshots; per-category points-lost movement |

Ordering rationale (`VISUAL DESIGN DECISION`): coaching sits **above** the category table because it is the answer, and the table is the evidence. A driver who reads only the first screenful still leaves with one concrete action.

### 3.1 Category table ordering

`BUSINESS/DATA CONTRACT` — default order is the fixed declaration order of `REQUIRED_METRICS` (over-rev, braking, acceleration, turning, idling, 140–160, 160–170, 170+), because that order is what breaks ties in `top_1_validation` and in coaching. `VISUAL DESIGN DECISION`: an optional secondary sort by points lost may be offered, but the default must be the declaration order so the list is stable between periods.

### 3.2 Progressive disclosure

One category row is expanded by default: the one identified as `LARGEST_LOSS`. `VISUAL DESIGN DECISION`. All others expand on click/Enter. Expansion reveals: the full scoring axis with band labels, points-lost per band, the driver's marker, the target marker, and three explanation cards (current state / potential / change vs basis).

## 4. DETAILED view — required behaviour

Purpose: explain how the aggregate was built, one row per reporting day, without ever implying that days sum to the period.

| # | Block | Required contents |
|---|---|---|
| 1 | Period header | identical component to Summary — same range, same comparison basis |
| 2 | Day KPIs | qualifying distance in the period · days with driving / reporting days · days evaluated by colour · the period score (with the note "liczone raz dla całego okresu") |
| 3 | Day table | week-segment groups → day rows → expandable per-day coefficient detail |
| 4 | Legend | the count-vs-colour distinction, the neutral threshold, and the "daily points do not sum" statement |

### 4.1 Row contents

`BUSINESS/DATA CONTRACT`. Each day row shows:

- date + weekday abbreviation (Polish, `Europe/Warsaw` day boundary — AS-IS `CLIENT_VARIANTS` §4.2: a trip belongs entirely to its **start** day);
- qualifying distance for the day;
- one **column per live category**, each cell holding that day's **event sum** as a plain number, with the cell background carrying the coefficient's scoring band (owner decision: no glyphs in cells, no day-status column);
- expand → per-category "N zdarzeń · X/100 km · próg «5–6»" plus one sentence explaining the day's evaluability;
- week-group rows and a dark period-totals row carrying the per-column event sums, which must reconcile exactly with the Summary's period figures.

**The count and the colour are different concepts and must be visually separated.** The count is monospaced text; the colour lives in the chip fill, its border and its glyph. Two rows with the same count legitimately differ in colour because their distances differ. The reference implementation demonstrates this: `Postój 7` on a 183 km day is yellow while `Postój 8` on a 129 km day is red.

**No daily point values are displayed.** `BUSINESS/DATA CONTRACT` (AS-IS `SCORING_RANKING_CONTRACT` §5.4): scoring is non-linear in a ratio, so daily points cannot sum to the period score. The design removes the temptation rather than labelling it away.

### 4.3 No aggregate day status

`BUSINESS/DATA CONTRACT`. There is **no** `day_status` and no whole-day Eco colour. No approved rule defines how several independent Eco categories collapse into one daily classification, and the design must not invent one (not "worst category wins", not an average, not a count of red categories, not a weighted score). A day carries **per-category statuses only**, each derived independently from that category's coefficient and scoring bucket. The row or card may summarise the day's *data* (distance, trips, event sums) but never assigns the day one Eco colour.

### 4.2 Grouping and density

| | Weekly Detailed | Monthly Detailed |
|---|---|---|
| Rows | all days of the MTD snapshot (1 … 31 depending on when the snapshot closed) | all days of the closed month (28–31) |
| Grouping | Monday-anchored segments **within the month**, matching `_month_bounded_weekly_periods` | same |
| Default expansion | the **latest** segment expanded and badged "najnowszy tydzień okresu"; earlier segments collapsed but summarised | all segments collapsed except the one with the most `red` days |
| Group summary line | segment distance · number of reporting days · count of days with point loss | same |
| Extra controls | "Rozwiń wszystkie tygodnie" (all week groups) | same |

`BUSINESS/DATA CONTRACT`: the weekly Detailed view lists the days of the **snapshot**, not the days of the newest week — because the snapshot is what was scored (owner answer, this round). The newest segment is merely expanded first.

## 5. Navigation

- Two independent controls: period (`Tygodniowy | Miesięczny`) and view (`Podsumowanie | Szczegóły dzienne`). Both are always visible; switching one never resets the other.
- View switch = tabs with a 3 px accent underline. Period switch = a two-segment pill. `VISUAL DESIGN DECISION`.
- Both are real `<button>`s in a `role="tablist"` structure, keyboard-operable, with `aria-selected`. See `ACCESSIBILITY_SPEC.md`.
- State is reflected in the URL hash (`#weekly/summary`) so a driver can bookmark or reload without losing place. `VISUAL DESIGN DECISION`.
- If the snapshot contains only one period type (e.g. the first month, no monthly row yet), the missing period button is disabled with the tooltip/hint "Podsumowanie miesięczne pojawi się po zamknięciu miesiąca" — not hidden. `VISUAL DESIGN DECISION`.

## 6. Copy rules

`BUSINESS/DATA CONTRACT` unless marked otherwise. UI language: **Polish**.

1. Never "ostatni tydzień" / "ten tydzień". Always dated ranges.
2. Never present a coefficient without its unit `na 100 km`.
3. Never present a raw count as if it were the scored value.
4. Never state a predicted future score. Only "próg X daje Y pkt" statements derived from the scoring table.
5. Never name another driver, a vehicle, a place or a route. Aggregates only.
6. Never show the internal words `EXCLUDED`, `INCLUDED`, `UNKNOWN_DRIVER`, `ranking_included`, `LOW_DISTANCE`, table or column names.
7. Distance is always "dystans kwalifikujący" — qualifying distance (AS-IS `G-15`).
8. Classification labels always appear with their numeric range: "akceptowalny · próg 40–84 pkt".
9. Motivational copy is not used. Every sentence either states a number, explains how a number arose, or states what would change it.

## 7. Fail closed — incomplete scoring is never a dashboard

`BUSINESS/DATA CONTRACT`. A reporting period whose snapshot cannot satisfy the complete Eco scoring contract is **not published as a driver dashboard**. The driver receives the `REPORT_NOT_READY` state instead (copy in `STATES_AND_EDGE_CASES.md` §5). Never render a screen that looks complete while silently omitting a scoring category that the current score requires, and never show a partial score.

This is not the same as a legitimate value: a category whose established scoring contract returns a valid result is normal data. In particular **over-rev is not missing data** — under the accepted V1 model every current driver receives its full points, so the snapshot is complete.

## 8. Coaching semantics

`BUSINESS/DATA CONTRACT`. Four deterministic insights, selected on the host (full rules in `COMPONENT_SPECIFICATIONS.md` C-13):

- `LARGEST_LOSS` — chosen by actual points lost: the category costing the most points now.
- `MOST_IMPROVED` — chosen by the **fall in the normalised coefficient** between comparable closed periods. A scoring-bucket crossing is **not** required; a driver may improve materially inside one bucket. Where points did change, that consequence is stated as well.
- `MOST_DETERIORATED` — chosen by the **rise in the coefficient**, on the same terms. Deterioration is never defined solely as additional points lost.
- `BEST_OPPORTUNITY` — current coefficient → next more favourable existing scoring threshold → the deterministic points gain at that threshold.

Every insight must let the driver see which metric triggered it, what changed or currently matters, and the scoring consequence where one exists. That explanatory content is a **semantic requirement, not a literal field named "Dlaczego"** — the final design carries it in the card's value line and sentence.

`BC` Never present a derived event figure as a budget: no "you may have N events", no "you can still have N violations", no allowed-violation language. Improvement is always communicated as **coefficient and scoring threshold**. Raw event totals remain descriptive only.
