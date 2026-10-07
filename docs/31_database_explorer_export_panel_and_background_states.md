# 31 — Database Explorer: export panel and background export states

Durable reference for the **export panel** (`DB-009`) and the **background
export lifecycle** (`DB-007`): what the three row scopes and two column scopes
mean, how the execution path is decided, and how a background export is
cancelled and requeued without ever letting a stale worker publish.

This is the eighth implementation slice of the approved redesign (stage `S8`),
built on the grid selection in
`docs/30_database_explorer_grid_selection_and_clipboard.md` and the hidden row
identity in `docs/29_database_explorer_hidden_row_identity.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screens `DB-009` and `DB-007`, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.13,
`SCREEN_STATE_MATRIX.md`, `COMPONENT_CATALOG.md`, `COPY_AND_TERMINOLOGY.md` §5,
criteria `DB-47`–`DB-54`.

---

## 1. One canonical export interaction

The pre-S8 page carried a legacy `Export report` form with a format `<select>`
and a note promising that *"exports always include all approved columns"*. Both
are gone. There is now exactly one export surface, the approved `DB-009` panel,
and one submit endpoint behind it.

```
PANEL NAMES A SCOPE  ->  SERVER RESOLVES IT  ->  SERVER DECIDES THE PATH
```

The browser never supplies a row set, a column list or a count that the server
is bound by. Everything the panel shows is information; everything that matters
is re-derived by the submit handler.

## 2. Row scopes (`DB-47`)

| Scope | Polish | What it exports |
|---|---|---|
| `view` | `Bieżący widok` | the whole validated current result set |
| `dataset` | `Cały zbiór danych` | the whole authorized dataset |
| `selection` | `Zaznaczone wiersze` | the unique rows an S7 rectangle touches |

- **`Bieżący widok` is the result, not the page.** Filters, search and the
  validated sort ride along; `page` and `limit` are dropped. Exporting only the
  rows that happened to be on screen would be the most surprising possible
  reading of "current view".
- **`Cały zbiór danych` drops filters and search and keeps everything else.**
  The validated sort survives, because ordering is a separate dimension from
  narrowing and dropping it would make the file order arbitrary. Every
  authorization check is unchanged: session, client grant, dataset grant,
  `can_export_rows`, the approved column universe and the global ceiling.
- **`Zaznaczone wiersze` is a row scope, not a cell scope.** One touched row is
  one exported row however many of its cells the rectangle covered, and the
  exported *columns* come from the separate column scope. A rectangle covering
  5 rows × 2 columns with `Jak na ekranie` exports 5 rows × every displayed
  column.

An unapproved filter column (`filter__record_id=…`) does not silently drop out
of the query and widen the export: the shared query builder refuses the whole
request, on the direct path and in the snapshot alike.

## 3. Column scopes

| Scope | Polish | What it exports |
|---|---|---|
| `screen` | `Jak na ekranie` | the resolved S5 display set, in display order |
| `approved` | `Wszystkie zatwierdzone` | the whole approved user-visible catalog |

`Jak na ekranie` re-runs the same two steps the sheet runs — `cols` narrowing,
then the S5 layout resolver — against a copy of the parameters. Because both
start from the authorized column set, a forged `cols`/`colorder` can only narrow
or rearrange approved columns. It can never widen the export, and it can never
reach the technical row identity, which is not in the input at all.

The identity is excluded on both paths and in both catalog states, including
the transitional `is_visible = true` + `is_row_identifier = true` one.

## 4. Selected rows and identity

```
S7 GRID  --row positions-->  EXPORT MODULE  --opaque S6 refs-->  SERVER  --identities-->  ONE BOUND QUERY
```

The grid publishes one thing outward: a `db-selection-change` event carrying the
**positions** of the rendered rows the rectangle touches. It does not publish
references, values or column names — the export module resolves a position to
the opaque reference the server already rendered on that `<tr>`, so the
selection module stays free of both row identity and export.

The server then:

1. resolves the configured identifier column from authorized dataset metadata;
2. re-resolves each reference through the S6 AES-GCM mechanism, bound to this
   dataset, client and identifier column;
3. deduplicates identities, preserving order;
4. runs one query with `WHERE <identifier> = ANY(%s)` — the identity list is a
   single bound array parameter, never interpolated — ordered by the validated
   sort with the identifier as a deterministic tie-breaker, bounded one row above
   the selection cap so an over-match is observable rather than truncated;
5. proves, from that same result set and inside the same read transaction, that
   **every requested identity matched exactly one physical row**.

### 4.1 All or nothing

Step 5 is the rule, and it is a cardinality check rather than a row count. The
export succeeds only if the set of identities that came back equals the set that
was requested and every one of them appears exactly once. Anything else refuses
the whole export:

| Situation | Why it must refuse |
|---|---|
| an identity matched no row | it was deleted since the user selected it; exporting the rest would silently drop a selected row |
| an identity matched several rows | the identifier is not unique; the file would contain rows the rectangle never covered |
| one missing **and** one duplicated | the row *total* is right and the selection is still wrong — which is exactly why `len(rows) == len(requested)` is not the test |

This is the same all-or-nothing posture the S6 single-row lookup already takes
by keeping `LIMIT 2` instead of accepting the first match.

The identity is selected only so multiplicity can be counted, and is stripped
from every row before the rows leave the fetch. It reaches no file, header,
browser, log, audit record or error message.

Failure modes collapse to one refusal. A malformed token, a tampered token, a
token minted for another dataset, a raw identifier, a missing identity and a
duplicated identity are all the same generic *stale selection* message, because
telling them apart would make the reference an oracle. Validation runs before
any format-specific rendering, so CSV and XLSX behave identically.

**A dataset with no configured row identity cannot use the scope at all.** The
option renders unavailable with its reason stated, and a forged POST is refused.
There is no positional, offset or business-field fallback — that is the whole
point of S6.

## 5. Why the selected scope is always a direct download

The S7 rectangle cannot cross a pagination boundary, and the page size is capped
at `PORTAL_DATABASE_MAX_PAGE_SIZE` (500). A selection is therefore structurally
below the 20 000-row direct cap, so it is downloaded immediately and **never**
becomes a background job. Nothing forces every scope through one implementation,
and no opaque reference is ever persisted into job metadata.

## 6. Execution path (`DB-48`)

| Rows | Path |
|---|---|
| `0 … 20 000` | direct download |
| `20 001 … 1 000 000` | background job, 3-day retention |
| `> 1 000 000` | refused — no file, no job |
| unknown | the panel says so; the server decides on submit |

None of these numbers changed. The panel states the path, the retention window
and the ceiling **before** the user commits; the submit handler re-derives the
path from its own count regardless, so the notice is information and never a
promise. An unavailable dataset total renders an explicit unknown — it is never
replaced by the filtered count, which would assert that the filters matched
everything.

Without the background-export schema the panel says so, rather than letting the
user choose a scope that has nowhere to run.

## 7. The background job snapshot

The snapshot stores canonical validated state only:

```json
{
  "snapshot_version": 1, "format": "csv",
  "columns": ["distance_km", "driver_name"],
  "sort": "driver_name", "direction": "asc",
  "filters": [{"column_name": "driver_name", "operator": "contains", "value": "Kowal"}],
  "search": "abc",
  "row_scope": "view", "column_scope": "screen", "expected_rows": 20001
}
```

The scope is resolved *into* the columns and the filter set at request time,
which is what keeps the **worker free of scope logic** — it reads the snapshot
exactly as it did before S8. `row_scope`/`column_scope` are provenance for audit
and requeue; `expected_rows` is the denominator the determinate progress bar
needs, and a job queued without it renders indeterminate rather than inventing a
percentage.

No raw query string is stored, no pagination, no rejected token and no identity.

**Replay is lossless or the job fails closed.** The filter records are written
by `_portal_database_canonical_filter_records`, the same grammar saved views use
(docs/37 §4), and `_portal_database_export_snapshot_params` converts them back
through the same strict, fail-closed path: revalidated against today's approved
columns, `is_filterable`, `can_filter_rows` and operator vocabulary, `in` values
re-emitted verbatim through `filter_exact__`, and then put back through the real
parser, whose canonical records must EQUAL the stored ones. This is what makes
`visible query semantics == exported query semantics` a proven property: an
exact value carrying a leading space, a trailing space, whitespace only or an
embedded newline exports as itself instead of being trimmed, split or dropped
into a broader query. A snapshot that cannot replay exactly is refused at the
worker's query stage with a user-safe sentence, before any client-database
connection is opened — never executed as a wider export.

## 8. The four background states (`DB-007`, `DB-50`)

| Presented state | Internal status | Actions |
|---|---|---|
| `W toku` | `queued`, `running` | `Anuluj` |
| `Gotowy` | `completed` and inside retention | `Pobierz` |
| `Pliki wygasły` | `expired`, or `completed` past retention | `Zleć ponownie` |
| `Błąd` | `failed` | `Zleć ponownie`, `Kopiuj ref` |

Two internal statuses map onto one presented state in each direction, for
reasons the user can act on: `queued` versus `running` is worker scheduling, and
a completed job past retention has lost the file that `Gotowy` promises.

**No state renders a disabled control.** An action the backend would refuse is
absent. An expired export offers no download affordance at all.

A **cancelled** job maps to no approved state and is therefore not listed. The
design has four states; the user deliberately stopped this one; it has no file,
no progress and no action. Inventing a fifth badge would add a user-facing state
the approved design does not have. The row itself stays in
`database_export_jobs` and in the audit trail, so the history is intact.

## 9. Race-safe cancellation

`Anuluj` moves the job to the terminal `cancelled` status. **No worker fence
changes**, because every worker transition was already conditional on the status
it expects:

| Worker step | Predicate |
|---|---|
| claim | `status = 'queued'` |
| lease refresh | `status = 'running' AND claim_token = <its own>` |
| mark failed | `status = 'running' AND claim_token = <its own>` |
| publish | `status = 'running' AND claim_token = … AND lease_expires_at > now() AND attempt_object_key = …` |
| stale recovery | `status = 'running' AND lease_expires_at <= now()` |

So:

- **Cancel before claim** — the job is no longer `queued`; the worker never
  claims it.
- **Cancel while generating** — the worker loses its fence at the next lease
  refresh or at publication. It cannot publish `READY`, cannot attach an
  artifact, and cannot overwrite the terminal state with a late failure.
- **Worker publishes first** — the cancel `UPDATE` matches no row and the user
  is told the export is already ready. Exactly one terminal outcome wins.
- **Stale claim token** — changes nothing, as before.
- **Stale recovery** — conditional on `running`, so it never resurrects a
  cancelled job.

The cancel statement locks the row (`FOR UPDATE`) before deciding, is scoped by
`requested_by_user_id` so another user's job is indistinguishable from a missing
one, and — when the job was `running` — moves its attempt-object ledger row to
`cleanup_pending` in the same transaction.

### 9.0 Both outcomes are audited

`Anuluj` and `Zleć ponownie` each write an audit event on success and on
refusal — `database_export_job_cancelled`, `database_export_cancel_refused`,
`database_export_job_requeued`, `database_export_requeue_refused`. All four are
registered in `PORTAL_AUDIT_EVENT_TYPES`, which is what makes them durable:
`_create_portal_audit_event` refuses any unregistered type, and
`_portal_audit_event_safe` swallows that refusal, so an emitted-but-unregistered
event leaves no trace at all. The records carry the user, client, dataset, the
safe job reference and the outcome — never a row identity, an opaque row token,
a claim token, a lease or storage detail.

### 9.1 Object cleanup is eventually consistent, and deliberately so

The lost publication fence means a cancelled attempt's object can never be
attached to an artifact, so it is never downloadable. Removing the object itself
is a separate, **eventually consistent** guarantee, because cancellation cannot
block on an upload that may still be in flight.

The dangerous ordering is:

```
cancel  ->  sweep deletes (nothing there yet)  ->  upload lands  ->  worker exits
```

A delete that found nothing proves only that nothing was there *at that instant*.
Treating it as terminal success is what would let the object that lands a moment
later survive forever with the ledger claiming it had been cleaned. So the rule
is:

| Delete outcome | Ledger |
|---|---|
| removed a real object | terminal success — nothing can recreate it |
| found nothing, settle window still open | **stays selectable**; the next sweep looks again |
| found nothing, settle window closed | terminal success |
| the delete itself failed | stays selectable (unchanged) |

The settle window is the export lease (`PORTAL_DATABASE_EXPORT_LEASE_SECONDS`),
measured from the moment cleanup was requested. That bound is not arbitrary: it
is already the liveness bound the rest of this system trusts — publication
requires a live lease and stale recovery treats an expired one as proof the
producer is no longer authoritative — so an attempt that begins writing after it
has elapsed cannot publish what it writes either.

**Durability does not depend on the worker.** The ledger row is the retry
trigger, so the guarantee holds even when the producing process uploads the
object and then exits immediately without running any cooperative cleanup. The
worker's own post-upload cleanup is a fast path, not the mechanism.

What is *not* claimed: the object does not disappear the instant a job is
cancelled. It may exist transiently while the producer is physically finishing
its upload, and until the next sweep. What is claimed is that it cannot remain
indefinitely and cannot become downloadable.

## 10. Requeue

`Zleć ponownie` exists only on `failed` and `expired`. It **re-authorizes from
scratch** — ownership, dataset grant and `can_export_rows` are today's answers,
not the answers that were true when the original export was queued — and
**recounts**, so a stored snapshot cannot carry a scope past a ceiling that is
enforced now.

It inserts a **new job** from the stored snapshot rather than reviving the old
row. The failed or expired record is history, and reviving it in place would let
two workers believe they own one logical attempt.

## 11. App-bar indicator (`DB-49`)

`n eksport w toku` renders in the app bar of **every** portal page, which is
what lets the user leave the Database Explorer without losing track of an export
still being prepared. It is one indexed count on the **platform** database,
scoped by `requested_by_user_id`; it never touches a client business database,
never polls, and degrades to no indicator on failure. It is absent at zero — the
approved component's idle state is absent, not an empty badge — and the count is
stated in words, so the state never depends on the dot.

## 12. Copyable reference (`DB-52`)

The job id, and nothing else. It is already the public reference the queue
redirect puts in the URL, so it discloses nothing new — no storage key, no
bucket, no claim token, no lease. Copy feedback goes to a polite live region and
never contains the failure text.

## 13. Schema and rollout

`db/migrations/065_database_export_job_cancellation.sql` widens the
`database_export_jobs.status` CHECK constraint to accept `'cancelled'`. It is
additive: no row changes status, no column is dropped, no data is rewritten, and
the constraint is resolved from the catalog rather than by assuming a generated
name.

**Required rollout order:**

1. apply migration 065;
2. deploy the API;
3. deploy the worker (no worker change is required for cancellation safety;
   redeploy only to keep the tree consistent).

Every intermediate state is safe. An old API and an old worker never write
`'cancelled'`, so the migration is harmless before the deploy. A new API against
an old worker is safe because every worker transition is already conditional on
a status it knows, and a job it finds in `cancelled` simply matches none of its
predicates.

**Nothing in this stage was applied to production.** No migration was run, no
worker restarted, no job mutated, no export executed.

## 14. What this stage is not

- **Not a Report Explorer change.** Database exports remain the existing
  `database_export` technical artifact with its owner, expiry and download
  authorization untouched. No report-instance model arrives here.
- **Not a row-checkbox feature.** No selection column, no select-all, no
  cross-page row selection.
- **Not saved views or server-side preferences.** Those are later stages.

## 15. Verification

`ops/tests_manual/test_portal_database_export_panel.py` covers the three scopes
and their query semantics, the two column scopes and display order, forged
column and filter state, the opaque-reference resolution with its dedupe and
refusal paths, the identity-less dataset, the selection bound, every threshold
boundary (0 / 1 / cap / cap+1 / ceiling / ceiling+1 / unknown), the panel's path
notice, `can_export_rows` absence and forged POSTs, the snapshot contents, the
four presented states and their action sets, expiry, sanitized failures, the
cancellation races, requeue re-authorization, the app-bar indicator scoping, the
rendered-page asset, the vocabulary and the migration's additivity. Its browser
half runs the shipped `data-grid-export.js` through
`ops/tests_manual/data_grid_export_harness.js`.

`ops/tests_manual/test_portal_database_async_exports.py` covers the object
lifecycle, including the exact `delete-before-upload -> late upload -> worker
exit` ordering above and the settle-window rule in both directions.

No production export, job or object was created during verification. Migration
065 has not been executed against any database: no disposable PostgreSQL test
DSN is configured in this environment and the repository's driver is absent from
the host interpreter, so the migration is covered structurally only.
