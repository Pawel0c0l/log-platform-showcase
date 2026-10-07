# 25 — Database Explorer: value distributions

Durable reference for the aggregate surface inside the Database Explorer column
menus: the distinct-value picker, the numeric histogram, the authorization that
governs both, the faceted scope the counts describe, the bounds, and the
on-demand loading model.

This is the fourth implementation slice of the approved redesign (stage `S4`),
built on the column-centric filter menus in
`docs/24_database_explorer_column_centric_filtering.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screen `DB-005` §4, `TABLE_AND_DATA_GRID_SPEC.md` §3.2 / §3.3 / §5.1.

---

## 1. What this adds

| Column family | Distribution | Source |
|---|---|---|
| text (incl. uuid, json) | distinct-value picker, up to 50 values with counts | `TGS` §3.2 |
| numeric | 14-bucket histogram with min/max labelled | `TGS` §3.3 |
| boolean, date/timestamp | **none** — identity count only | `TGS` §5.1 item 4 |

Every column menu also gains the identity's non-null count for the current
result set (`TGS` §5.1 item 1), which is why boolean and date columns still
issue one cheap aggregate even though they show no distribution.

## 2. This is a disclosure surface

A value/count list returns no rows but still describes client data, so the
endpoint re-derives the full authorization rather than trusting that the UI only
renders menus for eligible columns:

1. portal session (route);
2. `_get_portal_database_dataset_for_user` — client grant, dataset grant and
   `can_view_rows` in one gating query;
3. `can_filter_rows` — value discovery is filter reconnaissance, so viewing is
   not enough;
4. the column must be in the dataset's approved **visible** set **and** marked
   **filterable**.

Only after all four is any client-database statement built.

**Every authenticated refusal is byte-identical** — same `404`, same message —
for an inaccessible dataset, a dataset without filtering, and a column outside
the approved set. Otherwise the endpoint would answer "does this column exist?"
for anyone who asked.

An unauthenticated request is a different question and keeps the platform's
normal `401`. It discloses nothing, because the route refuses before any dataset
or column is resolved.

## 3. Endpoint

```
GET /user/database/datasets/{dataset_id}/distribution?column=<name>&<scope>
```

One route for all families. The caller supplies **only** the column; the
aggregation mode is resolved from the dataset catalog's declared type, and the
bucket count and row limit are module constants:

- `PORTAL_DATABASE_DISTINCT_VALUE_LIMIT = 50`
- `PORTAL_DATABASE_HISTOGRAM_BUCKETS = 14`

Neither is readable from the URL. A `mode=`, `buckets=` or `limit=` parameter is
simply ignored, so no part of the `GROUP BY`, the aggregate expression or the
bound can come from the browser.

The response is JSON containing only what the menu renders: values or buckets
with counts, the distinct/non-null/scope counts, min and max as exact decimal
text, a truncation flag, and a numeric `domain` state. It never carries the
physical table, the client database name, SQL, catalog metadata or an error
detail.

## 4. Scope: faceted, and stated

The aggregate runs over the **current effective view** — the same validated
conditions the row query uses, obtained from
`_build_portal_database_filter_conditions` rather than reimplemented — with one
deliberate exception:

> **The column's own filter is excluded from its own distribution.**
> Every other filter, and the global text search, still apply.

This is `scope: "filtered_excluding_column"` in the payload, and the menu states
it in words: *"Liczby uwzględniają pozostałe filtry, ale nie filtr tej
kolumny."*

The written handoff does not define this case. It was decided as faceted because
the alternative makes two approved behaviours impossible:

- `TGS` §3.2 makes the picker multi-selectable and turns a selection into an
  `in (…)` filter. Under literal scoping, once that filter is applied the picker
  would show only the values already chosen, so a value could never be added.
- `TGS` §3.3 says the threshold is visible *before* the filter is applied —
  "this is the point of the histogram". A domain clipped by the column's own
  filter cannot show where a new threshold would land.

The trade-off is that when a column is filtered, its own counts do not sum to
the visible row count. That is why the scope is stated rather than left to be
inferred; counts that silently disagreed with the toolbar counter would be
worse than counts that explain themselves. **If the design owner intends literal
scoping instead, this is the one decision to revisit.**

The global search stays applied even to the inspected column, because it is a
cross-column predicate, not that column's filter.

### Validate first, exclude second

Order matters, and it is the opposite of the obvious one:

```
raw request → full canonical validation → validated filters
            → semantic exclusion of the inspected column → aggregate WHERE
```

The builder takes an `exclude_column` argument and applies it **after** that
column has been validated exactly like every other. Deleting the column's
parameters before validating — which is what this stage first did — meant a
malformed filter on the inspected column vanished silently and the aggregate
answered over a broader population than the rows. A filter the canonical builder
rejects must refuse the distribution, never widen it.

Exclusion is **semantic**, keyed on the validated filter's target column rather
than on a list of raw parameter names. That is what makes it cover every form a
column can be filtered through — `op__`, `filter__`, `filter_from__`,
`filter_to__`, `filter_in__`, `filter_exact__`, the `dateop__` family, and the
**legacy `date_from`/`date_to` default-date range**, which reaches the builder
under the default date column's own name. Inspecting that column excludes the
legacy range; inspecting any other column keeps it. The row browser's legacy
date compatibility is untouched.

## 5. Categorical query

One statement, one scan:

```sql
WITH base AS (SELECT CAST(<col> AS TEXT) AS value FROM <schema>.<table> WHERE <scope>),
     grouped AS (SELECT value, count(*) AS value_count FROM base GROUP BY value)
SELECT g.value, g.value_count,
       (SELECT count(*) FROM grouped) AS distinct_count,
       (SELECT count(value) FROM base) AS non_null_count,
       (SELECT count(*) FROM base) AS scope_rows
FROM grouped g
ORDER BY g.value_count DESC, g.value ASC NULLS LAST
LIMIT %s
```

`CAST(… AS TEXT)` is not cosmetic. The `in (…)` operator compares
`CAST(col AS TEXT)`, so grouping the same way guarantees that selecting a value
filters exactly the rows it was counted from. It also lets `json` columns group
at all.

**Bounded, and honest about it.** `distinct_count` is exact, so a column with
more than 50 distinct values reports `truncated: true` and the menu says
*"Pokazano 50 z n wartości — lista jest niepełna"*. Fifty is never presented as
all of them.

### NULL, empty string and whitespace

`GROUP BY` keeps all three apart, and the payload preserves that: `null`, `""`
and `"   "` are three separate entries with three separate counts. The S2
requirement that these be distinguishable holds inside the picker.

In the UI they are treated differently on selection, because `in (…)` compares
text and can express neither NULL nor a "both" case:

- a whitespace-only value is ordinary text and is selectable into `in (…)`;
- the NULL and empty-string rows show their counts and offer the approved
  `puste` operator instead, which is defined as exactly NULL-or-empty.

No new filter semantics were invented for the picker.

### Lossless selection: `filter_exact__`

The `in (…)` operator now accepts two input forms that resolve to one semantic
result through one parser and one query builder:

| Parameter | Form | Parsing |
|---|---|---|
| `filter_in__<col>`, repeated `filter__<col>` | human | one value per line, trimmed, blanks dropped |
| `filter_exact__<col>` | exact | one value per parameter, **verbatim** |

The exact form exists because the picker can display and count values the human
form cannot carry back: whitespace-only text, leading or trailing spaces, and
embedded newlines all survive a `splitlines()` + `strip()` round trip as
something else, or as nothing. A value the picker counted must filter back to
exactly the rows it was counted from, so the picker submits one hidden
`filter_exact__` input per value and the server prefers that form when present.

Both forms deduplicate, both are capped at
`PORTAL_DATABASE_MAX_IN_VALUES = 50`, both reject rather than truncate beyond
it, and both end as bound parameters — the exact form is a list of query
parameters, never a serialized blob and never SQL. Existing hand-written and
no-script URLs keep the human semantics unchanged.

### The selection survives the fetched list

The picker holds the **whole** staged selection, not just what is on screen. It
seeds itself from the server's validated record (`data-db-selected`), so:

- values already selected are shown checked;
- values selected but absent from the fetched top 50 are preserved, and their
  count is stated;
- checking a value extends the selection; unchecking removes only that one;
- exceeding 50 is refused with the cap stated, and the previous valid selection
  stands.

Rebuilding the selection from the visible checkboxes — the stage's first
implementation — silently discarded every selected value the current top set did
not happen to contain.

### Local search over the fetched set

The picker has a search field that narrows the **already fetched** rows. It
issues no request, changes no applied filter, and hiding a selected row neither
deselects it nor drops it from staged state. The partial-list notice stays
visible, because searching a truncated list still searches only that list — not
the database.

## 6. Numeric query

One statement, one scan:

```sql
WITH base AS (SELECT CAST(<col> AS numeric) AS value FROM <schema>.<table> WHERE <scope>),
     bounds AS (SELECT count(*) AS scope_rows, count(value) AS value_count,
                       min(value) AS min_value, max(value) AS max_value FROM base)
SELECT b.scope_rows, b.value_count, b.min_value, b.max_value, g.bucket, g.bucket_count
FROM bounds b LEFT JOIN LATERAL (
  SELECT least(width_bucket(base.value, b.min_value, b.max_value, %s), %s) AS bucket,
         count(*) AS bucket_count
  FROM base WHERE base.value IS NOT NULL AND b.max_value > b.min_value GROUP BY 1
) g ON TRUE
ORDER BY g.bucket ASC NULLS LAST
```

Three details carry weight:

- **`CAST(… AS numeric)`, not float.** Bucket edges are derived from these
  bounds, and a double-precision round trip could move one. Edges are computed
  in Python with `Decimal` and returned as plain decimal text.
- **`least(…, 14)`.** `width_bucket` puts the maximum value in bucket 15,
  because its upper bound is exclusive. Merging it into 14 is what makes the
  bucket counts sum to the non-null count.
- **`LEFT JOIN LATERAL`.** The bounds row survives even when there is nothing to
  bucket, so the degenerate domains return a defined answer instead of an empty
  result set.

### Defined domain states

| `domain` | Condition | Rendering |
|---|---|---|
| `empty` | no rows in scope | a sentence, no bars |
| `all_null` | rows exist, every value NULL | a sentence, no bars |
| `single_value` | min == max | a sentence naming the value, no bars |
| `range` | min < max | 14 buckets |

NULLs are excluded from buckets and reported separately as `null_count`. No
degenerate case fabricates a bar.

## 7. On-demand loading

The initial page contains **containers only** — no values, no counts, no
buckets. `api/static/js/data-grid-distribution.js` fetches a column's
distribution the first time its menu opens, and never again for that menu.

This matters because the S3 sheet already grew the server-rendered DOM: embedding
distributions for 42 columns up front would put thousands of nodes on a page for
menus most users never open. The guarantees:

- no aggregate query during page rendering;
- one bounded request per opened menu;
- no prefetch, no N+1, no per-column query on load.

A late response for a superseded request is discarded (per-container request
token), so a slow column cannot paint over a menu the user has moved on from.

A container is also marked in flight for the duration of its request, so closing
and reopening a menu while its aggregate is still running does not fire a second
one. The flag is cleared on failure, which is what keeps a retry possible.

A failure renders an error line inside the distribution area only: the menu's
operator, value field and `Zastosuj` keep working, and reopening retries because
a failed request is not cached.

## 8. Interaction stays S3's

Everything the picker and histogram do is **staged**. Selecting values writes
`op__<col>=in` plus the newline value list into the menu's existing fields;
clicking a blank row sets `op__<col>=blank`. The menu's own `Zastosuj` is still
what applies them (`D-005`).

The histogram is informative. Its threshold marker follows what is typed in the
value fields, recomputed in the browser with no request and no change to applied
state — which is what lets the threshold be seen *before* the filter is applied
(`DB-10`). Buckets are not clickable; the approved contract does not give them
an interaction.

Strings reach the script as a translated JSON `data-` attribute on the
container. The row sheet ships no inline `<script>` and this stage did not
change that.

## 9. Progressive enhancement

Distributions are supplemental discovery and are JavaScript-dependent by design.
Without scripting the S3 menus are unchanged: operator select, value fields, a
real GET form and a real submit. Nothing about authorization or query semantics
depends on the browser.

## 10. Audit

Every request records `database_distribution_viewed`, `database_distribution_denied`
or `database_distribution_failed` with the user, client, dataset, the approved
column name and the aggregation mode, plus which columns the surrounding view
filtered — the same column-name-only convention the row browser uses.

Two fields, two trust levels, and they are not interchangeable:

- `column` and `filter_keys` are **approved dataset columns**. They are
  populated only from a catalog column the request actually resolved and from
  filters the canonical builder actually accepted. A denied or rejected request
  contributes nothing to them, so caller input cannot be written into metadata
  that later readers treat as catalog truth.
- `requested_column` is **caller input**, named as such, mirroring the existing
  `requested_dataset_id` convention, and still gated on the identifier shape so
  the audit trail cannot become a free-text sink.

No filter values, no operators and no aggregate results are logged.

## 11. Observed cost

Measured read-only against a representative production dataset
(`client_trips`, 809 620 rows, 4.3 GB database) inside a `READ ONLY`
transaction with the production 15 s statement timeout:

| Aggregate | Time | Result |
|---|---|---|
| categorical, unfiltered | 472 ms | 50 of 1 476 distinct values, 406 041 non-null |
| numeric histogram, unfiltered | 1 167 ms | 13 non-empty buckets; bucket counts sum exactly to 629 931 non-null values |
| categorical under one filter | 74 ms | filtered scope |

Well inside the timeout, and one such query per opened menu. No index was added
and none is required at this size; if a much larger dataset is onboarded, an
index on a frequently inspected column would be the first thing to consider —
that is a future performance note, not a schema change this stage makes.

## 12. Accepted S3 review corrections shipped here

Two defects the independent S3 review raised, both fixed in this stage:

- **`Wyczyść wszystkie` preserves every visible column.**
  `_portal_database_reset_url()` read the repeated `cols` parameter as a scalar
  and kept only the last value, so clearing filters silently hid every other
  column the user had chosen. It now copies the whole list. Covered in the S3
  suite, which owns that helper.
- **The submit guard recovers after browser Back.** `initSubmitGuard()` disables
  `Zastosuj` for the lifetime of a navigation; the back-forward cache restores
  that exact DOM, so `Back` landed on a page whose Apply no longer worked. A
  `pageshow` handler now releases the guard on a `persisted` restore only — an
  ordinary load does not release it, so duplicate-submit protection during the
  submission itself is unchanged.

## 13. Not in this stage

No cache, no materialized distribution, no schema change of any kind. No search
refinement inside the picker beyond the initial bounded top set. Column
reorder, resize, pinning, autofit, saved views, saved column sets, the row
drawer, range selection and the export redesign remain `S5`–`S8`.

## 14. Tests

```bash
cd /opt/log-platform
env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_value_distributions.py
```

Covers aggregate authorization on every path, the generated query shape, the
faceted scope, exact categorical counts with the NULL/empty/whitespace
distinction, the 50-value bound and its truncation flag, the 14-bucket contract
with decimal-exact edges, all four numeric domain states, response hygiene,
audit content, and the on-demand rendering guarantees. It also covers the
independent-review corrections: lossless value round-trip, validation before
faceting, legacy default-date faceting in both directions, the audit trust
boundary, and the refusal semantics. Frontend behaviour — when a request fires,
loading and error states, stale-response rejection, the in-flight guard and its
retry, off-list selection preservation, the 50-value cap, local search, and the
marker — is exercised by running the shipped script under
`ops/tests_manual/data_grid_distribution_harness.js`.
