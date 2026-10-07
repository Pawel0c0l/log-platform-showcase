# COMPONENT_SPECIFICATIONS

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`
**Canonical visual source:** `../Eco Driving Dashboard.dc.html` · tokens in `../Eco Driving Design System.dc.html`

Every reusable component. Each entry states: purpose · required data · displayed values · hierarchy · interaction · responsive · empty/unavailable · eligibility · semantic colour · relationship to Eco scoring.

Field names refer to `DATA_TO_UI_MAPPING.md` (snapshot contract). `VD` = `VISUAL DESIGN DECISION`, `BC` = `BUSINESS/DATA CONTRACT`.

---

## C-01 `PeriodHeader`

- **Purpose.** Make the covered period, its cumulative or closed nature, and the comparison basis unmistakable before any number is read.
- **Data.** `period_type`, `period_start_date`, `period_end_date_display`, `period_label`, `period_sequence_in_month`, `closed_snapshots_in_month`, `snapshot_closed_at`, `comparison.basis_start`, `comparison.basis_end`, `comparison.kind`.
- **Displays.** Eyebrow (`Okres tygodniowy — liczony od 1. dnia miesiąca` / `Okres miesięczny — zamknięty miesiąc`); the range at `metric-md` in monospace; a cumulative/closed chip in accent tint; a sequence chip; on the right, the comparison basis range and one explanatory sentence.
- **Hierarchy.** Range dominates. The comparison basis is secondary but must never be truncated or hidden behind a tooltip. `BC`
- **Interaction.** Static. `VD`
- **Responsive.** ≤ 900 px: right block wraps under the left block, left-aligned, order preserved.
- **Empty/unavailable.** No comparison basis → the right block renders "brak wcześniejszego okresu" plus the reason ("To pierwsza zamknięta okres lipca…"). Never an empty slot. `BC`
- **Semantic colour.** None beyond the accent chip.
- **Scoring relationship.** `BC` end date is `period_end_date − 1 day`; weekly `period_start_date` is always the month start.

## C-02 `ScoreCard`

- **Purpose.** The headline result and its classification, in one glance.
- **Data.** `eco_score_total`, `rating_type`, `comparison.previous_eco_score_total`.
- **Displays.** `display-score` number tinted by classification; `/ 100 pkt`; classification pill with glyph + label + numeric range; `DeltaChip`; the line "wobec `<basis range>` (`<previous>` pkt)".
- **Hierarchy.** Score > classification > delta > basis line.
- **Interaction.** None. `VD`
- **Responsive.** ≤ 700 px: `display-score-mobile`; delta chip moves to the right of the score block, still above the axis.
- **Empty/unavailable.** This component exists only for a qualified, publishable reporting-period snapshot. A period below the 100 km **period-level** qualification gate never renders `ScoreCard`; it is replaced by the global `INSUFFICIENT_DISTANCE` state. Any required category coefficient `NULL` means `scoring_complete = false` and the normal dashboard is not published (`REPORT_NOT_READY`). **Never render 0 for a missing score.** `BC`
- **Eligibility.** Independent of ranking: an unranked driver still gets the full card. `BC`
- **Semantic colour.** Classification tint only (§3.4 of `VISUAL_SYSTEM`), which derives from the score via the 85/40 thresholds. `BC`
- **Scoring relationship.** `eco_score_total = 100 + Σ points_lost`. The card must never display a score the snapshot did not carry.

## C-03 `TotalScoreAxis`

- **Purpose.** Where the score sits between classification thresholds, and how far the next threshold is.
- **Data.** `eco_score_total`, `comparison.previous_eco_score_total`, static thresholds 40 and 85.
- **Displays.** 16 px linear track 0–100 with three bands at 40 % / 45 % / 15 % width; ticks `0`, `40`, `85`, `100`; current marker (dark value chip + 2 px stem) above the track; previous-period ghost marker (hollow triangle + "poprz. 67") below; a right-aligned line "Do progu 85 pkt (bezpieczny): 14 pkt"; a four-item legend naming each band with its range.
- **Hierarchy.** Marker > bands > ticks > legend.
- **Interaction.** Hover/focus on the marker exposes the same text that is already printed in the "next threshold" line — the tooltip is never the only source. `VD`
- **Responsive.** ≤ 700 px: 14 px track, ghost marker keeps its position but drops its label; the "next threshold" line moves below the track.
- **Empty/unavailable.** No score → the track renders with no marker plus the note "Bez wyniku dla tego okresu — oś pokazana wyłącznie jako odniesienie do progów." `BC`
- **Semantic colour.** Band fills use the three status colours with their patterns.
- **Scoring relationship.** `BC` **This component resolves AS-IS `D-05`.** The scale is truly linear: 1 point = 1 % of width, so marker and band edges cannot disagree. A negative score pins the marker to the left edge **and** triggers the explicit note stating the actual value, so −48 is never indistinguishable from 0.

## C-04 `DeltaChip`

- **Purpose.** Direction and magnitude of change, with a stated basis.
- **Data.** current value, basis value, basis date range.
- **Displays.** direction glyph (`▲ ▼ =`), absolute magnitude, unit; status-tinted fill (green improvement, red deterioration, neutral unchanged/none).
- **Interaction.** None.
- **Empty/unavailable.** No basis → neutral chip reading "brak bazy" with the unit suppressed, and the basis line explains why. **Never render `+0` or `—` alone, and never fabricate a delta.** `BC`
- **Semantic colour.** Improvement/deterioration only. This is a *direction* palette, and it must be paired with the glyph (colour-blind safety).
- **Note.** For rank, "improvement" means a **lower** number: `delta = previous_position − current_position`, positive = up. `BC`

## C-05 `RankingCard`

- **Purpose.** The driver's standing inside the ranked population.
- **Data.** `ranking_state`, `ranking_position`, `ranking_total_participants`, `comparison.previous_ranking_position`.
- **Displays.** `18` at `metric-xl` + `/ 158`; a movement chip ("6 miejsc w górę" / "6 miejsc w dół" / "bez zmian" / "Nowo w rankingu" with `★`); a note naming the previous position and its period.
- **Hierarchy.** Position > population > movement > note.
- **Eligibility.** `BC` Rendered **only** when `ranking_state == RANKED`. For every other state, `RankingNotice` (C-06) takes its place. `ranking_position` must not even be present in the snapshot for non-`INCLUDED` drivers (AS-IS `PRIVACY_DATA_BOUNDARIES` §3 — "the most dangerous field in the snapshot").
- **Empty/unavailable.** Never renders with a missing number.
- **Semantic colour.** Movement direction only.
- **Scoring relationship.** `BC` Position is dense and unique within `(period, ranking_group)`; ties are broken by distance then identity, so equal scores can still differ in position. Never describe the position as a percentile.
- **Privacy.** `BC` Shows only the driver's own position and the population size — never a neighbour, a leaderboard, or a named colleague.

## C-06 `RankingNotice`

- **Purpose.** Make the *absence* of ranking read as an intentional state, not as broken data.
- **Data.** `ranking_state` ∈ `NOT_RANKED_BY_CONFIGURATION | NOT_ON_ROSTER | LEFT_RANKING`, optional `comparison.previous_ranking_position`. Period-distance qualification is resolved before the normal dashboard is rendered and is not a ranking state.
- **Displays.** `i` marker in a neutral square, a title, and 1–3 sentences. Occupies the same grid slot the `RankingCard` would, with the same height rhythm, so the layout does not collapse. `VD`
- **Copy per state.** `BC` — see `STATES_AND_EDGE_CASES.md` §3 for the exact strings. Never the words `EXCLUDED`, `ranking_included`, "wykluczony", or "błąd".
- **Interaction.** None.
- **Eligibility.** Mutually exclusive with C-05 and (except in `LEFT_RANKING`) with C-07.
- **Semantic colour.** Neutral only. This state is not a performance judgement. `BC`

## C-07 `GroupShareCard`

- **Purpose.** Population context for the classification: how common the driver's group is.
- **Data.** `rating_type`, `rating_group_share_percent`, `rating_group_distribution.{safe,acceptable,dangerous}`, `ranking_total_participants`.
- **Displays.** `52,5 %` at `metric-lg` + "uczestników rankingu należy do grupy **kierowców akceptowalnych**"; a 12 px three-segment distribution bar (patterned); the three labelled percentages; a footnote naming the population size and stating that no other driver's result is included.
- **Hierarchy.** Own share > distribution > footnote.
- **Eligibility.** `BC` Only when `ranking_state == RANKED` **and** a score exists. The underlying column is `NULL` for `EXCLUDED` / `UNKNOWN_DRIVER` (AS-IS `SCORING_RANKING_CONTRACT` §4.4) — the card is removed, not zeroed.
- **Empty/unavailable.** Distribution missing (`DEP-06` unresolved) → show the own-share number and hide the bar; never render a bar summing to less than 100 %.
- **Semantic colour.** Classification tints; segment order is always safe → acceptable → dangerous.
- **Scoring relationship.** `BC` Denominator is the `INCLUDED` league only. Copy says "uczestników rankingu", never "wszystkich kierowców".

## C-08 `DistanceKpi`

- **Purpose.** The exposure denominator, and the reminder that every coefficient depends on it.
- **Data.** `total_kilometers`, `comparison.previous_total_kilometers`, `trips_count`.
- **Displays.** `3 418` + `km`; a neutral delta chip vs basis; trip count; the standing line "Wszystkie wskaźniki są liczone na 100 km — dystans zmienia ich wartość przy tym samym zachowaniu."
- **Hierarchy.** Distance > delta > trips > explanation.
- **Empty/unavailable.** `DistanceKpi` exists only on a qualified reporting-period dashboard, therefore period distance is always `>= 100 km`. Periods below the gate render `INSUFFICIENT_DISTANCE` instead and do not expose the actual distance. `0 km` remains meaningful only at the **daily** row level as the neutral no-driving state.
- **Semantic colour.** `BC` **Neutral only.** More kilometres is neither good nor bad; colouring distance would imply a target.
- **Scoring relationship.** `BC` This is *qualifying* distance (matched, eligible trips only — AS-IS `G-15`, `CLIENT_VARIANTS` §4). Label accordingly; never "everything you drove".

## C-09 `CategoryRow`

- **Purpose.** One line that carries the whole causal chain: what happened → how it normalises → what it cost.
- **Data.** per category: `label`, `count`, `coefficient_per_100km`, `points`, `points_max`, `points_lost`, `bands[]`, `marker_band_index`, `comparison.previous_points_lost`, `not_measured`.
- **Displays.** six columns — label + max-points caption · raw count (monospace) · status chip with glyph + `2 / 100 km` · compact band ladder with the `▲` marker and `◇` target · `4 / 10` points with `−6 pkt` beneath · expand button.
- **Hierarchy.** `BC` The count is *neutral typography*; the status chip carries the colour. The two must never be merged into one coloured number, because that is exactly the confusion the product is built to remove.
- **Interaction.** Whole row is a disclosure control (button semantics on the toggle, `aria-expanded`); expanding reveals `ScoringAxis` (C-10) plus three explanation panels. Default: the `LARGEST_LOSS` category expanded. `VD`
- **Responsive.** ≤ 900 px: becomes a two-line stacked card — line 1 label + points, line 2 status chip + count, then the ladder full width.
- **Empty/unavailable.** `count == null` → "brak danych"; `coefficient == null` → status `neutral`, points `—`, ladder with no marker, and the expansion explains that the area cannot be evaluated. `BC`
- **Over-rev category.** `overrev` stays in the list (the 100-point contract is truthful), renders at 62 % opacity with the neutral caption "pełne 15 pkt w obecnym okresie", and is excluded from coaching. `BC` Do **not** state a technical cause the audit did not establish — no "brak pomiaru w tej flocie", no "measurement unavailable". Under the accepted V1 model all current drivers receive full over-rev points; that is a result, not missing data.
- **Semantic colour.** From `points` vs `points_max` only.

## C-10 `ScoringAxis` — the defining component

- **Purpose.** Carry forward the monthly e-mail's best idea: show the entire penalty ladder, mark the driver's rung, and price the next rung. This is the product's signature element.
- **Data.** `bands[] = [{label, points_lost, status}]` (from the axis contract), `marker_band_index`, `coefficient_per_100km`, `count`, `total_kilometers`, `points`, `points_max`, band upper bounds.
- **Displays.** five aligned rows over equal-width cells:
  1. band label — the coefficient range (`0`, `1–2`, `>10`);
  2. the colour cell (with pattern);
  3. points lost for that band (`0`, `−6`, `−20`);
  4. markers — `▲` "Twój wskaźnik" in the current band, `◇` "próg docelowy: +4 pkt" in the next better band;
  5. caption row for those two markers.
  Plus a header line ("168 zdarzeń · 3 418 km · wskaźnik 5 na 100 km") and three panels: **Stan obecny** (coefficient → band → points), **Potencjał** (what the next better band requires and pays), **Zmiana** (movement vs the dated basis).
- **Hierarchy.** Marker > current band > adjacent bands > distant bands.
- **Interaction.** Rendered inside the expanded `CategoryRow`; every cell is focusable in reading order with an accessible name "przedział 5–6, utrata 8 pkt, Twój obecny przedział". `VD`
- **Responsive.** ≤ 700 px: the ladder rotates to **vertical** — one row per band, band label left, colour cell as a 4 px left rail, points lost right, marker rows inline. All bands stay visible; nothing is scrolled horizontally. `VD`
- **Empty/unavailable.** No coefficient → all bands render, no marker, header shows "Wskaźnik nie został wyliczony dla tego okresu." This mirrors the AS-IS rule that an unmatched marker draws nothing rather than guessing. `BC`
- **Semantic colour.** Per band from the scoring table. Equal cell widths are deliberate: the axis is **ordinal**, and the AS-IS ordinal geometry is preserved because the underlying bands are unequal and open-ended.
- **Scoring relationship.** `BC`
  - Marker placement is driven by `points_lost` matched against the band table (AS-IS `REPORTING_AND_EMAIL_UX` §5.3) — **not** by re-deriving from the displayed rate. If no band matches, draw no marker.
  - Target band = `marker_band_index − 1`. Gain = `bands[i−1].points_lost − bands[i].points_lost`. Required coefficient = `upper_bound(bands[i−1])`.
  - `BC` **No event-budget language.** Never express the opportunity as a number of permitted events ("nie więcej niż N zdarzeń", "you may have N events"). The canonical metric is the coefficient and the threshold; note that the coefficient falls both with fewer events and with more qualifying exposure.
  - No band beyond the immediately better one is ever quoted as a promise.

## C-11 `SnapshotTrend`

- **Purpose.** Show the reporting model itself: a sequence of closed snapshots, each cumulative from the 1st.
- **Data.** `series[] = [{period_label, period_start, period_end_display, eco_score_total, is_current}]`, and for monthly also `comparison.previous_eco_score_total`.
- **Displays.** one bar per closed snapshot, labelled with its **full range** (`01.07–12.07`), value above the bar; current bar filled with its status colour and outlined 2 px dark, others neutral; a footnote stating that snapshots are cumulative and do not sum.
- **Interaction.** Optional focusable bars exposing "Okres 01.07–12.07: 67 pkt". No zoom, no pan. `VD`
- **Responsive.** ≤ 700 px: bars keep their order and shrink; labels rotate to two lines. Never fewer bars than the snapshot carries.
- **Empty/unavailable.** One snapshot only → render the single bar plus "To pierwszy zamknięty okres tego miesiąca." Skipped snapshot (AS-IS `G-08`) → the series simply has no bar for it; do not interpolate, do not label "week 1". `BC`
- **Semantic colour.** Status colours for the current bar only, so the eye lands on now.
- **Scoring relationship.** `BC` Bars are **not** weekly increments and must never be presented as such. Monthly mode adds the previous month's final score as a labelled reference line, not as a bar in the same series.

## C-12 `CategoryTrendList`

- **Purpose.** Per-category movement, ranked, without eight separate charts.
- **Data.** per category: `points_lost`, `comparison.previous_points_lost`.
- **Displays.** label · two small bars (basis, current) · signed delta in points.
- **Empty/unavailable.** `previous_points_lost == null` (`DEP-03`) → "brak bazy" and no bars for that row. `BC`
- **Semantic colour.** Bar colour = band status; delta colour = direction.
- **Note.** Excludes not-measured categories. `BC`

## C-13 `CoachingCard` / `CoachingGrid`

- **Purpose.** Four deterministic answers to "what next", each auditable.
- **Data.** `coaching[] = [{code, category_key, value, inputs}]` where `code ∈ LARGEST_LOSS | MOST_IMPROVED | MOST_DETERIORATED | BEST_OPPORTUNITY`.
- **Displays.** numbered marker + kind label · category · the headline value in the unit that selected it (`−10 pkt` for the loss and the opportunity, `−3 na 100 km` / `+2 na 100 km` for improvement and deterioration) · one sentence built from the coefficient, the threshold and — where it exists — the points consequence.
- **Explainability.** `BC` Semantic requirement, **not** a literal field named "Dlaczego" (that label was removed by the owner): each card must let the driver see which metric triggered the insight, what changed or currently matters, and the scoring consequence where one exists. The value line plus the sentence carry that; do not reintroduce a separate labelled field.
- **Hierarchy.** Kind label > category > value > explanation > why.
- **Interaction.** `VD` Optional: clicking a card scrolls to and expands the matching `CategoryRow` (`aria-controls`). No other behaviour.
- **Responsive.** 4 → 2 → 1 columns.
- **Empty/unavailable.** `BC`
  - A normal dashboard never renders without a complete score. Below-100-km reporting periods are replaced globally by `INSUFFICIENT_DISTANCE`; incomplete scoring snapshots are replaced globally by `REPORT_NOT_READY`.
  - Nothing lost anywhere → `LARGEST_LOSS` and `BEST_OPPORTUNITY` are omitted and a single card states that no area is losing points.
  - No comparison basis → `MOST_IMPROVED` / `MOST_DETERIORATED` are omitted (not rendered as "0").
  - The grid renders 1–4 cards; it must not pad to four with filler.
- **Semantic colour.** Top border only: red largest loss, green improvement, amber deterioration, **accent** opportunity — the opportunity is not a status.
- **Scoring relationship — the exact rules.** `BC` Deterministic, no model, no randomness, computed on the host:
  ```
  candidates = categories where not_measured == false and points_lost != null

  candidates = categories where not_measured == false and points_lost != null

  LARGEST_LOSS      = min(points_lost)                       # most negative → most points cost
                      ties → REQUIRED_METRICS declaration order

  # improvement / deterioration are COEFFICIENT movements, not bucket changes
  comparable = candidates where previous_coefficient_per_100km != null
                                 and coefficient_per_100km != null
  MOST_IMPROVED     = max(previous_coefficient − current_coefficient) > 0
  MOST_DETERIORATED = max(current_coefficient − previous_coefficient) > 0
  # the points consequence is REPORTED when it exists, never used to select

  BEST_OPPORTUNITY  = over candidates with points_lost < 0 and marker_band_index > 0:
                        gain   = bands[i−1].points_lost − bands[i].points_lost
                        effort = coefficient − upper_bound(bands[i−1])
                        sort by (effort asc, gain desc, declaration order)
  ```
  Hard constraints (AS-IS `DASHBOARD_DATA_FEASIBILITY` §6.3): never coach on `overrev`; never state a trend without the distance delta; never say "last week"; never coach a rank the driver cannot hold; the coaching component is never rendered for a `LOW_DISTANCE` period because the whole Eco dashboard is suppressed; never present daily points as additive.

## C-14 `DayGrid` (Excel-style daily table)

- **Purpose.** Show which days built the period result, in a layout a fleet reader can scan like a spreadsheet: rows are days, columns are violation categories.
- **Owner decision (authoritative).** One column per category, each cell holding the **sum of events for that day**, numbers only — **no glyphs inside cells**, and **no day-status column and no aggregate day colour**. The threshold is carried by the cell background, per category, independently.
- **Data.** per day: `date`, `weekday`, `kilometers`, `trips_count`, and per category `count` + `coefficient_per_100km` + `band_label` + `status`; per week group and per period: the column sums. **No `day_status` field exists.** `BC` There is no per-day minimum-distance gate: days below 100 km (and below 50 km) are shown normally once the overall reporting period is qualified. For any day with `kilometers > 0`, use that day's own distance to derive the category coefficient/status; `kilometers == 0` is the neutral no-driving case.
- **Structure.**
  ```
  Dzień │ Dystans │ Hamowania │ Przyspieszenia │ Skręty │ Postój │ 140–160 │ 160–170 │ >170 │ ⌄
  ── week group row: range · segment distance · per-column event sums ───────────────────────
     13.07 Pn │ 161 km │ 3 │ 2 │ 9 │ 7 │ 2 │ 0 │ 0 │ +
     …
  ══ Suma okresu: period distance · per-column period sums (dark row) ═══════════════════════
  ```
- **Cell.** the day's event count, tabular figures, background = the status of that day's coefficient in that category's scoring band (green / yellow / red / neutral tint). `BC` The number is never coloured to encode status; the background is.
- **Sums.** `BC` week-group rows and the dark `Suma okresu` row must reconcile exactly with the period figures on the Summary (distance and every category count). An unknown period count renders `brak` in the cells **and** in both sum rows — never `0`.
- **Hierarchy.** date → distance → category columns → totals row.
- **Interaction.** Week-group rows are `<button aria-expanded>` (keyboard-operable, `padding: 0` so cells stay aligned to the grid). The 48px trailing column holds a per-day `<button aria-expanded>` that reveals the day's coefficients ("Postój na biegu jałowym · 7 zdarzeń → 4/100 km · próg 3–4") plus one sentence on evaluability. "Rozwiń wszystkie tygodnie" toggles all groups.
- **Responsive.** `BC` **No horizontal scrolling at any width — not the page, not the card.** ≥ 1024 px: the full grid, columns `minmax(88px,108px) minmax(70px,88px) repeat(7, minmax(0,1fr)) 44px`. 700–1023 px: the grid keeps all seven category columns but drops to short column labels and 12 px cell text; distance moves under the date. < 700 px: the grid is replaced by one card per day — date + weekday, distance + trips as a neutral data chip, per-category chips (glyph + short label + count, tint = that category's threshold), and a "Pokaż wskaźniki dnia" disclosure. At high browser zoom the same transformation applies, driven by available width, never by a scroll container.
- **Empty / unavailable.** `BC` `kilometers == 0` (no qualifying driving) → neutral tint, cells read `0`, accessible name "brak przejazdów kwalifikujących", and the day chip reads "brak jazdy". Where the existing scoring contract yields no coefficient for another reason, the cell is `neutral` with the accessible name "brak oceny dla tego dnia". **There is no dashboard-specific minimum-distance threshold** — do not introduce one.
- **Semantic colour + colour independence.** `BC` Cell tint comes from that category's own coefficient for that day → its existing scoring bucket → `points` vs `points_max`. Never from the displayed count: the same count on two days can differ in colour because their exposure differs. Statuses never aggregate across categories. Because the owner removed the in-cell glyphs, colour is compensated by: (a) the number itself, (b) a per-cell accessible name / tooltip naming the coefficient, its band and the state in words, (c) a legend with the four tints and their meanings under the table, (d) the day-status chip retained on the mobile card, (e) the expanded day panel spelling out every category's coefficient and band. See `ACCESSIBILITY_SPEC.md` §1.
- **Scoring relationship.** `BC` Daily coefficient = `count / km × 100` rounded HALF_UP, banded with the same scoring table as the period. Daily points are never displayed. Daily rows are materialised in the snapshot.

## C-15 `StatusMarker`

- **Purpose.** One reusable atom for all four semantic states.
- **Data.** `status`, optional value, optional label.
- **Displays.** glyph + optional value/label in a tinted, bordered chip; patterned fill where the chip is large enough to show it.
- **Rules.** `BC` Always glyph + colour + (label or accessible name). Minimum 24 px tall; 44 × 44 px minimum hit area when interactive. Never colour alone; never a bare dot.

## C-16 `PeriodViewNav`

- **Purpose.** Move between the four experiences without losing context.
- **Displays.** view tabs (`Podsumowanie` / `Szczegóły dzienne`) with a 3 px accent underline; a period pill (`Tygodniowy` / `Miesięczny`).
- **Interaction.** `role="tablist"`, arrow-key navigation, `aria-selected`, focus ring; hash routing; switching period preserves view and vice versa. Unavailable period → disabled button with an explanatory hint, not hidden. `VD`
- **Responsive.** ≤ 700 px: tabs full width in one row, period pill on the row below.

## C-17 `AccessState` / `UnavailableState`

- **Purpose.** Keep failure legible and non-leaky.
- **States.** `INVALID_LINK`, `LINK_EXPIRED`, `SNAPSHOT_UNAVAILABLE`, `SERVICE_UNAVAILABLE`.
- **Displays.** glyph tile, title, 1–2 sentences, and (only for `SERVICE_UNAVAILABLE`) a retry button.
- **Rules.** `BC` No login form, no e-mail input, no password recovery, no "contact support with your driver id", no statement about whether a link ever existed, no driver identity, no token echo. Never render the dashboard shell with empty values behind an error. Copy in Polish; see `STATES_AND_EDGE_CASES.md` §5.
