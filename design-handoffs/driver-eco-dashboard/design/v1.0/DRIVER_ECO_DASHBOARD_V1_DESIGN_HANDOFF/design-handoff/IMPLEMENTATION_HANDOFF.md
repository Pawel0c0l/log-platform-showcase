# IMPLEMENTATION_HANDOFF

**Package version:** `v3.1 · 2026-08-18 · IMPLEMENTATION_READY`

For Claude Code. Read with `eco-driving-as-is-audit-handoff/` beside it.

---

## 1. Authority order

Read `FINAL_HANDOFF_MANIFEST.md` first — it names the authoritative artifact for each kind of question.

1. **This package** for UX, IA, states, copy rules and the snapshot the UI expects.
2. **`eco-driving-as-is-audit-handoff/`** for every fact about the existing system: formulas, tables, thresholds, period semantics, ranking rules, privacy classification, platform constraints.
3. **The canonical visual sources** — `../Eco Driving Dashboard.dc.html` and `../Eco Driving Design System.dc.html` — for layout, tokens and copy.
4. **The repository** for how things are actually wired (`jobs/ecodriving/eco_scoring.py`, `email_visuals.py`, the two aggregation jobs, the trend views).

If this package and the AS-IS audit disagree about *current* behaviour, the audit is right. If they disagree about *target* behaviour, this package is right — the differences are enumerated in `DESIGN_HANDOFF_INDEX.md` §4.

Do not re-derive the scoring model. Do not adjust thresholds, band tables, point weights, the 100-point scale, the 85/40 classification, the 100 km **reporting-period-only** gate, the ranking rules, or the weekly cumulative semantics.

## 2. Invariant — must not be changed while implementing

`BUSINESS/DATA CONTRACT`:

1. **Status derivation.** `raw violations + qualifying exposure/distance → Eco violation coefficient (integer, ROUND_HALF_UP) → existing scoring bucket/threshold → points → status`. Green = `points == points_max`; yellow = `0 ≤ points < points_max`; red = `points < 0`; neutral = no coefficient derivable under the existing contract. One rule, everywhere, period and day. Two identical raw counts may differ in colour. Do not port `LOST_POINTS_COLOR_RULES`, and do not create a dashboard-local threshold table.
2. **Counts and colours are separate.** A raw count is never coloured by itself, and a colour is never explained by a count alone.
3. **Weekly is cumulative MTD.** `period_start_date` = month start. Comparison basis = the previous closed MTD snapshot, labelled with both date ranges. The strings "ostatni tydzień" / "last week" / "this week" must not exist in the codebase's UI copy.
4. **Monthly compares closed month vs previous closed month.** The within-month shape comes from that month's closed MTD snapshots and is labelled as such.
5. **Closed periods only.** No live data, no provisional period, no polling, no auto-refresh, no relative "updated 2 hours ago" — show `snapshot_updated_at` and the closing date.
7. **100 km qualification is period-level only.** If the total qualifying distance for the whole closed reporting period is < 100 km, do not send/render any Eco result data for that period. Serve a minimal `INSUFFICIENT_DISTANCE` state; do not expose actual distance, categories, counts, trends, coaching or daily rows. Conversely, after the whole period qualifies, there is **no daily 50/100 km gate**: a 1–99 km day is shown with its actual distance and raw counts, and its category coefficients/statuses are calculated from that day's actual distance whenever derivable. `0 km` is the neutral no-driving day.
7. **Ranking eligibility.** `ranking_position` / `ranking_total_participants` / `rating_group_share_percent` exist in the payload only for `ranking_state == RANKED`. `EXCLUDED` and `UNKNOWN_DRIVER` drivers get the full dashboard minus rank, rank movement and group share. Never fabricate a delta from an absent rank.
8. **Coaching is deterministic** and computed on the host by the four rules in `COMPONENT_SPECIFICATIONS.md` C-13: `LARGEST_LOSS` on points lost; `MOST_IMPROVED`/`MOST_DETERIORATED` on **coefficient movement** (never requiring a bucket crossing); `BEST_OPPORTUNITY` on coefficient vs the next existing threshold plus its deterministic points gain. No model calls, no randomness, no predictions, never over-rev, never for a `LOW_DISTANCE` period because that period never renders the normal Eco dashboard. **No event-budget language anywhere.**
9. **No daily points** anywhere, and **no aggregate `day_status`** anywhere — a day carries per-category statuses only.
10. **Privacy exclusions and payload minimisation** exactly as listed in `DATA_TO_UI_MAPPING.md` §3: no `driver_key`, no `client_code`, no name/e-mail/phone/employee id/registration/location/route/GPS, no other driver's result. The Worker resolves identity internally; that resolution never becomes a dashboard field. One driver's presentation snapshot per response, never a client-wide bundle filtered in the browser.
11. **Fail closed.** A snapshot that cannot satisfy the complete Eco scoring contract is **not published as a dashboard**: the Worker serves `REPORT_NOT_READY`. Never a partial score, never a screen that looks complete with a scoring category missing. Over-rev at full points is a valid result, not missing data. Inherit the AS-IS posture (`SCORING_RANKING_CONTRACT` §7).
11. **Numbers are computed in Python.** `Decimal` + `ROUND_HALF_UP`. The browser formats and lays out; it never scores.
12. **Colour is never the only carrier of meaning** (glyph + label + position always accompany it).

## 3. Flexible — change freely if it serves the user

`VISUAL DESIGN DECISION`:

- Exact hex values, provided the four status states stay distinguishable in greyscale and meet the contrast table in `ACCESSIBILITY_SPEC.md` §2.
- Font choices (any grotesque + any monospace with Polish coverage), as long as numeric values stay monospaced/tabular.
- Card radii, paddings, shadow, grid gaps, exact breakpoint pixels.
- Chart pixel geometry: bar widths, axis height, marker shapes.
- Whether the category table is a table or a list of cards at `wide`.
- Chart forms, within the semantics fixed by `VISUAL_SYSTEM.md` §5 (the ring for loss distribution and the connector line over discrete period bars are the approved final choices).
- Which category is expanded by default (a sensible default is `LARGEST_LOSS`).
- Hash-routing scheme, `localStorage` key names.
- Whether coaching cards link to their category row.
- Skeleton-loading appearance.
- Framework: none is required. Plain HTML + CSS + a small amount of JS is sufficient and preferred for a static Cloudflare Pages site. If a framework is used, it must not become a reason to compute numbers client-side.

## 4. Genuine engineering dependencies

| id | What is needed | Consequence if unresolved |
|---|---|---|
| `DEP-01` | Whether an authoritative non-evaluable condition already exists in the Eco scoring contract | only the `neutral` daily state beyond no-driving | **do not invent a threshold.** Use the coefficient wherever the existing contract yields one; `kilometers == 0` → `neutral` no-driving. Derive any existing rule from the repository |
| `DEP-02` | Refresh cadence / whether the ten disabled Eco schedules get enabled | copy only: no "aktualizowane co tydzień" claim until schedules run |
| `DEP-03` | Per-category `LAG()` over `*_events_per_100km` **and** `*_maxpoints_subtract` (AS-IS `G-02`) | coefficient movement rows and `MOST_IMPROVED`/`MOST_DETERIORATED`, which select on coefficient movement | UI degrades to "brak bazy" |
| `DEP-04` | Which persisted periods predate the ranking-contract recalculation (AS-IS `G-07`) | `comparison.comparable=false` for rank across that boundary |
| `DEP-05` | Snapshot generator job + publisher + R2 layout + Worker token→key mapping with revocation (AS-IS `G-16`, `G-01`) | the whole delivery path; the access/data-gate states depend on the Worker's status codes |
| `DEP-06` | `rating_group_distribution` as a period-level query copied into each snapshot (AS-IS `S-10`) | distribution bar hidden; own share still shown |
| `DEP-07` | Materialising `days[]` at snapshot time (AS-IS `G-09` retention) | Detailed view for older periods |
| `DEP-08` | Per-pipeline adapter (`eco_driver_*` for ALPHA00001, `eco_person_*` for BRAVO00016) feeding one snapshot contract (AS-IS `CLIENT_VARIANTS` §7) | one dashboard, two adapters — do not build two dashboards |

## 5. Suggested build order

1. **Snapshot generator** as a registered job (`run(client, run_id, params)`), dry-run first, one transaction, fail-closed. Emit the contract in `DATA_TO_UI_MAPPING.md` §1 for one driver, one period. Assert the consistency rules in §4 of that document before writing.
2. **Derivations**: `ranking_state`, per-category status, band/marker/target, per-category previous values (`DEP-03`), daily rows, coaching selection.
3. **Static frontend**: shell + `C-01`/`C-02`/`C-03` against a fixture snapshot. Get the axis and the status rule right before anything else — they are the product.
4. **Summary** completion: ranking states, group share, distance, coaching, category table with the expandable axis, trends.
5. **Detailed**: groups, day rows, disclosure, compact mode, mobile cards.
7. **States**: the 15 data states from `STATES_AND_EDGE_CASES.md` as fixture snapshots, each with a screenshot test.
8. **Access path**: Worker, token mapping, revocation, the access/data-gate states.
9. **Accessibility pass** per `ACCESSIBILITY_SPEC.md` §7.

## 6. Test fixtures to build (one snapshot each)

`ranked_safe` · `ranked_acceptable` · `ranked_dangerous` · `not_ranked_by_configuration` · `not_on_roster` · `newly_ranked` · `left_ranking` · `insufficient_period_distance` · `first_closed_period_of_month` · `end_of_month_period` · `no_comparison` · `report_not_ready` · `zero_event_category` · `no_driving_day` · `skipped_period_in_series` · `monthly_31_days`.

Each fixture must be synthetic. `BC`: no production driver name, e-mail, registration, coordinate, identifier or token in any fixture, test, screenshot or artifact.

## 7. Explicitly out of scope for V1

Isolated week-over-week scoring · in-flight period · fleet-manager or operator views · notifications from the dashboard · export/print · language switching · dark mode · any change to the e-mail templates (a later change should retire `LOST_POINTS_COLOR_RULES` and stop using over-rev as the axis-legend example, but that is a separate task) · any change to `eco_scoring.py`.

## 8. Scope and safety confirmation for this design task

No production code, database, template, schedule, release or Cloudflare resource was created, modified or deployed to produce this package. No e-mail was sent. No production personal data was used: every value in every visual reference is synthetic.
