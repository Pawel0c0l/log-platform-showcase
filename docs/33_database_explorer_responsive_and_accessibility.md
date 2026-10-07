# 33 — Database Explorer: responsive bands and accessibility completion

Durable reference for the approved **responsive contract** (`RSP-001`–`RSP-003`)
and the **accessibility completion** of the Database Explorer: the three-band
breakpoint model, the collapsed navigation shell, the `RSP-002` filter drawer
and its staged-state guarantee, the `RSP-003` below-minimum advisory with its
session-scoped `Otwórz mimo to`, the 44 px interaction-target model, single-layer
`Esc` precedence, focus containment and return, `aria-sort`, live-region
ownership and reduced motion.

This is the tenth implementation slice of the approved redesign (stage `S10`),
built on the dataset catalogue and system states in
`docs/32_database_explorer_dataset_catalogue_and_system_states.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— `RESPONSIVE_SPEC.md`, `ACCESSIBILITY_SPEC.md`, `INTERACTION_SPEC.md` §2–§3 and
§9, `COMPONENT_CATALOG.md`, `COPY_AND_TERMINOLOGY.md` §9, `SCREEN_STATE_MATRIX.md`
`RSP-001`–`RSP-003`, criteria `RS-1`–`RS-11` and `AC-1`–`AC-14`.

**No schema change.** `S10` adds no migration, no table, no column, no query, no
endpoint and no persisted preference. It changes no authorization, no filter
grammar, no export semantics and no row-identity model.

---

## 1. The band model

Four bands, and nothing else. Every responsive rule in `api/static/css/portal.css`
and `api/static/css/data-grid.css` sits on one of these boundaries; an ad-hoc
width would make the final behaviour indeterminate, and the suite asserts that
no other `max-width` query exists.

| Band | Width | Shell | Database Explorer |
|---|---|---|---|
| `bp/compact` and wider | ≥1280 px | five inline nav items; 56 px app bar; 64 px context bar | docked 352 px filter panel; 520 px docked row panel; drag column resize |
| `bp/tablet-landscape` | 1024–1279 px | menu button `☰` **plus the active module name**; 52 px app bar; 58 px context bar; physical table name and approved-column count hidden | 44 px interaction targets; filter panel still docks at 352 px; resize handle keeps a 44 px drag target |
| `bp/tablet-portrait` | 768–1023 px | menu button only; context bar wraps to two lines; account name and sign-out move into the drawer | `RSP-002`: filter panel becomes a 400 px overlay drawer over a scrim; row panel becomes a full-width overlay; chip strip gives way to the `Filtry` count badge; column resize is menu-only |
| `bp/below-min` | <768 px | brand mark only | `RSP-003` advisory until accepted; after `Otwórz mimo to` the `RSP-002` layout renders at that width |

The one pre-existing exception is S7's own **≤768 px** range-selection cutoff.
That is the established `S7` contract (`docs/30_…`), it is deliberately unchanged
here, and it is the only Database Explorer rule that does not sit on a band
boundary.

### Superseded rules

Two pre-approval rules conflicted with the approved bands and no longer control
behaviour:

1. the row-detail overlay switched at **768 px**, which left the whole 768–1023 px
   band docked at 520 px on a viewport that cannot hold it. The overlay now
   begins at the top of the portrait band (≤1023 px);
2. the ≤1023 px block allowed the chip strip to wrap, which contradicts the
   approved "count badge only" treatment for that band.

Neither is preserved for backward compatibility.

### The table is always a table

No band converts rows into cards, stacks columns, or expands a row in place of
its columns. Horizontal scrolling is expected from 1024 px down. The scroll box
(`.db-table-scroll`) survives in every band — it is what the sticky header
sticks to, and removing it silently unsticks the header.

### The client is never hidden

The client display name and the client code are the one element that may not
degrade. What gives way below 1280 px is the physical table name
(`.lp-context-physical`) and the approved-column count (`.lp-context-colcount`).
`TYLKO ODCZYT` stays at every width.

## 2. Collapsed navigation (`RSP-001`)

From 1279 px down the horizontal navigation is replaced by the menu button and,
in the tablet-landscape band only, the active module name.

- The module name is a `<span>`. It is not a second navigation control: the real
  navigation item keeps `aria-current="page"`, and outside its band the span is
  `display: none` — absent, not merely invisible, so it never becomes a
  focusable or announced duplicate.
- The menu button carries `aria-expanded` and `aria-controls="lp-nav-drawer"`.
- The drawer is **modal**: `role="dialog"`, `aria-modal="true"`, focus enters it
  on open, is contained while open, and returns to the menu button on close. It
  closes on selection, `Esc`, the scrim and its own `×`.
- It renders the **same** `primary_nav_items` list the app bar does. There is one
  authorization model; the drawer can no more reveal an unauthorized entry than
  the app bar can, and route authorization is unchanged regardless.
- It carries the account name and sign-out, because the app bar drops both at
  ≤1023 px and keyboard reachability does not allow a control to disappear.
- A drawer left open when the user followed one of its links used to be restored
  by the back-forward cache exactly as it was — visible, over a scrim, with a
  stale `aria-expanded="true"`. A persisted `pageshow` now resets the layer, and
  moves focus back to the menu button only when focus would otherwise have been
  left inside the drawer that just disappeared.

## 3. The `RSP-002` filter drawer

**One panel, one form, one staged state.** The drawer is the docked panel
*relocated by CSS*, never a second copy. This is the whole reason the staged
filter contract survives the responsive move:

- a breakpoint change cannot apply, discard or duplicate a staged edit — nothing
  re-renders and nothing is moved in the DOM;
- no filter parameter is ever carried by two successful controls;
- `filter_exact__` values and S4 picker selections are the same fields they were
  at desktop, so distributions, operators and exact values behave identically;
- the drawer issues no aggregate request of its own. Value distributions are
  still fetched by the column menu, on demand, exactly as at desktop.

Dismissal semantics (`INTERACTION_SPEC.md` §3):

| Action | Result |
|---|---|
| Scrim click | close, **keep** staged edits, focus returns to `Filtry` |
| `×` in the drawer head | close, **keep** staged edits, focus returns to `Filtry` |
| `Esc` | close, **keep** staged edits, focus returns to `Filtry` |
| `Zastosuj` | applies once; the submit guard prevents a double request |
| `Wyczyść wszystkie` | the existing immediate reset link, unchanged |

Closing is never an Apply, never a Cancel and never a Clear. Reopening shows the
same staged state.

Semantics that are true only in drawer mode are applied only there:
`role="dialog"` + `aria-modal="true"` + focus containment at ≤1023 px, and
`role="group"` with no containment at ≥1024 px, where the panel is genuinely part
of the page. The drawer head and the scrim are `display: none` above the band, so
the docked panel has no focusable close button.

## 4. `RSP-003` — the below-minimum advisory

Below 768 px the Database Explorer states the limit plainly and offers two real
routes out. The copy is verbatim from `COPY_AND_TERMINOLOGY.md` §9, with the
dataset's own approved-column count substituted for the placeholder:

> **Poniżej 768 px**
> **Przeglądarka danych wymaga szerszego ekranu**
> Tabela z ⟨n⟩ kolumnami nie da się rzetelnie obsłużyć na tej szerokości. Nie
> zamieniamy jej na karty, bo porównywanie wierszy jest tu całym sensem pracy.
> **Przejdź do Raportów** · **Otwórz mimo to**
> Raporty i Eco Driving działają na tej szerokości w pełni.

- `Przejdź do Raportów` is a real link (`/user/reports`); `Otwórz mimo to` is a
  button, because it acts rather than navigates.
- The advisory is **server-rendered but shipped `hidden`** and revealed by
  `js/data-grid-responsive.js`. The viewport is a client fact, and with scripting
  unavailable nothing changes: the table renders as it always has. An advisory
  whose escape hatch could not act would be worse than no advisory.
- While the advisory is shown, the sheet and the demoted export section carry the
  `hidden` attribute, so they leave the tab order and the accessibility tree
  together. Focus moves to the advisory heading.
- It is a screen, not an announcement: no live region, no `role="alert"`.

### Session memory

`Otwórz mimo to` writes `"1"` to `sessionStorage` under the single namespaced key
`logplatform.db.narrow-ack`.

| Situation | Behaviour |
|---|---|
| First narrow load in a session | advisory shown |
| After `Otwórz mimo to` | sheet shown; no further interruption |
| Widen above 768 px, then narrow again in the same session | advisory stays away — no flicker |
| Narrow, widen, narrow again **without** accepting | advisory returns |
| Back/Forward (BFCache restore) | state re-derived from the band and the acknowledgement |
| New browser session | advisory eligible again |
| Storage unavailable or throwing | the acknowledgement is held in memory for that document only; the next load shows the advisory again |

The acknowledgement never enters the URL, the database, a user preference or
`localStorage`, is never sent to the server, and no security decision reads it.
It stores no dataset, client or business value.

### After accepting

The `RSP-002` layout renders at that width: the table stays a table, horizontal
scrolling works, the pinned column stays pinned, the filter drawer is full width,
hidden columns stay hidden, and S7 range selection remains disabled.

## 5. Interaction targets (44 px)

From 1279 px down the approved minimum is a 44 px **hit area**, not a 44 px
visual box. Compact grid density is untouched: no responsive rule changes the row
height tokens or the cell padding, and the suite asserts that.

| Family | Mechanism |
|---|---|
| Toolbar, pager, page size, density, presets, state actions, export actions, column-menu actions, column-panel controls, row-panel traverse/close, `Filtry` / `Kolumny` triggers | `min-height: var(--lp-height-control-touch)` (and `min-width` where the control is square) |
| Filter chip `×` and panel remove `×` | 16 px visual box, 44 px transparent `::after` hit area centred on the glyph |
| Column resize handle | 44 px drag target in the tablet-landscape band; `display: none` below it, where the column menu's width entry and `Dopasuj szerokość do treści` are the keyboard-first equivalents (`AC-2`) |
| Shell: menu button, drawer rows, drawer close, theme options, sign-out, export indicator | 44 px box |

A control removed by a band is removed with `display: none` or the `hidden`
attribute, so it leaves the tab order with the screen. No responsive duplicate is
visually hidden but focusable.

## 6. `Esc` precedence

One deterministic order, top to bottom:

```
navigation drawer  >  column menu  >  column panel  >  filter panel/drawer  >  row-detail panel  >  cell selection
```

Each owner yields two ways, so the order holds regardless of script load order:

1. **`event.defaultPrevented`** — a layer that already acted on this keypress
   marks it handled, and every layer below returns immediately;
2. **explicit state guards** — each owner also checks whether a higher layer is
   currently open.

One press closes exactly one layer. When no product surface is open, `Esc` is not
consumed and reaches the browser.

Two guard selectors were dead before this stage: `[data-db-menu]` never matched
anything, because the server renders `data-db-col-menu`. With a column menu open,
`Esc` therefore also closed the row panel and cleared the cell selection. The
selectors now name the attribute the page actually renders, and the S6/S7
harnesses were corrected to the shipped markup at the same time.

## 7. Focus

| Layer | Containment | On close |
|---|---|---|
| Navigation drawer | trapped | focus returns to the menu button |
| Column menu | trapped (S3, unchanged) | focus returns to the header |
| Column panel | trapped (S5, unchanged) | focus returns to `Kolumny` |
| Filter panel, docked (≥1024) | **not** trapped — it is part of the page | — |
| Filter drawer (≤1023) | trapped | focus returns to `Filtry` |
| Row panel, docked (≥1024) | **not** trapped | focus returns to the row (S6, unchanged) |
| Row panel, overlay (≤1023) | trapped, `role="dialog"`, named by its visible heading | focus returns to the row |

Focus containment is implemented once, in `js/data-grid-responsive.js`, for the
two layers whose modality depends on the band. It yields to the navigation
drawer, which owns containment while it is open. No positive `tabindex` exists
anywhere; focus order follows DOM order, and the responsive relocation moves no
control between DOM positions.

## 8. Table and status semantics

- **`aria-sort`** is emitted only on a header whose column is actually sortable.
  A non-sortable column states nothing rather than advertising `none`, which
  would claim an affordance that does not exist. At most one header carries a
  non-`none` value, because the canonical sort state holds exactly one column.
  The caret and the toolbar sentence remain the visual and lexical carriers, so
  the sort is never communicated by colour alone.
- The row sheet renders exactly one `<h1>`, visually hidden. The approved
  table-first geometry forbids a visible page title above the table (`DB-1`,
  `DB-2`), but the document still needs one meaningful top-level heading.
- Live regions keep one owner per event: the result counter, the column-panel
  status, the selection status and the export status. None nests inside another,
  and ordinary keyboard navigation produces no announcement.
- Loading remains `aria-busy` on the table region with `aria-hidden` skeleton
  rows, reset on `pageshow` (S9, unchanged).

## 9. Reduced motion

`prefers-reduced-motion: reduce` suppresses every layer motion: the column-menu
rise, the docked panel and the `RSP-002` drawer slide. The scrim still appears —
it is state, not motion.

The single exemption is the background-export progress indicator, which carries
information. Under reduced motion the indeterminate bar becomes a static block
rather than disappearing, the determinate bar keeps its width and
`aria-valuenow`, and both states state their progress in words
(`n / m wierszy`). Nothing is replaced by flashing, and there is no skeleton
shimmer in any mode.

## 10. Themes

Every rule added here resolves to an approved token; the suite asserts that no
ad-hoc colour was introduced. Light, dark and AUTO therefore share one geometry:
the band rules, the drawer widths, the hit areas and the focus treatment are
theme-independent by construction.

## 11. Deliberate deviations from the handoff

Two rows of `RESPONSIVE_SPEC.md` are not implemented as written, and both are
recorded rather than quietly dropped:

1. **Theme switcher inside the account menu at ≤1279 px.** The product has no
   account menu component; the collapsed surface that carries account controls is
   the navigation drawer. Relocating the switcher would mean either a second copy
   of a control that `theme.js` binds by attribute, or inventing a component this
   stage was not asked to build. The switcher stays in the app bar with a 44 px
   hit area.
2. **Column menu as a full-width bottom sheet below 768 px.** `RSP-003` and
   `SCREEN_STATE_MATRIX.md` both state that accepting the advisory renders the
   `RSP-002` layout at that width. The `RSP-002` treatment — an anchored popover
   capped at 92 vw — is therefore what renders, and it is the more specific rule.

Global search and the `⌘K` shortcut are not implemented in the shell at all;
their responsive rows are consequently out of scope here rather than deviations.

## 12. Verification

`ops/tests_manual/test_portal_database_responsive_accessibility.py` and
`ops/tests_manual/data_grid_responsive_harness.js`.

No browser automation is installed in this environment, so
**live browser verification is not available**. The evidence is therefore of two
deterministic kinds:

1. the JavaScript half **runs the shipped modules** (`shell.js`,
   `data-grid-filters.js`, `data-grid-responsive.js`) against a DOM stub with a
   width-driven `matchMedia`, so band changes, staged-state preservation, focus
   containment, `Esc` precedence and the session acknowledgement are observed
   rather than described;
2. the CSS half asserts the shipped stylesheets' own declarations — the exact set
   of band boundaries, the target families, the drawer rules, the survival of the
   scroll box, and the reduced-motion blocks.

What this cannot prove is computed geometry in a real engine: actual rendered hit
rectangles, real sticky-header painting, and true 200 % zoom reflow. Those remain
asserted at the CSS-contract level.
