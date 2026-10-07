# 24 — Database Explorer: column-centric filtering and sorting

Durable reference for the Database Explorer query surface: the per-column header
menu, the staged filter panel, the typed operator contract, `puste` semantics by
data family, `od–do` ranges, bounded `in (...)`, the active-filter chips and
count, and the URL state model that carries all of it.

This is the third implementation slice of the approved redesign (stage `S3`),
built on the table-first row sheet in
`docs/23_database_explorer_table_first.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screen `DB-005`, `TABLE_AND_DATA_GRID_SPEC.md` §2–§3, `INTERACTION_SPEC.md`
§3–§4.

---

## 1. What replaced what

The pre-redesign page carried a single always-expanded form with one field per
approved column — 36–42 fields for a real dataset — plus a separate bordered
active-filter panel. Both are gone. Filtering is now reached from two places,
and they share one parameter contract:

| Surface | Where | Application |
|---|---|---|
| **Column header menu** (`DB-005`) | a `<details>` inside each eligible `<th>` | **immediate** on confirmation |
| **Filter panel** | a `<details>` whose `<summary>` is the toolbar's `Filtry n` button | **staged** until `Zastosuj` |

This split is `D-005` and it is deliberate. A menu is a transient layer, so
dismissing it discards; the panel holds staged work, so dismissing it keeps.

There is no second filter-state model. Both surfaces are GET forms submitting
the same parameters to the same validator and the same query builder, which is
what makes the no-JavaScript path correct rather than merely present.

## 2. Parameter contract

All state lives in the URL (`D-007`). One filter per column; a second filter on
the same column replaces it. Filters combine with `AND`.

| Parameter | Meaning |
|---|---|
| `op__<column>` | operator token for a text / numeric / boolean column |
| `filter__<column>` | the single value; repeatable as the `in (...)` value list |
| `filter_from__<column>` · `filter_to__<column>` | `od–do` bounds |
| `filter_in__<column>` | newline-separated `in (...)` values (trimmed) |
| `filter_exact__<column>` | one exact `in (...)` value per parameter, taken verbatim |
| `dateop__<column>` | operator token for a date/timestamp column |
| `date__<column>` | value for `przed` / `po` |
| `date_from__<column>` · `date_to__<column>` | `między` bounds |
| `sort` · `direction` | the single sort column and `asc`/`desc` |
| `search` | global text search across approved text columns |

The value variants exist so that one form can carry a control per operator
without them colliding on a single parameter name. Whichever control the user
did not use submits an empty string; the operator decides which one is read.
Without that, a page with scripting disabled would submit every value field at
once and the last one would win.

A canonical URL may still use the simple form: `op__x=in` with repeated
`filter__x` values, and `op__x=between` with `filter__x` as the lower bound,
both resolve identically.

### Operator tokens

`PORTAL_DATABASE_FILTER_OPERATORS` and `PORTAL_DATABASE_DATE_FILTER_OPERATORS`
are the whole vocabulary. A token is an **allowlist key**, never SQL: each one
selects a fixed fragment through an explicit branch in
`_build_portal_database_filter_conditions()`. An unknown token is refused before
any statement is built.

| Family | Tokens | Labels |
|---|---|---|
| text | `contains` `eq` `neq` `in` `blank` | `zawiera` `=` `≠` `in` `puste` |
| numeric | `eq` `neq` `gt` `gte` `lt` `lte` `between` `blank` | `=` `≠` `>` `≥` `<` `≤` `od–do` `puste` |
| boolean | `is_true` `is_false` `blank` (+ absence = `wszystko`) | `tak` `nie` `puste` |
| date / timestamp | `older` `newer` `range` `blank` | `przed` `po` `między` `puste` |

`in (...)` exists only for text, because that is the only family the approved
contract gives it (its distinct-value picker, stage `S4`, is what will populate
it). Boolean has no substring or comparison operator at all.

## 3. `puste` by data family

`puste` is not one predicate. The family decides:

| Family | SQL |
|---|---|
| text | `(col IS NULL OR CAST(col AS TEXT) = '')` |
| numeric · boolean · date · timestamp | `col IS NULL` |

Two consequences are load-bearing:

- **Whitespace is not blank.** Nothing in the emitted SQL trims. Stage `S2`
  deliberately renders `NULL`, `''` and `'   '` as three different things so
  data verification is reliable; a filter that quietly collapsed them would undo
  that. The column menu states what `puste` covers for the column it belongs to.
- **Zero and false are values.** The numeric predicate is `IS NULL` alone, and
  boolean uses `IS TRUE` / `IS FALSE` / `IS NULL`, which are three disjoint
  sets — a `NULL` boolean is not `false`.

## 4. `od–do` and `między`

Inclusive at both ends, and either bound may be left blank for an open-ended
range (`DB-12`). Both bounds blank is not a filter and emits nothing.

Bounds are validated before they can reach the database: numeric bounds must
parse as finite numbers, date bounds as ISO date-times, and a lower bound above
the upper is refused with a message rather than turned into a statement that
cannot match. Values are bound parameters; only the comparison operator is SQL.

A two-sided range is **two conditions but one filter**, so it is one chip and
one entry in the panel.

## 5. `in (...)`

Values arrive either as repeated `filter__<column>` parameters — what a
multi-select submits — or one per line in `filter_in__<column>`, which is what
the typed control submits. Newline is the only separator, because commas occur
inside real values.

- Empty entries and duplicates are dropped; neither changes the result.
- `filter_exact__<column>` carries one value per parameter **verbatim** — no
  splitting, no trimming — so whitespace-only text, leading or trailing spaces
  and embedded newlines round-trip exactly. It is what the S4 value picker
  submits, and it wins over the typed form when both are present. See
  `docs/25_database_explorer_value_distributions.md`.
- One `%s` placeholder per value, always. There is no concatenated fragment
  anywhere in this path.
- **Cardinality is bounded at `PORTAL_DATABASE_MAX_IN_VALUES = 50`.** The
  approved design does not state a number for the operator, but it caps its
  distinct-value picker at "up to 50 most frequent values", so 50 is the most
  the approved UI can ever produce.
- Exceeding the bound is **reported, never truncated**. A silently shortened
  value list would answer a question the user did not ask.

## 6. Sorting

Sorting is invoked from the column menu only, as two links, so it applies
immediately. One sort column at a time; `direction` is `asc` or `desc`.

Authorization is unchanged: `_validate_portal_database_sort()` accepts only
columns the catalog marks sortable, and a rejected identifier produces a
validation error instead of an `ORDER BY`.

State is carried three ways, because icon state alone is insufficient:
`aria-sort` on the `<th>`, a `↑`/`↓` caret next to the label, and the sort
stated in words in the toolbar (`sortowanie Start ↓`).

Applying a sort returns to page 1 and preserves filters, columns, density and
page size.

### Canonical sort in generated URLs

After validation the row sheet normalizes `sort` and `direction` in the
parameter map it builds every link and form from — the same treatment `cols`
already received. Two things follow:

- an identifier the validator rejected can no longer ride along in generated
  URLs (it exposed no data, but it was noise that outlived its request);
- a defaulted sort becomes explicit, so a copied link reproduces the view
  (`DB-34`).

This changes what the page *emits*. It does not change what the server
*accepts*: a forged sort column is still refused.

## 7. Chips, the count badge, and reset

Chips and the `Filtry n` badge are built from `state["filter_entries"]` — the
query builder's record of the filters it actually applied — not by re-reading
the query string. A filter-shaped parameter that was rejected or was a no-op
therefore produces no chip and does not inflate the count.

- Every active filter is **exactly one** chip carrying column, operator and
  value (`DB-16`); an `in (...)` chip carries its count, as `Kierowca in (3)`.
- `Wyczyść wszystkie` copies the whole repeated `cols` list, so clearing filters
  never changes the column selection. (An independent review found it reading
  that parameter as a scalar; corrected in `S4`.)
- The strip lives **inside** the 48 px toolbar band and scrolls horizontally.
  A separate band below the toolbar would push the table header past the
  approved 168 px the moment a filter was applied (`DB-1`), so the sheet would
  change shape exactly when the user started working.
- A chip's `×` removes that filter and nothing else: other filters, the sort,
  the columns, the density and the page size all survive, and the page resets to
  1.
- `Wyczyść wszystkie` clears every column filter and the global search in one
  action, and leaves unrelated view settings alone.
- The **global search is not a chip** and does not count toward the badge; it is
  its own toolbar field (`PBC` §2.6). It is still cleared by
  `Wyczyść wszystkie`.

`state["active_filters"]` remains the separate, condition-level record used for
"is this result filtered?" and for the background-export snapshot.

## 8. Boolean became its own query family

`_portal_database_type_family()` previously mapped boolean onto `text`. Stage
`S3` gives it its own family, deliberately, because the approved contract gives
a boolean a four-way control and no substring or comparison semantics at all.

Two effects follow, both intended:

- a boolean column offers `wszystko` / `tak` / `nie` / `puste` and no longer
  offers `zawiera`;
- boolean columns leave the global text search, which the approved contract
  restricts to text columns and whose placeholder states the count.

`uuid` and `json` deliberately stay in the text family: the contract defines no
separate operator set for them, and `zawiera` over an identifier is useful.
`_portal_database_render_family()` remains presentation-only and separate.

## 9. Background exports

`_portal_database_snapshot_to_params()` rebuilds a queued export's filters, so
every new operator has to survive that round trip or a background export would
silently disagree with the view it was requested from. Value-less operators,
`in (...)` value lists and `between` bounds are all carried.

Snapshots written before the `between` record existed stored a two-sided range
as separate `gte` and `lte` records keyed by the same column, where the second
overwrote the first and a bound was lost. Those are now merged back into one
inclusive range.

## 10. Progressive enhancement

Every control is a real GET form or a real link. With scripting off a user can
sort, add a filter, edit one, remove one and clear them all; the column menus
and the filter panel are `<details>` elements that open natively, and menu forms
carry the rest of the view state forward as hidden inputs.

`api/static/js/data-grid-filters.js` adds only what markup cannot express:

- one menu open at a time;
- dismissal that **discards** pending menu edits (`Esc`, outside click, table
  scroll), by restoring the form to its server-rendered state;
- focus into the menu on open and back to its header on close;
- `Esc` closing the **topmost** layer only — the menu before the panel;
- operator-aware value controls;
- `Znajdź kolumnę` narrowing of the panel's add-a-filter list;
- single-submit protection so one `Zastosuj` cannot become two conflicting
  requests, with the control keeping its width — released again on a
  back-forward-cache restore, so browser `Back` lands on a usable Apply.
  (Both the guard and its release are corrections applied in `S4`.)

The script owns no authorization, no operator allowlist and no SQL semantics.
Anything it produces is validated exactly like a hand-written URL.

## 11. Security contract

Unchanged by this stage and asserted directly:

- dataset and client authorization, and `can_view_rows`;
- `can_filter_rows` fails closed — with it off the filter and search UI is
  **absent** (`DB-62`), and every operator, old and new, is refused from a
  forged URL;
- only catalog-approved filterable columns can be filtered, through any
  operator, including the value-less ones;
- only catalog-approved sortable columns can reach `ORDER BY`;
- `cols` still narrows only;
- identifiers are catalog-sourced and quoted; **every** value is a bound
  parameter;
- read-only client connections and the statement timeout;
- export permission and scope;
- the database-access audit record still names every filtered column and carries
  no operator or value.

## 12. Not in this stage

No distinct-value picker, no numeric histogram, no threshold marker and no
aggregate distribution endpoint — those arrived in stage `S4`; see
`docs/25_database_explorer_value_distributions.md`. This stage left the design's
reserved space for them empty rather than filling it with invented output.
Column reorder, resize, pinning, autofit, named column sets, the row-detail
drawer and range/TSV copy remain `S5`–`S7`. Below the docked breakpoint the
panel becomes a full-height overlay, but the rest of the `RSP-002` drawer
programme is `S10`.

No schema migration was introduced.

## 13. Tests

```bash
cd /opt/log-platform
env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_column_centric_filtering.py
```

The suite covers the operator vocabulary, every `puste` variant, range and
multi-value validation, the immediate-versus-staged split, chips and the count
badge, canonical sort URLs, progressive enhancement, and the security
invariants. Menu and panel behaviour is exercised by running the shipped script
under `ops/tests_manual/data_grid_filters_harness.js` rather than by reading it.

Related suites that also cover this surface:
`test_portal_database_table_first.py`,
`test_portal_database_explorer_phase2a/2b/2c.py`,
`test_portal_database_export_phase3c.py`, `test_portal_database_async_exports.py`.
