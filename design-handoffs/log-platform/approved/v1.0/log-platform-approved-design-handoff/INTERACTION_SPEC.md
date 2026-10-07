# Interaction Spec

Interaction semantics for every approved control. No implementation libraries are prescribed.

---

## 1. Universal control states

Every interactive element **MUST** define all applicable states. Values reference `DESIGN_TOKENS.md`.

| State | Rule |
|---|---|
| **Rest** | As drawn in the design files. |
| **Hover** | Background steps one surface level toward the accent-neutral: rows → `surface/subtle`; bordered buttons → `surface/subtle`; primary buttons → 8 % lighter (light theme) / 8 % darker (dark theme). Never a size or position change. |
| **Focus** | A **2 px outline in `accent/base` with a 2 px offset**, always visible, never removed. Focus **MUST NOT** rely on the hover treatment. Focus-visible semantics: keyboard focus shows the ring; pointer focus on a button need not. |
| **Active / pressed** | Background steps one further level; no transform. |
| **Selected** | Row: `accent/tint` background + 2 px accent inset on the pinned cell + identifier in `text/primary` weight 600. Toggle/segment: `action/primary-bg` fill with inverted text. Selection is **never** communicated by colour alone. |
| **Disabled** | Reserved for genuinely temporary unavailability (e.g. `Zastosuj` with no staged change). Permanently unavailable actions are **absent**, not disabled. Disabled = 40 % opacity + `cursor: default` + `aria-disabled`. |
| **Loading** | The control keeps its exact dimensions, its label is replaced by a progress affordance, and it becomes non-interactive. A loading control **MUST NOT** change width. |

## 2. Keyboard

### 2.1 Global

| Key | Behaviour |
|---|---|
| `Tab` / `Shift+Tab` | Move through: app bar → context bar → toolbar → table → footer → open panel. Panels are appended to the end of the order while open. |
| `⌘K` / `Ctrl+K` | Focus global search from anywhere. |
| `Esc` | Close the topmost transient layer only (see §4). |
| `Enter` | Activate the focused control; in a filter value field, commit the filter. |
| `Space` | Toggle the focused checkbox/segment. |

### 2.2 Table

| Key | Behaviour |
|---|---|
| `↑` / `↓` | Move row selection. With `DB-006` open, the panel content follows without closing. |
| `←` / `→` | Move cell selection within the row; scrolls the viewport to keep the cell visible. |
| `Shift + ↑↓←→` | Extend the cell range selection. |
| `Home` / `End` | First / last cell in the row. |
| `PageUp` / `PageDown` | Scroll one table viewport; does **not** change page. |
| `⌘C` / `Ctrl+C` | Copy the current cell or range as TSV. |
| `Enter` on a row | Open `DB-006`. |
| `Enter` / `Space` on a header | Open that column's menu. |

Column headers are focusable. The sort caret is **not** a separate tab stop — the header is one control that opens the menu.

### 2.3 Focus management for layers

- Opening a **menu** (`DB-005`) moves focus into the menu's first control. Focus is trapped while open. `Esc` returns focus to the header that opened it.
- Opening a **panel** (`DB-006`, `DB-008`, `DB-009`, filter panel) moves focus to the panel heading. Focus is **not** trapped for the pinnable filter panel (it is part of the page); it **is** trapped for the modal-like export panel and the mobile filter drawer.
- Closing any layer returns focus to the element that opened it. This is mandatory, not optional.

## 3. Dismissal semantics

| Layer | `Esc` | Outside click | Scroll of the underlying table | Route change |
|---|---|---|---|---|
| Column menu (`DB-005`) | Close, **discard** pending edits | Close, discard | Close, discard | Close |
| Row detail panel (`DB-006`) | Close | **No effect** — clicking another row moves the selection instead | No effect — panel follows the table | Close |
| Column panel (`DB-008`) | Close, discard | Close, discard | No effect | Close |
| Export panel (`DB-009`) | Close, discard | **No effect** — must use `Anuluj` | No effect | Close |
| Filter panel (docked) | Collapse | No effect | No effect | Persists across route changes within the module |
| Filter drawer (`RSP-002`) | Close, **keep** staged edits | Close, keep staged edits | n/a | Close |

Rule of thumb: **an outside click may dismiss a menu, but never a panel that holds staged work.**

## 4. Filter and sort application (`D-005`)

| Action | Applies |
|---|---|
| Sort from column menu | Immediately |
| Filter confirmed in column menu (`Enter` or its `Zastosuj`) | Immediately |
| Filter edited in the filter panel | On panel `Zastosuj` |
| Removing one filter (`×` on a chip or a panel entry) | Immediately |
| `Wyczyść wszystkie` | Immediately |
| Column visibility / order / width changes | On `DB-008` `Zastosuj` (widths: immediately on drag release) |
| Density change | Immediately |
| Page size change | Immediately, returning to page 1 |
| Global text search | On `Enter`, or after a 400 ms idle debounce |

Every application resets to **page 1** and preserves scroll position at the top of the table body.

## 5. Selection

- **Row selection** — single click selects one row and opens `DB-006`. The header checkbox selects all rows on the current page. `Shift`+click selects a contiguous run. `⌘`/`Ctrl`+click toggles individual rows.
- **Cell selection** — click a cell to anchor; drag or `Shift`+click to extend a rectangle. Cell selection and row selection are independent; a cell range does not open `DB-006`.
- Selection **MUST NOT** survive a filter, sort or page change. The footer selection count clears with it.
- Selection count is stated in the footer and drives the `Zaznaczone wiersze` export scope.

## 6. Context switching

| Switch | Behaviour |
|---|---|
| **Client** (context bar) | Stays in the current module. Clears dataset/report-type/driver selection that cannot survive. Clears filters that reference client-specific values. URL updates. |
| **Dataset** (rail or catalogue) | Clears all filters, sort, columns and selection — a different dataset has a different column set. Density and theme persist. |
| **Eco Driving month** (stepper) | Recomputes the ranking. Week selection resets to `Cały miesiąc` because week identity is month-relative. On `ECO-003`, applies `D-009`. |
| **Eco Driving weeks** (toggle cards) | Recomputes on each toggle. Zero weeks is an empty state. Non-contiguous selection warns inline without blocking. On `ECO-003`, applies `D-009`. |
| **Report type** (rail) | Filters the library to that type; the chip appears in the toolbar; period grouping is preserved. |
| **Theme** | No reload, no state loss, no scroll change. |

## 7. Browser Back

All view state lives in the URL. Therefore:

- `Back` from `DB-006` open → panel closes, table state intact.
- `Back` after applying a filter → the previous filter set, same page and sort.
- `Back` from `ECO-003` → `ECO-001` with the same client, month, weeks, sort, filters, page and scroll position.
- `Back` from `REP-003` → `REP-001` with the same filters and group scroll position.
- `Back` **MUST NOT** exit the module or land on a generic home page.
- `Forward` is symmetric.

Breadcrumb links and `‹ Wróć do…` affordances resolve to the same restored state as `Back` (`D-007`). Both surfaces confirm this in words: `filtry biblioteki zachowane`, `kontekst okresu zachowany`.

## 8. Row and cell click summary

| Target | Single click | Double click | Right click |
|---|---|---|---|
| Row (non-cell area, e.g. checkbox column) | Toggle row selection | — | — |
| Cell | Select cell, select row, open `DB-006` | Select the cell's full text for copy | Not used (no custom context menu) |
| Column header | Open column menu | — | — |
| Column header edge | — | Autofit width | — |
| Filter chip body | Open that column's menu with the filter loaded | — | — |
| Filter chip `×` | Remove the filter immediately | — | — |
| Report instance row | Nothing — actions are explicit buttons | — | — |
| Ranking row | Nothing — `Szczegóły` is explicit | — | — |
| Trip row identifier | Navigate to the same row in `DB-003` | — | — |

Note the deliberate asymmetry: **Database Explorer rows are clickable because inspecting a row is the job. Report and ranking rows are not, because their rows carry several distinct actions and an ambiguous whole-row target would be a guess.**

## 9. Transitions and animation

The approved design is deliberately close to static. Only four motions exist.

| Motion | Purpose | Duration | Easing | Reduced-motion |
|---|---|---|---|---|
| Panel open/close (`DB-006`, `DB-008`, `DB-009`) | Signal that the panel is a layer over the table, not a new page | 160 ms | `ease-out` on open, `ease-in` on close | Omit — appear/disappear instantly |
| Mobile filter drawer (`RSP-002`) | Same, plus scrim fade | 200 ms | `ease-out` | Omit; scrim still appears |
| Menu open (`DB-005`) | 4 px rise + opacity | 120 ms | `ease-out` | Omit |
| Export progress bar (`DB-007`) | Show that work is advancing | continuous | linear | Keep — it conveys information, not decoration. Replace with a discrete percentage if animation is fully suppressed. |

There are **no** page transitions, no row-enter animations, no skeleton shimmer, and no hover animations. Skeletons are static blocks: at these table sizes a shimmer across 100 rows is visual noise.

`prefers-reduced-motion: reduce` **MUST** suppress every motion in the table above except the export progress indicator.

## 10. Hover affordance discipline

- Column sort/filter carets are visible at rest in `text/muted`, and step to `text/secondary` on header hover. They **MUST NOT** be hover-only — a control the user cannot see does not exist.
- Row hover tints the row one surface level. Hover **MUST NOT** reveal action buttons that were previously invisible.
- Per-field copy icons in `DB-006` are visible at rest in `text/faint` and step to `text/muted` on row hover.
