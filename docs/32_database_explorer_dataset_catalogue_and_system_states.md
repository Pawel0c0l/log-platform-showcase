# 32 — Database Explorer: dataset catalogue and system states

Durable reference for the approved **dataset catalogue** (`DB-001` / `DB-002`)
and the Database Explorer **system states**: the two zero-row states and the
culprit contract (`DB-010`), the data-source error state and its diagnostic trio
(`DB-011`), the deep-link permission state, the malformed-view state, and the
loading/pending experience.

This is the ninth implementation slice of the approved redesign (stage `S9`),
built on the export panel in
`docs/31_database_explorer_export_panel_and_background_states.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screens `DB-001`, `DB-002`, `DB-010`, `DB-011`,
`PRODUCT_BEHAVIOR_CONTRACT.md` §2.1 and §2.14–2.17, `SCREEN_STATE_MATRIX.md`,
`TABLE_AND_DATA_GRID_SPEC.md` §6, `COPY_AND_TERMINOLOGY.md` §9.1, criteria
`DB-3`, `DB-55`–`DB-62`.

**No schema change.** `S9` adds no migration, no table, no column and no
persisted preference.

---

## 1. The catalogue is an authorization surface first

`/user/database` renders exactly the datasets
`_list_accessible_portal_database_datasets_for_user` returns, and that helper is
unchanged: effective dataset `can_view_rows` (direct or active group), dataset
active, client active, and effective client `can_view_database`.

Everything the page shows — the comparison table, the rail, the counts, the
permission badges, the search-free navigation — is derived from that one list.
There is no second source, so the rail cannot surface a dataset the table would
not.

**Absence, not disablement.** A dataset the account may not open does not appear
as a greyed row, does not carry a lock badge, does not contribute to a count, is
not in the rail, and leaks through no label. This is a different fact from a
*feature* being withheld:

| Situation | Presentation |
|---|---|
| Dataset not granted | **absent** everywhere |
| Dataset granted, `can_filter_rows` false | listed, neutral badge `Bez filtrów`; filter controls **absent** on the row sheet |
| Dataset granted, `can_export_rows` false | listed, neutral badge `Tylko podgląd`; export panel **absent** on the row sheet |

A withheld permission is a configuration fact rendered in a neutral badge, never
in the negative palette and never as a disabled control (`PBC` §2.1).

### No physical identity

The catalogue exposes product metadata only. No database name, no schema, no
table name and no connection identity reaches it. (The row sheet's context bar
still shows the physical table, which the approved `PBC` §2.2 composition
requires there; the catalogue does not.)

## 2. Structure (`DB-3`)

One comparison **table**, one row per dataset — never a card grid:

| Column | Source |
|---|---|
| `Zbiór` | dataset display name + description |
| `Klient` | client display name + client code |
| `Wiersze` | `count(*)` on the authorized client database (§3) |
| `Kolumny` | approved visible columns, S6 technical identity excluded |
| `Uprawnienia` | `can_filter_rows` / `can_export_rows` as neutral badges |
| action | `Otwórz arkusz` |

A 288 px **rail** groups the same datasets by client, each entry carrying the
dataset name and its row count and linking by ordinary dataset id.

The approved design also reserves a `Zapisane widoki` column and saved-view
chips. Saved views need server-side persistence, which is stage `S13`. The
column and the chips are therefore **absent** rather than faked; nothing on this
page pretends to remember anything.

The `Zasady dostępu` prose lives here, which is exactly why the row sheet
carries none above the table (`DB-2`).

## 3. `Wiersze` and the count bound

`DB-3` requires datasets to be comparable on row count, so the catalogue counts.
`_portal_database_catalogue_row_counts` issues one
`SELECT count(*) FROM <schema>.<table>` per listed dataset, grouping datasets by
client database so each database is opened **once**. Identifiers come from the
dataset catalog and are quoted; there are no bound values because there is no
predicate.

Two bounds keep a navigation page from becoming a scan:

- past `PORTAL_DATABASE_CATALOGUE_COUNT_LIMIT` (12) authorized datasets **no
  count runs at all** and every row renders the unknown marker;
- every statement carries the existing 15 s statement timeout on the read-only
  connection.

Any failure — an unresolvable database name, a refused connection, a timeout —
degrades that dataset's count to `?`. A catalogue that cannot count is still a
usable catalogue; one that invents a number is not, and `0` in particular is a
claim this product must never make by accident.

## 4. Zero authorized datasets

Its own state, with the approved copy: *"Nie masz przypisanych zbiorów
danych"* and who grants access. Not an empty table with headers, and no rail.

The approved action set names `Poproś o dostęp`. The repository has no
request-access route, mailbox or workflow, so rendering that button would be a
dead affordance. The state names the administrator as the grantor instead; the
button arrives when the capability behind it does.

## 5. Empty results: two states that must never be confused

Both keep the **table header rendered** (`DB-55`, `TGS` §6) — the user keeps the
shape of the data, which is what separates "nothing matched" from "it broke" —
and both keep the client context bar, the active chips and the current layout.

```
query applied no conditions,  OR  the dataset has 0 rows
        -> "Ten zbiór nie ma wierszy"     (blames nothing)

query applied conditions and the dataset has rows
        -> "Brak wierszy dla aktywnych filtrów"   (DB-010)
```

A dataset that is genuinely empty gets the empty-dataset message **even when
filters are set**: no filter can be responsible for a result that has no rows to
exclude, and blaming one would be a guess.

## 6. `DB-010` culprit contract

The approved sentence is a claim about the data:

> Zbiór ma ⟨48 213⟩ wierszy. Filtr `⟨Baza = KRK-02⟩` zawęża wynik do zera — bez
> niego zobaczysz ⟨1 274⟩ wiersze.

so it is answered with query evidence, never with a guess from control order.

For each **active validated constraint**, `_count_portal_database_rows_excluding`
counts the same authorized query with exactly that one constraint removed:

| Evidence | State |
|---|---|
| exactly one constraint restores rows | name it; offer `Usuń ⟨chip⟩` + `Wyczyść wszystkie` |
| several restore rows independently | `Wynik zerują niezależnie różne filtry…` — no filter named |
| none restores rows alone | `Żaden pojedynczy filtr nie odpowiada za pusty wynik…` |
| not analysed, or a count failed | `Aktywne filtry zawężają wynik do zera.` |

Only the first case licenses naming a culprit. Where the evidence does not
support uniqueness, the state says what the evidence does support and nothing
more.

### Semantic exclusion, not key deletion

Exclusion reuses the `S4` mechanism: the **whole** requested filter state is
validated first, and the candidate is dropped from what that validation
*emitted*. Removing parameters before validating would let a malformed filter
vanish silently and the count answer over a broader population — which here
would mean blaming an innocent filter for a result an invalid one produced. An
invalid filter therefore still fails validation and the diagnostic returns an
error rather than a number.

Because exclusion keys on the validated filter's target column it covers every
parameter family that column can be filtered through, including the legacy
`date_from`/`date_to` range. `S9` extended the same mechanism to the global text
search, which the builder now emits through the shared helpers under the
reserved name `__search__`, so search can be tested and named as a culprit in
the approved search terminology.

### Bound

Cost is bounded by the number of **active constraints**, never by the width of
the dataset: one count each, capped at
`PORTAL_DATABASE_CULPRIT_MAX_FILTERS` (6). Past that the state degrades to the
generic message and issues **no** queries. There is no per-column query, no
per-value query and no combinatorial subset search.

### Not a disclosure surface

The diagnostic answers exactly one question — *which approved active constraint
emptied this authorized result?* — using the same dataset, the same approved
column universe, the same canonical builder, the same read-only connection and
the same statement timeout. Every user value stays a bound parameter. The only
fact it can produce is a row count for a query the user could already run by
removing a filter in the UI. No new endpoint was added.

### User-facing names

The culprit is named with the chip text the toolbar already renders — approved
business label, operator vocabulary, value — never a physical column name and
never an internal parameter name such as `filter_exact__…`.

`Usuń ⟨chip⟩` reuses the chip's own removal URL, so it clears that filter and
resets to page 1 while preserving sort, density, page size and the whole `S5`
column layout. `Wyczyść wszystkie` reuses the existing reset URL with the same
guarantee.

## 7. `DB-011` data-source error

When the authorized client database cannot answer, the sheet renders the
approved error state **inside** itself: header, context bar, toolbar and chips
all survive, and the state replaces the rows.

- badge `BŁĄD ŹRÓDŁA DANYCH`
- title `Nie udało się odczytać zbioru`
- body that states the concrete cause where known and states explicitly that
  *permissions and dataset configuration are correct*
- **diagnostic trio**: timestamp · `ref <16 hex>` · `kod <CLASS>`
- actions `Ponów` · `Zawęź filtrami` · `Skopiuj referencję`

| Class | `kod` | Body |
|---|---|---|
| statement timeout | `DB-TIMEOUT` | names the 15 s limit on the client database |
| connection/config failure | `DB-UNAVAILABLE` | the client database is not responding |
| anything else | `DB-SOURCE` | the query to the client database failed |

The response status stays **502**: the approved state is rendered in the body,
but the status keeps saying the upstream did not answer.

**Never presented as zero rows.** With no answer from the source, both counts
render as unknown (`?`), the pager keeps the requested page rather than claiming
a total, the export panel is absent for that render (it could not state a
truthful scope count), and the row drawer is not attempted. An operational
failure reported as "0 wierszy" would be a wrong answer, not a degraded one.

**Nothing internal escapes.** No SQL, no DSN, no host, no exception class, no
physical schema/table, no traceback and no raw filter value reaches the page.
The exception class is recorded in the audit trail; the page carries the
reference.

### Error reference

`uuid4().hex[:16]`, generated server-side per failure. It is written into the
`database_rows_failed` audit event as `error_reference` alongside the existing
sanitized metadata, so support can correlate a user's reference to the failure
without storing anything new about the query. Timestamp, reference and code are
**never** read from query parameters — a support reference cannot be spoofed
through the URL.

`Ponów` is a link to the same canonical view. There is no auto-retry and no
background retry.

## 8. Permission and malformed-view states

Three failures the design keeps strictly apart:

| State | Meaning | Status |
|---|---|---|
| `BRAK DOSTĘPU` | the account may not open this dataset | 404 |
| `NIEPRAWIDŁOWY WIDOK` | the account may open it; the requested view is not one it has | 400 |
| `BŁĄD ŹRÓDŁA DANYCH` | the account may open it, the view is valid, the source failed | 502 |

An authorization failure is never collapsed into a retryable source error, and a
source error never reads as a permission problem.

**The permission state is deliberately anonymous.** It names no dataset, no
client, no column count and no capability, and it is byte-identical for an
unauthorized id and an unknown one — the existing indistinguishability is
preserved rather than traded for more specific copy. It offers one safe route
back to the authorized catalogue.

**The malformed-view state never echoes the rejected value.** `page`, `limit`,
`sort`, `direction`, filter and date parameters are attacker-controlled; the
state renders approved generic copy plus a link to the dataset's default view.
Validation itself is unchanged — a rejected view is still rejected, and a
validation failure never becomes silent query widening.

A fourth, quieter case: a dataset whose catalog approves no column yet is
neither a bad request nor a failure, and gets its own honest message.

## 9. Loading / pending (`DB-59`)

The application stays server-rendered. `js/data-grid-states.js` marks the sheet
busy for the lifetime of a navigation the browser has **already accepted**:

- a form submit inside the sheet (filter `Zastosuj`, search)
- a click on a sheet link that really navigates (sort, pagination, page size,
  column reload, row drawer)

An in-place density switch raises nothing, because it never requeries.

While pending, the table viewport carries `aria-busy="true"`, static skeleton
blocks are drawn from the rendered table's own `<col>` widths — as many rows as
the page shows, or the page size when it shows none — and the counters render
`…`. No shimmer (`INTERACTION_SPEC` §10). Cell values are never copied into a
skeleton.

**Truthfulness.** The skeleton is never server-rendered: a completed response
carries no `db-skeleton` and no `aria-busy`. The flag lives only in the DOM of a
document that is about to be replaced — never in the URL, never in
`localStorage`.

**History.** On `pageshow` and `pagehide` the flag is cleared, so a Back/Forward
restore of this exact DOM yields a usable page rather than a stuck skeleton.
`init()` also clears it on every fresh document.

**No new architecture.** Nothing is fetched, parsed or rendered from a response;
the module introduces no client-side data layer. The `S3` duplicate-submit guard
still owns control state and still releases on `pageshow`; the pending state
disables nothing, so editable controls keep working. With JavaScript
unavailable every trigger remains an ordinary form or link and the server
navigation is unaffected.

## 10. Copy

Every user-facing string added or replaced in this stage resolves through
`api/portal_ui/i18n.py`, verbatim from `COPY_AND_TERMINOLOGY.md` where that
document names the situation. The situations it does not name — the empty
dataset, the ambiguous-culprit fallbacks, the Database Explorer permission and
malformed-view states — follow its tone rules: name the cause, state the number,
keep "you lack permission" separate from "the system failed".

The last English sentence on the row sheet, *"Export is not enabled for your
access to this dataset"*, is replaced by the keyed Polish copy in the approved
permission vocabulary (`Tylko podgląd`). The capability semantics are unchanged:
no form, no submit, and the server still refuses a forged POST.

## 11. What `S9` did not change

- No migration, no saved views, no named column sets, no persisted preference,
  no dataset favourites, no recently-used list.
- No expansion of what any account may read: authentication, active-user rules,
  client grants, dataset grants, `can_view_rows`, `can_filter_rows`,
  `can_export_rows`, the hidden `S6` row identity and the approved visible column
  set are all untouched.
- No change to the 15 s statement timeout.
- No global responsive/accessibility pass — that is stage `S10`.

## 12. Verification

`ops/tests_manual/test_portal_database_catalogue_and_states.py` and the
browserless DOM harness
`ops/tests_manual/data_grid_states_harness.js` cover: catalogue authorization and
absence, the comparison table and rail, the bounded read-only counts and their
unknown degradation, the zero-dataset state, the deep-link permission state and
its indistinguishability, the malformed-view state and its refusal to echo input,
both empty states, all five culprit evidence positions, the query shape of the
diagnostic (count, identifiers, parameterization, one-candidate exclusion), the
error state with its trio and its audit correlation, reference anti-spoofing, and
the pending state's triggers, `pageshow` behaviour and absence of persistence.
