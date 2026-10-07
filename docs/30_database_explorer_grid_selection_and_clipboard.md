# 30 — Database Explorer: grid selection and clipboard

Durable reference for **page-local rectangular cell selection** and the
**spreadsheet clipboard** it feeds.

This is the seventh implementation slice of the approved redesign (stage `S7`),
built on the hidden row identity in
`docs/29_database_explorer_hidden_row_identity.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screen `DB-003`, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.11,
`INTERACTION_SPEC.md` §2.2/§3/§5/§8, `TABLE_AND_DATA_GRID_SPEC.md` §7,
`RESPONSIVE_SPEC.md` (table), `ACCESSIBILITY_SPEC.md` §4/§9,
`COMPONENT_CATALOG.md` (selection counter), criteria `DB-44` and `AC-2`,
decision `D-012`.

---

## 1. What the stage adds

```
ALREADY RENDERED ROWS  ->  ONE RECTANGLE  ->  TSV ON THE CLIPBOARD
```

Selecting and copying is a **client-side interaction over cells already on the
page**. It adds no endpoint, no query, no aggregate, no schema change and no
persistence. Selecting or copying issues no request of any kind; the module can
disclose nothing the page did not already show, so it introduces no new data
authorization surface.

| Concern | Where |
|---|---|
| interaction, rectangle, clipboard | `api/static/js/data-grid-selection.js` |
| selection visuals, footer, ≤768 px cutoff | `api/static/css/data-grid.css` |
| footer markup, hooks, vocabulary | `api/main.py` (`_portal_database_selection_strings`) |
| Polish strings | `api/portal_ui/i18n.py` (`db.select.*`) |

## 2. The selection model

- **Anchor and active cell.** Pressing a data cell sets both, producing a 1×1
  selection. The anchor never moves while a range is extended; the active edge
  does. Travelling back toward the anchor therefore shrinks the rectangle, and
  crossing it grows the range on the other side.
- **One rectangle, never a multi-range.** Extension is by pointer drag,
  `Shift`+click, or `Shift`+arrow. The approved contract asks for one contiguous
  range and nothing here creates disjoint ranges.
- **Semantic identity.** A cell is `(rendered row position, column name)`. The
  column is *never* a DOM index, because an S5 reorder changes what an index
  means. The header order is re-read on every selection change and every copy,
  which is what keeps a selection attached to the same values across a reorder,
  a pin or a live width change.
- **Keyboard.** `←`/`→` move the active cell within the row, `↑`/`↓` move it
  between rows, `Home`/`End` reach the row edges, and the `Shift` form of each
  extends the range instead. An unshifted move collapses the range onto the new
  active cell. Focus follows the active cell, which carries `tabindex="-1"`.

### 2.1 The page boundary is hard (`D-012` phase 1)

Extension clamps to the first and last **rendered** row and to the first and
last **displayed** column. There is no wrap-around, no automatic page request
and no hidden selection state for rows that are not on the page. Cross-page
selection is explicitly not authorized in phase 1.

### 2.2 Selection is ephemeral

It lives only in the open document. It is not in the URL, not in
`localStorage`, not in `sessionStorage` and not on the server, and there is no
`selection=` parameter. Reloading, sharing a link or navigating to another
result begins unselected — deliberately, because a selection is a transient
gesture and not a bookmarkable view.

Because a filter, a search, a sort and a page change are all real server
navigations here, the *mechanism* for `INTERACTION_SPEC.md` §5 ("selection MUST
NOT survive a filter, sort or page change") is simply that the next document has
none. For the two in-page paths that exist, the rules are explicit:

| Event | Rule |
|---|---|
| `popstate` (Back/Forward, e.g. a density URL) | clear |
| a selected column stops being displayed | clear |
| pure geometry: width drag, autofit, pin/unpin | preserve — identity is unaffected |
| viewport crosses the ≤768 px cutoff | deactivate and clear |

The clear-on-unresolvable rule is what stops a ghost range: if the rectangle no
longer resolves against the current header order, it is dropped rather than
approximated, so a hidden column's values can never be copied.

## 3. Coexistence with the rest of the grid

**Interactive controls keep their own meaning.** A press on a link, button,
input, select, textarea, label, `summary`, column menu, resize handle, column
panel or the row-detail trigger never starts a selection.

**Cell selection and row selection are independent** (`INTERACTION_SPEC.md`
§5): a cell range does not open `DB-006`. While selection is active — that is,
above the responsive cutoff — a press on a *data* cell anchors a range, and the
row drawer opens from its own `Szczegóły` control or from `Enter` on the row.
The `Szczegóły` column carries no `data-db-column`, so it is outside the
selection universe by construction. Below the cutoff, S7 is off and the pre-S7
whole-row click is unchanged.

**`Escape` follows topmost-component semantics** (`INTERACTION_SPEC.md` §3): an
open column menu, the column panel and the row drawer each own it first; only
with none of them on screen does `Escape` clear the selection.

**With the drawer open**, plain `↑`/`↓` stay with its row traversal (`DB-37`)
and only the shifted form extends the range.

**The technical row identity is untouched.** `record_id` is not in the page at
all, and the opaque S6 row reference is row plumbing, not cell data: it is never
selectable, never copied, never in the footer and never in the module's state.

## 4. The clipboard contract

### 4.1 Value source

The payload is built from the **canonical `data-db-copy` value** the S2 cell
contract already publishes — never from rendered text. So a grouped number
copies `1284.40`, a minute-precision timestamp copies its full ISO value, a
mid-elided identifier copies whole, a boolean copies its canonical value rather
than the `TAK`/`NIE` badge, and a collapsed `{ 3 pola }` preview copies the real
JSON. No markup ever reaches the clipboard.

### 4.2 Grammar

```
field    := plain | quoted
quoted   := '"' ( char | '""' )* '"'
row      := field ( TAB field )*
payload  := row ( CRLF row )*
```

A field is quoted when it contains a tab, `CR`, `LF`, a double quote, or leading
or trailing whitespace; internal quotes are doubled. This is the convention
Excel, LibreOffice and Sheets all read back as one cell, so a value carrying a
tab or a line break cannot shift a column or invent a row. **Source values are
never altered to simplify serialization.**

Cell order is the **visible** order, so an S5 reorder is reflected in the
payload. No column names are prepended: the approved contract asks for a
cell-range copy, and column naming is an export concern (`S8`).

### 4.3 NULL versus empty string — an approved copy-layer limitation

`SCREEN_STATE_MATRIX.md` states *copy yields empty* for a SQL NULL **and** for
an empty string. Both therefore serialise to an empty field, and the clipboard
cannot distinguish them. The distinction the product does preserve lives in the
rendering (`brak wartości` vs `pusty tekst`) and in the row-detail panel; S7
changes neither. This is recorded as a limitation rather than silently repaired,
because repairing it would mean inventing a clipboard token the design does not
have.

### 4.4 Write path and feedback

`navigator.clipboard.writeText` first; a rejected, absent or throwing
implementation falls back to the same hidden-textarea `execCommand` path the S2
single-cell copy uses. A failure produces a safe message and **keeps the
selection**, so the user can try again. Feedback goes to a polite live region in
the footer and states the cell count — never any of the copied data.

`Ctrl`/`⌘`+`C` is intercepted **only** when a grid selection exists and focus is
not inside an editable control. A user typing in a filter field keeps ordinary
`Shift`+arrow, `Ctrl+C` and `Escape` behaviour.

## 5. Footer and visual state

The footer **supplements**: the pager, the page-size control and the result
counter are unchanged. It gains the permanent copy hint
`zaznacz zakres i ⌘C, aby skopiować do arkusza`, a selection counter that is
hidden at zero, and one visually-hidden polite live region.

The counter reads `zaznaczono 3 wiersze × 4 kolumny · 12 komórek`. It states the
row count `TABLE_AND_DATA_GRID_SPEC.md` §7 requires and the cell count the
selection is actually made of. Polish plurals are selected by the same 1 / 2–4 /
rest rule the server uses, teens included; the words come from the catalogue and
the module holds only the rule.

Visually the range is `accent/tint` with a 2 px `accent/base` border around the
**rectangle** — composed from per-side CSS variables so no internal rule is
drawn and a pinned column inside the range reads as part of the same block. The
active cell adds a full inset outline, a geometric signal rather than a second
shade, because selection is never carried by colour alone. Hover, sticky and
open-row backgrounds are explicitly overridden so nothing punches a hole in the
rectangle.

## 6. Responsive cutoff

`RESPONSIVE_SPEC.md` disables cell range selection at **768 px and below**,
because a touch drag across cells conflicts with scrolling the table. The module
resolves the cutoff with `matchMedia("(min-width: 769px)")` against the number
the server ships in `data-db-select-min-width` — never a user-agent test — and
the CSS hides the footer selection state at the same width. Below it nothing is
intercepted: no drag, no keyboard interception, no range copy, and ordinary
browser text selection and copy are untouched. Crossing the cutoff with a range
selected deactivates the feature and clears the footer; returning above it
starts unselected.

## 7. What this stage is not

- **Not an export.** `S8` will add `Zaznaczone wiersze` as an export scope. S7
  creates no export request, changes no export payload, touches
  `database_export_jobs` not at all and adds no server endpoint.
- **Not a row-checkbox feature.** No selection column, no select-all, no
  cross-page row selection, no bulk-action toolbar.
- **Not a framework.** One bounded vanilla ES module, page-scoped through
  `PAGE_ASSETS`, with no dependency and no build step.

## 8. Verification

`ops/tests_manual/test_portal_database_grid_selection.py` drives the shipped
module through `ops/tests_manual/data_grid_selection_harness.js`, a browserless
DOM stub shaped like the server's markup. Coverage includes every drag
direction, shrink-back, `Shift`+click, the keyboard bounds, editable-control
non-interception, the drawer and `Escape` precedence, TSV round-trips (including
tabs, `CR`, `LF`, quotes and edge whitespace parsed back into the exact
rectangle), NULL/empty, long and structured values, S5 reorder/pin/width/hide,
the ≤768 px disable and breakpoint crossing, clipboard success and failure, a
200-row page, and the rendered page actually requesting the module.

No real-browser evidence was collected: no browser automation is installed in
this environment and installing it was not authorized.
