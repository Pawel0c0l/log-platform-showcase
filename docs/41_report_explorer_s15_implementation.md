# 41 — Report Explorer (`S15`): what was built

Durable record of the **implemented** Report Explorer: the physical schema, the publication
boundary, the read path, the authorization gate, file delivery, the `DB-53` adapter and the rollout
requirement.

**Routing.** `docs/39` owns *where report instances come from* — the owner source decision.
`docs/40` owns *what the framework must persist* — the requirement. This document owns *what
exists*. Where this document and `docs/40` ever disagree, `docs/40` states the requirement and this
one states the built result. The approved design handoff
(`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`) is untouched and
stays read-only; it owns WHAT and the UX, this document owns HOW.

```
S15_MIGRATION_CHAIN:                  db/migrations/068_portal_generated_reports.sql
                                      db/migrations/069_portal_generated_reports_integrity.sql
S15_STATUS:                           DEPLOYED
S15_MIGRATIONS_APPLIED_TO_PRODUCTION: YES  (2026-08-19; platform ceiling 069)
MIGRATIONS_066_067_APPLIED_TO_PRODUCTION: YES
S15_HISTORICAL_REPORT_BACKFILL:       NONE
S14_GLOBAL_SEARCH:                    DEFERRED_POST_INITIAL_RELEASE
SEEN_STATE_PERSISTENCE:               NOT_BUILT (docs/40 §13.2 = B)
```

**`068` alone is not the S15 schema.** An independent review of the first implementation returned
`CODEX_REVIEW_CHANGES_REQUIRED`, and `068` was by then reachable from `origin/main` and therefore
immutable. The findings are closed by `069`, a second, additive migration; the release requirement
declares both, so a release built from this tree refuses to activate against a database carrying only
`068`. §11 records what each finding was and what it became.

---

## 1. Physical schema — migration `068`

One additive migration, implementing exactly the three capabilities `docs/40` §14 authorizes. It
alters no existing relation, reads no existing row, seeds nothing and backfills nothing.

### 1.1 `portal_generated_report_definitions` — the report TYPE

| Column | Contract it carries |
|---|---|
| `definition_id` | surrogate identity, referenced by instances |
| `type_key` | **`I-1`** the stable machine identity. `UNIQUE`, and `CHECK`-shaped `^[a-z0-9][a-z0-9_]{1,62}[a-z0-9]$` so a display string or a filename cannot become one |
| `display_name`, `description` | `Typ raportu` and the detail header's account of what the report covers |
| `cadence_class` + `cadence_detail` | `Cykl` (`RP-2`). Class constrained to `weekly`/`monthly`/`quarterly`/`on_demand`; the detail is the schedule suffix, so the rail renders `tygodniowy · pon. 04:00` without re-deriving anything from a timer |
| `period_kind`, `period_timezone` | the period vocabulary this type produces and its declared boundary semantics; default `Europe/Warsaw`, inclusive both ends |
| `source_dataset_slug`, `source_date_column` | the `RP-18` binding, declared **client-independently** by slug and resolved per client at generation time. A `CHECK` makes it a pair — half a binding would render a link with no period filter, or a filter against no dataset |
| `generation_definition_ref` | a stable reference to the report's SQL/report definition in repository code. The SQL text is code, never a database payload |
| `file_contract_json` | the declared member set (format, role, which is main) |
| `retention_months` | `Retencja plików` when no member remains to read an expiry from |
| `is_active` | a retired type stops generating without deleting its history |

A second unique index on `(definition_id, period_kind)` exists only to be the composite FK target
that pins an instance to its type's calendar vocabulary.

### 1.2 `portal_generated_report_instances` — one occurrence

Identity is `instance_id`, a surrogate key, and it is the detail URL (`SH-12`, `SH-13`, `RP-11`).

| Invariant | How it is enforced |
|---|---|
| **`I-2`** one logical instance per `(client, type, period)` | `UNIQUE (client_code, definition_id, period_key)` — the idempotency key of the whole framework |
| **`I-4`** no overlapping period sequence | `UNIQUE (client_code, definition_id, period_kind, period_start)` **plus** the alignment `CHECK` below. Together they make "adjacent period" a total order rather than a guess |
| **`I-3`** period validity | `period_start <= period_end`, and a `CHECK` that a `week` starts on an ISO Monday and spans seven days, a `month`/`quarter` is calendar-aligned to its own length, and `day`/`none` is a single date. Written with immutable expressions only, so it is a real `CHECK` |
| one type, one calendar | composite FK `(definition_id, period_kind)` → the definition |
| lifecycle vocabulary | `CHECK generation_state IN ('pending','running','succeeded','failed')` |
| `succeeded` means published | `CHECK (generation_state <> 'succeeded' OR last_published_at IS NOT NULL)` |
| **`I-7`** a published report has files | deferred `CONSTRAINT TRIGGER` (§1.4) |
| **`I-8`/`I-11`** identity is write-once | `BEFORE UPDATE` trigger refusing any change to `client_code`, `definition_id` or the four period columns. A report silently changing owner is a cross-tenant disclosure, not a data-quality issue |
| history survives its client and type | both FKs are `ON DELETE RESTRICT` |
| `RP-18` provenance survives its dataset | `source_dataset_id` is `ON DELETE SET NULL`, beside a `source_provenance_json` snapshot that keeps the record readable years later |

`library_timestamp` is persisted, not expressed: it is `COALESCE(generation_finished_at,
generation_started_at)` (`docs/40` §3.4) and it is simultaneously the grouping key, the ordering key
and the `Rok` filter axis, so one btree index serves grouping, ordering and pagination — including
for a running instance that has no finish time yet.

`published_member_count`, `available_member_count` and `available_expires_at` are the availability
summary. They are **derived by trigger** from the member rows, never asserted by application code
(§1.4), which is what makes `Status` an indexable filter and the footer counter exact.

### 1.3 `portal_generated_report_files` — the member

| Invariant | How it is enforced |
|---|---|
| **`I-6`** at most one main file | partial `UNIQUE INDEX ... (instance_id) WHERE is_main_file` |
| **`I-6`** exactly one while members exist | deferred `CONSTRAINT TRIGGER` (§1.4) |
| **`I-5`** one member per stored object | partial `UNIQUE INDEX (instance_id, artifact_id) WHERE artifact_id IS NOT NULL`, plus `UNIQUE (instance_id, display_filename)` |
| **`I-9`** a member belongs to exactly one instance | FK `ON DELETE CASCADE`, and a trigger refusing a change of `instance_id` |
| **`I-10`** the member outlives its bytes | `artifact_id` is nullable and `ON DELETE SET NULL` |
| an available member is servable | `CHECK (NOT is_available OR artifact_id IS NOT NULL)`, and a `BEFORE` trigger that downgrades `is_available` the moment the object reference is cleared — so no code path can leave a downloadable marker over bytes that are gone |
| roles are a closed vocabulary | `CHECK semantic_role IN ('main_document','detailed_data','raw_data')` |

`is_main_file` is an explicit flag. `display_order` is presentation and decides nothing: `RP-13`
requires the main file to be distinguished visually rather than by position, and `docs/39` forbids
reading meaning out of a filename or an extension, so neither order, name nor extension may decide
it.

### 1.4 Why two constraint triggers exist

"A published instance has at least one member" and "exactly one of those members is the main file"
are **transaction** truths, not row truths: publication inserts members and stamps the instance in
one transaction, and regeneration empties the member set before refilling it. A row-local `CHECK`
would reject both legitimate intermediate states. They are therefore `DEFERRABLE INITIALLY
DEFERRED` constraint triggers that re-read the instance at commit rather than trusting the row
image the event carried.

### 1.5 Why no fourth persistence entity was required

Everything the approved contract renders resolves to the three authorized capabilities:

- `NOWY` would have needed a fourth relation and was **deferred** by the owner (`docs/40` §13.2 =
  B). It is not rendered anywhere, and no `RP-` criterion tests it.
- `Eksporty danych` needs none: it is a read-only adapter over `database_export_jobs` (§6).
- `RP-18` provenance is instance columns plus a snapshot, not a relation.
- History and period siblings read the instance rows themselves.
- Byte storage is `artifacts`, which already exists and is unchanged.

No `S15_SCHEMA_SCOPE_EXPANSION_REQUIRED` condition arose.

---

## 2. Module layout

```
api/report_explorer/
  models.py           the normalized, provider-independent domain the pages render
  periods.py          declared period vocabulary, validation and presentation
  errors.py           the failure vocabulary — every member is a renderable state
  access.py           the ONE authorization gate (§5)
  store.py            the read path over the three relations, client-scoped first
  exports_adapter.py  the read-only DB-53 provider over database_export_jobs
  publication.py      the write boundary a generator publishes through (§3)
  service.py          authorize the client, then the two providers, then one library
  html.py             escaping, formatting, and the approved Polish copy in one place
  pages.py            REP-001..REP-004 rendering; no FastAPI import
  page_routes.py      the FastAPI adapter
api/static/css/report-explorer.css
api/static/js/report-explorer.js
```

Nothing in the package imports `api.main`. The integration block at the end of `api/main.py`
injects the portal's canonical helpers — `_get_effective_client_access_for_user`,
`_get_portal_database_dataset_for_user`, `_database_export_schema_available`, `db_conn` and the
artifact delivery path — so there is exactly one implementation of each and no second
interpretation of the authorization gate.

**Routes.**

| Path | Purpose |
|---|---|
| `/user/reports` | the library (`REP-001`, `REP-002`) — the `Raporty` nav slot |
| `/user/reports/instances/{instance_ref}` | the detail page (`REP-003`) |
| `.../files/{member_ref}/preview` | inline bytes for the embedded preview |
| `.../files/{member_ref}/download` | one member as an attachment |
| `.../files/download-all` | `Pobierz wszystkie (n)` as one archive |
| `/user/reports/folders` | **compatibility only** — the pre-redesign folder index (§8) |

`instance_ref` is provider-prefixed (`gen-<uuid>` / `exp-<uuid>`), which is what lets one detail
route resolve both providers without guessing.

---

## 3. Publication boundary

`ReportPublicationService` is the only supported way to write the three relations. A generator
calls it with explicit facts and never with a filename:

```
register_definition(spec)                      -> definition_id      (upsert on type_key)
begin_generation(client, type_key, period, …)  -> GenerationAttempt  (claims the instance)
publish(attempt, files=[…], row_count, source) -> instance_id        (one transaction)
fail(attempt, safe_error_code, …)              -> None
expire_due_members()                           -> int
```

**Idempotency.** `begin_generation` upserts on `(client_code, definition_id, period_key)`. A retry
and a regeneration therefore reclaim the *same* instance: same identity, same detail URL, same place
in the period sequence. Uniqueness is what guarantees it — not generator discipline.

**Atomic member replacement.** `publish` deletes the previous member set and inserts the new one in
the same transaction as the publication stamp, so from a reader's point of view an instance
transitions to published once, with its complete file list. Three publications of one period leave
one instance and one member set; no duplicate membership can accumulate on retry.

**Fencing.** `begin_generation` mints a `claim_token`. `publish` and `fail` both take the row `FOR
UPDATE` and require the token to still match, so a crashed or superseded attempt can neither publish
over a newer one nor mark it failed.

**States without files.** `pending`/`running` and `failed` instances exist with zero members and no
`artifacts` row at all. `publish` refuses an empty member set at the boundary (`EmptyPublicationError`)
rather than letting a file-less "success" reach the constraint trigger.

**A published instance stays published while it is refreshed.** `begin_generation` does not clear
`last_published_at`, and `fail` does not either. A regeneration or a failed retry over an instance
whose files still exist therefore stays `Gotowy` with working actions, which is `docs/40` §3.2.

**Cross-client refusal.** `publish` verifies every supplied `artifact_id` belongs to the instance's
client before writing a single member.

**Approved sources today.** The first-release generator set is the `DB-53` export adapter (§6),
which needs no generated-report row at all. No cyclical generator has been authorized yet:
`docs/39` §7 states what one must declare, and `jobs/reports/*` remains Workflow B and is untouched.
The boundary above is what such a generator will call — it exists and is tested now precisely so
that a schema and a UI are not shipped without an authoritative publication path.

---

## 4. Read path and the library

`ReportExplorerService` authorizes the **client** first and only then consults a provider. There is
no statement in `store.py` that can return a row for a client the caller did not name: an id is a
filter, never a lookup key on its own.

The four approved statuses are derived in SQL, not in Python, because `Status` is a toolbar filter
and the footer counter must be exact:

| Condition | Presented |
|---|---|
| published, ≥1 available member, not past its horizon | `Gotowy` |
| published, none available | `Pliki wygasły` |
| never published, latest attempt `failed` | `Błąd generowania` |
| never published, otherwise | `W generowaniu` |

`REP-001`/`REP-002`: a 288 px report-type rail with each type's cadence, instance count and an
attention dot when the newest instance of that type presents as a genuine file-less failure;
instances grouped by **generation month** newest first under headings that state the rule in words,
with the toolbar restating it; text search, `Typ`, `Rok` and `Status` filters rendered as removable
chips; pagination 25/50/100/200/500 defaulting to 100; and the footer counter stating both numbers
(`1–14 z 14 po filtrach · 40 w bibliotece`). Rail counts sum to the library total by construction —
both are counted from the same client-scoped population.

`REP-003`: the header card with the approved 8-field metadata grid, the preview embedded in the
layout (not a modal, not a new tab) with a format switcher across previewable members, page
navigation and `Pełny ekran`, the `Pliki w tej pozycji` panel with the main file tinted by its flag,
and `Historia tego raportu` built from instance records alone — which is what lets an expired or
failed period render its own period, status, generation time and `0` files.

Period siblings are resolved by adjacent `period_start` within one `(client, type)`. At the newest
period the forward control is **absent**, not disabled (`RP-17`).

**Return state (`SH-13`).** The library state *is* the URL: filters, page, page size and the scroll
offset travel in the query string, every detail link carries them back, and `‹ Wróć do biblioteki`
reopens the list where it was left. There is no server-side cursor and no session memory. Only the
pixel offset is stamped by the page script, because only that is a browser fact.

---

## 5. Authorization

```
GENERATED_REPORT_VISIBILITY =
      authenticated portal account
  AND portal_clients.is_active
  AND effective can_view_reports for that client   (direct OR active-group)
```

Resolved through `_get_effective_client_access_for_user` — the canonical portal helper, injected
rather than reimplemented. Report Explorer adds **no** grant model of its own:

- no `portal_report_folders` requirement — folder membership is an artifact-query access mechanism
  and is never the S15 grant;
- no per-report-type grant;
- **no admin bypass** — an administrator with no report grant for a client sees that client's
  reports exactly as anyone else does: not at all.

`RP-19` falls out of the model rather than being special-cased: `can_view_database = true` with
`can_view_reports = false` for the same client is precisely the state that must be explained as two
separate grants, and the denial carries that distinction while disclosing nothing about the client's
reports.

**Every** list, detail, preview, download and archive request reauthorizes from current grants. A
rendered link is not a capability: a revocation between rendering a list and clicking a row takes
effect on the click.

**Non-disclosure.** A malformed reference, an unknown one, one belonging to another client and one
belonging to another account's export all produce the same not-found answer, so a response cannot be
used to probe for existence. An inactive client is indistinguishable from an absent one.

---

## 6. `DB-53` — the database-export adapter

Completed background exports appear in the library as the system report type `Eksporty danych`,
cadence `na żądanie · systemowy`, read **entirely** from `database_export_jobs` /
`portal_database_datasets` / `artifacts`. Not one row is copied into the generated-report relations,
and `DB_53_SCHEMA_CHANGE: NOT_REQUIRED` holds.

Scope is `completed` and `expired` jobs with a completion timestamp; `queued`, `running`, `failed`
and `cancelled` stay on the export management surface, which keeps its own lifecycle actions.
Visibility is owner-scoped (`requested_by_user_id`) **in addition to** effective `can_view_reports`
on the dataset's client.

Owner decision A (`docs/40` §13.1): an export declares **no** reporting period. `Okres
raportowania` and `Numer okresu` render `nie dotyczy`, exports are ordered by completion time, the
provider declares period-sibling and history support as unavailable, and nothing is read out of
`request_snapshot_json` for period purposes — so no export acquires a period it does not have.

---

## 7. File delivery

The chain is re-established on every byte request, in one statement:

```
authorized user → authorized client → authorized instance → authorized member → artifact
```

An artifact id is never an input; the artifact is reached only *through* the membership, and the
member query additionally requires the artifact's own `client_code` to equal the instance's — so
even a mis-published membership is unservable across a tenant boundary.

Bytes are served by the existing `_stream_artifact_download` path over the existing storage backend.
The response wears the **member's** user-facing filename and content type; the storage key, the
backend, the artifact id and the pipeline lineage never reach the response, the page or the audit
record. `Pobierz wszystkie (n)` builds one archive from members resolved through exactly the same
walk, under a total-size cap above which it degrades to the per-file downloads the panel already
offers.

A storage failure renders the approved file-store state — the list stays usable, only preview and
download are unavailable, and the state carries a copyable reference rather than a driver message.
Expired bytes render `Pliki wygasły` and the instance keeps its place in history.

Four audit event types were registered (`report_instance_file_served`,
`report_instance_file_unavailable`, `report_instance_archive_served`,
`report_instance_archive_unavailable`). Their metadata is the instance reference, the provider, the
member reference and the disposition — never a storage key, an artifact id or a filename.

---

## 8. Legacy `/user/reports`

`/user/reports` now renders the Report Explorer library, because two competing report libraries at
one navigation entry is exactly the ambiguity the approved contract forbids. The pre-redesign
folder surface is **not deleted**: `portal_report_folders`, its per-folder grants and its
preview/download routes are unchanged, its deep links (`/user/reports/folders/{folder_id}`,
`/user/reports/artifacts/{artifact_id}/…`) keep working with their original authorization, and its
index moved to `/user/reports/folders` as a compatibility-only route that the primary navigation
does not link.

---

## 9. Rollout

```
S15 SCHEMA DEPENDENCY:  068 → 069, before code
ACTUAL ROLLOUT ORDER:   064 → 065 → 066 → 067 → 068 → 069 → code
```

`068 → 069 → code` is `S15`'s own dependency, not the sequence an operator executed. The production
platform ceiling was `063` and `ops/db_migrate.sh` applies every unrecorded migration in filename
order, so the portal rollout necessarily began at `064`. It ran in full on 2026-08-19 and the ceiling
is now `069`. The authoritative rollout record, including what `064` and `065` are and why neither
was optional, is `docs/38` §6 — read that document for the rollout and this section for what `S15`
itself requires of the schema.

`068` and `069` are both declared in `db/schema_requirements.json` (scope `platform`, milestone
`S15`) with their columns, constraints, load-bearing partial unique indexes and the triggers that
enforce `I-6`/`I-7`. A release built from this tree therefore **refuses to activate** against a
platform database without them, rather than shipping a Report Explorer that renders an empty library
indistinguishable from a client with no reports. The declaration is verified physically: the gate
reports defects before the migrations and none after them.

**`068` alone does not satisfy the requirement.** A database carrying only `068` accepts every state
§11 describes — a forged availability summary, a replayed publication, an HTML payload marked
previewable — so activation must refuse it rather than run the corrected code over an uncorrected
schema. The `068` declaration deliberately no longer names
`portal_generated_report_instances_period_key_uniq`, because `069` drops that second, competing
logical identity and a correct final schema does not carry it.

The application also probes for the three relations at request time and, when they are absent,
states plainly that the library cannot be read instead of rendering an empty one.

```
S15_MIGRATIONS_APPLIED_TO_PRODUCTION
MIGRATIONS_064_065_066_067_068_069_APPLIED_TO_PRODUCTION
S15_CORRECTIVE_MIGRATION_APPLIED_TO_PRODUCTION
PRODUCTION_PLATFORM_MIGRATION_CEILING = 069
```

`064`–`069` were applied and the code deployed on 2026-08-19 under separate explicit owner
authorization. **No production report instance has been created**: the generated-SQL cyclical report
definitions and instances are empty, which is the expected consequence of
`S15_REPORT_SOURCE: SYSTEM_GENERATED_SQL_REPORTS_ONLY` and not a deployment defect. `DB-53` data
exports remain the populated report-adjacent surface. Defining the first cyclical report is still a
separate, separately authorized action.

---

## 10. Verification

`ops/tests_manual/test_portal_s15_report_explorer_postgres.py` — 44 assertions against a
**disposable** PostgreSQL 16 the suite starts and removes itself. It accepts no DSN, so there is no
input that can point it at `logdb`, staging or production.

It covers: migration application to a clean prerequisite schema and re-application; that `068` alters
no existing relation and seeds nothing; that the declared release requirement matches the DDL; the
definition, period, uniqueness, non-overlap, lifecycle, main-file, membership and survival
invariants, each as a statement PostgreSQL accepted or refused; first publication, retry,
regeneration, the next period, running and failed instances without files, refusal of a file-less
success, stale-attempt fencing, cross-client artifact refusal, multi-file roles and member expiry;
that neither the period nor the main file is ever read out of a filename; direct and group grants,
`RP-19`, no-grant, admin-without-bypass, inactive client, foreign instance/member/artifact ids and
reauthorization after revocation; and the rendered library, detail, states, `Dane źródłowe`
permission split, `DB-53` adapter, approved Polish copy, absence of any data-grid affordance and
absence of `S14` global search.

**No-backfill evidence.** The suite loads 638 historical `artifacts` rows *before* applying `068` and
asserts afterwards that the three relations are empty and the artifact count is unchanged. Existing
artifacts are not, and cannot become, report instances by applying this migration.

```
NO_HISTORICAL_REPORT_BACKFILL
```

The `S13` stage-boundary guard `test_no_s14_or_s15_entity_is_pre_built` was **retargeted, not
weakened**: it now asserts that `S14` remains absent from both `api/main.py` and migration `066`,
that `066` still introduces no report relation, and that `S15` exists as its own stage with its own
migration and its own module. The `S13` migration-number guard was likewise narrowed to "S13 owns
exactly 066 and 067" instead of "067 is the newest file in the tree", which would have forbidden any
later stage from existing.


---

## 11. Independent-review correction (`069`)

The first S15 implementation was reviewed against a real database and returned
`CODEX_REVIEW_CHANGES_REQUIRED` with nine findings. Every one of them had the same shape: something
the design treats as a FACT was, in the built system, a CLAIM that some caller could make. The
correction does not redesign S15 — it moves each of those facts to whichever layer can actually
establish it.

`068` was already reachable from `origin/main` when the review landed, so it is immutable under
`AGENTS.md` §4. The schema half of the correction is therefore
`db/migrations/069_portal_generated_reports_integrity.sql`, written to be correct both after `068` on
an existing database and as part of a clean replay of the whole chain.

### 11.1 One logical identity per period

`068` declared two uniqueness constraints over the same logical thing — `(client, type, period_key)`
and `(client, type, period_kind, period_start)` — while leaving `period_key` a free string. Two
consequences the review reproduced:

* two concurrent first generations of one period could collide on the period-START index while the
  service's `ON CONFLICT` named the period-KEY one, so the loser escaped as PostgreSQL `23505`
  instead of resolving to the existing instance;
* a caller-chosen key could be persisted over canonical dates, and the canonical retry that followed
  then collided with it rather than reusing it.

`069` collapses this to one identity. The period-key uniqueness is dropped and `period_key` becomes
a DERIVED name, bound by CHECK to the calendar period `(period_kind, period_start)` describes —
built from `EXTRACT`, `lpad` and concatenation only, because a CHECK requires immutability and
`to_char` / `date::text` are merely stable. `ReportingPeriod` refuses a non-canonical key for the
same reason at the boundary, and `canonical_period(..., key=...)` became an assertion rather than an
override. `begin_generation` now names the surviving identity as its conflict target, so a concurrent
first generation blocks on the index and takes the `DO UPDATE` branch on the same row.

### 11.2 An attempt is terminal, not merely current

`068` fenced on `claim_token`, which answers *"is this the current attempt?"* and never *"has this
attempt already finished?"*. One claim could `publish()` repeatedly, two concurrent `publish()` calls
could both replace the member set, and `fail()` could run after a successful publication and rewrite
a published report as failed.

`claim_token` now means ACTIVE and is CONSUMED by the completion that closes it;
`completed_claim_token` and `completed_claim_outcome` record which attempt closed the instance and
how. Three answers follow, and they are the whole contract:

| the claim is | `publish` / `fail` does |
|---|---|
| the active claim | the mutation, exactly once |
| a completed claim, same outcome | nothing — the replay is recognised and answered |
| a completed claim, different outcome | refuses with `AttemptAlreadyCompletedError` |
| neither | refuses with `StaleAttemptError` |

A network retry of a successful publication is therefore idempotent without performing a second
mutation to look idempotent, and a late `fail()` cannot contradict a publication that already
happened. The row is taken `FOR UPDATE`, so two concurrent callbacks holding one claim serialise and
the second wakes up holding a completed one.

### 11.3 The availability summary is a projection

`068` derived the summary from the MEMBER triggers, which is correct for member changes and silent
about instance changes — and the review's forgery was an ordinary `UPDATE` on the instance itself,
after which the read path rendered a report with no available file as `Gotowy`. A BEFORE trigger on
the instance now overwrites `published_member_count`, `available_member_count` and
`available_expires_at` from the member rows on every insert and update. Whatever a statement sets is
replaced by what membership says, before the row is stored, so the columns are unwritable by any
caller and the row-local CHECKs `068` declared become real invariants.

### 11.4 The artifact describes itself

Publication accepted `file_format='PDF'`, `content_type='text/html'`, `is_previewable=true` and the
detail page embedded it. Three independent layers now stand between a caller's description and an
inline response:

1. **Publication binds.** `content_type` and `size_bytes` are read from the `artifacts` row, never
   from the caller; the declared `file_format` must be consistent with that authoritative type; and
   `is_previewable` survives only for content this platform embeds. A caller may still turn
   previewability off, and can no longer turn it on.
2. **`069` constrains.** A format/content-type CHECK makes the inconsistent member unstorable, and
   `is_previewable` is allowed only for `application/pdf`.
3. **Delivery decides.** The preview route asks the service, per request, from the ARTIFACT's own
   content type — so a member written before either of the first two layers existed still cannot be
   embedded. The allowlist is not new policy: `api/artifacts/preview.py` already decided that
   `application/pdf` is the one type handed to the browser as-is. Inline responses carry
   `X-Content-Type-Options: nosniff`, and a non-previewable member gets a `415` state that keeps its
   download.

### 11.5 The archive bound is enforced on bytes

`Pobierz wszystkie` summed the members' DECLARED sizes against 256 MB and then read every object in
full, so a member declaring one byte over a four-gigabyte object passed the check and then allocated
four gigabytes. Declared size is now used for nothing: `head_object` metadata (falling back to the
server-written `artifacts.size_bytes`) refuses an over-limit set before a body is opened, and each
body is read in bounded chunks with the cumulative total re-checked after every chunk — so an object
larger than any metadata claimed costs one chunk, not its own size. The 256 MB product policy is
unchanged.

Over-limit now degrades TRUTHFULLY. It used to render the file-store failure state, which told the
user the storage was unavailable while every individual download would have succeeded; it renders a
`413` that offers the per-file alternative `RP-21` already provides. A partial archive is never
served: the buffer dies with the exception and the response is a state. Entry names are reduced to
their last path segment, stripped of separators and control characters, and de-duplicated
deterministically (`raport_1.pdf`, `raport_1 (2).pdf`); `069` additionally refuses to persist a
path-shaped `display_filename` at all.

### 11.6 `RP-18` provenance is validated, not accepted

Publication stored another client's dataset id, a slug inconsistent with the definition, an arbitrary
date column and an unrelated source range. Live navigation still reauthorized correctly, so nothing
escalated — but a provenance snapshot exists to be true years later, and it was not. Every field is
now resolved from a server-side authority or checked against one: the dataset must be the
definition's declared slug resolved for THIS instance's client and must be CURRENTLY ACTIVE in the
catalogue (`is_active IS TRUE`, the same predicate `_get_portal_database_dataset_for_user` enforces at
click time); the dataset NAME is read from the catalogue rather than accepted; the date column must be
the definition's and must exist in the dataset's approved column catalogue; and the applied range is
derived from the instance's own reporting period and only checked when supplied. A type that declares
no source binding cannot record provenance at all. A follow-up review probe showed the activity rule
missing — a NEW publication could still name a dataset already withdrawn from the catalogue as its
authoritative source — so the resolution now fails closed on it, with the same indistinguishable
refusal it gives for a foreign-client or unknown dataset.

The activity rule binds PUBLICATION, not history. A report published while its dataset was active
stays a valid historical report after that dataset is deactivated: nothing deletes it, invalidates its
identity, removes its files or rewrites its stored provenance. Whether its `Dane źródłowe` action is
offered later is the separate, click-time question below.

`RP-18` navigation is unchanged and still reauthorizes Database Explorer at click time through the
canonical gate, which independently evaluates current dataset availability and the current grant.
Report access does not grant database access.

### 11.7 The file contract is a contract

`file_contract_json` was bounded JSON that nothing read, so a definition could declare `PDF` + `XLSX`
and a publication could succeed with a single CSV. `api/report_explorer/contract.py` gives it a
finite schema — `role`, `format`, `main`, `required`, `max` — validated once at
`register_definition`, and proves the successful file set against it at publication: required members
present, cardinality respected, no undeclared member, and the main file being the declared main
output rather than merely a flagged one. An EMPTY declaration still constrains nothing, because a
definition that has not declared its outputs is claiming nothing rather than claiming they are
arbitrary. No new relation was required.

### 11.8 The library has no population ceiling

The library kept the newest 500 exports and the newest 5,000 generated instances before merging them
in Python, so past those counts an older record was unreachable while the footer counter — a
`count(*)` — still said it existed. A page and its total described two different populations.

There is now ONE merged ordering. Each provider contributes its own scoped `SELECT` of
`(provider, identifier, ordering timestamp)`; the service composes them with `UNION ALL`, orders by
`(library_timestamp DESC, instance_ref DESC)` and slices with `LIMIT/OFFSET`; and only the page's own
identities are hydrated, by each provider re-applying its full scope. Per request the database orders
`offset + limit` thin rows and at most `limit` rows are built. `DB-53` remains a read-only adapter —
it contributes SQL, not rows, and still copies nothing. `page` is clamped to the last page the
current filter actually has, so the offset can never exceed the real population and no record is
unreachable at any page size.

### 11.9 Affected regression suites

`test_platform_shell_unification_phase3a` was run to COMPLETION rather than to its first failure,
which surfaced two stale expectations. Neither was caused by S15 — both reproduce identically at
`2756f9b`, before the S15 implementation commit — and both were **replaced with a stronger check
rather than dropped**:

* the assertion on the English module captions `Reports Explorer` / `Client Database Explorer`.
  `_portal_layout` no longer renders `nav_items` labels at all; the shared shell builds its
  navigation from `portal_shell.primary_label_for` and the approved Polish vocabulary. What the line
  protected — the two user modules are distinguishable, each names itself, each offers the other — is
  now asserted structurally on the navigation markup.
* the assertion that the artifact combo/copy markers appear inline. That behaviour moved verbatim
  into `api/static/js/artifact-explorer.js` during asset extraction; the test now asserts the page
  loads that module and that the module still implements both enhancements.

One further stale fixture was corrected in `test_portal_server_preferences_and_saved_views`: the S13
schema-probe stub answered the expression-IDENTITY read — added when S13 readiness stopped being
decided by deparsed text — with the constraint inventory, because that query also mentions
`pg_constraint`, so every classification returned `INCOMPATIBLE` on a correct schema. An authorized
S13 schema produces no identity rows, which is what the stub now says. No product behaviour and no
assertion changed.

### 11.10 Evidence

`ops/tests_manual/test_portal_s15_review_remediation_postgres.py`, against the same disposable
PostgreSQL 16 harness. It reuses the S15 suite's prerequisite schema and service wiring, so it is
evidence about the same system rather than a friendlier one.

The concurrency proofs are deterministic, not hopeful. The `begin_generation` race holds an
uncommitted insert of the period on one connection, observes the second generation WAITING in
`pg_locks`, commits, and requires the waiter to resolve to the same instance — then repeats the
symmetric two-thread case so neither ordering can escape. The publish race runs two real connections
through one claim behind a barrier and requires the member set to be exactly one of the two published
sets. The archive bound is exercised through the real enforcement path with the policy number scaled
down, against an object-store fake whose objects may be larger than any metadata about them; the
256 MB constant itself is asserted separately.

Populations exceed BOTH former caps: 5,200 generated instances and 620 completed exports, walked page
by page at two page sizes, with the reachable set required to equal the counted set exactly — no
duplicate across adjacent pages, nothing missing, and the oldest export (which the 500-row cap hid
first) required to appear past position 500.

```
REPORT_INSTANCE_CONCURRENT_IDEMPOTENCY
REPORT_PUBLICATION_FENCING
REPORT_PREVIEW_DELIVERY_SAFETY
REPORT_BULK_ARCHIVE_BOUND
REPORT_AVAILABILITY_SUMMARY_INTEGRITY
REPORT_SOURCE_PROVENANCE_INTEGRITY
REPORT_FILE_CONTRACT_ENFORCEMENT
REPORT_LIBRARY_COMPLETE_PAGINATION
S15_AFFECTED_REGRESSION_EVIDENCE
```

The already-accepted behaviour is unchanged: the authorization gate, IDOR indistinguishability, file
expiry and history semantics, the detail/history route, the `REP-004` states, the legacy report-folder
compatibility route, `DB-53` as an adapter, and the `S13` stage-boundary retargeting.

```
MIGRATIONS_066_067_068_069_APPLIED_TO_PRODUCTION
S15_CORRECTIVE_MIGRATION_APPLIED_TO_PRODUCTION
NO_HISTORICAL_REPORT_BACKFILL
NO_PRODUCTION_REPORT_INSTANCE_CREATED
S14_GLOBAL_SEARCH: DEFERRED_POST_INITIAL_RELEASE
```
