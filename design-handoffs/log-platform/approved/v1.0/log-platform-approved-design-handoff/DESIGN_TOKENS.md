# Design Tokens

The approved visual system, implementation-independent. Light and dark are **the same layout with a different colour layer**; only colour tokens differ between themes.

Naming convention: `category/role`. Every light value has exactly one dark counterpart.

---

## 1. Colour

### 1.1 Surfaces

| Token | Light | Dark | Applied to |
|---|---|---|---|
| `surface/page` | `#f4f6f7` | `#0f1215` | Working-area background behind panels |
| `surface/raised` | `#ffffff` | `#16191e` | Tables, panels, cards, lists |
| `surface/subtle` | `#fafbfb` | `#1b1f25` | Toolbars, footers, panel headers, hover |
| `surface/header-cell` | `#f7f8f9` | `#1b1f25` | Table header cells |
| `surface/appbar` | `#14171c` | `#0a0c0f` | Top app bar (dark in both themes) |
| `surface/field` | `#fbfcfc` | `#0f1215` | Input and select interiors |
| `surface/inset` | `#eef0f3` | `#242a32` | Progress tracks, bar backgrounds, chip `×` buttons |
| `surface/preview-desk` | `#eef0f3` | `#242a32` | Document-preview backdrop (`REP-003`) |

### 1.2 Borders

| Token | Light | Dark | Applied to |
|---|---|---|---|
| `border/default` | `#e2e5e9` | `#262b33` | Panel and section edges |
| `border/strong` | `#d7dbe0` | `#313841` | Control borders, table header bottom edge |
| `border/row` | `#f0f2f4` | `#1f242b` | Row separators |
| `border/faint` | `#f4f6f7` | `#1f242b` | Field-row separators inside panels |
| `border/checkbox` | `#b9c0c9` | `#3e4650` | Unchecked checkbox outline |

### 1.3 Text

| Token | Light | Dark | Contrast on `surface/raised` | Applied to |
|---|---|---|---|---|
| `text/primary` | `#14171c` | `#eef1f4` | 15.8 : 1 / 14.1 : 1 | Values, headings, active labels |
| `text/secondary` | `#33393f` | `#c5ccd4` | 10.4 : 1 / 9.6 : 1 | Column headers, secondary labels |
| `text/muted` | `#5c646f` | `#99a1ab` | 6.0 : 1 / 6.1 : 1 | Metadata, descriptions, counters, carets |
| `text/faint` | `#8b939e` | `#737c87` | 3.1 : 1 / 3.4 : 1 | Section eyebrows, placeholders, `NULL` markers |
| `text/on-appbar` | `#f4f6f8` | `#eef1f4` | — | Active nav, brand |
| `text/on-appbar-idle` | `#b9c0c9` | `#99a1ab` | — | Inactive nav (data modes) |
| `text/on-appbar-quiet` | `#8b939e` | `#737c87` | — | Inactive nav (tools group) |

> `text/faint` is below 4.5 : 1 and is therefore restricted to **non-essential** text: uppercase section eyebrows, input placeholders, and the italic `brak wartości` / `pusty tekst` markers which are always accompanied by the field label. It **MUST NOT** be used for data values, counts, or any label a user must read to act.

### 1.4 Accent

| Token | Light | Dark | Applied to |
|---|---|---|---|
| `accent/base` | `#ff7a18` | `#ff8a33` | Active-nav underline, context rule, filter-count badge, selected-row inset, focus ring |
| `accent/on-surface` | `#a85a0d` | `#ffab63` | Accent **text and icons** on a panel surface: `Wyczyść wszystkie`, `Zestawy ▾`, sort carets, links, histogram threshold |
| `accent/tint` | `#fdf3ea` | `#2a1d10` | Filtered-column header, selected-row background, active period card |
| `accent/tint-cell` | `#fefaf6` | `#1a1712` | Filtered-column body cells |
| `accent/text-on-tint` | `#8a4409` | `#ffbe86` | Text on `accent/tint` |

> **Alpha is a state colour, never a container fill and never the primary button.** `accent/on-surface` was corrected from `#c26a15` to `#a85a0d` to reach 5.0 : 1 on white; the earlier value failed at 12 px.

### 1.5 Action

| Token | Light | Dark | Applied to |
|---|---|---|---|
| `action/primary-bg` | `#14171c` | `#eef1f4` | Primary button fill — the strongest neutral, not alpha |
| `action/primary-fg` | `#ffffff` | `#0f1215` | Primary button label |
| `action/secondary-bg` | `#ffffff` | `#16191e` | Secondary button fill |
| `action/secondary-fg` | `#33393f` | `#c5ccd4` | Secondary button label |
| `action/segment-on` | `#eef0f3` | `#2a3038` | Selected segment in a segmented control |
| `action/accent-fg-on-accent` | `#1a0c02` | `#1a0c02` | Text on an `accent/base` fill (count badges) |

### 1.6 Semantic state

| Token | Light fg / bg | Dark fg / bg | Applied to |
|---|---|---|---|
| `state/positive` | `#1f6b45` / `#f0f8f3` | `#6fd3a0` / `#14241c` | `TAK`, `Gotowy`, granted permission, improving Δ |
| `state/positive-border` | `#c6e3d3` | `#22432f` | Positive badge outline where used |
| `state/warning` | `#8a5b09` / `#fdf6e6` | `#e8c37a` / `#2a2211` | `NIE`, `W generowaniu`, `W toku`, partial data, mid-range loss |
| `state/warning-border` | `#f0dda6` | `#3d3315` | Warning badge outline |
| `state/warning-edge` | `#c9922a` | `#c9922a` | Left accent edge of a warning-status row |
| `state/negative` | `#9e2c26` / `#fdeceb` | `#ff9b96` / `#2a1615` | Errors, out-of-range values, failed generation, worsening Δ |
| `state/negative-border` | `#f3c9c6` | `#4a2422` | Negative badge outline |
| `state/neutral` | `#5c646f` / `#f7f8f9` | `#99a1ab` / `#1b1f25` | `Pliki wygasły`, disabled permission, `USUNIĘTY` |

### 1.7 Data visualization

| Token | Light | Dark | Applied to |
|---|---|---|---|
| `viz/bar-neutral` | `#d7dbe0` | `#2f353d` | Histogram bars outside the highlighted range; trend bars for past periods |
| `viz/bar-accent` | `#a85a0d` | `#ff8a33` | Highlighted histogram range, current trend period, progress fill |
| `viz/bar-track` | `#eef0f3` | `#242a32` | Score-bar and progress-bar track |
| `viz/score-good` | `#1f6b45` | `#6fd3a0` | Score bar ≥ good band |
| `viz/score-mid` | `#8a5b09` | `#e8c37a` | Score bar in the middle band |
| `viz/score-low` | `#9e2c26` | `#ff9b96` | Score bar in the lowest band |
| `viz/loss-high` | `#9e2c26` | `#ff9b96` | Component-loss bar, high share |
| `viz/loss-mid` | `#c9922a` | `#c9922a` | Component-loss bar, some loss |
| `viz/loss-none` | `#cfd4da` | `#3e4650` | Component-loss bar, no loss |

Data-visualization colour **MUST** always be accompanied by a number. No chart in this design conveys a value by colour alone.

## 2. Typography

| Token | Value |
|---|---|
| `font/sans` | `"IBM Plex Sans"` |
| `font/sans-fallback` | `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif` |
| `font/mono` | `"IBM Plex Mono"` |
| `font/mono-fallback` | `ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace` |
| `font/weight-regular` | 400 |
| `font/weight-medium` | 500 |
| `font/weight-semibold` | 600 |

Weights above 600 are **not** used. The design distinguishes hierarchy by size and colour, not by heavy weight.

### 2.1 Type scale

| Token | Size / line-height / weight / tracking | Family | Applied to |
|---|---|---|---|
| `type/display` | 40 / 1.1 / 600 / −0.02em | sans | Document titles in the design files only |
| `type/page-title` | 30 / 1.15 / 600 / −0.02em | sans | Catalogue page titles (`Zbiory danych klientów`) |
| `type/page-title-sm` | 28 / 1.15 / 600 / −0.02em | sans | Same at ≤1440 px |
| `type/detail-title` | 26 / 1.2 / 600 / −0.02em | sans | Driver name, report title |
| `type/metric-hero` | 52 / 1.0 / 600 / 0 | mono | Score and position in `ECO-003` |
| `type/client-name` | 17 / 1.3 / 600 / −0.01em | sans | Client name in the context bar (all modules) |
| `type/module-name` | 15 / 1.3 / 500 / 0 | sans | Module/dataset name beside the client |
| `type/section-title` | 14 / 1.35 / 600 / 0 | sans | Panel and card headings |
| `type/nav` | 13 / 1.3 / 400 or 600 / 0 | sans | Primary navigation |
| `type/body` | 13 / 1.55 / 400 / 0 | sans | Descriptions, prose |
| `type/control` | 12–13 / 1.3 / 500–600 / 0 | sans | Button and control labels |
| `type/table-header` | 12 / 1.15 / 600 / 0 | sans | Column header labels |
| `type/table-cell` | 12 / 1.3 / 400 / 0 | sans or mono | Cell values — mono for identifiers, numbers, timestamps |
| `type/table-metric` | 17 / 1.2 / 600 / 0 | mono | Score value in the ranking |
| `type/table-position` | 16 / 1.2 / 600 / 0 | mono | Ranking position |
| `type/meta` | 11 / 1.45 / 400 / 0 | mono | Metadata, counters, timestamps, references |
| `type/eyebrow` | 9–10 / 1.2 / 400 / 0.10–0.14em, uppercase | mono | Section eyebrows, column unit captions |
| `type/badge` | 11 / 1.2 / 600 / 0 | sans | Status badges with words |
| `type/badge-mono` | 9–10 / 1.2 / 400–600 / 0.04–0.06em, uppercase | mono | Technical badges (`TYLKO ODCZYT`, `BŁĄD ŹRÓDŁA DANYCH`) |

### 2.2 Mono usage rule

`font/mono` is mandatory for: identifiers and UUIDs, all numbers in table cells, timestamps and dates, hashes, physical table and column names, error references, counters, page indicators, and uppercase eyebrow labels. It is **forbidden** for prose, button labels, and human names.

Rationale: monospace makes column-wise digit comparison possible, which is the core scanning task.

## 3. Spacing

A 2 px-based scale. Use these steps only.

| Token | Value | Typical use |
|---|---|---|
| `space/0` | 0 | — |
| `space/1` | 2 px | Icon-to-caret gaps, segmented-control padding |
| `space/2` | 4 px | Tight inline gaps |
| `space/3` | 6 px | Badge and chip internal gaps |
| `space/4` | 8 px | Standard control-to-control gap |
| `space/5` | 10 px | Toolbar item gaps |
| `space/6` | 12 px | Table cell horizontal padding |
| `space/7` | 14 px | Panel internal gaps, card gaps |
| `space/8` | 16 px | Section gaps |
| `space/9` | 18 px | Panel padding, frame gaps |
| `space/10` | 20 px | Card padding |
| `space/11` | 22 px | Large card padding |
| `space/12` | 24 px | Content block gaps |
| `space/13` | 28 px | Working-area horizontal padding |
| `space/14` | 32 px | Wide metadata grid gaps |
| `space/15` | 44 px | — |
| `space/16` | 56 px | Document-level section gaps |

Container padding conventions: working area `28 px` horizontal at ≥1440 px, `20 px` at 1280 px, `16 px` at ≤1024 px, `14 px` at 768 px.

Sibling groups **MUST** be laid out with flex/grid `gap`, never with per-element margins or whitespace text nodes.

## 4. Radius

| Token | Value | Applied to |
|---|---|---|
| `radius/xs` | 1 px | Accent rules, progress bars, score bars, histogram bars |
| `radius/sm` | 2 px | Badges, chips, checkboxes, segments, pagination cells |
| `radius/md` | 3 px | Buttons, inputs, selects, list rows, nav items |
| `radius/lg` | 4 px | Panels, cards, table containers |
| `radius/pill` | 999 px | Status dots only |

Nothing in the approved design exceeds 4 px except status dots. The as-is 10–18 px radii are deliberately removed — they read as consumer-app softness at operational density.

## 5. Elevation

| Token | Value | Applied to |
|---|---|---|
| `elevation/none` | none | Tables, cards, panels docked in the layout — **the default** |
| `elevation/menu` | light `0 16px 40px rgba(20,23,28,0.16)` · dark `0 16px 40px rgba(0,0,0,0.55)` | Column menus, dropdowns |
| `elevation/drawer` | light `-8px 0 24px rgba(20,23,28,0.06)` · dark `-8px 0 24px rgba(0,0,0,0.35)` | Overlay drawer (`RSP-002`) |
| `elevation/scrim` | `rgba(20,23,28,0.42)` | Behind the overlay drawer only |

Shadows exist **only** to signal that something floats above the page. Docked panels use a border, never a shadow. The as-is `0 20px 60px` panel shadows are removed.

## 6. Component heights

| Token | Value | Applied to |
|---|---|---|
| `height/appbar` | 56 px | App bar |
| `height/context-bar` | 64 px | Client context bar |
| `height/breadcrumb-bar` | 56 px | Detail-page breadcrumb bar |
| `height/toolbar` | 48 px | Table toolbar, panel headers |
| `height/panel-header` | 42–46 px | Card and panel headers |
| `height/footer` | 44 px | Table footer |
| `height/control-lg` | 36 px | Context-bar selectors |
| `height/control` | 34 px | Context-bar buttons |
| `height/control-sm` | 32 px | Toolbar buttons, inputs, selects, primary/secondary buttons in panels |
| `height/control-xs` | 30 px | Compact in-panel buttons, page-nav buttons |
| `height/control-touch` | 44 px | **All** interactive controls at ≤1024 px |
| `height/segment` | 24–26 px | Segment inside a 30–32 px segmented control |
| `height/row-compact` | 32 px | Table row, `Zwarta` — the default |
| `height/row-comfortable` | 40 px | Table row, `Wygodna` |
| `height/row-ranking` | 38 px | Eco Driving ranking row |
| `height/row-header` | 36 px | Table header row |
| `height/row-header-2line` | 48–52 px | Header row with a unit caption |
| `height/badge` | 18–22 px | Status badges |
| `height/chip` | 24–32 px | Filter chips |

## 7. Fixed widths

| Token | Value | Applied to |
|---|---|---|
| `width/rail` | 288 px | Dataset rail, report-type rail |
| `width/rail-compact` | 252 px | Artifact-kind rail |
| `width/filter-panel` | 352 px | Docked filter panel |
| `width/column-panel` | 400 px | Column visibility, export panel |
| `width/row-panel` | 520 px | Row detail panel (`DB-006`) |
| `width/detail-side` | 480 px | Report detail right column |
| `width/drawer` | 400 px | Overlay filter drawer (`RSP-002`) |
| `width/search-global` | 300–340 px | App-bar search |
| `width/search-local` | 260–300 px | Toolbar search |

## 8. Breakpoints

| Token | Value | Meaning |
|---|---|---|
| `bp/wide` | ≥1680 px | Comfortable desktop; design baseline is 1920 px |
| `bp/desktop` | 1440–1679 px | Standard desktop |
| `bp/compact` | 1280–1439 px | Compact desktop; smallest full-fidelity layout |
| `bp/tablet-landscape` | 1024–1279 px | Nav collapses; touch targets grow |
| `bp/tablet-portrait` | 768–1023 px | Filter panel becomes a drawer |
| `bp/below-min` | <768 px | Database Explorer advisory (`RSP-003`) |
