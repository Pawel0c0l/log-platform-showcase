# Responsive Spec

Desktop operational use is primary; the design baseline is **1920 × 1080**. Responsive behaviour is nonetheless fully specified — none of it is left to implementation guesswork.

**Governing decision (`D-015`):** full support down to **768 px**. The table **never** converts into one card per row. Below 768 px, Database Explorer shows an advisory with two routes out; Report Explorer and Eco Driving remain fully usable.

Rationale for not converting to cards: comparing values across rows *is* the work. A card list destroys column alignment, which is the only thing that makes 42 columns scannable.

---

## Breakpoints

| Name | Range | Character |
|---|---|---|
`bp/wide` | ≥1680 px | Design baseline. Everything docked, nothing collapsed. |
`bp/desktop` | 1440–1679 px | Full fidelity; page titles step down one size. |
`bp/compact` | 1280–1439 px | Smallest full-fidelity layout. Working-area padding 20 px. |
`bp/tablet-landscape` | 1024–1279 px | Nav collapses; touch targets 44 px; filter panel undocks on demand. |
`bp/tablet-portrait` | 768–1023 px | Filter panel becomes an overlay drawer; context bar compresses. |
`bp/below-min` | <768 px | `RSP-003` advisory for Dane; other modules adapt. |

---

## Shell

| Element | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| App bar height | 56 px | 52 px | 52 px | 52 px |
| Brand | mark + wordmark | mark + wordmark | mark + wordmark | mark only |
| Primary nav | 5 inline items | **menu button** `☰` + active module name | menu button only | menu button only |
| Global search | 300–340 px field | icon button, expands over the bar | icon button | icon button |
| Theme switcher | 3-way segmented | inside the account menu | inside the account menu | inside the account menu |
| Account | name + avatar | avatar only | avatar only | avatar only |
| Export indicator | text + dot | dot only | dot only | dot only |

The collapsed nav opens as a **left overlay drawer** listing the two groups (`Praca`, `Narzędzia`) with 44 px rows, over a scrim. It closes on selection, `Esc`, or scrim click.

## Client context bar

| Element | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Height | 64 px | 58 px | 56 px | 56 px |
| Client name | 17 px | 15 px | 14 px | 14 px |
| Client code chip | visible | visible | visible | visible |
| Layout | one line: client · module | two lines: client + code / module | two lines, truncated with ellipsis | two lines, truncated |
| Physical table name | visible | hidden | hidden | hidden |
| Approved-column count | visible | hidden | hidden | hidden |
| `TYLKO ODCZYT` | visible | visible | moved under the client name | visible |
| Page actions | all inline | primary + `⋯` overflow | primary + `⋯` | `⋯` only |

**The client name and code are never hidden at any width.** This is the one element that may not degrade.

## Context selectors (Eco Driving)

| Element | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Month stepper | inline in the period bar | inline | inline, full width | inline, full width |
| Week cards | one row, all weeks | one row, horizontally scrollable | one row, horizontally scrollable | one row, scrollable |
| `Cały miesiąc` / `Wyczyść` | inline above the cards | inline | inline | inline |
| Basis line | one line | wraps to two | wraps to three | wraps |
| Fleet histogram | 420 px in the period bar | 320 px | **moves below the basis line**, full width | full width |

The basis line and the week cards are **never** collapsed behind a control. They are the answer to "what am I looking at".

## Table

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Structure | table | table | table | table (`Otwórz mimo to`) |
| Horizontal scroll | when needed | **always expected** | always expected | always expected |
| Sticky columns | checkbox + identifier | identifier only | identifier only | identifier only |
| Right-edge fade | present | present | present | present |
| Row height | 32 / 40 | 34 | 34 | 34 |
| Header height | 36 px | 34 px | 34 px | 34 px |
| Visible columns | user selection (default 12) | user selection; **default trimmed to 8** | default trimmed to 6 | 6 |
| Column resize | drag | drag (44 px hit area) | via column menu only | via column menu only |
| Row click → `DB-006` | 520 px docked panel | 520 px docked panel | **full-width overlay drawer** | full-width overlay |
| Cell range selection | yes | yes | disabled (touch conflicts with scroll) | disabled |

**The table remains a table at every width.** No card conversion, no column stacking, no row expansion in place of columns.

## Filters

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Filter panel | docked 352 px, pinnable | **collapsed by default**; opens as a docked 352 px panel | **overlay drawer 400 px** over a scrim | overlay drawer, full width |
| Active filters when collapsed | chip strip in a 40 px band | chip strip, horizontally scrollable | count badge on `Filtry` only | count badge only |
| Column menu | anchored popover 298 px | anchored popover 298 px | anchored popover, max 92 vw | full-width sheet from the bottom |
| Date presets | chips in the panel | chips in the panel | chips in the drawer | chips in the drawer |
| Drawer commit | — | — | `Zastosuj` / `Wyczyść` pinned at the drawer bottom, 44 px | same |

The drawer **keeps staged edits** when dismissed by scrim or `Esc` (unlike the column menu, which discards). Reopening shows the staged state.

## Actions and touch targets

| Aspect | ≥1280 | ≤1024 |
|---|---|---|
| Minimum interactive height | 30 px | **44 px** — buttons, icon buttons, pagination cells, drawer rows, checkboxes' hit area |
| Chip `×` hit area | 16 px visual / 24 px hit | 16 px visual / **44 px hit** |
| Pagination | numbered cells 26 px | numbered cells 44 px, fewer numbers |
| Overflow | none | `⋯` menu for secondary page actions |

Visual sizes may stay compact; **hit areas** grow. A 44 px hit area around a 16 px glyph is correct.

## Cards and lists (Report Explorer)

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Type rail | 288 px docked | 288 px docked | **type selector above the list** | selector |
| Instance row | 6 regions in one line | name+type / period+status / files+actions in 3 columns | regions stack in 2 rows within one bordered row | stack, full width |
| Format badges | inline with sizes | inline with sizes | inline, sizes hidden | inline |
| Actions | inline buttons | inline buttons | full-width buttons at the row bottom | full-width |
| Group headings | inline with count | inline with count | inline with count | inline |

The instance stays **one bordered row** — it never becomes a stack of separate cards, because the left status edge is what makes the list scannable.

## Report detail (`REP-003`)

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Layout | preview + 480 px right column | preview + 400 px right column | **single column**: header, preview, files, history | single column |
| Metadata grid | 4 columns | 3 columns | 2 columns | 1 column |
| Preview | embedded, paged | embedded, paged | embedded, width-fit | embedded, width-fit |
| Format switcher | segmented | segmented | segmented | select |

## Eco Driving ranking

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Sticky columns | position | position | position | position |
| Visible metric columns | 8 | 5 (`> 160`, `> 170`, `Długi postój` hidden by default) | 3 | 3 |
| Unit toggle | inline in the toolbar | inline | inline | inline |
| Group counts | inline text | inline text | inside the filter drawer | inside the drawer |
| Fleet histogram | in the period bar | in the period bar, narrower | below the basis line | below |

Speed-threshold columns (`> 140`, `> 160`, `> 170`) are the **first to be hidden** as width shrinks, and are always restorable from the column panel.

## Eco Driving driver detail

| Aspect | ≥1280 | 1024–1279 | 768–1023 | <768 |
|---|---|---|---|---|
| Score card + trend | side by side | side by side, trend narrower | **stacked** | stacked |
| Metric hero size | 52 px | 44 px | 40 px | 40 px |
| Identity grid | 4 columns | 3 columns | 2 columns | 1 column |
| Component table + week contribution | side by side (`1fr` + 400 px) | stacked | stacked | stacked |
| Trip table | full width, all columns | horizontal scroll | horizontal scroll | horizontal scroll |
| Breadcrumb | full path | full path, truncating the middle | client + period + driver only | driver only + back |

## Below 768 px (`RSP-003`)

Database Explorer shows a full-screen advisory:

- States the reason plainly: a 42-column table cannot be operated honestly at this width.
- States what we deliberately did **not** do: convert to cards.
- Two routes: `Przejdź do Raportów` (primary — Raporty works fully) and `Otwórz mimo to` (secondary — renders the `RSP-002` layout at that width).
- Notes that Raporty and Eco Driving are fully usable at this width.

The decision belongs to the user. `Otwórz mimo to` is a real escape hatch, not a dead end, and the choice is remembered for the session.

## Print

Not a designed target. Report files are the print artifact; the UI is not styled for print in this iteration. Recorded so that Claude Code does not invent print CSS.
