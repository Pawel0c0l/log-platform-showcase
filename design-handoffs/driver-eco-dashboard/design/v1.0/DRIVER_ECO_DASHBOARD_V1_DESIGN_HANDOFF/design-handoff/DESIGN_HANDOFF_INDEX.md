# DESIGN_HANDOFF_INDEX — Driver Eco Driving Dashboard V1

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`
**Package type:** UX/UI + interaction design handoff. Implementation-ready specification. **No production code, data, template, schedule or Cloudflare resource was created or modified to produce it.**
**Audience:** Claude Code, implementing in the existing `log-platform` repository plus the new Cloudflare delivery path.
**Companion package (authoritative for AS-IS facts):** `eco-driving-as-is-audit-handoff/` (`v1.0 · 2026-08-18`).

---

## 1. Scope

One product — the driver-facing Eco Driving dashboard — in four experiences built from one component system:

| id | period_type | view | Primary question it answers |
|---|---|---|---|
| `W-SUM` | `weekly` | `summary` | "Where do I stand in this month so far, and what should I change?" |
| `W-DET` | `weekly` | `detailed` | "Which days of the month so far produced that result?" |
| `M-SUM` | `monthly` | `summary` | "How did the closed month end, versus the month before?" |
| `M-DET` | `monthly` | `detailed` | "Which days of the closed month produced that result?" |

Out of scope: operator/fleet-manager views, live telematics, authentication flows, e-mail template redesign, any change to Eco scoring.

## 2. Reading order

| # | Artifact | Read it for |
|---|---|---|
| 0 | `FINAL_HANDOFF_MANIFEST.md` | the package contract: every artifact, its authority scope, and whether Claude Code must read it |
| 0b | `CLAUDE_CODE_IMPLEMENTATION_BRIEF.md` | **START HERE for implementation** — self-contained contract: decisions, invariants, snapshot schema + assertions, derivation formulas, screens, copy, delivery, build order, acceptance checklist |
| 1 | `DESIGN_HANDOFF_INDEX.md` (this file) | scope, decision log, what is invariant vs flexible |
| 2 | `PRODUCT_UX_CONTRACT.md` | information architecture and behaviour of the four experiences |
| 3 | `VISUAL_SYSTEM.md` | tokens: type, colour, spacing, surfaces, chart language, iconography |
| 4 | `COMPONENT_SPECIFICATIONS.md` | every component: purpose, data, hierarchy, interaction, empty state |
| 5 | `SCREEN_SPECIFICATIONS.md` | the four screens, block by block, with content order |
| 6 | `RESPONSIVE_AND_INTERACTION_SPEC.md` | breakpoints, transformations, interaction and motion rules |
| 7 | `STATES_AND_EDGE_CASES.md` | the required driver/data states + access states, and how one system covers them |
| 8 | `ACCESSIBILITY_SPEC.md` | colour-independence, keyboard, focus, touch, chart labelling, contrast |
| 9 | `DATA_TO_UI_MAPPING.md` | every displayed element → AS-IS data concept → snapshot field |
| 10 | `IMPLEMENTATION_HANDOFF.md` | what is authoritative, what is flexible, what is a genuine dependency |
| 11 | `VISUAL_REFERENCES.md` | how to open and read the high-fidelity references |

Visual artifacts (self-contained, open in any browser):

| File | Contents |
|---|---|
| `../Eco Driving Dashboard.dc.html` | the working high-fidelity reference: W-SUM / W-DET / M-SUM / M-DET, data/eligibility states, mobile references, access states |
| `../Eco Driving Design System.dc.html` | tokens, component anatomy, scoring-axis anatomy, status-marker matrix |

## 3. Relationship to the AS-IS audit

The AS-IS package is authoritative for **what exists**. This package is authoritative for **what to build**. Where they differ, this package states the difference explicitly and cites the AS-IS section.

Priority order used throughout (and to be used when a question arises later):

1. Owner-approved target decisions (recorded in §4).
2. Verified AS-IS audit evidence.
3. Existing monthly/weekly e-mail references.
4. Design judgement (recorded as `VISUAL DESIGN DECISION`).

Two labels appear on every consequential statement in this package:

- **`VISUAL DESIGN DECISION`** — changeable by a designer without asking anyone; changing it does not change what a number means.
- **`BUSINESS/DATA CONTRACT`** — changing it changes the meaning of a number a driver reads. Not a design choice. Do not "improve" these while implementing.

## 4. Decision log — every AS-IS open decision, resolved or escalated

| AS-IS id | Question | Resolution in this package | Class |
|---|---|---|---|
| `D-01` | Private trips at BRAVO00016 | Deliberate per-client ingestion policy. **Never surfaced to drivers.** No UI element, no footnote, no client-conditional copy. | `BUSINESS/DATA CONTRACT` (owner-approved) |
| `D-02` | What an `EXCLUDED` driver sees | Full dashboard — score, classification, categories, coefficients, points lost, trends, distance, detailed days, coaching. **No** rank, **no** rank movement, **no** rating-group share. Rendered as a designed state (`RankingNotice`), not as blanks. Raw label `EXCLUDED` never shown. | `BUSINESS/DATA CONTRACT` (owner-approved) |
| `D-03` | Weekly cumulative vs isolated weeks | **Option A — cumulative month-to-date.** Current closed MTD snapshot compared against the previous weekly mailing's closed MTD snapshot. Isolated-week derivation is explicitly **not** built for V1. | `BUSINESS/DATA CONTRACT` (owner-approved) |
| `D-04` | What a "day" is worth | Detailed rows show **counts + distance + per-category coefficient-derived status**; the coefficient is revealed on expand; **no daily points anywhere**; **no aggregate day status**; **no dashboard-specific minimum-distance rule** — a day with no qualifying driving is the `neutral` no-driving state | `BUSINESS/DATA CONTRACT` |
| `D-05` | Total-score axis distortion | **Fixed.** The total-score axis is a true linear 0–100 scale; band boundaries sit at 40 % and 85 % because that is where 40 pts and 85 pts fall. Scores < 0 are pinned to the left edge **and stated numerically** with an explicit note, so −48 and 0 never look identical. | `VISUAL DESIGN DECISION` (resolves an AS-IS defect) |
| `D-06` | Over-rev category | **Kept** in the 100-point model and in the category list, visually de-emphasised (62 % opacity) with the neutral caption "pełne 15 pkt w obecnym okresie", **excluded from coaching**. `BC` No claim about a technical cause the audit did not establish; full over-rev points are a result, not missing data | `VISUAL DESIGN DECISION` + coaching contract |
| `D-07` | Display name | **No name.** Heading is "Twoje Eco Driving". No name is exported to the browser. | `BUSINESS/DATA CONTRACT` (owner-approved) |
| `D-08` | In-flight period | **Closed periods only.** No live/provisional state, no streaming metaphors, no "as of now". | `BUSINESS/DATA CONTRACT` (owner-approved) |
| `D-09` | Refresh cadence and staleness | Freshness is always visible: `snapshot_updated_at` plus the closing date of the period. Cadence itself is an operations decision — `DEP-02`. | contract for the UI, dependency for ops |
| `D-10` | Group label wording | **Unchanged Polish labels**: `bezpieczny` / `akceptowalny` / `niebezpieczny`, always paired with their numeric score range so the label is never the only carrier of meaning. UI language is Polish. | owner-approved |
| `G-03` | Two label sets | **Resolved: `AREA_SCORE_METRICS.label_html` set is canonical.** See `VISUAL_SYSTEM.md` §6. `top_1_validation` / `top_2_validation` strings must be mapped to canonical labels, never rendered raw. | `BUSINESS/DATA CONTRACT` |
| `G-04` | Two colour schemes | **Resolved: the derived rule wins everywhere** (green = full category points, yellow = points lost but category still ≥ 0, red = category < 0, neutral = not evaluated). `LOST_POINTS_COLOR_RULES` is not used by the dashboard and should be retired from the e-mail path in a later change. | `BUSINESS/DATA CONTRACT` |
| `G-05` | `ranking_total_participants` unused | **Now shown**: `18 / 158`, never a bare `#18`. | `VISUAL DESIGN DECISION` |
| `G-06` | Un-rounded coefficient not persisted | Dashboard shows **only the integer scoring coefficient** for period figures. No 1-decimal rate anywhere in period context, so display can never contradict the band that scored it. Daily coefficients are computed from daily counts/distance and are labelled as daily. | `BUSINESS/DATA CONTRACT` |
| `G-08` | Weekly `LAG()` has no gap detection | The comparison basis is always rendered as **explicit dates**; the words "ostatni tydzień" / "last week" are banned from the UI copy. | `BUSINESS/DATA CONTRACT` |
| `G-09` | Retention could truncate daily grain | The snapshot **materialises** `days[]` at generation time. The UI never re-derives days. | `BUSINESS/DATA CONTRACT` |
| `G-15` | 58 % of BRAVO00016 trips unmatched | Distance is labelled "dystans kwalifikujący" (qualifying distance), never "wszystko, co przejechałeś". | `BUSINESS/DATA CONTRACT` |

## 5. Genuine implementation dependencies (not design questions)

| id | Dependency | Blocks | Proposed default |
|---|---|---|---|
| `DEP-01` | Whether the existing Eco scoring contract carries an authoritative non-evaluable condition (e.g. a minimum exposure) that the daily view must mirror | the `neutral` daily state beyond the no-driving case | **none invented.** Design applies no dashboard-specific threshold: where the existing contract yields a coefficient, it is used; `kilometers == 0` is the `neutral` no-driving state. If the repository proves an existing rule, Claude Code derives it from the repository |
| `DEP-02` | Snapshot refresh cadence / whether the ten disabled Eco schedules get enabled (AS-IS `D-09`) | freshness copy only; the UI works either way | show `snapshot_updated_at` verbatim; no "updated weekly" promise in copy until schedules are enabled |
| `DEP-03` | Per-category previous-period **coefficients and** points lost (AS-IS `G-02`: trend views `LAG()` only score/km/rank) | category movement rows, `MOST_IMPROVED`/`MOST_DETERIORATED` (which are selected on coefficient movement), change section | new window query over `*_events_per_100km` and `*_maxpoints_subtract`; if absent, the UI degrades to `comparison_unavailable` per category |
| `DEP-04` | Historical rows spanning two ranking contracts (AS-IS `G-07`) | multi-period rank trends | do not render a rank trend across the recalculation boundary; snapshot should mark affected periods `comparable: false` |
| `DEP-05` | Opaque driver key + revocable capability-token mapping (AS-IS `G-16`) | all access states | Worker resolves token → exactly one R2 object key through a mutable lookup |
| `DEP-06` | `rating_group_distribution` is a period-level (not row-level) query (AS-IS `S-10`) | group-share distribution bar | snapshot generator computes it once per period and copies it into every driver's snapshot |

## 6. What "done" means for the implementer

A build is faithful when all of the following hold:

1. Every green/yellow/red pixel traces the chain `raw violations + qualifying exposure → coefficient → existing bucket → points vs points_max → colour`, per category — never to a raw count, never to a dashboard-local threshold table, and never aggregated into a whole-day colour.
2. Weekly copy and labels make cumulative MTD unmistakable; no element implies an isolated week.
3. Ranked / newly ranked / left-ranking / not-ranked-by-configuration / below-distance states each render their designed variant, and none renders an empty slot.
4. The scoring axis appears in both Summary views and carries current band, adjacent bands, points lost per band, the driver's marker, and the next-better-band target with its point gain.
5. Coaching output is reproducible from the snapshot alone: same snapshot in, same four insights out, no model call, no randomness.
6. No location, route, registration, e-mail, phone, employee id, driver name, or any other driver's score/rank appears in the payload or the DOM.
7. Monthly Detailed is usable at 31 rows on a phone, with **no horizontal scrolling at any width**.
8. An incomplete scoring snapshot is never rendered as a dashboard — `REPORT_NOT_READY` is served instead.
9. Coaching improvement/deterioration is selected on coefficient movement, not on a points-bucket change, and no screen states an allowed number of violations.

## 7. Superseded artifacts (non-authoritative)

`archive-non-authoritative/` holds earlier drafts kept only for provenance: `Eco Driving Dashboard V1.dc.html` (pre-approval visual language) and the four style explorations (`Styl A–D`). **None of them is authoritative.** The implementation-ready package has exactly one dashboard visual source (`Eco Driving Dashboard.dc.html`) and one design-system source (`Eco Driving Design System.dc.html`); no active document references any other visual file.

### Owner correction — distance qualification (v3.1)

The 100 km threshold applies only to the **total qualifying distance of the complete reporting period**. Below that threshold, no Eco data for the period is exposed in the normal dashboard; use `INSUFFICIENT_DISTANCE`. It does not apply to individual days. In a qualified period, days below 100 km (including below 50 km) remain visible with distance and raw event sums, and their per-category coefficients/statuses use the actual daily distance.
