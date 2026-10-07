# 40 — Report-generation framework: domain and persistence contract

Authoritative durable design contract for the **system-generated report framework** that produces
`S15` Report Explorer report instances.

It defines **what the persistence layer must guarantee** — entities, identity, lifecycle, file
membership, period semantics, authorization and the provider interface `S15` consumes. It
deliberately stops before physical schema.

```
NO DDL IN THIS DOCUMENT          PHYSICAL SCHEMA IS OWNED BY docs/41
```

This document itself changes no code, no schema and no migration, and it edits nothing inside the
approved design handoff
(`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`), which stays
read-only. What changed on 2026-08-19 is its *status*: the §14 authorization it was written to
request has been granted for capabilities 1–3 (§0).

**Routing.** `docs/39` owns *where Report Explorer report instances come from* (the source decision)
and is not restated here. `docs/38` §5 owns the release-stage position. This document owns *what the
generated-report framework must persist* — the requirement. `docs/41` owns *what was built* — the
migration, the physical relations, the publication boundary and the `S15` implementation.

---

## 0. Position

```
S15_REPORT_SOURCE:                       SYSTEM_GENERATED_SQL_REPORTS_ONLY   (docs/39 §1, unchanged)
S15_WORKFLOW_B_ARTIFACTS:                OUT_OF_SCOPE                        (docs/39 §2, unchanged)
S15_ARTIFACT_BACKFILL:                   NOT_REQUIRED                        (docs/39 §6, unchanged)
S15_ARTIFACT_SCHEMA_CHANGE:              NOT_REQUIRED                        (docs/39 §8, unchanged)

REPORT_GENERATION_PERSISTENCE:           REQUIRED
REPORT_GENERATION_SCHEMA_AUTHORIZATION:  GRANTED_FOR_CAPABILITIES_1_2_3     (§14, owner, 2026-08-19)
REPORT_GENERATION_SEEN_STATE_CAPABILITY: NOT_AUTHORIZED_DEFERRED            (§13.2 = B)
S15_EXPORT_PERIOD_PRESENTATION:          NOT_APPLICABLE                     (§13.1 = A)
REPORT_GENERATION_PERSISTENCE_BUILT:     db/migrations/068                  (docs/41 §1)
S15_MIGRATION_APPLIED_TO_PRODUCTION:     NO
```

> **Authorization status.** The owner granted §14 capabilities 1–3 on 2026-08-19 and resolved both
> §13 decisions. Capability 4 (per-account seen state) was **not** granted and is deferred, so `NOWY`
> is not rendered in the initial release. This document keeps its original shape — it remains the
> *contract*, not the DDL — and the physical schema that implements it, plus every implementation
> fact, is owned by `docs/41_report_explorer_s15_implementation.md`. Where the two ever disagree, the
> contract states the requirement and `docs/41` states what was built.

### 0.1 Why persistence is required

The approved behaviour cannot be served by executing report SQL when a user opens the page, and
cannot be served by metadata whose files are produced on request. Each row below is an approved
requirement that a stateless design cannot satisfy:

| Approved behaviour | Evidence | Why dynamic execution fails |
|---|---|---|
| `W generowaniu` rendered **before** any file exists, with no actions at all | `PBC` §3.3, `RP-8` | A state that exists only between two moments in time must be durably recorded; nothing to execute yet |
| `Błąd generowania` retained, with the period still listed and zero files | `PBC` §3.3, `RP-9`, prototype `Tydzień 26 · 2026` | A failed period has no result set to compute; the failure itself is the fact |
| `Pliki wygasły` — the record survives its files | `PBC` §3.3 ("history retains the record"), prototype history `2026-W22`, `2026-W21` with `0` files | Nothing remains to recompute from once bytes are gone; only a retained record can render it |
| `Wygenerowano` as a historical fact, distinct from the reporting period | `PBC` §3.1, §3.2, §3.6 | Execution time of a page view is not a generation time |
| Grouping by **generation month**, newest first, with per-group counts that match | `PBC` §3.1, `RP-3`, `RP-6` | Requires a stored generation timestamp per instance |
| Stable instance counts, footer counter `1–14 z 14 po filtrach · 40 w bibliotece`, rail counts summing to the library total | `PBC` §3.1, §3.4, `RP-5`, `RP-6`, `RP-7` | Counting instances requires instances to exist as rows |
| Pagination 25/50/100/200/500, default 100 | `TRACEABILITY_MATRIX` (`REP-001`), `PBC` §2.9 | Stable paging requires a stable, orderable population |
| Stable detail URL, period siblings, 8-period history | `PBC` §3.6, §3.7, `RP-15`–`RP-17` | Requires durable instance identity and adjacency |
| Per-file byte sizes shown in the library, before any download | `PBC` §3.2, §3.5, `RP-10` | A size is a property of a materialized file |
| `Retencja plików` (`do 15.07.2028`) | `PBC` §3.6, prototype metadata grid | An expiry is a property of a stored object |
| `Wiersze w raporcie` (`4 118`) retained after expiry | `PBC` §3.6 | A count of a result set that no longer exists |

**Conclusion.** The framework requires *durable generated-report instances* and *durable
materialized file membership*. Files are bytes in the existing object/artifact storage; the product
meaning of those bytes lives in the new domain relations defined below.

### 0.2 Nothing in the repository already solves this

Verified against the current tree:

- no relation, column or code path named for a generated report instance/period/cycle exists
  (`grep` over `db/migrations/*.sql`, `api/main.py`, `jobs/`, `ops/` finds none);
- `ops/tests_manual/test_portal_database_export_panel.py:1643` asserts `"report_instances" not in
  source` — `S8` deliberately did not introduce one;
- `ops/tests_manual/test_portal_server_preferences_and_saved_views.py::test_no_s14_or_s15_entity_is_pre_built`
  asserts `report_instance`, `report_period`, `report_cycle`, `report_library` appear in neither
  `api/main.py` nor migration `066`;
- `artifacts` carries no reporting period, no instance identity and no report lifecycle
  (`docs/39` §9);
- `database_export_jobs` (`db/migrations/043`, `065`) is the **only** existing model that resembles a
  report instance, and it is an export job, not a cyclical report (§9).

> **Consequence for the implementation task.** `test_no_s14_or_s15_entity_is_pre_built` is a *stage
> boundary* guard for `S13`. When the generated-report schema and `S15` land, that test must be
> retargeted deliberately — it is not a defect and must not be silently deleted.

---

## 1. Domain entities

Four conceptual entities. The fourth is conditional on an owner decision (§13.2).

```
Report Definition (type)  1 ──< n  Report Instance  1 ──< n  File Member  ──> object bytes
                                          ^
                                          └──< n  Instance Seen State (per account, CONDITIONAL)
```

### 1.1 Generated Report Definition (report type)

The configuration entity behind the left rail. **Owns cadence, period logic, source binding and the
file contract.** One row per canonical report type.

| Responsibility | Contract |
|---|---|
| Canonical type identity | A stable machine key, immutable once instances exist. Never a display string. |
| Display name | `Typ raportu` in the rail, the metadata grid and the type filter (`Raport 207`). Required. |
| Description | The detail header's *"description of what the report covers"* (`PBC` §3.6) and the rail subtitle (`naruszenia prędkości`). Required where the design renders it. |
| Cadence | `Cykl` — see §6. Required (`RP-2`). |
| Period kind + boundary semantics | The period vocabulary this type produces (§5). Required for every cyclical type. |
| Source-data binding | The dataset this type reads, expressed client-independently (§12). Required (`RP-18`). |
| Generation definition reference | A stable reference to the report's SQL/report definition (module key or definition id) plus the parameters it takes. The SQL text itself is repository code, not a database payload. |
| File contract | The declared members a successful generation produces: format, semantic role, which one is the main file (§4, §7). |
| Retention policy | The retention window applied to produced files (§11). Required where `Retencja plików` renders. |
| Enablement | Active/inactive, so a retired type stops generating without deleting its history. |
| Client scope | Which clients the type is generated for. May be a declared per-client enablement or a definition-per-client row; either satisfies the contract, and the choice is a schema-task decision. |

**Not owned here:** anything about an individual period. A definition never carries an instance's
period, status, row count or files.

### 1.2 Generated Report Instance

The unit the library lists and the detail page renders. **One logical instance per
`(client, report type, reporting period)`** — see §2.

| Responsibility | Contract |
|---|---|
| Durable identity | A stable surrogate identity that survives regeneration. It is the detail URL (`SH-12`, `SH-13`, `RP-11`). |
| Client | The owning client, in platform `client_code` terms (`portal_clients`). Required. |
| Report type | The owning definition. Required. |
| Reporting period | `period_key`, `period_start`, `period_end`, `period_kind` (§5). Required, always supplied by the definition, never inferred. |
| Display name | The instance label the library renders and text search matches (`Tydzień 28 · 2026`). Persisted, produced by the definition, so search and ordering read one authoritative string (`PBC` §3.4 — search is over *"Nazwa raportu lub okres"*). |
| Generation lifecycle | Latest-attempt state plus the last successful publication (§3). Required. |
| Generation timestamps | `generation_started_at` and `generation_finished_at`; the derived **library timestamp** orders and groups the library (§3.4). Required. |
| Row count | `Wiersze w raporcie`, captured at generation and retained after files expire. Required (`PBC` §3.6). |
| Source-data provenance | The resolved dataset and the filter/period actually applied, snapshotted at generation (§12). Required (`RP-18`). |
| File-availability denormalization | Enough persisted state to evaluate `Gotowy` vs `Pliki wygasły` without scanning members, because status is a **filter** and pagination must stay stable (§3.5, §10). Required. |
| Attempt bookkeeping | Attempt count, last failure reference, and whatever fencing the generator needs for crash-safe retry (§3.6). |
| Timestamps | `created_at`, `updated_at`. |

### 1.3 Generated Report File Member

| Responsibility | Contract |
|---|---|
| Owning instance | Required; a member never exists without one. |
| Stable object identity | The stored bytes. **Recommendation:** reference an `artifacts` row (§8). |
| Display filename | `raport-207-2026-W28.pdf` — the user-facing name. Required. |
| Format / content type | `PDF` / `XLSX` / `CSV` badge plus MIME. Required (`RP-10`). |
| Byte size | Rendered in the library badge *and* the files panel. Required (`PBC` §3.2, §3.5). |
| Previewability | Whether the detail page may embed it; a non-previewable member offers download only (`RP-14`). Required. |
| Semantic role | `dokument główny` · `dane szczegółowe` · `dane surowe`, declared by the definition. Required (`PBC` §3.5). Not a Workflow B `artifact_role`. |
| Main-file flag | An explicit boolean, **not** ordering — `RP-13` requires the main file to be distinguished visually, not by position. Required. |
| Content metric | The secondary fact the panel renders next to the size: `12 stron` / `3 arkusze` / `4 118 wierszy`. A metric *kind* plus a value, nullable. |
| Expiry / availability | Per-member expiry and an availability marker that survives object removal (§11). Required. |
| Display order | The panel's order. Presentation only; carries no semantics. |

### 1.4 Report Instance Seen State — CONDITIONAL, NOT BUILT

Needed only if the owner includes `NOWY` in the initial release (§13.2). Per `(account, instance)`
first-seen fact. Additive: a separate relation, referenced by nothing in §1.1–§1.3.

```
SEEN_STATE_PERSISTENCE: NOT_AUTHORIZED_NOT_BUILT   (owner decision, §13.2 = B)
```

The owner deferred it. No seen-state relation exists, `NOWY` is never rendered, and the three core
entities are unchanged by that decision — which is exactly what §13.2 predicted. It stays described
here because deferring a capability is not the same as withdrawing its design.

---

## 2. Identity invariants

| # | Invariant |
|---|---|
| I-1 | **Definition uniqueness.** The canonical type key is unique platform-wide. The display name is not an identity. |
| I-2 | **Instance uniqueness.** `(client, report type, period_key)` is unique. This is the idempotency key of the whole framework: retries, re-runs and crash recovery cannot produce a second logical instance for the same period. |
| I-3 | **Period agreement.** `period_key`, `period_start`, `period_end` and `period_kind` must be mutually consistent and are written together by the definition. `period_start <= period_end`. A key that disagrees with its dates is invalid data, not a display quirk. |
| I-4 | **Period non-overlap.** Within one `(client, type)`, instances of the same `period_kind` must not overlap. Adjacency (`RP-16`, `RP-17`) is only well defined on a non-overlapping, totally ordered period sequence. |
| I-5 | **Member uniqueness.** A member is unique within its instance by its stable object identity, and a display filename is unique within an instance. |
| I-6 | **Main-file cardinality.** **At most one** member per instance is the main file; **exactly one** where the instance has any available member. Zero-member instances have none. |
| I-7 | **Successful publication implies membership.** A successful generation publishes at least one member. "Succeeded with zero files" is not a valid published state — it would render as `Pliki wygasły`, which would be untrue. |
| I-8 | **Regeneration replaces, never forks.** Regenerating an existing period updates the same logical instance: same identity, same detail URL, same position in the period sequence. Members are replaced atomically (§4.4). |
| I-9 | **No version history.** No supersession chain, no generation-number dimension, no multiple visible instances per period. The approved product requires adjacent *period* siblings (`RP-16`) and an 8-**period** history (`PBC` §3.6) — never two rows for one period. Attempt bookkeeping is operational state on the single instance, not a user-visible version. |
| I-10 | **History outlives files.** Deleting or expiring bytes never deletes an instance (§11). |

---

## 3. Generation lifecycle

### 3.1 Durable generation state

The instance persists the state of its **latest attempt** and the fact of its **last successful
publication**. Minimum vocabulary:

```
pending    — instance exists, generation not started
running    — an attempt holds the instance
succeeded  — an attempt published members atomically
failed     — the latest attempt terminated without publishing
```

Plus a durable `last_published_at` (null until the first successful publication).

### 3.2 Presented status is derived, never stored as the badge

The four approved states (`PBC` §3.3) are a **presentation** over generation state *and* file
availability:

| Condition | Presented | Actions |
|---|---|---|
| never published, latest attempt `pending`/`running` | `W generowaniu` | none (`RP-8`) |
| never published, latest attempt `failed` | `Błąd generowania` | `Zgłoś problem` (`RP-9`) |
| published **and** ≥1 available member | `Gotowy` | `Otwórz raport`, `Pobierz` |
| published **and** 0 available members | `Pliki wygasły` | none |

Two derivation rules that must be recorded, because they decide behaviour the four states do not
name:

- **Regeneration of an already-published instance stays `Gotowy`.** Its files are still downloadable
  and its actions are still valid; flipping it to `W generowaniu` would remove working actions
  (`RP-8`) and hide files that exist. A refresh is invisible to the reader.
- **A failed attempt over a still-published instance stays `Gotowy`** for the same reason. The
  failure is operational state (attempt bookkeeping, alerting), not a user-facing status that would
  contradict the files on screen. The rail's attention dot (`PBC` §3.1, *"an attention dot when the
  latest instance failed"*) reads the presented status of the newest instance, so it fires on a
  genuinely file-less failure.

No fifth badge is invented, and no state renders a disabled control.

### 3.3 Which layer owns which transition

| Layer | Owns |
|---|---|
| Generation lifecycle | `pending → running → succeeded \| failed`, retries, `last_published_at` |
| File availability | member expiry, member removal, the `Gotowy ⇄ Pliki wygasły` boundary |

File expiry **never** rewrites generation state. Generation state **never** decides whether bytes
still exist.

### 3.4 Library timestamp

The library groups by generation month and orders by generation time (`PBC` §3.1, `RP-3`), including
rows that have not finished — the prototype shows `W generowaniu` in the July group with
`rozpoczęto 22.07 04:00`, and `Błąd generowania` with the time its attempt ended.

```
library_timestamp = COALESCE(generation_finished_at, generation_started_at)
```

This single derived value is the grouping key, the ordering key and the `Rok` filter axis. It must be
persisted or trivially indexable (§10). `Wygenerowano` renders `generation_finished_at` for
published instances and `rozpoczęto <generation_started_at>` while an unpublished attempt runs.

### 3.5 Availability denormalization

`Status` is a toolbar filter and the footer counter must be exact (`PBC` §3.4, `RP-5`). Deriving
`Gotowy` vs `Pliki wygasły` by scanning members per row would make filtering and counting unstable
and unindexable. The instance therefore maintains an availability summary — an available-member
count and/or an effective expiry instant — updated in the **same transaction** as any member
publication, expiry or removal.

### 3.6 Crash, retry and idempotency

Conceptual requirements only; mechanism is the schema task's choice, but the repository already has a
proven pattern in `database_export_jobs` (claim token + lease + status-conditional transitions,
`db/migrations/043`, `docs/31` §9).

| Requirement | Contract |
|---|---|
| No duplicate instances | Guaranteed structurally by I-2, not by generator discipline. The generator upserts on `(client, type, period_key)`. |
| No double publication | An attempt publishes only while it still holds the instance; a stale attempt must be unable to publish over a newer one. |
| Crash recovery | A `running` attempt that lost its holder is recoverable without operator intervention and without a second instance. |
| Retry safety | A retry of a failed period reuses the same instance and leaves its identity, URL and period untouched. |
| Orphaned bytes | Objects written by an attempt that never published must be identifiable for cleanup, exactly as `database_export_attempt_objects` does for exports. |

---

## 4. Materialization contract

### 4.1 When a file becomes a member

A file is a member **only** when the generation attempt publishes it. Bytes written by an in-flight
attempt are not members, are not counted, are not listed and are not downloadable.

### 4.2 Publication is atomic to the consumer

From Report Explorer's perspective an instance transitions to published **once**, with its complete
member set. There is no observable window in which an instance is `Gotowy` with a partial file list.
This mirrors the export worker, which inserts the artifact row and flips the job to `completed` in
one fenced transaction (`ops/database_export_worker.py`).

### 4.3 The cases

| Case | Contract |
|---|---|
| In progress | Instance exists, zero members, `W generowaniu`, no actions |
| Failure before publication | Instance exists, zero members, `Błąd generowania`, `Zgłoś problem` only |
| Success | ≥1 member (I-7), exactly one main file (I-6), `Gotowy` |
| Partial failure | Not publishable. Either the whole declared member set publishes, or the attempt fails with zero members. No half-published instance is visible. |
| Multiple outputs | Normal (`D-013`, `PBC` §3.5): several formats of one content, or genuinely different attachments; each with its own size, role and previewability |
| Expiry | Members become unavailable; the instance and its member records survive (§11) |
| Regeneration | New member set published atomically; the previous set is superseded in the same transaction and its objects queued for cleanup (I-8) |

### 4.4 Member replacement

Regeneration must not leave two live member sets. Replacement is a single transaction: supersede the
old members, insert the new ones, update the availability summary and the generation timestamps.

---

## 5. Reporting-period contract

Extends `docs/39` §4–§5, which remain binding: the period is a declaration of the report definition
and is **never** inferred from generation time, execution time, file creation time, filename,
systemd schedule or Workflow B schedule.

| Element | Contract |
|---|---|
| `period_key` | The stable identity and the `Numer okresu` display value: `2026-W28`, `2026-07`, `2026-Q3`. **A stable key, not an incrementing counter.** Unique within `(client, type)` (I-2). |
| `period_start` | First day covered, inclusive. |
| `period_end` | Last day covered, inclusive — the approved rendering `06–12.07.2026` is inclusive-inclusive. |
| `period_kind` | The vocabulary the key belongs to (`week`, `month`, `quarter`, …, plus a non-periodic marker for on-demand types). Persisted so I-3/I-4 are checkable. |
| Boundary semantics | Declared explicitly by the definition. Default for this platform: **calendar dates in `Europe/Warsaw`**, inclusive both ends. A definition meaning anything else must say so; a reader must never assume. |
| Instant range | Where an instant range is required — notably the `Dane źródłowe` filter (`RP-18`) — it is the derived half-open interval `[period_start 00:00, period_end + 1 day 00:00)` in the declared zone. Derived, not separately stored. |
| Display label | The library/period-navigation label (`Tydzień 28 · 2026`, `06–12.07.2026`). Derived from `period_kind` + dates, or persisted with the instance display name. |
| Validation | `period_start <= period_end`; key/kind/date agreement (I-3); no overlap within `(client, type, period_kind)` (I-4). |
| Relation to cadence | Cadence (§6) states how often instances are produced; the period states what an instance covers. They are related but independent: a weekly type generated on Wednesday for the previous Mon–Sun week has cadence `tygodniowy · śr. 04:00` and period `2026-W28`. |
| Relation to generation timestamp | Strictly distinct. The prototype's `Tydzień 28` covers `06–12.07.2026` and was generated `15.07.2026 04:22`. A design carrying one timestamp cannot render both. |

Report Explorer **never** computes a period. It reads what the definition wrote.

---

## 6. Cadence contract

Cadence is **report-definition metadata** (`RP-2`, `PBC` §3.1, §3.6), rendered in the rail and in the
detail metadata grid.

- Approved vocabulary: `tygodniowy` · `miesięczny` · `kwartalny` · `na żądanie`
  (`COPY_AND_TERMINOLOGY` §6), rendered with its schedule detail where one exists:
  `tygodniowy · pon. 04:00`, `miesięczny · 1. dnia`, `kwartalny`, `na żądanie · systemowy`.
- Cadence is a **product statement about the report type's business cycle**. It is not derived from,
  and must not be read out of, systemd timers, cron entries, worker queues, Workflow B mail
  schedules or ingestion frequency (`docs/39` §12).
- The persisted form must carry both the cadence class and its human detail, so the rail can render
  the full string without re-deriving it.
- A non-cyclical type declares the on-demand cadence explicitly and produces no period sequence
  (§9, §13.1).
- Keeping the declared cadence and the actual schedule in agreement is an operational concern of the
  generator, not a Report Explorer read-path concern.

---

## 7. File-member contract

| Fact | Source | Required |
|---|---|---|
| Format badge (`PDF`/`XLSX`/`CSV`) | member | yes (`RP-10`) |
| Byte size, per file, in the library and the panel | member | yes (`PBC` §3.2, §3.5) |
| Display filename | member | yes |
| Previewable | member | yes (`RP-14`) |
| Semantic role (`dokument główny`, `dane szczegółowe`, `dane surowe`) | definition's file contract, stamped on the member | yes (`PBC` §3.5) |
| Main file | explicit flag (I-6) | yes (`RP-13`) |
| Content metric (`12 stron`, `3 arkusze`, `4 118 wierszy`) | member | where the design renders it |
| Expiry / availability | member (§11) | yes |
| Download / preview target | stable object identity (§8) | yes |

`Pobierz wszystkie (n)` where `n > 1`, plain `Pobierz` at `n = 1` (`RP-21`), is a presentation rule
over the available-member count — no extra persistence.

---

## 8. Byte storage — reuse recommendation

| Candidate | Classification | Recommendation |
|---|---|---|
| `artifacts` (`artifact_id`, `storage_backend`, `storage_key`, `sha256`, `size_bytes`, `content_type`, `expires_at`, `preview`) | **REUSE_AS_INFRASTRUCTURE** | **Recommended** as the byte-storage and delivery primitive |
| `_stream_artifact_download`, `_read_artifact_preview_bytes`, the artifact preview/download routes | **REUSE_AS_INFRASTRUCTURE** | Reuse permitted and expected |
| `portal_clients` / `portal_user_clients` / `portal_groups` / `portal_group_clients` + `_get_effective_client_access_for_user` | **REUSE_AS_DOMAIN_MODEL** | Required — the authorization boundary (§9) |
| `portal_database_datasets` | **REUSE_AS_DOMAIN_MODEL** | Required — the `Dane źródłowe` binding (§12) |
| `database_export_jobs` | **REUSE_AS_DOMAIN_MODEL, for its own provider only** | Adapter (§9); never a store for cyclical instances |
| `ingest.raw_file`, Workflow B lineage, `artifacts.report_type` / `artifact_role` / `raw_file_id` | **NEITHER** | Out of scope (`docs/39` §2, §5) |
| `portal_report_folders` / `portal_report_folder_users` | **NEITHER** | Artifact-query access grants; not the generated-report authorization model (`docs/39` §11) |

**Why `artifacts` as infrastructure is the grounded choice.** `db/migrations/043` already extended
`artifacts` with `owner_user_id`, `expires_at` and `expired_at` precisely so a platform-generated
file could be stored, expired and delivered; `ops/database_export_worker.py` already publishes a
generated file that way while keeping its *domain* identity in `database_export_jobs`. The
generated-report framework follows the identical split:

```
domain meaning  →  report instance + file member   (new relations)
bytes + delivery →  artifacts row + storage backend (existing infrastructure)
```

**The coupling this must not create.** Referencing an `artifacts` row for bytes does **not** make
Workflow B the report source, does not import `report_type` / `artifact_role` / `raw_file_id`
semantics, and does not make Workflow B artifacts visible in Report Explorer. Report Explorer reads
the domain relations only; a member's artifact reference is an implementation pointer. A generated
report member carries no `run_id`, no `raw_file_id` and no workflow lineage, and its `owner_user_id`
is null — a generated report belongs to a **client**, not to a requesting user.

The member record must survive its artifact row (nullable reference, cleared on object cleanup) so
`Pliki wygasły` and the history panel stay truthful (§11).

---

## 9. Authorization

```
GENERATED_REPORT_VISIBILITY =
      authenticated portal account
  AND portal_clients.is_active
  AND effective can_view_reports for that client   (direct OR active-group, additive OR)
```

Grounded in the existing model, unchanged by this document:

- `portal_user_clients.can_view_reports` (`db/migrations/033`) and
  `portal_group_clients.can_view_reports` (`db/migrations/037`);
- `_get_effective_client_access_for_user` in `api/main.py` — direct OR active-group `bool_or`,
  with `portal_clients.is_active` reported and gated separately. This is the canonical helper and
  must be the single gate.

Settled points:

| Question | Answer |
|---|---|
| Does a generated report require a `portal_report_folders` grant? | **No.** Folders are an artifact-query access mechanism (`docs/39` §11). Requiring one would gate generated reports on artifact plumbing. |
| Does `RP-19` still hold? | **Yes, and more truthfully.** `RP-19` requires an account with dataset access but not report access to see a state explaining that the two are separate grants. Under this model that is exactly `can_view_database = true` and `can_view_reports = false` for the same client — two independent flags on the same relation. |
| Per-report-type grants in the initial release? | **No.** No approved criterion requires per-type authorization. A future per-type grant is an additive extension and is explicitly out of the core schema scope. |
| Database exports? | Additionally **owner-scoped** — visible only to `database_export_jobs.requested_by_user_id` (§10.2). |
| Preview vs download split? | Not required for generated reports. `RP-14`'s download-only case is a **file-format** property (previewability), not a permission. The folder-level `can_preview` / `can_download` flags are not part of this model. |

This document creates no authorization schema and changes no authorization behaviour.

---

## 10. Report Explorer provider interface

`S15` consumes **one normalized domain interface** with two providers. The interface is a product
abstraction; the providers are not forced into shared storage.

### 10.1 Normalized fields and actions

| Field | Notes |
|---|---|
| `provider` | `generated_report` \| `database_export` |
| `instance_id` | Provider-scoped stable identity; the detail URL |
| `client_code` | Required by both providers |
| `type_key`, `type_label`, `type_description` | Rail identity and header copy |
| `cadence_label` | `tygodniowy · pon. 04:00` … `na żądanie · systemowy` |
| `period_key`, `period_start`, `period_end`, `period_label` | Nullable **only** for a provider that declares no period (§13.1) |
| `instance_label` | `Tydzień 28 · 2026`; the text-search subject with the period label |
| `library_timestamp` | Grouping/ordering axis (§3.4) |
| `generated_at_display` | `Wygenerowano`, or `rozpoczęto …` while running |
| `status` | One of the four approved states, derived per §3.2 |
| `row_count` | `Wiersze w raporcie`; nullable |
| `retention_until` | `Retencja plików`; nullable |
| `source_dataset_ref` | Dataset + applied filter for `Dane źródłowe` (§12); nullable |
| `files[]` | Format, filename, size, previewable, semantic role, main flag, content metric, download/preview target |
| `capabilities` | Per-provider declaration: supports period siblings, supports history, supports per-file roles |
| `siblings` | Previous/next per §11.3; absent where unsupported or at a boundary (`RP-17`) |
| `actions` | Derived from status (§3.2); an unavailable action is **absent**, never disabled |

### 10.2 Provider 2 — database exports (adapter, no duplication)

Every field is satisfiable from state already persisted by `db/migrations/043`/`044`/`065`, with one
open display decision:

| Normalized field | Existing source |
|---|---|
| identity | `database_export_jobs.job_id` |
| type | fixed `Eksporty danych`, cadence `na żądanie · systemowy` |
| client | `portal_database_datasets.client_code` via `dataset_id` |
| status | `completed` inside retention → `Gotowy`; `expired`, or `completed` past `expires_at` → `Pliki wygasły` (`docs/31` §8) |
| generation timestamp | `completed_at` |
| row count | `row_count` |
| retention | `expires_at` |
| source data | `dataset_id` + `request_snapshot_json` — the export's own replayable filter snapshot |
| file | `artifact_id` → `artifacts` (filename, size, content type); single member, main file, `csv`/`xlsx` → not previewable → `Pobierz` only (`RP-14`) |
| visibility | owner-scoped `requested_by_user_id`, **plus** effective `can_view_reports` on the dataset's client |
| period | **OWNER DECISION 1** (§13.1) |

Recorded, product-settled:

- **Scope of the adapter:** *completed* exports (including completed-then-expired). `PBC` §2.13 and
  `DB-53` require a **completed** export to be reachable. `queued` / `running` / `failed` /
  `cancelled` stay on the export management surface (`DB-007`), which remains separate and valid.
- **Same library, two product purposes:** `Eksporty danych` is a system report type inside the
  Report Explorer library; the export management page keeps its own lifecycle actions
  (`Anuluj`, `Zleć ponownie`, `Kopiuj ref`), which are **not** report actions.
- **No duplication.** Export rows are never copied into generated-report storage. Doing so would
  create two identities, two lifecycles and a synchronization problem for zero product gain.

```
DB_53_SCHEMA_CHANGE: NOT_REQUIRED
```

---

## 11. History, siblings and retention

### 11.1 Retention splits into two layers

| Layer | Lifetime | Rule |
|---|---|---|
| Instance record (period, status, `Wygenerowano`, row count, provenance) | durable history | **Never deleted because file retention elapsed** |
| File members / bytes | retention window | expire and are removed |

`Pliki wygasły` is exactly the state where the first survives the second (`PBC` §3.3 — *"history
retains the record"*). The prototype's history panel proves the requirement: `2026-W22` and
`2026-W21` render period, status, generation time and `0` files.

Member records persist after their bytes are gone, carrying an availability marker and their expiry;
the artifact reference is cleared on object cleanup. Available-member count reaching zero is what
moves a published instance to `Pliki wygasły` (§3.5).

`Retencja plików` renders the instance's effective retention — the latest expiry among its members,
or the definition's declared policy applied to the generation timestamp when no member remains.

### 11.2 History panel

Last 8 periods of the same `(client, type)` ordered by period, each with its own period, status,
generation time and file count, current period highlighted, expandable to all periods
(`PBC` §3.6, `RP-15`). Requires only the instance record — never the files.

### 11.3 Sibling navigation

- **Generated reports:** previous/next by adjacent **reporting period** within the same
  `(client, type)`, ordered by `period_start` (not by key text, not by generation time), library
  filters preserved (`RP-16`). At the newest period the forward control is **absent**, not disabled
  (`RP-17`). I-4 is what makes "adjacent" well defined.
- **Database exports:** no cyclical period exists, and none is invented. The provider declares
  period-sibling support as unavailable; where a previous/next affordance is shown at all it orders
  the same owner's completed exports of the same dataset by completion time descending. No approved
  criterion requires export history — `DB-53` requires reachability only — so the panel may be
  truthfully absent. This stays compatible with either resolution of §13.1.

---

## 12. Source-data provenance (`RP-18`)

`Dane źródłowe` must navigate to Database Explorer with **this report's dataset and this report's
period applied as a filter**, when the account has access to that dataset (`RP-18`, `PBC` §3.6).

Pointing at current mutable dataset configuration is not sufficient: datasets are renamed,
deactivated, re-pointed at another table and deleted (`portal_database_datasets`, `db/migrations/035`),
while a report instance is a historical fact that must stay truthful years later.

**Minimum semantic contract:**

| Level | Contract |
|---|---|
| Definition | Declares its source binding **client-independently**, by dataset `slug` — `portal_database_datasets` is `UNIQUE (client_code, slug)`, so one definition serves many clients by resolving the slug per client at generation time. Also declares the date column used for the period filter, if any. |
| Instance | Persists the **resolved** dataset reference plus a durable descriptive snapshot: dataset id, slug, dataset display name, the date column used and the exact applied period range. |
| Read path | Builds the `Dane źródłowe` link from the snapshot; checks live access with the existing dataset-access helpers; degrades to a truthful unavailable state when the dataset no longer exists or the account lacks access — never to a silent wrong link. |

**Schema impact: yes** — instance-level provenance columns, plus the definition-level binding. Small,
but it is core scope (§14): without it `RP-18` is unimplementable truthfully. `docs/31` shows the
repository already accepts this pattern — `database_export_jobs.request_snapshot_json` is a
replayable snapshot of exactly this kind.

---

## 13. Owner decisions — RESOLVED 2026-08-19

Both decisions were open when this document was written and neither blocked defining the core
persistence scope. Both are now resolved by the owner; the options are preserved so the resolution
is readable against what was actually weighed.

```
DECISION_1_EKSPORTY_DANYCH_PERIOD: A   — render `nie dotyczy`, order by generation time
DECISION_2_NOWY:                   B   — defer post-initial-release, no seen-state persistence
```

### 13.1 Decision 1 — `Eksporty danych` period presentation

The approved prototype establishes the type and its cadence (`na żądanie · systemowy`) but shows no
export instance row, so `Okres raportowania` and `Numer okresu` for an export are unspecified.

| Option | Behaviour | Note |
|---|---|---|
| **A** | Render `nie dotyczy`; identify and order exports by generation time | **RECOMMENDED** — truthful, invents nothing |
| **B** | Where the export request explicitly declares a data date range, show that range; otherwise `—` | Truthful; requires reading `request_snapshot_json` and accepting a mixed population |
| **C** | Treat the request/generation date as the reporting period | **NOT RECOMMENDED** — invents reporting-period semantics from execution time, contradicting `docs/39` §5 |

**Persistence impact: none, under A or B.** The normalized interface already allows a null period for
a provider that declares no period support, and B reads a snapshot that `database_export_jobs`
already persists. Neither option forces export rows into cyclical instance storage.

> **RESOLVED — Option A.** An export declares no reporting period. `Okres raportowania` and
> `Numer okresu` render `nie dotyczy`, exports are identified and ordered by their completion
> timestamp, and the provider declares period-sibling support as unavailable (§11.3). Nothing is
> read out of `request_snapshot_json` for period purposes, so no export can acquire a reporting
> period it does not have. `DB_53_SCHEMA_CHANGE: NOT_REQUIRED` is unaffected.

### 13.2 Decision 2 — `NOWY`

`NOWY` marks an **unseen** instance (`PBC` §3.2, `COPY_AND_TERMINOLOGY` §6 — *"Unseen marker"*). No
`RP-` criterion tests it.

| Option | Consequence |
|---|---|
| **A — include in the initial release** | Adds **one** additive relation: per-`(account, instance)` seen state. **Recommended by the approved contract**, which names the badge. |
| **B — defer post-initial-release** | No seen-state persistence now; `NOWY` is simply never rendered. |

**Effect on the core schema: none, either way.** Seen state references the instance and is referenced
by nothing; the definition, instance and member contracts are byte-for-byte identical under A and B.
A is one extra table plus a read-path join; B is that table's absence. This is an *optional
capability*, not a core-schema variable.

> **RESOLVED — Option B, deferred.** No seen-state relation is created and no per-account read
> tracking exists. `NOWY` is not rendered anywhere in the initial release. No `RP-` criterion tests
> the badge, so its absence costs no approved acceptance criterion, and re-adding it later is one
> additive relation plus a read-path join exactly as described above — the core schema does not have
> to move.

---

## 14. Schema authorization package — GRANTED 2026-08-19

```
S15_SCHEMA_AUTHORIZATION: GRANTED_FOR_CAPABILITIES_1_2_3
                          CAPABILITY_4_NOT_GRANTED_DEFERRED
```

The minimum conceptual schema capability that had to be explicitly authorized before any DDL could
be written. The owner granted capabilities 1, 2 and 3 and withheld capability 4.

**What the grant covers.** The three core capabilities below and the auxiliary indexes, constraints
and DB-native enforcement that serve them. It is not a grant to add a fourth durable business
entity: if correct implementation had turned out to need one, the implementation was required to
stop and return `S15_SCHEMA_SCOPE_EXPANSION_REQUIRED` rather than widen the schema quietly. It did
not — the three capabilities were sufficient.

**What the grant does not cover.** Applying the migration to production, historical backfill,
converting existing ingestion artifacts into instances, deployment, or any production write. Those
remain a separate authorization gate.

| # | Capability | Scope | Status |
|---|---|---|---|
| 1 | **Generated-report definition/type persistence** | canonical type key, display name, description, cadence class + detail, period kind + boundary semantics, source-dataset binding (slug) + date column, generation-definition reference, declared file contract (formats, roles, main file), retention policy, enablement, client scope | **CORE — AUTHORIZED** |
| 2 | **Generated-report instance persistence** | identity; `client_code` → `portal_clients`; definition reference; `period_key` / `period_start` / `period_end` / `period_kind`; display name; generation state + attempt bookkeeping; `generation_started_at` / `generation_finished_at` / `last_published_at`; row count; source-data provenance snapshot; availability summary; `created_at` / `updated_at`; **unique `(client, type, period_key)`** | **CORE — AUTHORIZED** |
| 3 | **Generated-report file-member persistence** | instance reference; object reference (nullable `artifacts` pointer); display filename; format + content type; byte size; previewable; semantic role; explicit main-file flag with at-most-one-per-instance enforcement; content metric kind + value; expiry + availability marker; display order | **CORE — AUTHORIZED** |
| 4 | **Per-account report-instance seen state** | `(account, instance)` first-seen fact | **CONDITIONAL — NOT AUTHORIZED, deferred (§13.2 = B)** |

Explicitly **out** of this authorization request: tags, favourites, custom folders, global search,
sharing, subscriptions, analytics, per-report-type grants, Workflow B integration, artifact backfill,
any change to `artifacts`, `portal_report_folders`, `database_export_jobs` or `ingest.*`.

### 14.1 Schema-design constraints the later migration must respect

| Concern | Constraint |
|---|---|
| Location | Platform database, alongside the other `portal_*` relations (`db/migrations/`), not a per-client business database. Report Explorer is a portal surface reading the platform's own generated output. |
| Naming | Follow the `portal_*` convention of `033`–`067`. |
| Client separation | By `client_code` FK to `portal_clients`, matching `portal_database_datasets`; every instance query is client-scoped first. |
| Foreign keys | instance → definition: **RESTRICT** (a type with history is never deleted out from under it); instance → `portal_clients`: match the existing portal convention; member → instance: **CASCADE**; member → `artifacts`: **SET NULL**, so cleanup never destroys the member record (§11.1); instance → `portal_database_datasets`: must not delete history — a nullable reference plus the descriptive snapshot (§12). |
| Uniqueness | I-1, I-2, I-5; main-file cardinality (I-6) enforced structurally, e.g. a partial unique index, not application discipline. |
| Nullability | Required: client, type, period fields, generation state, `created_at`. Nullable with meaning: `generation_finished_at` (unfinished), `last_published_at` (never published), `row_count` (not yet known / not applicable), member expiry (no retention), artifact reference (bytes cleaned up). |
| Lifecycle integrity | Status vocabulary constrained by CHECK, as `database_export_jobs` does; the availability summary maintained in the same transaction as member changes (§3.5). |
| Idempotent generation | The uniqueness constraint is the idempotency mechanism (I-2); the generator must not rely on read-then-write. |
| Retention | No cascade path may delete an instance because bytes expired (§11.1). |
| Extension boundary | Seen state, per-type grants and subscriptions must be addable as separate relations without altering §14 items 1–3. |

### 14.2 Query axes the schema must serve efficiently

Physical indexes are the migration task's decision; these are the axes (`PBC` §3.1, §3.4, `RP-3`–`RP-7`):

- library page: `(client_code, type)` filtered, ordered by `library_timestamp` DESC — with type, year
  and status filters applied, paginated 25–500 (default 100);
- rail: instance count per `(client_code, type)`, plus the newest instance per type for the attention
  dot;
- counters: filtered count and library total for the same client;
- detail: instance by identity; its members; its 8 most recent periods by `period_start` DESC;
- siblings: previous/next by `period_start` within `(client_code, type)`;
- text search: instance display name and period label;
- retention sweep: members due to expire.

---

## 15. What this document does not do

It writes no DDL, allocates no migration number, changes no schema, implements no Report Explorer UI
or API, implements no report-generation job, writes no report SQL, backfills nothing, mutates no
database, touches no approved design-handoff file, and authorizes no production action. It is a
design contract awaiting the authorization requested in §14.
