# 37 — Portal server-side preferences and saved Database Explorer views

Durable reference for the approved **account-scoped persistence** slice (stage
`S13`): the per-account theme override (`D-011`, `SH-8`), named **saved
Database Explorer views** (`D-007`), and named **column sets** (`DB-30`).

This is the thirteenth implementation slice of the approved redesign, built on
the Database Explorer state model established in `docs/23`–`docs/26`, the hidden
row identity in `docs/29`, the export surfaces in `docs/31` and the catalogue in
`docs/32`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— `UNRESOLVED_DECISIONS.md` `D-007` and `D-011`,
`TABLE_AND_DATA_GRID_SPEC.md` §5.2–§5.4,
`PRODUCT_BEHAVIOR_CONTRACT.md` §1.4, §2.1, §2.7 and §2.18,
`COPY_AND_TERMINOLOGY.md` §2–§3, `COMPONENT_CATALOG.md` (saved-view chip,
saved-view selector), criteria `SH-7`–`SH-10` and `DB-30`.

> **Migration status.** `S13` adds
> `db/migrations/066_portal_account_preferences_and_saved_views.sql` and its
> correction
> `db/migrations/067_portal_saved_object_structural_integrity.sql`.
> **Both were applied to production on 2026-08-19**, as part of the Portal V1
> rollout chain `064 → 069`; the production platform migration ceiling is `069`.
> §9 records the rollout order that was executed.

---

## 1. What S13 persists, and what it deliberately does not

`D-007` resolved where view state lives, and `S13` is the part of that answer
that needs a database:

| State | Lives in | Owner |
|---|---|---|
| Filters, search, sort, page, page size, visible columns, order, widths, pins, open row | **URL** | `docs/24`, `docs/26`, `docs/29` |
| Named saved views | **server**, per account per dataset | this document |
| Named column sets | **server**, per account per dataset | this document |
| Density | **browser** | `docs/23` |
| Theme override | **server**, per account | this document |

Nothing else was added. There is no sharing, no group ownership, no ACL, no
version history, no folders and no favourites: the approved design defines saved
objects as private to their owner, and a column that could later mean "shared"
would be an authorization surface nobody has approved.

`S13` also does **not** build any part of `S14` (global `⌘K` search) or `S15`
(Report Explorer). It establishes the account-scoped CRUD pattern those stages
may reuse; it pre-builds none of their entities.

> **Release position.** `S13` is `DEPLOYED` — implementation-closed, independently
> accepted, and live in production release `956dea3766b1` since the Portal V1
> rollout. `S15` is likewise `DEPLOYED`. `S14` remains
> `DEFERRED_POST_INITIAL_RELEASE` by owner decision — still designed and still in
> the roadmap, but not a first-release blocker. See
> `docs/38_portal_initial_release_plan.md` §6 for the verified production state.

## 2. Schema (migrations 066 and 067)

Three relations, all additive. 066 creates them by the repository's
`IF NOT EXISTS` convention; **067 is what makes the resulting structure a stated
post-condition** rather than a hope — see §2.3.

### `portal_user_preferences`

| Column | Type | Notes |
|---|---|---|
| `user_id` | `UUID` PK | `REFERENCES artifact_users(user_id) ON DELETE CASCADE` |
| `theme` | `TEXT NOT NULL DEFAULT 'auto'` | `CHECK (theme IN ('auto','light','dark'))` |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**An absent row is AUTO.** That is why the migration needs no backfill and why
every pre-existing account is valid the moment it applies.

**Why a table and not a column on `artifact_users`.** `artifact_users` is read
by the session path on every authenticated request. Application code naming a
not-yet-migrated column there would fail *login*, not one feature. A separate
relation makes the migration-absent case detectable and confines its blast
radius to the theme control.

### `portal_database_saved_views` and `portal_database_column_sets`

Identical shape, different payload contract:

| Column | Type | Notes |
|---|---|---|
| `saved_view_id` / `column_set_id` | `UUID` PK | `DEFAULT gen_random_uuid()` |
| `owner_user_id` | `UUID NOT NULL` | `REFERENCES artifact_users(user_id) ON DELETE CASCADE` |
| `dataset_id` | `UUID NOT NULL` | `REFERENCES portal_database_datasets(dataset_id) ON DELETE CASCADE` |
| `view_name` / `set_name` | `TEXT NOT NULL` | non-empty, `char_length <= 80` |
| `view_state_json` / `layout_state_json` | `JSONB NOT NULL` | object, `octet_length(::text) <= 8192` (067) |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

* `UNIQUE (owner_user_id, dataset_id, <name>)` — names are unique **per account
  per dataset**, and exactly as typed after trimming. Uniqueness is
  case-**sensitive**: the approved copy defines no case rule, and refusing
  `Trasy` because `trasy` exists would be a surprise the design does not ask
  for.
* `idx_portal_database_{saved_views,column_sets}_owner (owner_user_id,
  dataset_id, <name> ASC)` — the listing path; the unique constraint indexes the
  lookup path.
* `ON DELETE CASCADE` on both foreign keys. A deleted account and a deleted
  dataset both make the object meaningless and unreachable by any authorization
  query. **This is not the same as a revoked grant** — see §7.

### 2.3 Migration 067 — structural integrity and the byte-exact bound

`CREATE TABLE IF NOT EXISTS` is the repository's additive convention, and it has
one property this stage cannot live with on its own: a **pre-existing
same-named relation** is silently accepted. The statement is skipped, the
migration reports success, and S13 activates against a relation that may have no
owner foreign key, no cascade, no uniqueness and no payload bound — which is the
whole of its authorization and integrity story. The column names are all present
in that state, so a column-name probe declares it ready.

067 removes that outcome. It:

1. **refuses to stand in for 066** — an absent prerequisite relation raises
   `42P01` rather than half-creating a schema;
2. **completes additively what can be completed** — a missing column, a missing
   or differently-defined constraint, a missing or wrongly-defined index. Every
   constraint and index it owns is dropped by its authorized NAME and re-added,
   so a same-named wrong definition converges instead of surviving;
3. **raises on what it cannot complete without reinterpreting data** — a
   required column of the wrong type (`view_state_json` as `TEXT`,
   `owner_user_id` as `TEXT`) fails the migration;
4. **fails rather than weakening a rule** — `ADD CONSTRAINT` validates existing
   rows, so a payload that breaks the corrected byte bound fails the migration
   instead of being granted an unenforced constraint;
5. **asserts its own post-conditions** before committing — every constraint by
   name, every foreign key cascading, both listing indexes.

It runs in one transaction, drops no relation, drops no column and deletes no
row.

**Why a second file rather than an edit to 066.** `ops/db_migrate.sh` keys
`public.schema_migrations` by FILENAME and SKIPs a file it has already applied.
Editing 066 would therefore be silently ineffective on every database that had
already run it — including the disposable and development instances S13 was
built against — while looking correct in the repository. Migration history stays
append-only and the **local sequence 066 → 067** is what establishes the
authorized structure on a database in any prior state.

**The payload bound is 8192 UTF-8 BYTES.** 066 wrote `char_length(...) <= 8192`,
which counts Unicode code points, while the design and the application both
called the limit "8 KiB". A payload of 8192 Polish or CJK characters passes a
code-point bound and occupies well over 8 KiB. 067 states the rule as
`octet_length(...) <= 8192` and `_portal_saved_state_json` measures
`len(encoded.encode("utf-8"))`, so the application and the database refuse
exactly the same documents. The **name** bound stays in characters: a name is a
label a person types, and the approved copy bounds it at 80 characters.

### 2.4 Runtime schema readiness

`_portal_s13_schema_state()` classifies the schema this deployment is actually
running against, and it compares **definitions**, not names:

| State | Meaning | Behaviour |
|---|---|---|
| `ready` | all three relations carry the authorized column types, nullability, defaults, constraint rules and listing indexes | full S13 |
| `absent` | none of the three relations exists | documented pre-S13 tolerance (§9) |
| `incompatible` | something S13-shaped exists but is not the authorized structure — including a constraint whose NAME is right and whose RULE is not | S13 surfaces are not offered; this is an **operational fault**, not a deployment that predates the feature |
| `error` | the platform database could not answer | operational fault |

A name is not a contract. A relation can carry every expected column and a
constraint carrying every expected name while the rule behind that name points
at the wrong relation, covers the wrong columns, does not cascade, accepts a
theme the renderer cannot express, or bounds the payload in code points instead
of bytes — and every one of those rules is something the application's own
statements rely on: `ON CONFLICT (owner_user_id, dataset_id, view_name)` needs
that exact uniqueness, an insert that omits `saved_view_id` needs that default,
and owner-scoped authorization needs the cascade. The probe therefore verifies,
against migration 067 as the source of truth: column type, nullability and the
exact default expression the inserts depend on; each constraint's kind, column
list, foreign-key target relation and columns, `ON DELETE` action and validation
state; for every CHECK, the whole canonical expression; that no unauthorized
primary key, uniqueness rule or CHECK exists on those relations; and the listing
indexes' column order, uniqueness and freedom from predicates or computed
columns.

**A CHECK is decided by its whole expression, never by fragments of it.**
Matching a constraint by the tokens its text contains — the expected literal
set, or required and forbidden substrings — cannot see the direction of a
comparison, because the inverted rule is written with the same tokens.
`octet_length(state::text) >= 8192` names `octet_length` and `8192`,
`theme NOT IN ('auto','light','dark')` names all three approved themes, and
`jsonb_typeof(state) <> 'object'` names `jsonb_typeof` and `'object'`; each of
those enforces the opposite of the authorized rule while satisfying every
fragment test. The probe therefore compares
`pg_get_expr(pg_constraint.conbin, conrelid)` — PostgreSQL's deparse of the
STORED PARSE TREE, not the source text — against the one canonical form
migrations 066 then 067 produce, whitespace-normalized and case-sensitive.
Because it is a deparse, `theme IN (...)` and `theme = ANY (ARRAY[...])`
collapse onto one string while a different rule cannot. The expected forms are generated from
`PORTAL_THEME_MODES`, `PORTAL_SAVED_OBJECT_NAME_MAX_CHARS` and
`PORTAL_SAVED_OBJECT_STATE_MAX_BYTES`, so the limit the server enforces and the
limit it demands of the database cannot drift apart, and
`test_portal_s13_persistence_postgres.py` asserts each of them equals the
migrations' own catalog output on a disposable PostgreSQL 16.

The cost of that exactness is that a schema written differently but meaning the
same is called `incompatible` rather than `ready`. That is the safe direction:
S13 declines to activate against a schema it did not authorize.

**The deparsed text names a function; it does not identify one.** `pg_get_expr`
renders through the CALLER's `search_path` — a function prints unqualified
exactly when the caller's path makes THAT function the visible one — so the
whole-expression comparison alone was decided partly by a session setting the
probe does not control. Reproduced on PostgreSQL 16.11 with a hand-written
`evil.octet_length(text)` returning `1`, installed under the authorized
constraint name:

| caller `search_path` | authorized 066+067 rule | the `evil` imposter |
|---|---|---|
| `"$user", public` | `octet_length(...)` | `evil.octet_length(...)` |
| `evil, pg_catalog` | `pg_catalog.octet_length(...)` | `octet_length(...)` |

Under `evil, pg_catalog` the imposter deparsed to the exact expected string and
the probe answered `ready` for a database with no payload bound — a shadowed
schema accepted a 20 009-byte document — while the authorized schema deparsed
qualified and was called `incompatible`. `pgcrypto` reproduces the second
column without an attacker at all: it installs a `public.gen_random_uuid()`
beside the `pg_catalog` one, so `search_path = public, pg_catalog` made the
authorized default render as `pg_catalog.gen_random_uuid()`.

Two mechanisms close it, and they fail in opposite directions:

1. **A fixed deparse context.** The probe pins `search_path` to `pg_catalog`
   for its own transaction (`set_config(..., is_local => true)`), so the table
   above collapses to its left column whatever the caller's path was. Should the
   setting ever fail to apply, the authorized schema renders qualified and is
   called `incompatible`; it cannot produce a false `ready`.
2. **Catalog identity, with no text in it.** PostgreSQL records a `pg_depend`
   row from a CHECK constraint or a column default to every function, operator,
   type and collation its parse tree references — except pinned system objects,
   which dependency recording skips. So "built entirely out of `pg_catalog`
   builtins" is exactly "has no such dependency row", stated in OIDs. One row is
   `incompatible`. This reads no expression text and consults no session
   setting, so it is what actually prevents a false `ready`.

Neither is a list of blessed function names. `test_portal_s13_persistence_postgres.py`
runs the probe `358145bc` shipped and the current one against the SAME
disposable database for shadowed `octet_length`, `jsonb_typeof`, `char_length`
and `gen_random_uuid` identities, across five caller `search_path` settings: the
old probe answers `ready` for all four under `evil, pg_catalog`; the current one
answers `incompatible` under every path, and `ready` for the authorized schema
under every path.

**Bounded.** One connection, one transaction-local `set_config`, and four
catalog reads — columns with type, nullability and default expression;
constraints with kind, columns, foreign-key target, delete action, validation
state and deparsed expression; indexes with column order, uniqueness, validity
and predicate/expression presence; and the `pg_depend` identity read, which
returns nothing at all on a correct schema — each restricted to the three S13
relations in `public`. Comparing whole expressions instead of fragments adds no
query and no row: it is string equality over data those reads already returned.
No stored row is read. The pinned `search_path` is transaction-local, so it is
discarded on commit or rollback and cannot travel to a later request over a
reused connection; a test drives the probe twice over one connection that is
never closed and reads the caller's own `search_path` back unchanged. The probe reads `pg_class` / `pg_attribute` / `pg_attrdef` /
`pg_constraint` / `pg_index`, which always exist, so an exception from the probe
is a database fault by construction and is never read as a missing migration.

`_portal_preferences_schema_available()` is `state == ready`, so the three
non-ready states all withhold the S13 controls — but they remain
distinguishable, and only `absent` is allowed to mean "this deployment predates
S13".

## 3. Theme: the account row is the durable authority

Allowed values are exactly the approved three, `auto` · `light` · `dark`, using
the repository's canonical identifiers (identical to `MODES` in
`api/static/js/theme.js`). AUTO is the absence of an override and is the default.

**Precedence, from the top:**

1. the **account row**, when the server can read it. `_portal_layout` resolves it
   through `_get_portal_user_theme_state(user_id) -> (theme, scope)` and puts it
   on `<html>` before the first byte, so there is no flash and no dependence on a
   browser value that may belong to a different account;
2. the **browser-local mirror**, only where no account preference exists — the
   sign-in screen, or a database confirmed not to have migration 066;
3. AUTO, which follows `prefers-color-scheme`.

**Three authority scopes, because there are three situations.** Collapsing the
last two is what let a database outage hand an authenticated account's durable
preference to whatever the browser happened to be holding:

| `scope` | Rendered on `<html>` | When | `theme.js` |
|---|---|---|---|
| `server` | `data-theme-scope="server"` + `data-theme-mode` | the account row answered (an absent row is AUTO, and is a durable account fact) | takes the server value; **overwrites** the mirror with it |
| `local` | *(no marker)* | nobody is signed in, or `portal_user_preferences` is CONFIRMED not to exist | pre-S13 browser-local behaviour, unchanged |
| `unavailable` | `data-theme-scope="unavailable"` + `data-theme-mode` | an authenticated account whose preference could NOT be read: a database fault, a permission denial, a partially migrated schema | renders the neutral default and **neither reads nor writes** the mirror |

`unavailable` exists because the mirror is per-BROWSER and carries no account
identity. Reading it while account B's own preference is unreadable would let B
silently adopt whatever account A last chose on that machine and present it as
B's durable state. The server remains the authority; it simply cannot answer, so
the page renders AUTO and the control states that a choice is not stored.

**"Confirmed" means confirmed.** PostgreSQL reports `42P01` both for a relation
that does not exist and for one whose SCHEMA the role cannot reach, so the
SQLSTATE alone cannot separate "this database predates S13" from "this role was
locked out of it". On the failure path the absence is therefore re-checked
against `pg_catalog`, which is readable regardless of schema privileges, and
only a catalog-confirmed absence enters the pre-S13 tolerance. Every other
failure — `42501`, `53300`, `57014`, `40001`, a connection failure, an
undefined COLUMN on an existing relation — is `unavailable`.

**Reconciliation, and why account switching is safe.** When the document
declares server scope, `theme.js` takes the server value as current and
**overwrites** the local mirror with it. It never reads the mirror to decide. So
signing into account B in a browser where account A chose dark renders B's own
preference and rewrites the mirror to B's value; returning to A restores A's.
The mirror holds one key, one theme mode, and **no account identity** — it is a
per-browser cache, never a cross-account authority.

**Interaction.** The switcher is a real `<form method="post">` to
`/user/preferences/theme`, so it works without scripting; `theme.js` intercepts
the submit and performs the same write in place. Switching therefore performs no
navigation and no reload, and route, query string, URL-held filters and scroll
position all survive (`SH-9`). Light and dark remain the same geometry
(`SH-10`).

**Failure is stated, never implied.** `_set_portal_user_theme` returns whether
the row was written. On failure the scripted path receives
`{"stored": false}` (HTTP 503) and announces
`shell.theme.save_failed` in a live region without updating the mirror; the
no-script path gets a 503 page carrying the same sentence and a link back.
`SH-8` promises the choice follows the account to another device, and a write
that did not happen must not be presented as one that did.

**Write ordering: the last accepted choice wins.** Preference writes are
SERIALIZED and carry only the latest intent — at most one request is in flight,
and while it is, a further choice REPLACES the queued one instead of starting a
second request. Two overlapping writes could otherwise be applied by the server
in the order they ARRIVED rather than the order they were made, leaving the
account row on an earlier choice than the one the user is looking at (click
dark, click light, the light write lands first, the dark write lands second, the
account is dark and the screen is light). A response that is no longer the
user's current intent also never writes the mirror and never sets the status at
the moment it arrives: it describes a choice the user has moved on from.

**Last confirmed durable mode.** A superseded response must not overwrite a
newer optimistic choice, but it still carries knowledge — the server said what
it durably stored. `theme.js` remembers that value; it starts as the mode the
server rendered the page with, and each accepted write advances it to the mode
the **server** reports having stored (`{"stored": true, "theme": …}`), not to
the mode the client happened to send. If the LATEST intent then fails, the
visible state, the browser mirror and the pressed control are reconciled onto
that last confirmed durable value and the failure is stated. Without it, an
earlier write that succeeded plus a latest write that failed left three
different answers to one question: screen on the failed choice, mirror on the
page's original value, account row on the superseded one.

So once the interaction settles there is exactly one answer. If the latest
intent was persisted, it is final everywhere. If it was not, the screen, the
mirror and the account row are all the last preference the server confirmed, and
the status line says the choice was not stored. The mirror still holds only
values the server confirmed, still carries no account identity, and is still
never consulted as durable account state.

## 4. Saved views

A saved view is a **named, account-owned, dataset-scoped snapshot of canonical
Database Explorer view state**. It is not a query, not a URL and not a grant.

### Stored contract

`_portal_database_canonical_view_state` builds the document, and it builds it
from the same functions a page render uses — `_build_portal_database_rows_query`
for the validated filter set and `_portal_database_resolve_column_layout` for
the layout. There is **no second query grammar**.

```json
{
  "state_version": 1,
  "filters": [{"column_name": "...", "operator": "...", "value": "..."}],
  "search": "...",
  "sort": "...", "direction": "asc|desc",
  "page": 1, "limit": 100,
  "layout": {"cols": [...], "colorder": [...], "colw": {"col": 180}, "colpin": [...] | null}
}
```

`filters` is the normalized record shape the background-export snapshot already
round-trips; `_portal_database_canonical_filter_records` is the one place that
produces it, shared by both so they cannot drift.

**Page and page size are carried** because `D-007` names both as view state and
a saved view is defined as a named record that resolves to such a URL. **Density
is not**, because `D-007` assigns it to the browser.

**Never stored:** the S6 opaque row reference or any row identity, the
row-fields mode, the S7 rectangular selection or clipboard state, export scope
and job state, loading state, one-shot panel intents (`colpanel`, `colsel`,
`dbfilters`), staged-but-unapplied filter edits, SQL, and any URL.

`colpin` is `null` when the user never chose (the transitional default in
`docs/26`) and a list otherwise, **including the empty list**, which is the
explicit "nothing pinned".

### Save, reopen, update, delete

A save POST carries **no state document in its body**. It posts to the sheet's
own state expressed as ordinary query parameters, and the handler re-parses them
through exactly the pipeline a page render uses. So "the state that was saved"
is by construction "the state the server would have rendered", and state the
sheet would refuse to render is never stored.

Save → reopen → re-derive is a **fixed point**: the document the server writes
is the document the reopened URL produces. Without that the selector's
`modified` state would report differences the user never made.

| Action | Route | Approved copy |
|---|---|---|
| Save current view | `POST /user/database/datasets/{id}/views` | `Zapisz jako widok` |
| Open / apply | `GET /user/database/datasets/{id}/views/{view_id}/open` | chip on `DB-001`, entry in the `DB-003` selector |
| Overwrite (and correct the name) | `POST /user/database/datasets/{id}/views/{view_id}` | `Zapisz zmiany` |
| Delete | `POST /user/database/datasets/{id}/views/{view_id}/delete` | `Usuń` |

The `DB-003` selector reports the approved three states: at rest it names
`Zapisane widoki`; with a view open it names that view; and it adds
`zmieniony` when the current canonical document differs from the stored one. The
open view is referenced by a `view=<id>` query parameter, which is a
**reference, not state** — it selects nothing, reaches no query, and an id that
does not resolve for this account on this dataset is dropped from the canonical
parameters before anything is rendered.

### Revalidation on open (`J`)

Opening re-resolves the dataset through
`_get_portal_database_dataset_for_user`, re-reads the approved column universe
through `_get_portal_database_visible_columns`, and re-parses the document
through the current parsers. Then:

| Stale condition | Behaviour |
|---|---|
| Stored filter names a column that is no longer approved or no longer filterable | **fail closed** (`stale_column`) |
| `can_filter_rows` revoked and the view carries filters or a search | **fail closed** (`filtering_revoked`) |
| Stored sort names a column that is gone | **fail closed** (`stale_column`) |
| Stored operator/value the current parser refuses | **fail closed** (`stale`) |
| Malformed, truncated, non-object or wrong-`state_version` payload | **fail closed** (`stale`) |
| Stored layout names columns that are gone | **normalised** — intersected with the approved universe |

The asymmetry is deliberate and is the security-relevant half of the design:
**dropping a filter would broaden the result** into a question the user never
asked, so it is a refusal; hiding a column is presentation, and the approved
universe is the ceiling either way.

#### Lossless filter representation, and the fixed point that proves it

A stored filter must reopen with **exactly** its stored meaning, and the way
that is guaranteed is a fixed point rather than a second hand-written
conversion.

`_portal_database_saved_filter_params` converts each record into live request
parameters and validates it strictly first: the record shape, the column against
today's approved universe, `is_filterable`, the operator against
`PORTAL_DATABASE_FILTER_OPERATORS` **and** against the operators the column's
type family offers, the value/`values`/`value_to` presence rules for that
operator, the `in` cardinality and duplicate rules, and one record per column.
Anything that cannot be expressed with its stored meaning refuses the whole
view.

`in` values are emitted through **`filter_exact__`**, the one parameter the S3
parser takes verbatim. `filter__` is the *human textarea* form: it is trimmed,
split on newlines and has blank entries dropped, which is right for someone
typing and wrong for replaying a stored value. Through it `' Kowalski'` becomes
`'Kowalski'`, a whitespace-only value disappears entirely, and a value holding a
newline becomes two — and when a value list empties, the whole constraint
vanishes and the view reopens as a **broader** query than the one that was
saved. Leading and trailing spaces, whitespace-only text, the empty string,
commas, wildcard characters, newlines and non-ASCII are all values a column can
hold, and the distinct-value picker already relies on `filter_exact__` to filter
a displayed value back to itself byte for byte.

Single-value operators keep `filter__`, which is the only parameter the parser
reads for them. That is sound because the parser already trimmed the value on
the way in, so a canonical stored value is its own trimmed form and re-emitting
it is a fixed point. A stored value that is NOT its own trimmed form could not
have come from the live parser, cannot be reproduced through it, and is refused.

Finally the built parameters are run through the real
`_build_portal_database_rows_query` and canonicalized again, and the result must
EQUAL the stored filter set and search. Any drift — a trimmed value, a collapsed
list, a constraint the parser declined to emit — is a refusal **before a single
row is read**. That equality is also what makes the selector's `zmieniony` state
honest: reopening a saved view unchanged cannot report a change, because the
comparison is between the same canonical document on both sides.

**A refusal reads nothing.** The refusal is decided during the open route, which
issues no dataset query at all, and the redirect it emits carries no filter
parameter of any kind. "The saved view was not opened" is the outcome; "the
saved view was opened without its filter" is not a state the code can reach.

**The background export replays through the same contract.** A queued export
stores the same canonical filter records (`_portal_database_canonical_filter_records`
is the one grammar both write), and
`_portal_database_export_snapshot_params` converts a stored snapshot back
through `_portal_database_saved_filter_params` and the same fixed-point check,
so `visible query semantics == exported query semantics`. The earlier export
converter rebuilt the request by hand — trimming values, dropping the ones that
emptied and emitting `in` lists through `filter__` — so a view holding an exact
whitespace-bearing value DISPLAYED one population and EXPORTED a broader one.
A snapshot that cannot replay with exactly its stored meaning now fails the
export closed: the worker refuses at the query stage and
`_portal_database_iter_export_rows` refuses before opening a client-database
connection, with a user-safe sentence that names no column, operator or value.
Pre-`between` snapshots that stored a two-sided range as separate `gte` and
`lte` records for one column are merged into today's single record first; any
other second record for one column is not representable and is refused.

The emitted redirect is built by `_portal_database_url` from validated
parameters and the dataset id the route resolved. **No URL is ever stored, read
or followed**, so a saved view cannot become an open redirect.

### Catalogue integration (`DB-001`)

`Zapisane widoki` renders as inline chips that are direct entry points. The
chip list is queried for **exactly the already-authorized dataset list the page
is about to render**, so a dataset the account cannot currently open contributes
no chip, no name and no count — its saved views are not merely hidden, they are
never queried. Without the S13 schema the column is **absent**, not an empty
column pretending the feature exists.

## 5. Named column sets

A column set stores the reusable **column-layout state only**:

```json
{"state_version": 1, "cols": [...], "colorder": [...], "colw": {...}, "colpin": [...] | null}
```

No filters, no search, no sort, no page and no density. That distinction is the
point: `saved view = broad Database Explorer view state`, `column set = reusable
column-layout state`.

`Zestawy ▾` lives in the `DB-008` column panel, alongside the
`Domyślne kolumny` entry `TGS` §5.3 requires — which is the existing
column-layout reset, not a second reset model. It sits **outside** the panel's
own form, because saving and deleting a set are their own POSTs and forms cannot
nest.

**Apply semantics.** Applying replaces the layout family wholesale (so a set
that pins nothing does not inherit the current view's pins) and **preserves
every unrelated row-view fact**: validated filters, the global search, sort,
direction, page size **and the page number** all survive. Changing which columns
are on screen must not silently change which rows are — and `page` is row-view
state, not layout. A user reading page 7 who applies a column set stays on page
7; the rows a page holds do not depend on the column layout, so there is nothing
for a reset to protect.

**Authorization safety.** `_portal_database_saved_layout_params` intersects
every identifier with today's approved column universe, re-clamps widths through
the S5 bounds, and hands the result to `_portal_database_resolve_column_layout`
— so the pin ceiling, the pinned-width bound and the at-least-one-visible rule
remain exactly as authoritative for a stored set as for a hand-written URL. A
set whose columns have all disappeared falls back to the approved set, never to
zero columns, and the user is told with `set_narrowed`.

## 6. Ownership and IDOR

Every S13 endpoint is an authorization boundary, and all of them share one rule:
**the account comes from the signed session, never from the request.** No S13
route accepts an owner or user parameter, and every object statement carries
`owner_user_id = %s` in its own `WHERE` — there is no load-then-check anywhere,
because a load-then-check leaks existence through timing, through error shape
and through whatever the check forgets.

Consequently a caller cannot enumerate, load, rename, overwrite, delete, or
infer the name, dataset or existence of another account's object. Every such
probe returns the same `not_found`, identical to an object that never existed;
a non-UUID id is answered as absence before it reaches the driver. A denied
probe records no metadata about the other account's object.

### 5.1 The per-owner per-dataset object cap is atomic

One account may keep **50 saved views and 50 column sets per dataset**. The
approved design states no number; this is a repository-consistent bound in the
spirit of the other Database Explorer caps, and it is enforced with a stated
refusal rather than by silently dropping the oldest.

The cap is a constraint over a SET of rows, and PostgreSQL has no such
constraint. Under READ COMMITTED two transactions that both count 49 both see a
true answer and both commit, leaving 51; folding the count into the INSERT's
`WHERE` does not help, because the subquery reads the same snapshot. The
read-then-write pair is therefore made mutually exclusive for the scope it
applies to, with a transaction-scoped advisory lock keyed by
`(relation, owner, dataset)` — `pg_advisory_xact_lock(hashtext(...),
hashtext(...))`.

That needs no new relation, no counter row to keep consistent, no serializable
isolation for the whole request and no retry loop, and it is released by COMMIT
or ROLLBACK with no unlock path a failure could skip. Two different owners, two
different datasets and the two object types never contend with each other, and a
hash collision could only make two unrelated scopes take turns — it can never
let one scope exceed its cap, because every writer in a scope takes the same
key. The quota bounds **creation** only: rename and update stay available at the
cap.

## 7. Dataset scoping and revoked grants

Creating and opening both require current access to the dataset. **The existence
of a persisted object is never proof of current permission.** When a grant is
revoked the object is no longer visible or actionable — the dataset resolves to
the generic unavailable state, indistinguishable from a dataset that never
existed — but the object is **not deleted**: a temporary grant change is not a
reason to destroy user data, and the approved handoff requires no such deletion.

## 8. Audit

Twelve event types are registered in `PORTAL_AUDIT_EVENT_TYPES`:
`portal_theme_preference_updated` / `_refused`, and
`database_saved_view_` / `database_column_set_` `created` / `updated` /
`deleted` / `opened` (`applied` for sets) / `refused`.

Metadata is **safe facts only**: object type, operation, dataset id, the
resulting theme enum, the saved object's own id, the outcome, and state
*counts* (`filter_count`, `visible_column_count`, `pinned_column_count`).

Never recorded: a saved query string, any filter value, the search text, the
object's **name**, the whole payload, or any row identity. A denied IDOR probe
records no fact about the other account's object.

## 9. Rollout order — required

**Migration first, then code.**

> **Executed.** This order ran as part of the Portal V1 rollout on 2026-08-19, inside the wider
> platform chain `064 → 065 → 066 → 067 → 068 → 069 → code` that `ops/db_migrate.sh` necessarily
> applies (`docs/38` §6). What follows is `S13`'s own dependency, kept because it states why the
> order matters — not because the step is outstanding.

1. apply `db/migrations/066_portal_account_preferences_and_saved_views.sql` and
   then `db/migrations/067_portal_saved_object_structural_integrity.sql` to the
   platform database — `ops/db_migrate.sh` applies them in filename order, so
   running it once is the whole step;
2. then deploy the application.

The reverse order does not break the portal — `_portal_preferences_schema_available()`
and the per-call guards degrade to the pre-S13 behaviour (browser-local theme,
no saved views, no column sets), which is the same convention migration 043
established for background exports. That tolerance exists so a mixed-version
window is survivable; **it is not a permanent fallback**, and it must not be
read as making the migration optional. A deployment left in that state simply
does not have `SH-8` or `DB-30`.

**The tolerance applies to a CONFIRMED absence only.** `absent` means none of
the three relations exists, verified against `pg_catalog`. A partially applied
migration, a structurally incompatible relation, a permission denial and an
unreachable database are all operational faults: they withhold the S13 controls,
but they are never presented as "this deployment predates the feature", and they
never let the browser-local mirror act as an account's durable theme (§3).

**066 alone is not the target state.** It creates the relations; 067 is what
makes the structure a stated post-condition. A database that ran only 066 over a
pre-existing same-named relation can be structurally wrong while reporting
success, and the runtime probe classifies exactly that as `incompatible`.

**Rerun semantics.** `ops/db_migrate.sh` applies each file at most once, keyed by
filename, so neither migration is re-run in normal operation. 067 is nevertheless
written so that a second execution is a no-op on a correct schema and a repair on
an incomplete one. Full rerunnability is not the goal; the requirement is that
neither a re-execution nor a partial prior state may be accepted while the
resulting schema is wrong.

`S13` declares **no entry in `db/schema_requirements.json`** and no release
capability, exactly like the S8 export migration: the portal degrades when the
relation is absent, so binding release activation to it would refuse a release
over a colour preference.

**No rollout was performed by this change.** The migrations it introduced were applied later, by the
separately authorized Portal V1 rollout: `MIGRATIONS_066_067_APPLIED_TO_PRODUCTION` (2026-08-19),
`PRODUCTION_PLATFORM_MIGRATION_CEILING = 069`.

## 10. Verification

| Suite | What it owns |
|---|---|
| `ops/tests_manual/test_portal_server_preferences_and_saved_views.py` | The response and canonicalization contract: migration shape, the theme enum and account isolation, the failed-write contract, saved-view content and stale-state handling, IDOR at the response level, column-set apply semantics, catalogue visibility, hostile names and payloads, audit content, the stage boundary. |
| `ops/tests_manual/test_portal_s13_persistence_postgres.py` | The statements themselves, against a real PostgreSQL 16: the 066+067 sequence and its re-execution, structure, foreign keys and cascade rules, indexes, the theme/name/payload constraints including the byte-exact bound, owner-scoped CRUD, duplicate-name refusal on create and rename, the per-dataset bound, eight malformed same-named relations repaired or refused, the count-then-insert race reproduced and then held at 50 under eight concurrent creates, and absent / correct / partial / permission-denied / unreachable proved distinct; and the expression-identity evidence — shadowed `evil.octet_length` / `jsonb_typeof` / `char_length` / `gen_random_uuid` run through both the `358145bc` probe and the current one on the same database across five caller `search_path` settings, plus the transaction-local `search_path` leak test. |
| `ops/tests_manual/theme_engine_harness.js` | The shipped `api/static/js/theme.js`, executed: the three authority scopes, the account-isolation contract on the browser mirror, and the serialized last-intent write ordering under reordered and failing responses. Driven by the response suite. |
| `ops/tests_manual/disposable_postgres.py` | The provisioner the PostgreSQL suite runs on, self-testable. |

The PostgreSQL suite **starts its own** `postgres:16` container on a free
loopback port, asserts the server really is major version 16, creates a database
of its own inside it, and removes the container in a `finally`. It takes **no
DSN**: the previous shape reset whichever database `PORTAL_S13_TEST_DSN` named,
which made an operator's exported DSN decide whose `public` schema got dropped.
There is now no input that can point it at a database it did not create. It
needs `.venv/bin/python`, a working Docker daemon and a local `postgres:16`
image; without them it prints `LIVE_MIGRATION_TEST_NOT_AVAILABLE` and exits 0. It
never authorizes running anything against production.

No live browser is available in this environment, so
`LIVE_BROWSER_VERIFICATION_NOT_AVAILABLE` still holds for the theme engine: the
harness proves the shipped script's logic, not a real browser's event loop.

## 11. Copy

Verbatim from `COPY_AND_TERMINOLOGY.md`: `Zapisany widok` / `Zapisane widoki`,
`Zestaw kolumn` / `Zestawy ▾`, `Zapisz jako widok`, `Zapisz zmiany`,
`Zapisz jako zestaw`, `Domyślne kolumny`, `Zastosuj`.

The handoff defines no copy for the field labels, the empty states or the
refusal messages. Those are recorded additions, derived from the approved nouns
and routed through the translation catalogue like every other string (`D-010`):
`Nazwa widoku`, `Nazwa zestawu`, `Brak zapisanych widoków.`,
`Brak zapisanych zestawów kolumn.`, `zmieniony`, `Usuń`, the `db.saved.*`
refusal sentences, and `shell.theme.save_failed`.
