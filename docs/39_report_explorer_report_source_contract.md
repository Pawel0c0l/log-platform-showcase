# 39 — Report Explorer report-source and reporting-period contract

Authoritative record of **where `S15` Report Explorer report instances come from**, what a report
definition must declare to be shown there, and which schema work `S15` does and does not require.

This document is the single owner of that decision. `docs/38_portal_initial_release_plan.md` §5
references it and does not restate it; `docs/40` derives the persistence contract from it and
`docs/41` records what was built. This document itself changes no code, no schema and no migration,
and it edits nothing inside the approved design handoff
(`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`), which stays
read-only.

---

## 1. Owner decision

```
S15_REPORT_SOURCE:                              SYSTEM_GENERATED_SQL_REPORTS_ONLY
S15_WORKFLOW_B_ARTIFACTS:                       OUT_OF_SCOPE
S15_ARTIFACT_BACKFILL:                          NOT_REQUIRED
S15_REPORTING_PERIOD_SOURCE:                    EXPLICIT_SQL_REPORT_CONTRACT
S15_SCHEMA_CHANGE_FOR_ARTIFACT_PERIOD_INFERENCE: NONE
S15_REPORT_IDENTITY_FROM_FILENAME:              FORBIDDEN
S15_GENERATED_REPORT_SCHEMA:                    AUTHORIZED_2026_08_19        (docs/40 §14)
S15_GENERATED_REPORT_SCHEMA_BUILT:              db/migrations/068            (docs/41 §1)
S15_MIGRATION_APPLIED_TO_PRODUCTION:            NO
```

Two of these read as prohibitions and are worth stating as one rule, because every heuristic this
document rejects is a variation of it:

> **A filename, a folder name and a display label are presentation. They are never report identity,
> never a reporting period and never a file's semantic role.** Identity is a surrogate key the
> generation contract writes; the period is a declaration; the role is an explicit field.

Report Explorer shows **only reports the platform intentionally generates from data it already
stores**, through dedicated report-specific SQL/report definitions. Files that arrive from outside —
e-mail-ingested reports and the pipeline outputs derived from them — are not Report Explorer report
instances.

> **Status, 2026-08-19.** When this decision was recorded, that generated-report domain did not
> exist. It does now: `db/migrations/068` creates it and `docs/41` records what was built. The
> decision itself is unchanged — no artifact was converted, no period was inferred and no historical
> row was backfilled. A *cyclical* SQL report generator is still separate, still unauthorized work.

> **Report Explorer is not Artifact Explorer and is not a Workflow B report browser.**

---

## 2. Two domains, deliberately separate

| | **Artifact / Workflow B domain** | **Report Explorer domain (`S15`)** |
|---|---|---|
| What it holds | files that *arrived* — e-mail-ingested reports and the pipeline outputs derived from them | reports the platform *generated* from data it already stores |
| Storage today | `ingest.raw_file`, `artifacts` (`kind`, `artifact_role`, `report_type`, `metadata_json`), Workflow B stage outputs | `portal_generated_report_definitions` / `_instances` / `_files` (`db/migrations/068`, `docs/41` §1); bytes reuse `artifacts` as infrastructure only |
| Unit of work | an artifact | a report instance for a reporting period |
| Identity | pipeline lineage (raw file → run → stage → artifact) | an explicit, stable report-instance identity declared by the report definition |
| User surface | `Artefakty` — operator triage, explicitly **not** a client-data access mode (`docs/34` §1, `PRODUCT_BEHAVIOR_CONTRACT.md` §0) | `Raporty` — a client-facing data-access mode (`PRODUCT_BEHAVIOR_CONTRACT.md` §0, §3) |
| Feeds `S15`? | **No** | **Yes** |

The approved contract already draws this line. `PRODUCT_BEHAVIOR_CONTRACT.md` §0 lists `Raporty` as
the mode whose purpose is to *"consume generated/cyclical deliverables"* with the unit of work *"a
report instance for a period"*, and lists `Artefakty` separately as *"technical/admin artifact
inspection … **not** a data-access mode"*. The two **MUST NOT** be collapsed. This document records
that the underlying data source follows the same separation.

---

## 3. What the approved product contract actually requires

Checked against the approved handoff, not against implementation history:

| Question | Finding |
|---|---|
| Do `REP-001`–`REP-004` require artifacts? | **No.** `SCREEN_CATALOG.md` describes browsing instances by client → type → period, a detail page with preview/history, and the library states. No screen names a storage table. |
| Do `RP-1`–`RP-21` require artifacts, Workflow B, e-mail or `portal_report_folders` as the *source*? | **No.** None of the 21 criteria names a source table or ingestion path. `RP-19` names *report-folder access* as an **authorization** distinction, not as the report-instance source. |
| Do `SH-12` / `SH-13` require them? | **No.** They constrain breadcrumbs on `REP-003`. |
| Does anything in the handoff bind Report Explorer to a storage model? | **No.** `REPOSITORY_IMPLEMENTATION_GUIDANCE.md` maps one bridge — a completed Database Explorer background export surfacing as the report type `Eksporty danych` (`DB-53`) — see §11. |

**Conclusion.** The approved product criteria constrain *behaviour*, never *provenance*. The earlier
coupling of `S15` to artifacts was a repository implementation assumption, not a product
requirement. Correcting the source model therefore weakens no approved criterion: the client → type
→ period axis, report detail, embedded preview, per-file actions, period siblings, same-report
history, the required empty / no-access / file-store-error states, and the not-a-data-grid rule
(`RP-1`) all stand unchanged.

---

## 4. Reporting-period contract

> **Every report exposed through Report Explorer must explicitly return or declare its authoritative
> reporting period, at minimum `period_start` and `period_end`, with defined boundary semantics.
> Reporting period is part of the report-generation contract and is not inferred from execution
> time, file creation time, receipt time, filename, or Workflow B artifact metadata.**

The reporting period comes from the **business logic of the report definition** — the period the
report is *about*. It is a different fact from when the report was produced, and the approved design
already treats them as different fields: `PRODUCT_BEHAVIOR_CONTRACT.md` §3.1 groups instances by
**generation** month while §3.2 and §3.6 render `Okres raportowania` as its own value alongside
`Wygenerowano`. A design that carried only one timestamp could not satisfy both.

Boundary semantics (inclusive/exclusive end, time zone) must be stated by the report definition, not
assumed by the reader. `Europe/Warsaw` is the platform's business time zone elsewhere; a report
definition that means something else must say so.

Presentation fields the approved contract also requires per instance or per type — `Numer okresu`
(§3.6), the type's `Cykl` (§3.1, `RP-2`), and the period's display label used in group headings and
period navigation (§3.1, §3.7) — are **report-contract requirements**, satisfied either by direct
declaration or by derivation from a declared period kind. They are recorded here as requirements on
the future contract, not as prescribed columns.

**This document prescribes no physical schema.** It states what a future report definition must
*mean*, not where it is stored.

---

## 5. No period inference

A Report Explorer implementation **MUST NOT** treat any of the following as the authoritative
reporting period, unless a future report definition itself explicitly defines that value as its
period:

- `generated_at` / generation or execution time;
- `artifacts.created_at` or any artifact timestamp;
- e-mail receipt time;
- filename or `display_filename`;
- `artifacts.report_type` or any `report_key`-style label;
- `raw_file_id` or raw-file arrival time;
- Workflow B stage timestamps or run timestamps;
- existing `metadata_json` contents.

Grouping the library by generation month (§3.1) is a **presentation** rule over a separately
declared generation timestamp. It is not, and must not become, a substitute for the reporting
period.

---

## 6. No artifact backfill

```
S15_ARTIFACT_BACKFILL: NOT_REQUIRED
```

`S15` does not require classifying, repairing or backfilling the current artifact population.
Artifact rows without a client, a report type or a period stay irrelevant to the Report Explorer
library, because artifacts are not Report Explorer report instances. No heuristic period inference,
no compatibility migration and no artifact data mutation is authorized or needed for `S15`.

---

## 7. Contract for a future Report Explorer-compatible report

When the report-generation framework is designed — a separate, separately authorized piece of work —
a report definition intended for Report Explorer must provide or declare at least:

1. **canonical report type** — the stable type identity the left rail groups by;
2. **client** — the client the instance belongs to, in platform client-code terms;
3. **authoritative reporting period** — `period_start`, `period_end`, declared boundary semantics
   (§4);
4. **generation timestamp** — when this instance was produced, distinct from (3);
5. **stable report-instance identity** — an explicit identity, or a deterministic identity contract
   sufficient to recognise the same instance across regeneration, so that period siblings and
   same-report history (`RP-15`, `RP-16`) resolve without heuristics;
6. **lifecycle/status** — enough to render the four approved states `Gotowy`, `W generowaniu`,
   `Błąd generowania`, `Pliki wygasły` (`PRODUCT_BEHAVIOR_CONTRACT.md` §3.3), since status governs
   which actions exist at all (`RP-8`, `RP-9`);
7. **file/member metadata**, when the report produces files — per file: format, filename, size,
   previewability, and the semantic role the detail page renders (`dokument główny`, `dane
   szczegółowe`, `dane surowe`), including which file is the main one (`RP-13`, §3.5);
8. **generation cycle** (`Cykl`) — required by `RP-2` for the rail and by
   `PRODUCT_BEHAVIOR_CONTRACT.md` §3.6 for the detail metadata grid;
9. **period key** (`Numer okresu`, e.g. `2026-W28`) — a field of the approved 8-field metadata grid
   (§3.6). A stable period key, never an incrementing counter;
10. **row count** (`Wiersze w raporcie`) — a field of the same metadata grid, and it must survive
    file expiry;
11. **file retention** (`Retencja plików`) — a field of the same metadata grid;
12. **source dataset binding** — the dataset and the applied period filter `Dane źródłowe` navigates
    to, required by `RP-18`.

> **Correction.** An earlier revision of this section described items 8–12 as
> *"optional-but-approved"*. That was wrong. Checked against the approved handoff, every one of them
> is **mandatory product-contract data**: `Cykl` is tested by `RP-2`, the dataset binding by `RP-18`,
> and `Numer okresu`, `Wiersze w raporcie` and `Retencja plików` are three of the eight fields the
> approved `REP-003` metadata grid renders (`PRODUCT_BEHAVIOR_CONTRACT.md` §3.6,
> `COPY_AND_TERMINOLOGY.md` §6). A report definition that omits any of them cannot render the
> approved detail page.

The purpose of this list is that a future report-generation agent produces data Report Explorer can
consume **without heuristics**. It is not a table design, and it does not authorize one — the
durable persistence contract derived from this list is
`docs/40_report_generation_framework_persistence_contract.md`.

---

## 8. Schema position for `S15`

```
S15_ARTIFACT_SCHEMA_CHANGE:      NOT_REQUIRED       (unchanged — `artifacts` is not retrofitted)
S15_REPORT_GENERATION_CONTRACT:  REQUIRED           (unchanged — recorded in docs/40)
S15_GENERATED_REPORT_SCHEMA:     AUTHORIZED         (owner, 2026-08-19; docs/40 §14 capabilities 1–3)
```

> **Status update, 2026-08-19.** The sentence below that read *"no migration is authorized or
> justified now"* described the state of this document when it settled the **source** question. The
> owner has since granted the `docs/40` §14 schema authorization for the three core generated-report
> capabilities, so `S15` now creates its own additive migration for generated-report definitions,
> instances and file members. **Nothing else in this document changes**: `artifacts` is still not
> retrofitted, `portal_report_folders` is still not the navigation model, no reporting period is ever
> inferred, and no historical artifact is ever converted into a report instance. The authorized
> schema is exactly the *new* domain this document said the source decision implied — never a
> migration of the old one.

This supersedes the earlier planning conclusion that `S15` required schema authorization *because
the artifacts model lacks a reporting period and a report-instance identity*. That conclusion was
technically correct about `artifacts` (§9) and irrelevant to `S15` once artifacts stopped being the
source.

The truthful distinction:

- **no schema change is required now** to retrofit Workflow B / artifacts for `S15`, and none is
  authorized;
- **the future report-generation framework may need its own storage**, and if it does, that schema
  is designed and explicitly authorized as part of *that* work, under the normal migration rules —
  not under `S15` and not under this document.

That framework's design is now complete and recorded in
`docs/40_report_generation_framework_persistence_contract.md`, which establishes that it **does**
need storage of its own and states the exact conceptual capabilities awaiting authorization
(`S15_SCHEMA_AUTHORIZATION_REQUIRED`). Nothing in that document is itself an authorization, and it
changes nothing in this one: no artifact migration is required, requested or authorized.

This document does **not** claim `S15` can never involve schema work. It stated that the missing
piece was a report-generation *contract*, not an artifact *migration* — and that remains exactly what
the authorized schema builds. The physical result is owned by
`docs/41_report_explorer_s15_implementation.md`.

---

## 9. Preserved findings about the artifact / Workflow B domain

These remain accurate statements about that domain. They are the **reason artifacts are not the
Report Explorer source** — they are no longer evidence that `S15` needs an artifact-oriented
migration.

| Finding | Evidence |
|---|---|
| `artifacts` carries no authoritative reporting period. | DDL in `api/main.py`: `created_at`, `report_type`, `client_code`, `display_filename`, `file_ext`, `layout_version`, `metadata_json`, `preview`, `expires_at`. No period column; `db/migrations/007`, `022`, `023`, `026`, `043` add none. |
| `artifacts` carries no first-class report-instance identity and no report lifecycle. | Same DDL: identity is `artifact_id` per file; there is no instance relation grouping several files of one report for one period, and no status enum matching §3.3. |
| Workflow B occurrence identity is pipeline lineage. | `raw_file_id` (`db/migrations/007`), `run_id`, `workflow_name`, `stage_name`, `artifact_role` — an ingestion/processing lineage, not a business report instance. |
| `artifacts.report_type` has known normalization limits and is not a product dimension. | `docs/34` §4 records that the Artifact Explorer rail deliberately uses `artifacts.kind` and *not* `report_type`, because merging those fields "would invent a dimension the product does not have". |
| The current artifact population does not support the client → type → period axis. | Follows from the three rows above: no period, no instance identity, and `client_code` nullable. |
| Existing `/user/reports` is folder-and-artifact based. | `api/main.py` routes `/user/reports`, `/user/reports/folders/{folder_id}`, `/user/reports/artifacts/{artifact_id}/preview|download`. |
| Preview/download authorization primitives exist and work. | Per-folder `can_preview` / `can_download` and per-user grants in `db/migrations/034`; `_list_accessible_portal_report_folders_for_user` in `api/main.py`. |

---

## 10. Existing `/user/reports`, authorization and file delivery

Two questions that must not be conflated:

- **Where report instances come from** — answered by §1: the future system-generated SQL report
  domain. The existing artifact-backed `/user/reports` listing is **not** that source.
- **How an authorized user is served a file** — existing infrastructure. The preview/download routes,
  the folder-grant checks and the storage-backend delivery path are reusable implementation
  primitives, and putting artifacts out of *product* scope does not invalidate them.

Reuse is **permitted, not mandatory**. No current repository or product contract requires `S15` to
build on those routes; whether it does is an `S15` implementation decision, made when `S15` is
implemented.

`/user/reports` is also the existing pre-redesign surface at the `Raporty` nav slot. `S15` replacing
or superseding it is a product-surface question, separate from the source-model question this
document settles.

---

## 11. `portal_report_folders`

`db/migrations/034_portal_report_folders.sql` defines `portal_report_folders` and
`portal_report_folder_users`: a client-scoped folder whose membership is an artifact query
(`search_query_json`), carrying `can_preview` / `can_download` and per-user grants.

It is therefore **an artifact-folder access-grant mechanism**, not the Report Explorer navigation
model and not a report-instance store. The approved design uses "report-folder access" only in that
authorization sense — `RP-19` requires an account with dataset access but no report-folder access to
see a state explaining that the two are separate grants (`PRODUCT_BEHAVIOR_CONTRACT.md` §3.8).

Nothing here deletes, redefines or migrates it, and this document creates no new authorization
schema. How `S15` authorizes access to *generated* reports is an open question for the
report-generation work (§14).

---

## 12. `RP-2` — `Cykl`

`RP-2` requires each report type in the left rail to show its generation cycle
(`tygodniowy · pon. 04:00`), and §3.6 shows `Cykl` in the detail metadata grid. No persisted cadence
source exists in the repository today.

Under §1, cadence is part of the **future report-definition contract**: a report definition that is
generated on a schedule declares its own cadence. It **MUST NOT** be derived from Workflow B mail
schedules, systemd timers or ingestion frequency, which describe when files arrive rather than what
a report type's business cycle is. No physical cadence storage is designed or authorized here.

---

## 13. Superseded: raw / normalized Workflow B file roles

An earlier investigation asked whether Workflow B `raw` / `normalized` artifacts should appear as
`dane surowe` in the `REP-003` files panel. That question is **superseded and is not an `S15`
blocker**: Workflow B artifacts are out of `S15` scope. A future generated report defines its own
file members and their semantic roles (§7 item 7), including whether it emits a `dane surowe` member
at all.

---

## 14. Open questions for the report-generation work — all resolved

Recorded here as they were raised, each with the decision that closed it:

1. **`Eksporty danych` (`DB-53`).** A completed Database Explorer background export must be
   reachable from Report Explorer as the system report type `Eksporty danych`
   (`IMPLEMENTATION_ACCEPTANCE_CRITERIA.md` `DB-53`, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.13,
   `TRACEABILITY_MATRIX.md`). This is a **platform-generated** deliverable, not a Workflow B or
   e-mail-ingested one, and `db/migrations/043` already gives it a first-class instance identity,
   a status lifecycle (`queued`/`running`/`completed`/`failed`/`expired`) and an expiry — the one
   existing model that already resembles a report instance. What it does not have is a *reporting
   period* in the cyclical sense, and its cycle is on-demand rather than scheduled. How
   `Okres raportowania`, `Cykl` and `Numer okresu` are declared for this type is an `S15` design
   question. `docs/31` §14 remains accurate: that stage introduced no report-instance model.
   **Resolved in `docs/40` §10.2** as a separate provider adapter over the existing
   `database_export_jobs` state, with no duplication into generated-report storage and no schema
   change (`DB_53_SCHEMA_CHANGE: NOT_REQUIRED`); only the period *display* remains an open owner
   decision (`docs/40` §13.1).
2. **Authorization model for generated reports** — whether report-folder grants, dataset grants or a
   new model governs access to generated report instances. Not decided here; no new authorization
   schema is created by this document. **Resolved in `docs/40` §9**: authenticated account + active
   client + effective `can_view_reports` (direct OR active-group), with no `portal_report_folders`
   requirement and no per-report-type grant, creating no authorization schema.
3. **Whether the report-generation framework needs its own storage** — answered when that framework
   is designed and separately authorized (§8). **Answered in `docs/40`: yes**, and the conceptual
   capabilities are listed there. **Authorized by the owner on 2026-08-19** for `docs/40` §14
   capabilities 1–3; capability 4 (per-account `NOWY` seen state) was withheld and deferred
   (`docs/40` §13.2 = B). The migration that implements the grant is local and has **not** been
   applied to production.

4. **`Eksporty danych` period presentation** — **resolved** as `docs/40` §13.1 Option A: an export
   declares no reporting period, `Okres raportowania` and `Numer okresu` render `nie dotyczy`, and
   exports order by completion time. No export acquires a period it does not have.

---

## 15. What this document does not do

This is a decision record. It does not itself implement `S15`, write Report Explorer queries, UI or
API routes, create report tables, allocate a migration number, modify `artifacts` or Workflow B,
backfill data, parse report periods, apply any migration, or authorize any production action. The
implementation carried out under the 2026-08-19 authorization is described in
`docs/41_report_explorer_s15_implementation.md`; `jobs/reports/*` remains Workflow B (`stage2`,
`stage3`, `workflow_b`, per-report postprocessors) and is untouched by it.
