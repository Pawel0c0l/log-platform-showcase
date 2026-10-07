# Table and Data Grid Spec

The Database Explorer grid is the central product surface. This document is the detailed contract for it. The Eco Driving ranking table, the Eco Driving trip table and the Artifact table inherit these rules unless stated otherwise.

Mental model: **a professional data grid adapted to the web.** Not a visual copy of Excel — the borrowed properties are a table-first hierarchy, powerful per-column controls, obvious sort/filter state, efficient scanning, sensible column sizing and visibility, compact handling of large datasets, and minimal interruption between the user and the data.

---

## 1. Layout

### 1.1 Vertical composition

| Band | Height | Sticky |
|---|---|---|
| App bar | 56 px | Yes — viewport top |
| Client context bar | 64 px | Yes |
| Toolbar | 48 px | Yes |
| **Table header row** | 36 px | Yes — to the table viewport top |
| Table body | remaining | scrolls |
| Footer | 44 px | Yes — viewport bottom |

**The table body begins 168 px from the viewport top and consumes all remaining height.** Nothing scrolls above the table. This is the defining structural requirement; the as-is product placed the table fifth in a stack of bordered panels, below the fold.

### 1.2 Header treatment

- Background `surface/header-cell`, bottom edge 1 px `border/strong`.
- Label `type/table-header` in `text/secondary`; the sorted column's label steps to `text/primary`.
- A filtered column's header uses `accent/tint` background with `accent/text-on-tint` label and an `≡` marker.
- Sort caret `↓`/`↑` in `accent/on-surface` immediately after the label.
- Menu caret `▾` in `text/muted`, right-aligned within the header cell. Visible at rest.
- Numeric columns right-align both label and caret group; the caret sits to the **left** of the label so it stays adjacent to the values.
- Where a column carries a unit caption (Eco Driving metric columns), the header becomes two-line at 48–52 px: label on the first line, `type/eyebrow` unit on the second.

### 1.3 Sticky columns

- The **checkbox column (30 px)** and the **primary identifier column** are sticky-left in Database Explorer.
- In the Eco Driving ranking, the **position column** is sticky-left.
- Sticky cells carry the row background explicitly (so the scrolling content passes behind them) and a 1 px `border/default` right edge.
- Only these columns are sticky by default. Users may pin additional columns from the column menu; pinned columns accumulate to the left in pin order.
- Total pinned width **MUST NOT** exceed 40 % of the table viewport; further pinning is refused with an inline message.

### 1.4 Horizontal scrolling

- The table scrolls horizontally within its own viewport. The shell never scrolls horizontally.
- A **30 px right-edge fade** (gradient from transparent to the table surface) signals that content continues. This is a required affordance, not decoration: without it a clipped table reads as a broken artboard.
- Horizontal scroll position persists across sort and filter changes, and across row selection.

### 1.5 Vertical scrolling

- The body scrolls; the header stays. Native scrollbars.
- Scroll position resets to the top on any query change (filter, sort, page, page size, dataset). It persists on row selection, panel open/close, theme change and density change.

### 1.6 Density and visible rows

| Density | Row height | Rows visible at 1080 px viewport |
|---|---|---|
| `Zwarta` (default) | 32 px | ~28 |
| `Wygodna` | 40 px | ~22 |

Row height is uniform within a page. A row **MUST NOT** grow to fit content — long values truncate instead. This is what makes vertical scanning possible.

### 1.7 Column widths

- Widths are explicit per column, derived from the column's data family (see §5) and then user-adjustable.
- **Minimum** 64 px — below that a header label cannot be read.
- **Maximum** 480 px for text columns; identifiers and addresses truncate rather than exceed it.
- Numeric columns size to their widest expected value plus padding, never wider.
- Resize by dragging the header's right edge. A live 1 px `accent/base` guide follows the pointer. Release commits.
- Double-click the header edge, or `Dopasuj szerokość do treści` in the column menu, autofits to the widest value on the **current page** (not the whole dataset — that would require a full scan).
- `table-layout: fixed` semantics: the sum of explicit widths defines the table width. **The declared table width MUST equal the sum of its column widths**; a mismatch silently drops the last column out of view.

---

## 2. Sorting

| State | Header rendering |
|---|---|
| Unsorted | Label `text/secondary`; menu caret `text/muted`; no direction caret |
| Ascending | Label `text/primary`; `↑` in `accent/on-surface` |
| Descending | Label `text/primary`; `↓` in `accent/on-surface` |

- **One sort column at a time.** Multi-sort is not approved and **MUST NOT** be implemented.
- Sort is invoked only from the column menu (`Sortuj rosnąco` / `Sortuj malejąco`, or `Sortuj A → Z` / `Z → A` for text).
- Default sort per dataset is its configured date column, descending. Where no date column is configured — the current production state — the default is the dataset's primary approved column, ascending, and the toolbar states it.
- The active sort is also stated in words in the toolbar: `sortowanie Start (UTC) ↓`. Icon state alone is insufficient (see `ACCESSIBILITY_SPEC.md`).
- Applying a sort resets to page 1 and preserves filters, columns, density and selection-clearing rules.
- `NULL` ordering: nulls sort **last** in both directions, and the column menu states this.

---

## 3. Filtering

### 3.1 Model

- One filter per column. A second filter on the same column **replaces** the first.
- Filters combine with `AND`. `OR` across columns is not approved.
- The global text search is an additional `AND` predicate across approved text columns.
- Every active filter is represented **exactly once** in the UI, as a chip in the toolbar strip or an entry in the filter panel, carrying column name, operator and value.

### 3.2 Text columns

| Operator | Label | Semantics |
|---|---|---|
| contains | `zawiera` | Case-insensitive substring. **Default.** |
| equals | `=` | Exact, case-insensitive |
| not equals | `≠` | Exact negation; excludes `NULL` |
| blank | `puste` | `NULL` **or** empty string — the menu states that it covers both |

Plus a **distinct-value picker**: up to 50 most frequent values with occurrence counts and proportional bars, multi-selectable. Selecting values produces an `in (…)` filter rendered as a chip with the count (`Kierowca in (3)`). The picker is search-filterable.

### 3.3 Numeric columns

| Operator | Label |
|---|---|
| equals | `=` |
| not equals | `≠` |
| greater than | `>` |
| greater or equal | `≥` |
| less than | `<` |
| less or equal | `≤` |
| between | `od–do` |
| blank | `puste` |

Plus a **value distribution histogram** (14 buckets) with min and max labelled, and the current threshold marked in `accent/on-surface`. The threshold is visible **before** the filter is applied — this is the point of the histogram.

`od–do` is inclusive at both ends and either bound may be left blank for an open-ended range.

### 3.4 Date / time columns

| Operator | Label | Semantics |
|---|---|---|
| before | `przed` | Strictly earlier |
| after | `po` | Strictly later |
| between | `między` | Inclusive both ends; either bound may be blank |
| blank | `puste` | `NULL` |

- Input is a date-time field with a calendar picker. The picker opens on field focus, closes on selection or `Esc`, and never blocks typing a value directly.
- For the dataset's configured default date column, the filter panel additionally offers presets: `Dziś`, `Wczoraj`, `7 dni`, `30 dni`, a named current month, `Poprzedni miesiąc`. The selected preset is the highlighted chip.
- Where no default date column is configured — the current production state — presets are **absent** and the per-column date filter is the only path. The design must not assume the configuration exists.
- Timezone: values are stored and filtered in UTC. Columns whose name declares a local variant are filtered in that local zone. The column menu states which zone applies.

### 3.5 Boolean columns

| Option | Label |
|---|---|
| no filter | `wszystko` |
| true | `tak` |
| false | `nie` |
| null | `puste` |

Rendered as a four-way segmented control, not a dropdown.

### 3.6 Filter visibility and reset

- The `Filtry` toolbar button carries a count badge in `accent/base`.
- Active filters appear as chips in a horizontal strip (docked-panel-collapsed mode, and `RSP-001`) or as entries in the filter panel's `Aktywne — n` section.
- Each chip has a `×` that removes that filter **immediately**.
- `Wyczyść wszystkie` removes all column filters and the global search, immediately.
- Removing the last filter returns the unfiltered result; the counter drops the "filtered" clause.

---

## 4. Cell rendering

| Data family | Alignment | Family | Rules |
|---|---|---|---|
| **Identifier / UUID** | left | mono | Truncate mid-value with `…` preserving the leading 8 and trailing 4 characters, e.g. `7f03c4e1…0021`. Full value in `DB-006` and on copy. |
| **Short string** | left | sans | As-is; ellipsis at the cell edge. |
| **Long string** | left | sans | Single line, ellipsis at the cell edge. Never wraps, never grows the row. Full value in `DB-006`. |
| **Integer** | right | mono | Thousands grouped with a **non-breaking space** (`48 213`). No trailing decimals. |
| **Decimal** | right | mono | Comma decimal separator (Polish locale), fixed to the column's declared precision (`28,40`). Column header carries the unit. |
| **Percentage** | right | mono | Value + `%` with a non-breaking space (`11,2 %`). |
| **Date** | left | mono | `YYYY-MM-DD`. |
| **Timestamp** | left | mono | `YYYY-MM-DD HH:MM` in the table; full `YYYY-MM-DD HH:MM:SS±HH:MM` in `DB-006` and on copy. Never a relative "3 days ago" in a data cell. |
| **Boolean** | centre | sans | Badge with a **word**: `TAK` in `state/positive`, `NIE` in `state/warning`. Colour is never the only signal. |
| **Enum / status** | left | sans | Badge with the value's label and its semantic colour. |
| **`NULL`** | as column | sans | `brak wartości`, italic, `text/faint`. |
| **Empty string** | as column | sans | `pusty tekst`, italic, `text/faint`. |
| **Zero** | right | mono | `0` in `text/muted` — dimmer than a non-zero value but **not** `text/faint`; zero is data, not absence. |
| **JSON / structured** | left | mono | Collapsed single-line preview `{ 4 pola }` / `[ 12 elementów ]`; the full pretty-printed value renders in `DB-006`. Never raw JSON in a cell. |
| **Out-of-range numeric** | right | mono | Value in `state/negative` with weight 600 where the column declares a threshold (e.g. `Vmax ≥ 110`). The threshold is a display rule, not a filter. |

> **`NULL` vs empty string is a hard requirement.** The as-is product renders both through a single `str()` path so they are indistinguishable, which makes data verification unreliable. Two distinct markers are mandatory.

---

## 5. Column controls

### 5.1 Column menu (`DB-005`)

Opened from the header. Fixed 298 px width. Sections, top to bottom:

1. **Identity** — column label, physical column name, data type, non-null count for the current result set.
2. **Sort** — two rows, current direction highlighted.
3. **Filter** — operator row (segmented, type-appropriate) plus the value control.
4. **Distribution** — distinct-value picker (text) or histogram (numeric); absent for boolean.
5. **Column actions** — `Przypnij kolumnę po lewej`, `Ukryj kolumnę`, `Dopasuj szerokość do treści`.
6. **Commit** — `Zastosuj`, `Wyczyść`, plus the keyboard hint `↵ zastosuj · esc`.

Behaviour: `Enter` applies, `Esc` discards, outside click discards, scrolling the table closes and discards. Applying closes the menu.

### 5.2 Column visibility panel (`DB-008`)

- 400 px. Header states `n z m` visible.
- Search by column name; tabs `Wszystkie` / `Widoczne` / `Ukryte`; `Zestawy ▾` for named sets.
- Each row: drag handle `⠿`, checkbox, label, type badge, pin indicator.
- Hidden columns render their label in `text/faint` with an unchecked box.
- `Zastosuj` commits visibility and order together. `Zapisz jako zestaw` prompts for a name.
- **At least one column MUST remain visible**; unchecking all blocks `Zastosuj` with an inline message.
- Reordering is drag-based with a live insertion indicator; pinned columns cannot be dragged below unpinned ones.

### 5.3 Reset

- `Zestawy ▾` includes a `Domyślne kolumny` entry restoring the dataset's default visible set, order and widths.
- Resetting columns does **not** clear filters or sort.

### 5.4 Persistence (`D-007`)

| State | Lives in |
|---|---|
| Filters, sort, page, page size, visible columns, order, widths, open row | **URL** |
| Named saved views | **Server**, per account |
| Named column sets | **Server**, per account per dataset |
| Density | **Browser** |
| Theme override | **Server**, per account (`D-011`) |

Nothing else persists between visits. Opening a dataset fresh yields its defaults.

---

## 6. Table states

| State | Rendering |
|---|---|
| **Row hover** | Row background → `surface/subtle`. Sticky cells adopt the same tint so the row reads as one unit. |
| **Row selected** | `accent/tint` background, 2 px `accent/base` inset on the pinned identifier cell, identifier in `text/primary` weight 600, filled checkbox. |
| **Cell selected** | 2 px `accent/base` outline inset within the cell. |
| **Range selected** | Range background `accent/tint`; outer 2 px `accent/base` border around the rectangle; footer states the row count. |
| **Filtered column** | Header `accent/tint` + `≡`; body cells `accent/tint-cell`. |
| **Sorted column** | Label `text/primary` + direction caret in `accent/on-surface`. |
| **Loading** | Header rendered; body shows skeleton rows at the current density and page size, each a `surface/inset` block per column at that column's width. No shimmer. Counters show `…`. Table height unchanged. |
| **Empty (no matches)** | Header rendered; centred message naming the culprit filter with the count without it; two actions. |
| **Empty (dataset empty)** | Header rendered; message stating the dataset has no rows and when it last loaded. |
| **Error** | Header rendered; failure-class badge; permission-OK statement; timestamp + reference + code; three actions. |

In **every** empty and error state the **table header remains rendered**. The user must keep the shape of the data even when there are no rows — this is what distinguishes "nothing matched" from "something is broken".
