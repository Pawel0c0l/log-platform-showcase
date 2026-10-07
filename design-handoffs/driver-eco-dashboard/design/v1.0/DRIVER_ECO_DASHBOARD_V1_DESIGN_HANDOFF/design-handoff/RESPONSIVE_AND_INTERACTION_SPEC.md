# RESPONSIVE_AND_INTERACTION_SPEC

**Qualification invariant:** no responsive layout for Eco data is rendered when the whole reporting period is below 100 km; use the single neutral `INSUFFICIENT_DISTANCE` state. Daily layout itself has no 50/100 km minimum threshold.

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

The dashboard is opened from an e-mail link, so the phone is the primary device in practice and the desktop is where the analysis happens. Both are designed, neither is a shrink of the other.

---

## 1. Breakpoints

| Name | Range | Shell |
|---|---|---|
| `compact` | ≤ 700 px | single column, 16 px gutters, cards flush to the container |
| `medium` | 701–1023 px | two columns where content allows; day table becomes cards |
| `wide` | ≥ 1024 px | full two-column summary; day table as a real table |
| max content width | 1240 px | centred, `canvas` background outside |

`VISUAL DESIGN DECISION`: most reflow is achieved with `repeat(auto-fit, minmax(310px, 1fr))` and `flex-wrap`, so the layout degrades gracefully even between the named breakpoints. Only three components need an explicit media-query switch: `ScoringAxis`, the day table, and the category table.

## 2. Transformations

### 2.1 Summary

| Block | `wide` | `compact` |
|---|---|---|
| ScoreCard | left column, score at 62 px, axis inside the card | full width, score at 44 px, delta chip right of the score, axis below with a 14 px track and unlabelled ghost marker |
| Ranking / Group / Distance | right column, stacked | two 1/2-width KPI tiles (ranking, distance) then the group card full width; `RankingNotice` always full width |
| CoachingGrid | 4 columns | 1 column, order = LARGEST_LOSS, BEST_OPPORTUNITY, MOST_DETERIORATED, MOST_IMPROVED (`VD`: on a phone the action comes before the history) |
| CategoryTable | 6-column table | stacked cards: line 1 label + points, line 2 status chip + count, then the band ladder full width; expansion opens the vertical axis |
| Trends | 2 columns | 1 column; SnapshotTrend keeps every bar, labels on two lines |

### 2.2 `ScoringAxis` (the one component that changes form)

- `wide`: horizontal ladder, one equal-width cell per band, five stacked rows (band label / colour / points lost / marker / caption).
- `compact`: **vertical list**, one row per band — 4 px colour rail on the left, band label, points lost right-aligned, and the `▲`/`◇` markers inline with their captions. The current band gets a 2 px dark outline, the target band a dashed accent outline.
- Never: horizontal scrolling, hidden bands, or a truncated ladder. All bands are always present because the ladder *is* the explanation. `BC`

### 2.3 Detailed

| Aspect | `wide` | `compact` |
|---|---|---|
| Structure | full category grid, group header rows | one card per day inside collapsible group sections |
| Day identity | date + weekday in the first column | date + weekday top-left, day status top-right |
| Distance | own column | second line with trip count |
| Categories | one grid column per category, cell = event sum, tint = that category's threshold | chips wrapped below the distance line, 44 px min touch height |
| Status | per-category cell tint only — no status column, no aggregate day colour | per-category chip tint only; the day chip shows distance + trips, never an Eco colour |
| Disclosure | `+` button per row | "Pokaż wskaźniki dnia" text button, full row width |
| 31 rows | grouped, one group expanded | groups collapsed; expanding one group renders 5–7 cards — never 31 cards at once `BC` |

### 2.4 The daily grid never scrolls sideways

`BUSINESS/DATA CONTRACT` — this supersedes any earlier `min-width` + scroll-container instruction:

| Available width | Daily presentation |
|---|---|
| ≥ 1024 px | full grid: `minmax(88px,108px) minmax(70px,88px) repeat(7, minmax(0,1fr)) 44px`; all seven category columns, event sums in cells |
| 700–1023 px | same grid, short column labels, 12 px cell text, distance under the date; still all seven categories |
| < 700 px | grid replaced by one card per day: date + weekday, a neutral chip with distance + trips, per-category chips (short label + count, tint = that category's threshold), "Pokaż wskaźniki dnia" disclosure |

All important category values, the coefficient-derived status of each, and the accessible names survive every transformation. High browser zoom takes the same path: the layout transforms on available width, never into a horizontal scroll container. The card mode must remain usable at 28–31 days by keeping week groups collapsed.

Rule: **no horizontal scrolling anywhere, at any breakpoint.** `BC` A 31-row spreadsheet that must be panned sideways is the specific failure mode this spec exists to prevent.

## 3. Interaction inventory

| Interaction | Trigger | Behaviour | Persistence |
|---|---|---|---|
| Switch view | tab click / `←``→` inside the tablist | swaps Summary ↔ Detailed, keeps period | URL hash `#weekly/detailed` |
| Switch period | pill click | swaps weekly ↔ monthly, keeps view, resets day expansions | URL hash |
| Expand category | click/Enter/Space on the row's disclosure button, or activating a loss-distribution legend row / near-threshold card | reveals `ScoringAxis` + 3 explanation panels; only one category open at a time (`VD` accordion, to keep the page short) | not persisted |
| Expand day | disclosure button on the row/card | reveals per-category coefficients for that day | not persisted |
| Expand/collapse week group | group header button | toggles that segment | not persisted |
| Expand all groups | "Rozwiń wszystkie tygodnie" | toggles all segments; label flips to "Zwiń wszystkie tygodnie" | not persisted |
| Coaching → category | click a coaching card | scrolls the matching category row into the viewport and expands it | not persisted |
| Retry | button in `SERVICE_UNAVAILABLE` | one fetch attempt; no auto-retry loop, no polling | — |

Nothing else is interactive. There is no filtering, no date picker, no export, no sharing, no settings. `VD`

## 4. Motion

`VISUAL DESIGN DECISION`, deliberately minimal:

- disclosure open/close: 140 ms height/opacity ease-out;
- tab underline: 120 ms transform;
- hover/focus tints: 100 ms;
- nothing else animates. No entrance animations, no counting-up numbers, no chart draw-in. A dashboard about closed historical data must not feel like it is arriving live.
- `@media (prefers-reduced-motion: reduce)`: all transitions drop to 0 ms; disclosure becomes an instant show/hide.

## 5. Loading and error behaviour

| Phase | Behaviour |
|---|---|
| First paint | render the shell (header, nav) immediately; content area shows skeleton blocks matching the final layout (no spinner) `VD` |
| Snapshot fetched | render once, fully. **Never** render partially-filled components while data streams. `BC` |
| Fetch fails (5xx / network) | `C-17 SERVICE_UNAVAILABLE` replaces the content area; header stays | 
| Token invalid / expired | full-page `C-17 INVALID_LINK` / `LINK_EXPIRED`; no shell, no nav, so no impression that data exists behind it `BC` |
| Snapshot missing for a period | `C-17 SNAPSHOT_UNAVAILABLE` inside the content area, nav still usable so the other period remains reachable |
| Incomplete snapshot | do **not** render. The generator refuses to publish incomplete snapshots (AS-IS `SCORING_RANKING_CONTRACT` §7); the UI's job is to show the designed unavailable state, never a half-filled screen. `BC` |

## 6. Performance / payload

- One fetch, one JSON document, one driver. `BC` A client-wide bundle filtered in the browser is a data breach, not an optimisation (AS-IS `PRIVACY_DATA_BOUNDARIES` §4).
- Target payload ≤ 60 kB for a monthly snapshot with 31 materialised days.
- No client-side scoring, no `Decimal` re-derivation, no rounding in JS beyond layout maths. `BC`
- Fonts: 2 families, 4 + 3 weights, `display=swap`, Latin Extended subset (Polish diacritics required). A system-font fallback stack must be specified so the first paint is never blank.
