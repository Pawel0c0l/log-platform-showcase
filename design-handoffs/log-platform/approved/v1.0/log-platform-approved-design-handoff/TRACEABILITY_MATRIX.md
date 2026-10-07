# Traceability Matrix

Requirement / behaviour → Screen ID(s) → Component(s) → Spec section → Acceptance criterion.

Its purpose is to stop an approved behaviour from being quietly lost during implementation. If a row cannot be demonstrated in the running product, that behaviour has regressed.

Abbreviations: `PBC` = `PRODUCT_BEHAVIOR_CONTRACT.md` · `TGS` = `TABLE_AND_DATA_GRID_SPEC.md` · `EDS` = `ECO_DRIVING_ANALYTICS_SPEC.md` · `INT` = `INTERACTION_SPEC.md` · `RSP` = `RESPONSIVE_SPEC.md` · `A11Y` = `ACCESSIBILITY_SPEC.md` · `TOK` = `DESIGN_TOKENS.md` · `ACC` = `IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`

---

## Client context

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Active client visible on every screen of every module | all | Client context bar, Client selector, Client code chip | `PBC` §1.3 | `SH-6`, `RS-10` |
| Client context survives empty, loading, permission-denied and error states | `DB-010`, `DB-011`, `REP-004`, `ECO` states | Empty state, Error state | `PBC` §2.14–2.17, §3.8 | `SH-6`, `DB-60`, `RP-19` |
| Client switch stays in the current module | all | Client selector | `PBC` §1.3, `INT` §6 | `SH-6` |
| Client is unambiguous in Eco Driving specifically | `ECO-001`–`ECO-003` | Client selector (bordered), Breadcrumb bar, Metadata grid | `EDS` level 1 | `EC-1` |
| Client never hidden by responsive degradation | `RSP-001`–`RSP-003` | Client context bar | `RSP` § Client context bar | `RS-10` |

## Dataset context

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Only assigned datasets are listed | `DB-001`, `DB-002` | Rail, Comparison table row | `PBC` §2.1 | `DB-61` |
| Only approved columns are visible or selectable | `DB-003`, `DB-008` | Column visibility panel, Client context bar | `PBC` §2.1, §2.7 | `DB-26` |
| Read-only semantics are stated and enforced in the UI | `DB-003`, `DB-004`, `DB-006` | Read-only badge | `PBC` §2.3 | `DB-4` |
| Physical table name and approved-column count are visible | `DB-003` | Client context bar | `PBC` §2.2 | `DB-4` |
| Permission flags are shown as configuration, not error | `DB-001` | Permission badge | `PBC` §2.1 | `DB-62`, `DB-54` |
| Dataset switch clears filters, sort and columns | `DB-001` → `DB-003` | Rail, Data table | `INT` §6 | `DB-34` |

## Column filtering

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Filtering is column-centric, not a form above the table | `DB-003`, `DB-005` | Column menu, Filter chip | `PBC` §2.5, `TGS` §3 | `DB-20`, `DB-9` |
| Type-appropriate operators per data family | `DB-005` | Operator row | `TGS` §3.2–3.5 | `DB-9`, `DB-11`, `DB-12` |
| Numeric filtering shows the distribution and the threshold before applying | `DB-005` | Value histogram | `TGS` §3.3 | `DB-10` |
| Text filtering offers distinct values with counts | `DB-005` | Distinct-value picker | `TGS` §3.2 | `DB-11` |
| Open-ended date ranges are allowed | `DB-005` | Date-time field | `TGS` §3.4 | `DB-12` |
| Column menu applies immediately; the panel stages | `DB-003`, `DB-005` | Column menu, Filter panel | `PBC` §2.5, `INT` §4 | `DB-13`, `DB-14` |
| Dismissing a menu discards; dismissing the drawer keeps | `DB-005`, `RSP-002` | Column menu, Overlay drawer | `INT` §3 | `DB-15`, `RS-6` |
| Global text search is retained and its scope is stated | `DB-003` | Search field | `PBC` §2.6 | `DB-25` |

## Active-filter visibility and reset

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| One filter = one chip with column, operator, value | `DB-003`, `RSP-001` | Filter chip | `PBC` §2.5, `TGS` §3.6 | `DB-16` |
| Removing one filter is immediate | `DB-003` | Filter chip | `INT` §4 | `DB-17` |
| Clear-all removes column filters and the text search | `DB-003` | Filter panel | `TGS` §3.6 | `DB-18` |
| Filter count is visible without opening the panel | `DB-003`, `RSP-002` | Filter count badge | `TGS` §3.6 | `DB-19` |
| A filtered column is marked in the header | `DB-003` | Table header cell | `TGS` §1.2 | `DB-21`, `AC-7` |
| Filtered vs total counts are both visible | `DB-003` | Result counter | `PBC` §2.9 | `DB-24` |

## Sorting

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Sorting is invoked from the column header | `DB-005`, `ECO-001` | Column menu, Table header cell | `TGS` §2 | `DB-22` |
| Single-column sort only | all tables | Data table | `TGS` §2 | `DB-23` |
| Sort state is communicated non-visually and in words | `DB-003` | Table header cell, Result counter | `TGS` §2, `A11Y` §2, §8 | `DB-22`, `AC-6`, `AC-9` |
| Null ordering is defined and stated | `DB-005` | Column menu | `TGS` §2 | `DB-9` |

## Column visibility, order, width

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Visible columns are user-selectable from approved columns | `DB-008` | Column visibility panel | `PBC` §2.7 | `DB-26` |
| Columns can be reordered | `DB-008` | Drag handle | `TGS` §5.2 | `DB-27` |
| Columns can be resized and autofitted | `DB-003`, `DB-005` | Column resize handle, Column menu | `TGS` §1.7 | `DB-29` |
| At least one column must remain visible | `DB-008` | Inline warning | `TGS` §5.2 | `DB-28` |
| Column sets can be named and reused | `DB-008`, `DB-001` | Column visibility panel, Saved-view chip | `TGS` §5.2, §5.4 | `DB-30` |
| Persistence follows `D-007` exactly | `DB-003` | — | `TGS` §5.4 | `DB-34`, `DB-31` |

## Density, pagination, copy

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Two densities, browser-persisted | `DB-003` | Segmented control | `PBC` §2.8 | `DB-31`, `DB-5` |
| Pagination 25–500, default 100 | `DB-003`, `ECO-001`, `REP-001` | Pagination | `PBC` §2.9 | `DB-32`, `DB-33` |
| No virtualization in phase 1 | all tables | Data table | `PBC` §2.9 | `DB-33` |
| Range selection copies as spreadsheet cells | `DB-003` | Cell, Selection counter | `PBC` §2.11 | `DB-44`, `AC-2` |

## Row detail

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Row click opens a right-hand panel | `DB-006` | Row detail panel | `PBC` §2.10 | `DB-36` |
| Table stays visible and keeps scroll position | `DB-006` | Row detail panel, Data table | `PBC` §2.10 | `DB-36` |
| Keyboard traversal without closing | `DB-006` | Icon button, Field row | `INT` §2.2 | `DB-37` |
| Focus returns on close | `DB-006` | Row detail panel | `INT` §2.3, `A11Y` §5 | `DB-38`, `AC-4` |
| Fields grouped, searchable, switchable 12↔42 | `DB-006` | Field group header, Search field | `PBC` §2.10 | `DB-39` |
| Works without a configured row identifier | `DB-006` | Row detail panel | `PBC` §2.10 | `DB-36` |

## Value rendering

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| `NULL` and empty string are distinguishable | `DB-003`, `DB-006` | Cell, Field row | `TGS` §4 | `DB-40` |
| Numbers, timestamps, booleans follow the family rules | all tables | Cell | `TGS` §4 | `DB-41`, `DB-42` |
| Zero is data, not absence | all tables | Cell | `TGS` §4 | `DB-43`, `AC-10` |
| Long text truncates without changing row height | `DB-003` | Cell | `TGS` §1.6, §4 | `DB-45` |
| Structured values collapse in-cell, expand in the panel | `DB-003`, `DB-006` | Cell, Field row | `TGS` §4 | `DB-46` |

## Export

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Three scopes with their own counts | `DB-009` | Export panel, Radio row | `PBC` §2.13 | `DB-47` |
| The download path is stated before commit | `DB-009` | Path notice | `PBC` §2.13 | `DB-48` |
| Background export is non-blocking and indicated | `DB-007` | Export activity indicator | `PBC` §2.13 | `DB-49` |
| Four export states with distinct action sets | `DB-007` | Status badge, Progress bar | `PBC` §2.13 | `DB-50`, `DB-51`, `DB-52` |
| Background exports surface in Report Explorer | `DB-007`, `REP-001` | Rail | `PBC` §2.13, §3.1 | `DB-53` |
| Export absent when not permitted | `DB-001`, `DB-003` | Permission badge | `PBC` §2.17 | `DB-54` |

## Empty, loading and error

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Table header survives every empty and error state | `DB-010`, `DB-011` | Data table | `TGS` §6 | `DB-55` |
| The empty state names the culprit filter and offers removal | `DB-010` | Empty state | `PBC` §2.14 | `DB-56`, `DB-57` |
| Filtered-empty and truly-empty are different messages | `DB-010` | Empty state | `PBC` §2.14 | `DB-58` |
| Loading preserves layout height | `DB-003` | Skeleton row | `PBC` §2.15 | `DB-59` |
| Errors separate permission from infrastructure and give a reference | `DB-011`, `REP-004` | Error state, Diagnostic trio | `PBC` §2.16, §3.8 | `DB-60`, `RP-20` |
| Permission-denied never renders a blank table | `DB-003` | Empty state | `PBC` §2.17 | `DB-61` |

## Report period and library

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Report Explorer is not a data grid | `REP-001`, `REP-002` | Report instance row | `PBC` §0, §3 | `RP-1` |
| Axis is client → type → period | `REP-001` | Rail, Group heading | `PBC` §3.1 | `RP-2`, `RP-3` |
| The grouping and ordering rule is stated and obeyed | `REP-001`, `REP-002` | Group heading, Result counter | `PBC` §3.1 | `RP-3`, `RP-6` |
| Filters govern what is rendered | `REP-001` | Filter chip | `PBC` §3.4 | `RP-4` |
| Counts are internally consistent | `REP-001`, `REP-002` | Result counter, Rail | `PBC` §3.4 | `RP-5`, `RP-6`, `RP-7` |
| Reporting period is explicit per instance | `REP-001`, `REP-003` | Report instance row, Metadata grid | `PBC` §3.2, §3.6 | `RP-3`, `RP-15` |
| Status governs available actions | `REP-001` | Status badge, Status left-edge | `PBC` §3.3 | `RP-8`, `RP-9` |
| Multiple files are visible and individually actionable | `REP-001`, `REP-003` | Format badge, File entry | `PBC` §3.5 | `RP-10`, `RP-13`, `RP-14`, `RP-21` |
| Preview is a dedicated page, embedded | `REP-003` | Document preview | `PBC` §3.6 | `RP-11`, `RP-12` |
| Report history is in context | `REP-003` | History table | `PBC` §3.6 | `RP-15` |
| Period siblings preserve library filters | `REP-003` | Sibling nav pair | `PBC` §3.7 | `RP-16`, `RP-17` |
| Bridge to source data | `REP-003` → `DB-003` | — | `PBC` §3.6 | `RP-18` |
| Report-folder access is distinct from dataset access | `REP-004` | Empty state | `PBC` §3.8 | `RP-19` |

## Eco Driving month and week selection

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Month is the primary period | `ECO-001` | Stepper | `EDS` level 2 | `EC-1`, `EC-7` |
| Weeks are multi-selectable with visible date ranges | `ECO-001` | Toggle card | `EDS` level 3 | `EC-3` |
| Selected weeks are summed into one ranking | `ECO-001`, `ECO-003` | Basis line, Week-contribution table | `EDS` level 3, §5.5 | `EC-3`, `EC-20` |
| Non-contiguous selection is allowed with a warning | `ECO-001` | Inline warning | `EDS` level 3 | `EC-4` |
| Zero weeks is an empty state | `ECO-001` | Empty state | `EDS` level 3 | `EC-5` |
| Partial weeks are labelled and counted honestly | `ECO-001` | Toggle card, Basis line | `EDS` level 3 | `EC-6` |
| One sentence is the source of truth for the period | `ECO-001`, `ECO-003` | Basis line, Breadcrumb bar | `EDS` level 3 | `EC-2`, `EC-1` |

## Eco Driving ranking

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Position and score equally prominent | `ECO-001` | Metric hero, Score bar | `EDS` level 4 | `EC-8` |
| Fleet distribution provides interpretive context | `ECO-001`, `ECO-003` | Distribution histogram | `EDS` level 4, §5.1 | `EC-9` |
| Rates per 100 km by default, with one global unit toggle | `ECO-001`, `ECO-002` | Unit toggle, Table header cell | `EDS` § Rate vs sum | `EC-10`, `EC-11` |
| One ranking; groups are filters with counts | `ECO-001` | Filter chip | `EDS` level 4 | `EC-12` |
| Same sort/filter vocabulary as Database Explorer | `ECO-001` | Column menu, Filter chip | `EDS` level 4 | `EC-13` |
| Lineage qualifier stated once | `ECO-001` | Technical badge | `EDS` § lineage | `EC-29` |

## Eco Driving drill-down and score components

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Six-section order: score → trend → identity → components → weeks → trips | `ECO-003` | — | `EDS` level 5 | `EC-14` |
| Component metrics show threshold, points, maximum, loss and share | `ECO-003` | Share bar | `EDS` §5.4 | `EC-15`, `EC-16`, `EC-17` |
| Persisted entry identity, exclusive end, opaque ID | `ECO-003` | Metadata grid | `EDS` §5.3 | `EC-18`, `EC-19` |
| Trend renders only real periods | `ECO-003` | Trend bars | `EDS` §5.2 | `EC-31` |
| Week breakdown is diagnostic, not a second ranking | `ECO-003` | — | `EDS` §5.5 | `EC-20` |
| Scoring values come from the repository | `ECO-001`–`ECO-003` | — | `EDS` header, `D-002` | *(none — deliberately)* |
| Removed panels stay removed | `ECO-001`–`ECO-003` | — | `EDS` § removed | `EC-30` |

## Contributing trips

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Trip-level values are sums, and say so | `ECO-003` | Table header cell | `EDS` §5.6 | `EC-21` |
| The full approved trip column set is present | `ECO-003` | Data table | `EDS` §5.6 | `EC-22` |
| Trips are freely sortable and filterable | `ECO-003` | Column menu, Filter chip | `EDS` §5.6 | `EC-23` |
| The default trip filter states both counts and is escapable | `ECO-003` | Result counter | `EDS` §5.6 | `EC-24` |
| Trip → raw row closes the evidence chain | `ECO-003` → `DB-003` | — | `EDS` §5.6, level 6 | `EC-25` |
| Missing trip grant is explained, not hidden | `ECO-003` | Empty state | `EDS` § states | `EC-26` |
| Context switch on a detail page follows `D-009` | `ECO-003` | — | `EDS` § states | `EC-27`, `EC-28` |

## Cross-cutting

| Requirement | Screens | Components | Spec | Criteria |
|---|---|---|---|---|
| Theme follows the OS with a per-account override | `SHL-003` | Theme switcher | `PBC` §1.4 | `SH-7`, `SH-8`, `SH-9`, `SH-10` |
| Full viewport width is used | all | App shell | `PBC` §1.1 | `SH-2` |
| The table is the first element of the working area | `DB-003` | App shell, Data table | `PBC` §2.2 | `DB-1`, `DB-2` |
| Navigation communicates the three-mode product model | `SHL-001` | Nav item | `PBC` §1.2 | `SH-3`, `SH-4`, `SH-5` |
| Return navigation preserves list state | `ECO-003`, `REP-003` | Breadcrumb bar | `PBC` §1.6, `INT` §7 | `SH-13`, `DB-35` |
| Table never becomes cards | `RSP-001`–`RSP-003` | Data table | `RSP` § Table | `RS-3`, `RS-4` |
| Touch targets grow at ≤1024 px | `RSP-001`, `RSP-002` | Button, Icon button | `RSP` § Actions | `RS-2` |
| Below-minimum width is honest and escapable | `RSP-003` | Advisory screen | `RSP` § Below 768 | `RS-7`, `RS-8` |
| No state relies on colour alone | all | all | `A11Y` §2 | `AC-9` |
| Focus is always visible and always returned | all | all | `A11Y` §3, §5 | `AC-1`, `AC-4` |
| Alpha is a state colour, never a primary button | all | Button | `TOK` §1.4, §1.5 | `VF-6`, `VF-7` |
| Every colour resolves to a token | all | all | `TOK` | `VF-8` |
| Polish strings via translation keys | all | all | `COPY_AND_TERMINOLOGY.md` | `AC-13` |
