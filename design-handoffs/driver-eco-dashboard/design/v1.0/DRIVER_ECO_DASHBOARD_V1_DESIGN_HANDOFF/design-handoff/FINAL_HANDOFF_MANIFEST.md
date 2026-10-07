# FINAL_HANDOFF_MANIFEST

**Classification:** `DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF_IMPLEMENTATION_READY`
**Package version:** `v3.1 · 2026-08-18` — the single effective version. Versions `v1.0` (modular set) and `v2.0` (first implementation brief) are **withdrawn**; every document in this folder now carries `v3.1`.
**Companion package:** `eco-driving-as-is-audit-handoff/` `v1.0` — required, shipped separately.
**Scope of this package:** UX/UI + interaction design and the implementation contract. No production code, database, template, schedule, migration, commit or Cloudflare resource was created or modified. No production PII appears in any artifact.

---

## 1. Authority map — who decides what

| Authority | Artifact | Rule |
|---|---|---|
| **BUSINESS / DATA** | `eco-driving-as-is-audit-handoff/` (AS-IS facts: formulas, thresholds, period semantics, ranking, ingestion, privacy) **+** the owner-approved target decisions recorded in `CLAUDE_CODE_IMPLEMENTATION_BRIEF.md` §1 and `DESIGN_HANDOFF_INDEX.md` §4 | Never reinterpret. If design and data semantics conflict, **data semantics win.** |
| **UX** | this `design-handoff/` set, headed by `PRODUCT_UX_CONTRACT.md` | Information architecture, behaviour, states, copy rules. |
| **VISUAL** | `../Eco Driving Dashboard.dc.html` (product in context) and `../Eco Driving Design System.dc.html` (tokens and component anatomy) | The design of record for layout, colour, type, motion and copy. Where a Markdown value disagrees with these files, these files win. |
| **IMPLEMENTATION BRIDGE** | `CLAUDE_CODE_IMPLEMENTATION_BRIEF.md` | Self-contained contract: decisions, invariants, snapshot schema + assertions, derivations, screens, copy, delivery, build order, acceptance checklist. **Read this first.** |

Priority order when anything conflicts:
1. Owner-approved decisions and invariants (brief §1, §2).
2. AS-IS audit.
3. Canonical `.dc.html` visual sources.
4. The rest of `design-handoff/`.
5. Engineering judgement — visual details only.

## 2. Final artifact inventory

| # | File | Purpose | Authoritative for | Version | Claude Code must read |
|---|---|---|---|---|---|
| 1 | `FINAL_HANDOFF_MANIFEST.md` | package contract and authority map | which artifact decides what | v3.1 | **yes — first** |
| 2 | `CLAUDE_CODE_IMPLEMENTATION_BRIEF.md` | complete implementation contract | invariants, snapshot schema, assertions, derivations, build order | v3.1 | **yes — second** |
| 3 | `DESIGN_HANDOFF_INDEX.md` | reading order, decision log, dependency register | traceability of every resolved decision | v3.1 | yes |
| 4 | `PRODUCT_UX_CONTRACT.md` | IA and behaviour of the four experiences | Summary/Detailed structure, weekly vs monthly semantics, copy rules, fail-closed rule, coaching semantics | v3.1 | **yes** |
| 5 | `VISUAL_SYSTEM.md` | tokens, chart language, number formatting | type, colour, spacing, surfaces, charts, iconography | v3.1 | **yes** |
| 6 | `COMPONENT_SPECIFICATIONS.md` | C-01…C-17 component contracts | per-component data, hierarchy, interaction, responsive, empty states | v3.1 | **yes** |
| 7 | `SCREEN_SPECIFICATIONS.md` | the four screens block by block | content order per screen, acceptance checks | v3.1 | yes |
| 8 | `RESPONSIVE_AND_INTERACTION_SPEC.md` | breakpoints, transformations, interaction, motion | narrow/zoomed behaviour, no-horizontal-scroll rule | v3.1 | **yes** |
| 9 | `STATES_AND_EDGE_CASES.md` | data / eligibility / comparison / access states | exact per-state behaviour and copy | v3.1 | **yes** |
| 10 | `ACCESSIBILITY_SPEC.md` | contrast, keyboard, colour independence, chart labelling | AA contract and its verification list | v3.1 | **yes** |
| 11 | `DATA_TO_UI_MAPPING.md` | DISPLAY vs SEMANTIC value + snapshot expectations | what each UI concept requires from data | v3.1 | **yes** |
| 12 | `IMPLEMENTATION_HANDOFF.md` | invariant vs flexible vs dependency | what may be changed while implementing | v3.1 | yes |
| 13 | `VISUAL_REFERENCES.md` | how to open and read the visual sources | which state selector shows which case | v3.1 | yes |
| 14 | `../Eco Driving Dashboard.dc.html` | **canonical Dashboard visual source** | final design of all four experiences + states | v3.1 | **yes — open it** |
| 15 | `../Eco Driving Design System.dc.html` | **canonical Design System visual source** | tokens and component anatomy | v3.1 | **yes — open it** |

Both `.dc.html` files are portable: they open directly in any browser, need no build step, and degrade to system fonts offline.

## 3. Non-authoritative archive

`../archive-non-authoritative/` — kept only for provenance, **must not be implemented from**:

| File | Why superseded |
|---|---|
| `Eco Driving Dashboard V1.dc.html` | pre-approval visual language (warm neutrals, monospace); replaced by the canonical dashboard |
| `Styl A - Aurora.dc.html` | rejected style exploration |
| `Styl B - Pastelowa glina.dc.html` | rejected style exploration |
| `Styl C - Blok kolorystyczny.dc.html` | rejected style exploration |
| `Styl D - Nordic BI.dc.html` | the chosen direction, but a single-screen sketch — its evolved form is the canonical dashboard |

No active document references any of these files. There is exactly **one** dashboard visual source and **one** design-system visual source.

## 4. Reconciliation record — what this pass resolved

| Conflict / defect in the reviewed package | Resolution (now consistent in every document) |
|---|---|
| Missing visual sources; competing `V1`/`V2` filenames | one canonical dashboard (`Eco Driving Dashboard.dc.html`) + one design system (`Eco Driving Design System.dc.html`); superseded drafts archived; every reference updated |
| Two effective versions (modular v1.0 vs brief v2.0) | single version `v3.1` stamped on every document; the brief and the modular set say the same thing |
| Donut allowed vs prohibited | **kept**, as the owner-approved `LossDistributionRing`: enrichment only, `aria-hidden` segments, legend rows as the real controls, every value present as text. `VISUAL_SYSTEM.md` §5 is now the single ruling |
| Continuous trend line vs discrete snapshots | **both semantics honoured**: one bar per closed period, individually labelled with its full date range and value, plus a connector through the bar tops for direction. Nothing between bars is data — no smoothing, interpolation, area fill or live metaphor |
| Coaching "why" field vs no "why" field | the literal `Dlaczego` field is removed; explainability is a **semantic requirement** met by the value line + sentence (which metric, what changed, what scoring consequence) |
| Responsive table: `min-width: 960px` scroll vs no-horizontal-scroll | **no horizontal scrolling at any width**; the daily grid transforms: full grid ≥ 1024 px → compact grid 700–1023 px → day cards < 700 px; same path at high zoom |
| Colour possibly readable as count-derived | one identical invariant chain in all eight required documents plus the visual sources |
| Invented daily distance thresholds | **removed everywhere**: no 50 km or 100 km daily gate. The authoritative 100 km threshold applies only to the total qualifying distance of the entire reporting period. `day.kilometers == 0` is the neutral no-driving state; 1–99 km days are shown normally once the period qualifies |
| Aggregate `day_status` | **removed** from snapshot, desktop grid, mobile cards, rails/chips, fixtures, mapping and brief; per-category statuses only |
| `driver_key` / `client_code` in the browser payload | **removed**; the Worker resolves identity internally and that resolution never becomes a dashboard field |
| Partial-scoring dashboard state | **removed** as a normal state; replaced by fail-closed `REPORT_NOT_READY`; over-rev at full points is explicitly *not* missing data |
| Improvement/deterioration defined by points bucket | now selected on **coefficient movement**; points consequence reported, never used to select |
| `max_events_indicative` event-budget language | **removed**; improvement is communicated as coefficient + threshold only |
| Over-rev "brak pomiaru w tej flocie" | replaced by the neutral, factual "pełne 15 pkt w obecnym okresie"; no unsupported technical cause |

## 5. Preserved product contracts (not reopened)

Summary vs Detailed structure · weekly vs monthly split · cumulative MTD weekly semantics · month-close behaviour · coefficient-based colours · EXCLUDED treatment (full dashboard minus rank, rank delta and group share) · per-client private-trip difference (ALPHA00001 excludes, BRAVO00016 includes; never surfaced in UI) · over-rev inside the 100-point model · closed-period-only freshness · deterministic coaching · privacy exclusion of location and registration · capability-link access model with no login.

## 6. Visual-reference coverage

Openable in `../Eco Driving Dashboard.dc.html` via the period/view switches and the state selector: Weekly Summary · Weekly Detailed · Monthly Summary · Monthly Detailed · ranked (safe / acceptable / dangerous) · unranked EXCLUDED · newly ranked · driver leaving ranking · no comparison available (first closed period of month) · insufficient reporting-period distance (all Eco data suppressed) · largest-loss, most-improved, most-deteriorated and best-opportunity coaching · two near-threshold cards · zero-event category (and the teaching case of a nonzero count still green) · no-driving day · `REPORT_NOT_READY` · all six access/data-gate states · representative mobile Summary and mobile Detailed (390 px frames). Component anatomy and tokens: `../Eco Driving Design System.dc.html`. All data synthetic.

## 7. Validation performed before packaging

Deterministic search across every active document and both visual sources:

| Check | Result |
|---|---|
| active document referencing a missing visual HTML file | none |
| reference to a superseded `V1`/`V2` visual filename | none |
| `MIN_DAILY_EVALUATION_KM` / `min_daily_evaluation_km` / any 50 km or 100 km daily rule | none |
| colour defined directly from a raw event count | none — the coefficient chain is stated identically in all required documents |
| aggregate `day_status` in any snapshot contract or UI spec | none |
| `driver_key` in a driver-facing/browser contract | none (listed as forbidden) |
| `client_code` in a driver-facing/browser contract | none (listed as forbidden) |
| normal dashboard state permitting invalid partial scoring data | none — `REPORT_NOT_READY` is the only behaviour |
| coaching improvement/deterioration defined solely by bucket change | none — coefficient movement is the selector |
| text telling the driver how many violations are allowed | none |
| claim about the technical cause of full over-rev points | none |
| conflicting horizontal-scrolling requirements | none — one rule, three breakpoint behaviours |
| canonical visual-source names consistent across documents | yes |
| version labels consistent | yes — `v3.1` everywhere |

## 8. Remaining genuine engineering dependencies

`DEP-02` refresh cadence / disabled Eco schedules (copy only) · `DEP-03` per-category previous coefficients and points lost · `DEP-04` periods predating the ranking-contract recalculation · `DEP-05` snapshot generator, publisher, R2 layout, token→key mapping with revocation · `DEP-06` period-level `rating_group_distribution` · `DEP-07` materialising `days[]` against retention · `DEP-08` per-pipeline adapter feeding one snapshot contract.

None of these is a design question; each is resolved from the repository or by operations.
