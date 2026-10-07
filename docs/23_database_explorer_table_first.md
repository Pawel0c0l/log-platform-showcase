# 23 — Database Explorer: table-first row sheet

Durable reference for the Database Explorer row-browsing surface: the
table-first layout, the toolbar and footer, sticky behaviour, row density and
its precedence rules, the filtered-vs-total counter, and the type-aware cell
rendering contract.

This is the second implementation slice of the approved redesign (stage `S2`),
built on the shared shell foundation in `docs/22_portal_ui_foundation_and_shared_shell.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screens `DB-003`/`DB-004`, `TABLE_AND_DATA_GRID_SPEC.md`.

---

## 1. Layout

The defining structural requirement is that **the table is the primary working
surface**. The pre-redesign page put it fifth, below a background-export
explainer, a dataset summary card, an active-filter panel and a full filter
form; the dataset itself was usually below the fold.

Vertical composition, all heights from tokens:

| Band | Height | Behaviour |
|---|---|---|
| App bar | 56 px | shared shell |
| Client context bar | 64 px | shared shell — client, dataset, `TYLKO ODCZYT`, physical table, approved-column count |
| Toolbar | 48 px | search, `Filtry n`, `Kolumny n/m`, density, result counter, sort summary |
| Table header row | 36 px | sticky to the table viewport |
| Table body | remaining | the only scrolling region |
| Footer | 44 px | pagination, page size |

`56 + 64 + 48 = 168`, so the table header begins exactly 168 px from the top of
the viewport (`DB-1`). At 1080 px that leaves `1080 − 168 − 44 − 36 = 832 px` of
body, which is **26 rows at `Zwarta`** and 20 at `Wygodna` (`DB-5` requires ≥ 26).
`ops/tests_manual/test_portal_database_table_first.py` computes this from the
tokens, so a regression in any single token fails a test rather than needing a
screenshot.

The sheet claims the remaining viewport height, so the table body scrolls rather
than the document. The page therefore renders with `lp-work-flush`, which drops
the working-area padding, and `_portal_layout` suppresses the in-page path and
title: repeating the dataset name above the sheet would push the table down for
information the context bar already carries.

### Demoted controls

The column picker and export controls sit **below** the sheet in
`.db-secondary`, as collapsed `<details>` sections that the toolbar buttons link
to. The pre-redesign filter form that also lived there was retired by stage `S3`
in favour of the column header menus and the staged filter panel — see
`docs/24_database_explorer_column_centric_filtering.md`.

## 2. Sticky behaviour

- The header row is sticky to the table viewport, not to the document, so it
  stays visible while the body scrolls and never collides with the app bar or
  the context bar.
- **Only the first displayed column is pinned.** The approved design also pins a
  selection checkbox column and the configured row identifier. Neither exists at
  this stage: selection arrives with range copy, and row identity configuration
  is stage `S6`. Pinning anything else would mean inventing an identity or
  surfacing an internal key the dataset has not approved.
- Sticky cells paint their own background, or the scrolling content shows
  through them.
- A 30 px right-edge fade signals that content continues horizontally. It is a
  required affordance, not decoration: a horizontally clipped table with no
  signal reads as a broken artboard. `data-grid.js` toggles it from the real
  scroll state, so it never lies.

## 3. Density

Two modes: `Zwarta` (32 px rows, the approved default) and `Wygodna` (40 px).
Only the row height differs — the frame, control heights and column widths are
identical, exactly as light and dark are (`SH-10`).

**Precedence, in this order:**

1. an explicit `density` in the URL — a shared or bookmarked link keeps showing
   what its author saw, so existing deep links do not change meaning;
2. the browser-local preference in `localStorage["logplatform.database.density"]`;
3. the approved default, `Zwarta`.

The server can only distinguish (1) from (3), so it marks case (1) with
`data-density-locked` on the sheet; `data-grid.js` applies a stored preference
only when that marker is absent.

The control is rendered as two links carrying `?density=`, so it works without
scripting. With scripting, the click is intercepted: density switches in place,
so **filters, sort, page, page size, visible columns and scroll position all
survive untouched** — no requery, no navigation.

### URL and history coherence

Because a valid URL density outranks the stored preference, switching in place
without touching the URL would leave the address bar asserting one density while
the table showed another — and a reload would silently undo the change. So the
click also does `history.pushState` with the link's own href, which already
carries every other parameter and the route.

`pushState` (not `replaceState`) is deliberate: it gives Back and Forward
something to move between. A `popstate` handler re-derives the density from the
history URL using the same precedence, so the rendered state always follows the
current entry. Nothing is refetched — the rows on screen already belong to that
view.

Going Back to a URL whose density differs from the stored preference renders the
**URL's** density; localStorage keeps the last explicit choice for future URLs
that carry no density parameter. The server states its own default as
`data-density-default`, so the script never has to infer it from a page whose
rendered density came from the URL.

Density is browser-local by design. Server-side per-account persistence needs a
schema change and belongs to a later, separately authorized stage.

## 4. Result counter

The approved counter distinguishes the filtered result from the size of the
dataset:

- filtered: `1 274 z 48 213 wierszy`
- unfiltered: `48 213 wierszy`
- filtered, dataset size unavailable: `1 274 z ? wierszy`

The filtered number comes from the existing count query. The dataset total needs
a second, unfiltered `count(*)`, in `_count_portal_database_rows_unfiltered()`.

That function deliberately calls `_count_portal_database_rows()` with an **empty
parameter map** rather than building its own SQL. The dataset has already passed
the caller's permission check, and reusing the same builder keeps identifier
quoting, the read-only connection and the statement timeout identical. It is
passed the same authorized column list, so it can neither reach another dataset
nor widen the approved column set.

### When the second count runs

**Only when the validated query actually constrained the result**, decided from
`state["active_filters"]` — the query builder's own record of the conditions it
emitted — never from the presence of raw query-string input.

The distinction is not academic. A `search=` term against a dataset with no
approved searchable text columns produces no condition, so the result already
*is* the whole dataset: running a second count would both cost a query and
label an unfiltered view as filtered.

Raw input still drives the **controls**: a value the user typed stays clearable
through `Wyczyść wszystkie` even when it applied nothing. What the query did and
what the user typed are two different questions, and the code names them
separately (`query_filtered` vs `controls_active`).

### When the second count fails

The dataset total renders as an explicit unknown — `1 274 z ? wierszy`, with a
title explaining that the total could not be established.

It **must not** fall back to the filtered count. Doing so renders
`1 274 z 1 274 wierszy`, which asserts that the filters matched every row in the
dataset. That is a false statement about the data, and it is the worst kind of
failure for a verification tool because it looks like a successful answer.

Failing soft means degrading information honestly, not inventing a value. The
page still renders, the match count is still correct, and a failed informational
count never becomes a page error.

Numbers are grouped with a **non-breaking space** (`48 213`), the approved
separator — a normal space would let a number wrap mid-value at a column edge.

## 5. Cell rendering

`_portal_database_cell_html(value, column)` renders by data family. It is
**presentation only**: the underlying value is never altered, and
`data-db-copy` always carries the faithful source text, so a copy yields what
the database holds rather than what the cell had room to show.

| Family | Rendering |
|---|---|
| SQL `NULL` | `brak wartości`, italic, faint |
| Empty string | `pusty tekst`, italic, faint |
| Integer | right-aligned mono, grouped (`48 213`) |
| Decimal | right-aligned mono, comma separator, **source precision preserved** (`28,40`) |
| Zero | `text/muted` — dimmer than a value but not faint; zero is data, not absence |
| Date | mono `YYYY-MM-DD` |
| Timestamp | mono `YYYY-MM-DD HH:MM`; the full ISO value stays on copy |
| Boolean | badge with a word, `TAK` / `NIE` — never colour alone |
| Identifier / UUID | mono, mid-truncated keeping the leading 8 and trailing 4 (`7f03c4e1…0021`) |
| JSON / structured | collapsed `{ 4 pola }` / `[ 12 elementów ]`; copies as real JSON |
| Long text | single line, ellipsis at the cell edge; full value on copy |

Decimals reuse the source text instead of going through a float, so `28.40`
keeps its trailing zero and nothing is silently rounded. Polish plurals follow
the 1 / 2–4 / other rule, including the 12–14 exception.

### `NULL` vs empty string

**This was a real data-verification defect, and the fix is a hard requirement.**
The pre-redesign renderer sent both through one `str()` path and printed the same
em dash, so a missing value and a present-but-empty one were indistinguishable
in the browser. Values that differ in the source must not look identical.

Three cases now render distinctly: `NULL` → `brak wartości`, `""` →
`pusty tekst`, and whitespace-only text as its own content with the exact value
preserved for copy. This was **display semantics only** at this stage; the
`puste` operator arrived in `S3` and deliberately does not trim, so the three
cases stay distinguishable under filtering too.

### Presentation families are not query families

`_portal_database_render_family()` is deliberately separate from
`_portal_database_type_family()`. The latter decides which operators a column
offers and which columns the global text search reaches. Keeping them apart is
what let this stage render uuid, json and boolean values properly without
touching query semantics.

Stage `S3` later moved **boolean** into its own query family, because the
approved filter contract gives it a four-way control and no substring
semantics; `uuid` and `json` still resolve to `text` there. See
`docs/24_database_explorer_column_centric_filtering.md` §8.

## 6. Assets

Styling and behaviour live in `api/static/css/data-grid.css` and
`api/static/js/data-grid.js`, requested per page through
`_portal_layout(extra_assets=…)`. The two inline `PORTAL_DATABASE_BROWSER_CSS` /
`PORTAL_DATABASE_BROWSER_SCRIPTS` blobs in `api/main.py` were retired; their
surviving rules moved into the stylesheet and onto the approved tokens, so the
module follows light and dark instead of being dark-only.

`data-grid.js` carries three enhancements, and every one degrades safely:
density falls back to a link, the fade is simply absent, and cells stay
selectable text. Column-menu and filter-panel behaviour lives in the separate
`data-grid-filters.js` added by stage `S3`.

## 7. Preserved invariants

Unchanged by this stage, and asserted by the existing phase2a/2b/2c suites plus
`test_portal_database_table_first.py`:

- dataset permission resolution and `can_view_rows`;
- SELECT lists derived only from approved visible columns — `cols` may only
  narrow, never widen;
- read-only client connections and the statement timeout;
- parameterized values and catalog-derived quoted identifiers;
- `can_filter_rows` / `can_export_rows` gating, including that controls are
  absent rather than disabled;
- export semantics, scope and column set;
- pagination model and page sizes (25 / 50 / 100 / 200 / 500, default 100);
- database-access audit events.

No schema migration, no new filter operator, and no row-identity configuration
were introduced.

## 8. Not in this stage

Column header menus and their operators (delivered by `S3`), distinct-value
pickers, histograms, the row-detail drawer, range selection and TSV copy, column
reorder/resize/pinning, saved views and column sets, and the full
empty/loading/error state programme. They belong to `S3`–`S9`.

## 9. Tests

```bash
cd /opt/log-platform
env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_table_first.py
```

Related suites that also cover this surface:
`test_portal_database_explorer_phase2a/2b/2c.py`,
`test_portal_database_rows_phase3b.py`, `test_portal_database_export_phase3c.py`,
`test_portal_dark_theme_ui.py`.
