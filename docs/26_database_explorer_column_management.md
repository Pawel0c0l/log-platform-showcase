# 26 — Database Explorer: column management and URL state

Durable reference for the Database Explorer's column-management surface and the
URL contract that makes a configured sheet reproducible: which approved columns
are visible, in what order, at what width, and which of them are pinned.

This is the fifth implementation slice of the approved redesign (stage `S5`),
built on the value distributions in
`docs/25_database_explorer_value_distributions.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screen `DB-008`, `TABLE_AND_DATA_GRID_SPEC.md` §1.3 / §1.7 / §5.1 / §5.2 /
§5.3, `INTERACTION_SPEC.md` §4, `ACCESSIBILITY_SPEC.md` §4, criteria `DB-26`
through `DB-29` and `DB-34`.

---

## 1. Scope

Column management is **presentation state only**. It rearranges what the
approved-column catalog already permits and never changes what the query may
read.

| Concern | Owner |
|---|---|
| which physical columns exist at all | dataset catalog (`portal_database_dataset_columns`) |
| which of them a user may see | catalog `is_visible` + client/dataset grants + `can_view_rows` |
| which visible columns are displayed | `cols` — narrowing only, unchanged since phase 2B |
| order, width, pin state | `colorder`, `colw`, `colpin` — this document |

No schema change, no new endpoint, no server-side persistence. Named column sets
and saved views (`DB-30`) remain a later stage and are deliberately absent
rather than approximated.

## 2. URL contract

All four parameters live on the row-browser route
`GET /user/database/datasets/{dataset_id}`.

| Parameter | Form | Meaning |
|---|---|---|
| `cols` | repeated, one per column (comma lists also accepted) | the displayed subset of the approved visible set |
| `colorder` | `a,b,c` | leading column order; the rest is derived |
| `colw` | `a:180,b:96` | explicit widths in pixels |
| `colpin` | `a,b` | pinned columns in pin order; **present but empty** means "nothing pinned" |

Two intent parameters are consumed by the server and never re-emitted:

| Parameter | Purpose |
|---|---|
| `colw__<column>` | the column menu's explicit width field; merged into `colw` |
| `colsel` | marks a real `DB-008` submit, so an empty selection is refusable |
| `colpanel` | keeps the `DB-008` panel open across a no-script reorder |

### 2.1 Resolution order

The server resolves the layout in exactly this order, and the result is the one
source the `<colgroup>`, the header, every body cell, the column menus and the
`DB-008` panel render from:

1. **approved** — the catalog's visible column set;
2. **visible** — `cols` narrowed against it, catalog order preserved;
3. **order** — `colorder` tokens intersected with the approved set, deduplicated
   first-wins, restricted to the visible set, then the **fill rule**: every
   visible column the order did not name is appended in catalog order;
4. **widths** — `colw` pairs for approved columns, clamped, plus any
   `colw__<column>` merged on top;
5. **pins** — `colpin` intersected with the ordered visible set, deduplicated,
   then bounded (§4);
6. **display order** — pinned columns first in pin order, then the rest in the
   order from step 3.

Because pins are hoisted, the resolution is idempotent: re-parsing the canonical
`colorder` produces the same arrangement.

### 2.2 Canonicalization

After resolution the parameters are rewritten to what the server actually
accepted, exactly as `cols` and the sort already were. A rejected, unknown,
unapproved or duplicated identifier therefore never rides into a generated link,
and a copied address reproduces the layout on screen.

- `colorder` is emitted as the **shortest prefix** the fill rule expands back to
  the resolved order. Moving one column to the front of a 42-column dataset
  costs one token, not forty-two. It is dropped when the order is the default.
- `colw` carries only widths that differ from the column's family default, since
  a default is reconstructible, and only for columns that are currently
  displayed. Hiding a column therefore drops its explicit width: re-showing it
  returns it to its family default rather than to a width the user can no longer
  see. This keeps the parameter proportional to what is on screen.
- `colpin` is dropped when the pins equal the transitional default (§4), emitted
  as a list when the user chose pins, and emitted **empty** when the user
  explicitly unpinned everything.

Measured worst case for a 42-column dataset with a full reversal, an explicit
width on every column and the maximum pin set: **~1.7 kB of query string** at
the ~18-character identifiers of the test fixture. The length scales with the
identifiers themselves, so the same pathological layout measures ~3.4 kB at
~20-character names and ~5.0 kB at ~33-character names; a generated column-action
link, which also carries the filter and sort state it must preserve, sits at the
upper end of that band. No hard browser limit is claimed here; the point is that
the representation stays in the same order of magnitude as the existing filter
state and is bounded by `PORTAL_DATABASE_MAX_LAYOUT_ENTRIES` regardless of what
the caller supplies.

## 3. Widths

| Rule | Value | Source |
|---|---|---|
| minimum | 64 px | `TGS` §1.7 |
| maximum | 480 px | `TGS` §1.7 |
| default | per data family (identifier/text 200, integer/decimal 120, boolean 96, date 120, timestamp 168, structured 200) | `TGS` §1.7, §5 |
| declared table width | the sum of the column widths | `TGS` §1.7 |

The table is laid out `table-layout: fixed` with a server-rendered
`<colgroup>`, so the declared width and the sum agree by construction — a
mismatch would silently drop the last column out of view.

A width parameter must be a non-negative decimal integer. Anything else —
negative, zero, fractional, non-numeric, absurdly long — is **rejected**, not
guessed; a numeric value outside the bounds is clamped. A duplicate definition
resolves to the first **valid** one, so the canonical form of a crafted
`a:64,a:480` is stable. Crafted width state can therefore never produce
unbounded CSS.

Width is presentation: resizing issues no query, changes no value rendering, and
leaves the S2 truncation contract intact.

### 3.1 Autofit

Double-clicking the header edge, or `Dopasuj szerokość do treści` in the column
menu, fits the column to the widest value **on the current page** — never a
fresh scan of the dataset.

The estimate is deterministic and implemented identically on both sides:

```
width = clamp(52 + longest_rendered_text_length * 7, 64, 480)
```

It measures what the cell **renders** — the truncated identifier, the grouped
integer, the collapsed `{ 4 pola }` preview — so a structured value does not fit
its raw JSON, and a single extreme value is capped by the maximum rather than
producing an absurdly wide column.

Because the server already holds the page's rows, autofit works with scripting
unavailable: the affordance is a real link to the computed width.

## 4. Pinning

Pinned columns accumulate to the left in pin order, each at the cumulative sum
of the widths before it, so several pins sit side by side and never overlap. The
region carries one right edge, on its last column.

**Why pin state is in the URL.** The approved persistence table (`TGS` §5.4,
`D-007`) names filters, sort, page, page size, visible columns, order, widths
and the open row, and does not mention pinning either way. S5 carries it in the
URL as `colpin` because the alternative is incoherent here: a visibility change
is a real server round trip, so pin state held only in the page would be lost
every time the user showed or hid a column, and a copied address would not
reproduce the sheet on screen — which is the contract `D-007` exists to protect.
The deviation is additive and stays inside the approved boundary: `colpin` can
only pin a column that is already approved and already displayed, and it is
omitted entirely while the transitional default (§4.1) stands. It does **not**
introduce the server-side persistence that named column sets and saved views
(`DB-30`) still require.

Bounds, both enforced:

| Bound | Server | Client |
|---|---|---|
| pin count | ≤ 4 | ≤ 4 |
| pinned width | ≤ 576 px (40 % of a 1440 px reference) | ≤ 40 % of the live table viewport |

The server cannot know the viewport, so it enforces the bounded absolute
fallback; `data-grid-columns.js` enforces the approved proportional rule at the
moment the user pins and refuses the additional pin inline with the reason. A
refusal never mutates another column's width. Pins beyond the bound are dropped
from the tail and the refusal is stated on the page.

### 4.1 Transitional identity limitation

The approved design pins a **selection checkbox** and a **configured row
identifier**. Neither exists yet: `is_row_identifier` is not configured for the
production datasets and row selection is a later stage. S5 therefore keeps a
transitional default — **the first displayed column is pinned** — and makes it
fully user-controllable, which is the strongest correct state available without
fabricating an identity the dataset has not approved.

The default is derivable and is not serialized, so generated links stay free of
pin state until the user chooses one. `colpin=` (present, empty) is how the user
says "nothing pinned".

Pinning is presentation. A pinned column does not become a key, a row
identifier, or a row-detail identity.

## 5. Interaction and history

| Action | Applies | History |
|---|---|---|
| visibility (`DB-008` `Zastosuj`, `Ukryj kolumnę`) | server round trip — revealing a column changes the row `SELECT` | normal navigation |
| order (`DB-008` `Zastosuj`) | in place | one `pushState` |
| resize drag | live while dragging, committed on release | one `pushState` per completed resize |
| keyboard resize (arrows on the focused handle, 16 px steps) | live per press | one `pushState` per burst |
| autofit | in place | one `pushState` |
| pin / unpin | in place | one `pushState` |

Order, width and pin changes never requery: the rows on screen already belong to
the view. Intermediate drag movement and key repeats are applied to the DOM but
not pushed, so a resize leaves one history entry rather than one per pixel.

`Back`/`Forward` re-resolve the layout from the popped URL through the **same
resolver** the initial render uses, so the address bar and the rendered sheet
cannot describe different arrangements. A history entry whose column selection
differs reloads instead, because the rendered rows no longer carry the right
columns.

## 6. Reset

`Domyślne kolumny` restores the dataset's default visible set, order, widths and
pin state, and returns to page 1. It does **not** clear filters, search, sort,
density or page size (`TGS` §5.3).

The reverse holds too: `Wyczyść wszystkie` clears filters and the global search
and leaves the column layout untouched. The two resets own disjoint domains.

Hiding a column neither clears its filter nor its sort. Visibility and query
semantics are separate concerns: the active-filter chip remains, it can still be
removed, and a valid sort on a hidden column stays effective and stays stated in
words in the toolbar.

## 7. Progressive enhancement

With JavaScript unavailable:

- the `DB-008` panel is a plain GET form — checkboxes carry visibility, `▲`/`▼`
  links carry order, checkboxes carry pin state, `Zastosuj` commits;
- the column menu's pin, unpin, hide and autofit actions are links;
- the explicit width field is a number input with the same bounds;
- controls that would do nothing without the script — the column search, the
  `Wszystkie`/`Widoczne`/`Ukryte` tabs and the `⠿` drag handle — ship **hidden**
  and are revealed by the script, so a scriptless page never offers a dead
  control.

`data-grid-columns.js` adds drag reorder, live resize with the guide, in-place
application and history synchronisation. It owns no authorization.

## 8. Accessibility

- reorder has a keyboard path (`▲`/`▼` links, and `ArrowUp`/`ArrowDown` on the
  focused `⠿` handle) and announces the new position through a live region;
- resize has the approved keyboard equivalent — explicit width entry in the
  column menu — plus arrow adjustment on the focused handle;
- the resize affordance's accessible name includes the column and its current
  width;
- pin, unpin and hide are ordinary links;
- a hidden column is signalled by an unchecked box and an explicit row state,
  not by colour alone;
- `Domyślne kolumny` is named for what it resets.

Sticky positioning does not disturb the `<th scope="col">` relationships.

## 9. Security contract

Unchanged from S2–S4, and specifically:

- the approved-column catalog remains the maximum universe; `cols` still only
  narrows it;
- every layout identifier is intersected with the approved set before it is used
  **or re-emitted**, so an unapproved name can neither be displayed, ordered,
  widened nor pinned, and is never echoed into the page;
- layout parameters reach no SQL — the generated statement, its bound values,
  the aggregate scope and the audit record are untouched;
- exports ignore layout state entirely, exactly as they already ignore `cols`;
  display order does not change what an export means;
- no new endpoint, no persistence table, no migration.

## 10. Implementation and tests

| File | Role |
|---|---|
| `api/main.py` | layout resolution, canonicalization, `DB-008` panel, column actions, `<colgroup>` and sticky offsets |
| `api/static/js/data-grid-columns.js` | drag reorder, live resize, autofit, pin presentation, URL/history synchronisation |
| `api/static/css/data-grid.css` | fixed table layout, pinned region, resize affordance, `DB-008` panel |
| `api/portal_ui/i18n.py` | the Polish vocabulary for all of the above |
| `ops/tests_manual/test_portal_database_column_management.py` | the deterministic suite |
| `ops/tests_manual/data_grid_columns_harness.js` | DOM/history stub that executes the shipped script |
