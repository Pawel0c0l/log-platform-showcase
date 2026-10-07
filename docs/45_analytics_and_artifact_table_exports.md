# 45 — Table exports: Artifact Explorer and the Analizy panel

Durable reference for the exports added to every table an operator or analyst
reads on screen but previously could not take away: the **Artifact Explorer**
catalogue and folder tables, and the four **Analizy** (Eco Driving) tables —
periods, ranking, driver composition and driver progression.

Before this change exactly one table in either surface exported: the Eco
contributing-trip table. The Database Explorer's export panel (`DB-009`,
`docs/31`) was and remains a different, richer surface with row/column scopes
and a background lifecycle; nothing here changes it.

---

## 1. The one property these exports are built around

```
THE FILE IS THE VIEW — the whole filtered result, not the visible page,
                       and no column the screen does not render.
```

Everything below is a consequence of that sentence.

**Not the page.** Every export drops `page`/`limit`/`offset` and keeps the
filters and the validated sort. A file holding the fifty rows that happened to
be on screen would look correct and be wrong. Each export bar states the row
count of the current filtration, so the number is visible *before* the click.

**Not a wider row set.** Every export runs the **same query the table ran**,
through the same permission predicate. Artifact Explorer pages
`_list_artifact_browser_items(..., action="view")`; the Eco exports call the
same `EcoDrivingApiService` methods their pages call, so the same permission
pair, client scoping and audit event apply. No export re-implements
authorization, because a second gate is a second thing to keep in step.

**Not a wider column set.** An export is exactly where "the page shows a safe
subset" gets bypassed by serialising the underlying row dict. So:

| Surface | What holds the columns together |
|---|---|
| Ranking table | `ranking_view_models.columns(unit)` renders the header, the cells **and** the file; a cell's clipboard value is `text_cell(col.value(entry))`, the same expression the CSV writer uses |
| Driver detail | `composition_rows` / `progression_rows` build the rows the panel renders and the file contains; `composition_order` is one function, so both agree on metric order |
| Artifact Explorer | `_artifact_explorer_export_columns()` is asserted equal to the rendered `<th>` set by `test_artifact_explorer_export.py`; `storage_key`, `bucket_name`, `metadata_json`, `owner_user_id` and `content_type` are in every row the query returns and in neither the table nor the file |

**Machine values, not screen values.** A file gets the full SHA-256 and a byte
count where the screen shows `9f2c41ab8e7d…` and `4,0 MB`; km as a number with
the unit in the heading, because `36,00 km` pastes into Excel as text and cannot
be summed. Stated rather than discovered: **the file does not look identical to
the page.** This is the trade the trip export already made (`trip_export.py`).

---

## 2. Scope resolution — the part that has failed before

The Eco surfaces exist in two period modes, and an export must route to the one
its page used. `UI-20260827-05` is what happens otherwise: a link built by
reading `period_key` back off a rendered payload worked on the persisted-period
page and silently failed on the ranking-basis page.

So the **page states the scope** (`trip_view_models.ExportScope`) and the
renderer takes it as an argument:

* `month` present → the basis methods (`get_basis_ranking`,
  `get_basis_ranking_entry`), with the **canonical** weeks the server resolved,
  never the submitted order, and never a `period_key` that would widen a week
  selection to a whole month;
* otherwise → the persisted-period methods, with `period_key`;
* neither → **422 with the reason**, never an invented scope.

The filename is built from the resolved scope and never from the query, with a
per-table prefix (`ranking_`, `kierowca_`, `okresy_`, `przejazdy_`,
`artifacts__`) so a folder of downloads stays readable.

---

## 3. Ceilings — refuse, never truncate

| Surface | Ceiling | Above it |
|---|---|---|
| Eco tables | `trip_export.MAX_EXPORT_ROWS` = 50 000 | `413` naming the row count **and** the limit |
| Artifact Explorer | `ARTIFACT_EXPLORER_MAX_EXPORT_ROWS` = 50 000 | `413` naming both, after **one** query |

Neither surface gains a background-export lifecycle. That is `DB-007`'s, it is
scoped to Database Explorer, and inventing one here would be a product-model
change rather than an export.

A silently short spreadsheet is the worst outcome for something a person is
about to do arithmetic on, so both refuse loudly instead.

---

## 4. What is offered where

| Table | Export bar | Route |
|---|---|---|
| Artifact catalogue | above the table, after the filters | `GET /artifact-explorer/export` |
| Artifact virtual folder | above the folder's artifact table | `GET /artifact-explorer/folders/{folder_id}/export` |
| Eco periods (landing) | above the period table | `GET /user/eco-driving/periods/export` |
| Eco ranking | between the toolbar and the table | `GET /user/eco-driving/rankings/export` |
| Driver composition | in the panel head, beside the unit toggle | `GET /user/eco-driving/ranking-entry/export?table=composition` |
| Driver progression | in the panel head | `GET /user/eco-driving/ranking-entry/export?table=progression` |
| Driver trip evidence | in the panel head | the existing `…/trips/export` — see below |

Both formats are offered as plain links (`XLSX`, `CSV`); no JavaScript module is
involved, so the Database Explorer grid layer still does not load on an
Artifacts page and the `S11` asset boundary in `docs/34` is intact.

**Zero rows → no buttons.** An enabled button producing a header-only file is a
worse answer than an absent one.

**The trip-evidence panel does not get its own export.** It is a 25-row preview
of the contributing-trip set, its caption already states the true total, and its
download is that total through the endpoint the full trip surface uses. A second
export producing a 25-row file would give one question two answers.

**A table that is not on screen is not in a file.** A non-qualified period
renders an insufficient-distance state instead of a composition table, and
`?table=composition` there returns `409` rather than the metrics that state
exists to withhold. An unrecognised `table` is `400`, never a default.

---

## 5. Boundary this crosses, deliberately

`docs/34` records that stage `S11` added **no new API surface** to Artifact
Explorer, and `PRODUCT_BEHAVIOR_CONTRACT.md` §5 / `SCREEN_CATALOG.md` `ART-001`
describe an inspect-only operator table whose only row action is `Inspekcja`.

Two read-only GET routes now exist there. **Owner decision, 2026-09-08:** the
approved design scoped export to `DB-009` (Database Explorer) alone, and the
owner asked for export on every table in Artifact Explorer and the Analizy
panel. What `S11` established is otherwise untouched — no artifact lifecycle
state, no ownership, no mutation, no saved views, no product-taxonomy move, and
the shared-shell and asset contracts unchanged. Reading rows the account may
already read, in a different container, is the whole of the addition.

---

## 6. Deterministic coverage

| Suite | What it holds |
|---|---|
| `ops/tests_manual/test_artifact_explorer_export.py` | column/table parity, unrendered fields absent from both formats, whole-filtered-result paging, RBAC rows absent from the file, ceiling, format contract, machine values, export-bar links, folder branch |
| `ops/tests_manual/test_eco_driving_analytics_table_exports.py` | ranking file = ranking table, clipboard = file for every cell, unit-aware metric columns, basis vs period-key routing, canonical weeks in the link, detail table refusals, periods export, format contract |
| `ops/tests_manual/test_artifact_explorer_visual_modernization_s11.py` and the existing Eco suites | that the `_ranking_table` refactor onto the column enumeration changed no rendered byte |
