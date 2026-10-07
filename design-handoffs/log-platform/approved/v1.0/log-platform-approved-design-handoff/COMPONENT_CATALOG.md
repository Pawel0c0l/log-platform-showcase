# Component Catalog

Reusable components in the approved design. This is a **design-system inventory**, not a prescription of a React/Vue/Web-Component architecture — how these are decomposed in code is the repository's decision.

Format: `Component | Purpose | Variants | States | Used on | Reference`

---

## Shell

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **App shell** | Frame: 56 px app bar + 64 px context bar + full-width working area | light, dark | — | all | `SHL-001/002` |
| **App bar** | Brand, primary nav, global search, theme switch, account | light, dark; compact (≤1024) | — | all | `SHL-001/002`, `RSP-001` |
| **Brand mark** | 6×18 px accent bar + wordmark | full, mark-only (≤768) | — | all | `SHL-001` |
| **Nav item** | Top-level module link | data-mode, tools-group | rest, hover, focus, active | all | `SHL-001` |
| **Nav menu button** | Collapsed nav trigger at ≤1024 px | — | rest, hover, focus, open | `RSP-001/002` | `RSP-001` |
| **Global search field** | Cross-module search with `⌘K` hint | app-bar (300–340 px), icon-only (≤768) | rest, focus, results-open | all | `SHL-001` |
| **Theme switcher** | AUTO · ☀ · ☾ segmented override | — | per-segment selected | all | `SHL-003` |
| **Account chip** | User name + 28 px avatar | with name, avatar-only (≤1024) | rest, hover, focus, menu-open | all | `SHL-001` |
| **Client context bar** | Client identity + page identity + page actions | selector, static label; two-line (≤1024) | rest, open | all modules | `DB-003`, `REP-001`, `ECO-001` |
| **Client selector** | Bordered client picker | plain-label (Dane, Raporty), bordered (Eco Driving) | rest, hover, focus, open | `DB-003`, `REP-001`, `ECO-001` | `ECO-001` |
| **Client code chip** | Mono client code on a tinted ground | light, dark | — | all modules | `DB-003` |
| **Read-only badge** | `TYLKO ODCZYT` technical badge | — | — | `DB-003/004/006` | `DB-003` |
| **Breadcrumb bar** | Full analytical path + back + sibling nav on detail pages | Eco Driving, Report | rest | `ECO-003`, `REP-003` | `ECO-003` |
| **Sibling nav pair** | `‹ previous` / `next ›` within the same collection | driver, report period | rest, hover, focus; edge = absent | `ECO-003`, `REP-003` | `ECO-003` |
| **Export activity indicator** | Live `n eksport w toku` in the app bar | — | idle (absent), active | `DB-007` | `DB-007` |
| **Operator-mode badge** | Marks a technical surface | — | — | `ART-001` | `ART-001` |

## Actions and inputs

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Button** | Action | primary (neutral fill), secondary (bordered), quiet (text-only accent) | rest, hover, focus, active, loading, disabled (temporary only) | all | all |
| **Button size** | — | lg 36, md 34, sm 32, xs 30, touch 44 | — | all | `DESIGN_TOKENS.md` |
| **Icon button** | Single-glyph action (`↑` `↓` `×` `‹` `›` `⋯`) | 26, 28, 32, 44 px | rest, hover, focus, active | panels, pagination | `DB-006` |
| **Text input** | Single-line value entry | default, with leading icon, with unit | rest, hover, focus, invalid, disabled | filters, search | `DB-005` |
| **Search field** | Scoped search with a stated scope in the placeholder | global, toolbar, in-panel | rest, focus, has-value | all tables | `DB-003` |
| **Select** | Single choice from a list | inline, bordered | rest, focus, open | `REP-001`, export | `REP-001` |
| **Segmented control** | 2–4 mutually exclusive options | 2-up, 3-up, 4-up | per-segment selected, focus | density, unit toggle, theme, format, boolean filter | `DB-003`, `ECO-001` |
| **Checkbox** | Boolean selection | row, column-visibility, header select-all | unchecked, checked, indeterminate, focus | tables, `DB-008` | `DB-008` |
| **Radio row** | One-of-N with a per-option count | — | selected, rest, focus | `DB-009` | `DB-009` |
| **Date-time field** | Date + time entry with a calendar picker | from, to, single | rest, focus, picker-open, blank-allowed | date filters | `DB-005` |
| **Toggle card** | Selectable card with a label and a sub-caption | week, period | selected, rest, partial, hover, focus | `ECO-001` | `ECO-001` |
| **Stepper** | Previous / label / next | month | rest, edge (control absent) | `ECO-001` | `ECO-001` |
| **Drag handle** | Reorder affordance `⠿` | — | rest, grabbing | `DB-008` | `DB-008` |

## Table

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Data table** | The primary working surface | Database, Eco ranking, Eco trips, Artifact, Report history | loading, populated, filtered, sorted, empty, error | `DB-003`, `ECO-001/003`, `ART-001`, `REP-003` | `TABLE_AND_DATA_GRID_SPEC.md` |
| **Table header cell** | Column label + sort + menu affordance | text, numeric (right), centred, two-line with unit caption | unsorted, asc, desc, filtered, hover, focus, resizing | all tables | `DB-003` |
| **Sticky column** | Pinned identity column | checkbox, identifier, position | scrolled (edge shadow) | all tables | `DB-003` |
| **Column resize handle** | Width adjustment on the header edge | — | rest, hover, dragging | `DB-003` | `DB-003` |
| **Table row** | One record | compact 32, comfortable 40, ranking 38 | rest, hover, selected, range-selected | all tables | `DB-003` |
| **Cell** | One value | identifier, string, integer, decimal, percentage, date, timestamp, boolean, enum, null, empty, zero, json, out-of-range | rest, selected, in-range, truncated | all tables | `TABLE_AND_DATA_GRID_SPEC.md` §4 |
| **Skeleton row** | Loading placeholder at final dimensions | per density | — | all tables | §6 |
| **Right-edge scroll fade** | Signals horizontal continuation | light, dark | visible when scrollable | all wide tables | `DB-003` |
| **Pagination** | Page navigation + page size | full, compact (≤1024) | current, available, edge-muted | all tables | `DB-003` |
| **Result counter** | `n z m` with the filtered/total distinction | table, library | — | all tables | `DB-003` |
| **Selection counter** | Rows selected + copy hint | — | zero (hidden), n selected | `DB-003` | `DB-003` |
| **Jump-to field** | Direct navigation by name or position | — | rest, focus | `ECO-001` | `ECO-001` |

## Filtering

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Column menu** | Per-column sort, filter, distribution, column actions | text, numeric, date, boolean | closed, open, applied, dismissed | all tables | `DB-005` |
| **Operator row** | Type-appropriate operator picker | text (4), numeric (8), date (4), boolean (4) | selected, rest, focus | `DB-005` | `DB-005` |
| **Distinct-value picker** | Multi-select of frequent values with counts and bars | — | loading, populated, searched, all-selected | `DB-005` text | `DB-005` |
| **Value histogram** | 14-bucket numeric distribution with the threshold marked | — | — | `DB-005` numeric | `DB-005` |
| **Filter chip** | One active filter: column + operator + value + remove | toolbar strip, panel entry, compact (≤1024) | rest, hover, focus | all tables | `DB-003` |
| **Filter panel** | Aggregate filter surface: active list, add-filter, date presets | docked 352 px, drawer 400 px | collapsed, docked, drawer-open, staged-changes | `DB-003`, `RSP-002` | `DB-003` |
| **Filter count badge** | Numeric badge on the `Filtry` button | — | zero (hidden), n | all tables | `DB-003` |
| **Date preset chips** | Quick ranges for the default date column | — | selected, rest | filter panel | `DB-003` |
| **Column visibility panel** | Show/hide, reorder, pin, save sets | 400 px | rest, searched, tab-filtered, reordering, all-hidden-blocked | `DB-008` | `DB-008` |
| **Unit toggle** | `/ 100 km ⇄ Σ suma` across all metric columns | — | rate, sum | `ECO-001/002/003` | `ECO-001` |

## Panels and overlays

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Row detail panel** | All fields of one record, grouped | 520 px | open, loading, traversing, closed | `DB-006` | `DB-006` |
| **Field row** | Label + physical name + value + copy | text, mono, null, empty, long | rest, hover | `DB-006` | `DB-006` |
| **Field group header** | Section eyebrow + field count | — | — | `DB-006`, `ECO-003` | `DB-006` |
| **Export panel** | Scope, columns, format, path notice | 400 px | rest, above-threshold, unavailable | `DB-009` | `DB-009` |
| **Path notice** | States immediate vs background before commit | positive (immediate), warning (background) | — | `DB-009` | `DB-009` |
| **Overlay drawer** | Panel as an overlay with a scrim at ≤768 px | filter | open, closed | `RSP-002` | `RSP-002` |
| **Popover menu** | Generic anchored menu | column, select, account | open, closed | all | `DB-005` |

> There is **no modal dialog** in the approved design. Every layer is either an anchored menu, a docked panel, an overlay drawer, or a dedicated page. This is deliberate: modals interrupt, and this product's work is continuous inspection.

## Status and feedback

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Status badge (word)** | Semantic state as a word + colour | positive, warning, negative, neutral | — | `REP-001`, `DB-007`, tables | `REP-001` |
| **Technical badge (mono)** | Uppercase technical marker | read-only, error class, operator mode, lineage, artifact state | — | all modules | `DB-003` |
| **Permission badge** | Feature grant on a dataset | granted, not-granted (neutral) | — | `DB-001` | `DB-001` |
| **New badge** | Unseen report instance | — | — | `REP-001` | `REP-001` |
| **Attention dot** | Latest instance of a type failed | — | — | `REP-001` rail | `REP-001` |
| **Progress bar** | Determinate background-export progress | — | 0–100 % | `DB-007` | `DB-007` |
| **Status left-edge** | 3 px status colour on a list row | per status | — | `REP-001` | `REP-001` |
| **Empty state** | Names the cause and offers a corrective action | filtered-empty, truly-empty, no-access, zero-selection | — | all modules | `DB-010`, `REP-004` |
| **Error state** | Failure class, permission-OK statement, copyable diagnostics, actions | data-source, file-store, generation | — | `DB-011`, `REP-004` | `DB-011` |
| **Diagnostic trio** | Timestamp + reference + code, copyable | — | — | error states | `DB-011` |
| **Inline warning** | Non-blocking advisory | non-contiguous weeks, partial data, all-columns-hidden | — | `ECO-001`, `DB-008` | `ECO-001` |
| **Advisory screen** | Full-screen limit notice with routes out | below-min-width | — | `RSP-003` | `RSP-003` |

## Data display

| Component | Purpose | Variants | States | Used on | Reference |
|---|---|---|---|---|---|
| **Metric hero** | 52 px mono value with a denominator | score, position | — | `ECO-003` | `ECO-003` |
| **Score bar** | Proportional score in a cell | good, mid, low | — | `ECO-001` | `ECO-001` |
| **Distribution histogram** | 24-bucket fleet distribution with median/mean | period bar, with subject marked | — | `ECO-001/003` | `ECO-001` |
| **Trend bars** | Value per period across 8 periods | — | current highlighted, fewer-periods | `ECO-003` | `ECO-003` |
| **Share bar** | Proportion of a total with the percentage stated | — | — | `ECO-003`, `DB-005` | `ECO-003` |
| **Metadata grid** | 4-column label/value grid of persisted fields | 8-field (report), 16-field (Eco entry) | — | `REP-003`, `ECO-003` | `ECO-003` |
| **Basis line** | The single authoritative sentence describing the active period | — | full, partial-week, gapped | `ECO-001` | `ECO-001` |
| **Rail** | Grouped left navigation list with counts | dataset 288, report-type 288, artifact-kind 252 | rest, active item, searched, collapsed (≤1024) | `DB-001`, `REP-001`, `ART-001` | `DB-001` |
| **Comparison table row** | Dataset as a comparable row, not a card | — | rest, hover | `DB-001` | `DB-001` |
| **Report instance row** | Report as a status-bearing card-row | per status | rest, hover | `REP-001` | `REP-001` |
| **File entry** | Format, name, size, semantic note, actions | main (tinted), secondary, non-previewable | rest, hover | `REP-003` | `REP-003` |
| **Format badge** | File format + size on a library row | — | — | `REP-001` | `REP-001` |
| **Document preview** | Embedded paged document view | PDF, spreadsheet, unavailable | loading, paged, full-screen | `REP-003` | `REP-003` |
| **History table** | Same object across previous periods | 8-period, expandable | current-highlighted | `REP-003` | `REP-003` |
| **Saved-view chip** | Named view as a direct entry point | — | rest, hover | `DB-001` | `DB-001` |
| **Saved-view selector** | Active view name + switch | — | rest, open, modified | `DB-003` | `DB-003` |

> **Not in this catalog, deliberately:** tooltip (no information in the approved design is tooltip-only), modal dialog (see above), toast (feedback is inline or in the app-bar indicator), KPI card (rejected in favour of the component table — decorative KPI cards were explicitly out per the original brief), and card-per-row mobile layouts (rejected by `D-015`).
