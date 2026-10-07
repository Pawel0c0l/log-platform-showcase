# 38 — Portal initial release plan and stage status

Authoritative record of **which stages of the approved Log Platform redesign the first production
release requires**, which are deliberately deferred, and what must happen before any production
rollout of the portal.

> **Portal V1 shipped.** The first production release is deployed and stable: active release
> `956dea3766b1`, platform migration ceiling `069`, `S13` and `S15` `DEPLOYED`, `S14` deferred by
> owner decision. §6 carries the verified production state; §3–§5 are the plan that produced it and
> are retained as the record of how, not as outstanding work.

This document governs **release prioritization only**. It does not modify, withdraw or reinterpret
the approved design handoff
(`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`), which stays
read-only and unchanged.

> **A designed feature is not automatically an initial-release blocker.**
> Presence in the approved handoff means the feature is a committed product design. It becomes a
> first-release blocker **only when this document lists it as required for the first release**.
> Deferring a stage removes it from the release gate; it does **not** reject, cancel or withdraw the
> design.

---

## 1. Stage ledger

| Stage | Scope | Durable doc | State |
|---|---|---|---|
| `S1` | Portal UI foundation and shared application shell | `docs/22` | implementation-closed |
| `S2` | Database Explorer table-first row sheet | `docs/23` | implementation-closed |
| `S3` | Column-centric filtering | `docs/24` | implementation-closed |
| `S4` | Value distributions | `docs/25` | implementation-closed |
| `S5` | Column management and URL state | `docs/26` | implementation-closed |
| `S6` | Hidden row identity | `docs/29` | implementation-closed |
| `S7` | Grid selection and clipboard | `docs/30` | implementation-closed |
| `S8` | Export panel and background states | `docs/31` | implementation-closed |
| `S9` | Dataset catalogue and system states | `docs/32` | implementation-closed |
| `S10` | Responsive bands and accessibility | `docs/33` | implementation-closed |
| `S11` | Artifact Explorer visual modernization | `docs/34` | implementation-closed |
| `S12` | Eco Driving presentation modernization (`ECO_DRIVING_PRESENTATION_NON_BLOCKED_SUBSET`) | `docs/35` | implementation-closed |
| — | `ECO_DRIVING_ARBITRARY_WEEK_SELECTION_DYNAMIC_RECOMPUTATION` (`EC-1`–`EC-7`, S12 follow-up; carries no `Sn` of its own) | `docs/36` | implementation-closed |
| `S13` | `PORTAL_SERVER_SIDE_PREFERENCES_AND_SAVED_DATABASE_VIEWS` | `docs/37` | **`DEPLOYED`** — implementation-closed at `94e347fe014acfb6e45773f8c3ae8714e65366d9`, final independent review `CODEX_REVIEW_ACCEPTED`; migrations **066 + 067 applied to production** 2026-08-19 (§6) |
| `S14` | Global search (`⌘K` / `Ctrl+K`, `SH-11`) | design only — `PRODUCT_BEHAVIOR_CONTRACT.md` §1.5 | **`DEFERRED_POST_INITIAL_RELEASE`** (§2) |
| `S15` | Report Explorer (`REP-001`–`REP-004`, `RP-1`–`RP-21`) | `PRODUCT_BEHAVIOR_CONTRACT.md` §3 for behaviour; `docs/39` report source; `docs/40` persistence contract; `docs/41` implementation | **`DEPLOYED`** — review findings remediated, final independent review `CODEX_REVIEW_ACCEPTED`; migrations **068 and 069 applied to production** 2026-08-19 (§6) |

`implementation-closed` means the stage's own acceptance evidence is complete in this repository.
**It does not mean deployed.** Deployment state is tracked separately (§6).

Stage numbering for `S14` and `S15` is the repository's own, recorded in `docs/37` §1; the approved
handoff numbers screens and criteria, not stages.

---

## 2. Owner decision — global search

```
S14_GLOBAL_SEARCH: DEFERRED_POST_INITIAL_RELEASE
```

| Question | Answer |
|---|---|
| Is it designed and approved? | **Yes** — `PRODUCT_BEHAVIOR_CONTRACT.md` §1.5, criterion `SH-11`. Unchanged. |
| Is it still in the product roadmap? | **Yes** — §1 lists it, positioned after the first production release. |
| Is it implemented? | **No.** No app-bar search field, no `⌘K` binding, no cross-module search endpoint exists. |
| Is it rejected or cancelled? | **No.** |
| Does it block the first production release? | **No.** |
| When is it reconsidered? | After the first production version has been evaluated in real usage (§3 step 6). |

Release consequences:

- `SH-11` is **outside the first-release acceptance gate**. Verification of the release candidate
  must record it as *deliberately deferred*, not as a failure.
- The app bar ships without the global search field. Its absence is a recorded release decision, not
  a visual-fidelity defect.
- The **dataset-scoped** search inside Database Explorer (`DB-25`, `db.search.placeholder`) is a
  different feature, already implemented, and is not affected.
- `SCREEN_CATALOG.md` names global search as one entry point into `DB-003`. The other approved entry
  points — the `DB-001` catalogue, saved-view chips and deep links — remain, so `DB-003` stays
  reachable without `S14`.

---

## 3. Initial-release sequence

| Step | Work | Gate | State |
|---|---|---|---|
| 1 | `S15` Report Explorer — the remaining required first-release product stage (§5) | stage acceptance evidence complete | **done** |
| 2 | Release-candidate verification and readiness phase: integration, regression, security, accessibility and operational-readiness verification of the whole portal as one candidate | evidence recorded | **done** |
| 3 | Resolve every release-blocking finding from step 2 | no open release blocker | **done** |
| 4 | **Production rollout — separately and explicitly authorized** (§6) | owner authorization for that specific operation | **done**, 2026-08-19/20 |
| 5 | Evaluate the first production version in real usage | — | in progress — first observation `PORTAL_V1_POST_ROLLOUT_STABLE` |
| 6 | `S14` global search, as an optional post-release product update | re-planned then | not started |

Steps 1–3 were repository work. Step 4 was an operational action this document does **not**
authorize; it was carried out under separate explicit owner authorization, and a future rollout
needs its own.

---

## 4. Release-candidate definition

The first production candidate is complete when, and only when:

1. every stage marked **required** in §1 is implementation-closed — currently `S1`–`S13` plus `S15`;
2. the §3 step-2 verification and readiness phase has been run against that candidate;
3. every release-blocking finding from it is resolved.

**The absence of global search does not classify the release as incomplete.** A candidate missing
only `S14` satisfies the definition above.

The portal is **not** production-ready before steps 2 and 3 complete. This document does not declare
it ready and must not be read as doing so. Both steps completed for Portal V1, the candidate was
rolled out under separate explicit authorization, and the deployed state is recorded in §6 — this
definition is what the next candidate must satisfy, not an open gate on the current one.

Out of first-release scope for reasons recorded in the approved handoff, not by this decision:

- Administration visual modernization — deferred, no approved screens (`D-003`);
- a dedicated saved-view management screen — deferred (`D-003`); saved views are created inline and
  listed in `DB-001`;
- the Eco Driving reconciliation and score-definition panels — removed this iteration by owner
  decision (`D-003`);
- authentication/login screens — out of design scope.

---

## 5. Next required stage

```
NEXT_STAGE_ID:      NONE_REQUIRED_FOR_V1
S15_NAME:           Report Explorer
S15_STATUS:         DEPLOYED
```

> **Post-rollout, 2026-08-20.** `S15` is deployed. `068` and `069` are applied to production and the
> Report Explorer is live. The two dated notes below record the state at the time of implementation
> and are kept for that reason; where they say a migration has "not been applied to production",
> read §6 instead — that is no longer true.

> **Status, 2026-08-19.** `S15` is built. The physical schema, the publication boundary, the read
> path, the authorization gate, file delivery, the `DB-53` adapter and the rollout requirement are
> recorded in `docs/41_report_explorer_s15_implementation.md`, which this section does not restate.
> `db/migrations/068_portal_generated_reports.sql` is local and has **not** been applied to
> production. What follows describes the stage's purpose and dependencies and remains accurate.
>
> **Correction, 2026-08-19.** An independent review of the first implementation returned
> `CODEX_REVIEW_CHANGES_REQUIRED`. `068` was by then reachable from `origin/main` and therefore
> immutable, so the integrity findings are closed by a second additive migration,
> `db/migrations/069_portal_generated_reports_integrity.sql`, also local and also **not** applied to
> production. `docs/41` §11 is the durable record of what each finding was and what it became; the
> stage's scope, source decision and product behaviour are unchanged by it.

**Purpose.** Deliver the `Raporty` module on the approved design: a report **library** whose axis is
client → type → period (`REP-001`, `REP-002`), a dedicated report **detail** page with embedded
preview, per-file actions, period siblings and same-report history (`REP-003`), and the library's
empty / no-access / file-store-error states (`REP-004`). `PRODUCT_BEHAVIOR_CONTRACT.md` §0 and §3
make it explicitly **not** a data grid: no column-visibility control, no density control, no
per-column sort menus (`RP-1`).

**Acceptance surface.** `RP-1`–`RP-21`, plus `SH-12` and `SH-13` for `REP-003`, plus `DB-53` (a
completed background export is reachable from Report Explorer as the type `Eksporty danych`).

**Report source — owner decision.** `S15` shows **only reports the platform intentionally generates
from data it already stores**, through future dedicated SQL/report definitions. E-mail-ingested and
Workflow B pipeline artifacts are **not** Report Explorer report instances. The full contract —
domain separation, the reporting-period rule, the no-inference prohibition, the no-backfill position
and the future report-definition contract — is `docs/39_report_explorer_report_source_contract.md`,
which this section does not restate.

```
S15_REPORT_SOURCE: SYSTEM_GENERATED_SQL_REPORTS_ONLY
```

**Dependencies already in place:**

| Dependency | Where |
|---|---|
| Shared shell, tokens, theme engine, nav `Raporty` slot | `S1` — `docs/22`, `api/portal_ui/shell.py` |
| Per-account theme persistence the module inherits | `S13` — `docs/37` |
| Database Explorer, for the `Dane źródłowe` bridge (`RP-18`) | `S2`–`S10` — `docs/23`–`docs/33` |
| Background-export job model behind `DB-53` (`Eksporty danych`) | `S8` — `docs/31`, `db/migrations/043` |
| Preview/download and folder-grant primitives that **may** be reused for file delivery | `db/migrations/034`, `/user/reports*` in `api/main.py` — reuse permitted, not mandatory (`docs/39` §10, §11) |

**Now in place:** the generated-report persistence and the publication boundary a report
generator writes through (`docs/41` §1, §3), so a schema and a UI were not shipped without an
authoritative path by which a report becomes a Report Explorer instance.

**Still not in place:** any *cyclical* SQL report generator itself. None has been authorized;
`jobs/reports/*` remains Workflow B and is untouched. `docs/39` §7 records what such a report
definition must declare, and `docs/41` §3 records the boundary it will call. The one generated
deliverable reaching the library today is the completed background export, through the read-only
`DB-53` adapter.

**Schema impact.**

```
S15_ARTIFACT_SCHEMA_CHANGE:              NOT_REQUIRED
S15_REPORT_GENERATION_CONTRACT:          REQUIRED                        (docs/40)
REPORT_GENERATION_SCHEMA_AUTHORIZATION:  GRANTED_2026_08_19              (docs/40 §14, capabilities 1–3)
S15_SEEN_STATE_CAPABILITY:               NOT_AUTHORIZED_DEFERRED         (docs/40 §13.2 = B)
S15_MIGRATION:                           068_portal_generated_reports.sql (docs/41 §1)
S15_CORRECTION_MIGRATION:                069_portal_generated_reports_integrity.sql (docs/41 §11)
S15_MIGRATIONS_APPLIED_TO_PRODUCTION:    NO
S15_HISTORICAL_REPORT_BACKFILL:          NONE                            (docs/41 §10)
```

`artifacts` is still not retrofitted, and this supersedes the earlier planning statement
`NEXT_STAGE_SCHEMA_AUTHORIZATION_REQUIRED`, which followed from investigating
`artifacts`/`portal_report_folders` as the report source: those tables genuinely carry no reporting
period and no report-instance identity (`docs/39` §9), but under the owner decision they are the
wrong domain, so their gaps were never an `S15` migration requirement. The current artifact
population needs no classification and **no backfill**.

What the owner *did* authorize on 2026-08-19 is the new domain `docs/40` derived from that source
decision: generated-report **definitions**, **instances** and **file members**, as one additive
platform migration. The conditional fourth capability — per-account `NOWY` seen state — was withheld
and deferred, so no seen-state relation exists and the badge is not rendered. `DB-53` still needs no
migration: completed background exports reach Report Explorer through a read-only provider adapter
over the existing `database_export_jobs` state, never by duplication (`docs/40` §10.2).

---

## 6. Production state of `S13`/`S15`

```
PORTAL_V1_PRODUCTION_ROLLOUT:            SUCCESS          (2026-08-19/20)
PORTAL_V1_POST_ROLLOUT:                  STABLE
PRODUCTION_PLATFORM_MIGRATION_CEILING  = 069
MIGRATIONS_064_065_066_067_068_069_APPLIED_TO_PRODUCTION
ACTIVE_RELEASE                         = 956dea3766b1
PREVIOUS_RELEASE                       = 2756f9bfd2ab
S13                                    = DEPLOYED
S15                                    = DEPLOYED
S14                                    = DEFERRED_POST_INITIAL_RELEASE
```

Verified read-only against production: `max(filename)` in `public.schema_migrations` is
`069_portal_generated_reports_integrity.sql`, and `ops/manage_release.py status` reports
`current -> releases/956dea3766b1`, `previous -> releases/2756f9bfd2ab`. Report Explorer is live;
`log-platform-api.service` and `database-export-worker.service` are healthy; portal health,
readiness and the V1 production smoke all PASS.

**Generated-SQL cyclical report definitions and instances are currently empty.** `DB-53` data
exports remain the populated report-adjacent surface. That is the expected consequence of the owner
decision in §5 (`S15_REPORT_SOURCE: SYSTEM_GENERATED_SQL_REPORTS_ONLY`) — the library shows only
reports the platform intentionally generates, and none has been defined yet. It is **not** a
deployment defect and must not be reported as one.

The subsections below record how the rollout was planned and executed. They are kept because the
chain they describe is the chain that ran, and because the same reasoning applies to the next one —
not because anything in them is still pending.

`CREDENTIAL_ROTATION: OWNER_DECLINED`. Historical credential exposure remains an owner-accepted
residual risk for this release and is closed as a decision, not deferred as work.

### 6.1 The forward chain is the whole chain, not the portal's part of it

**The portal rollout order was `064 → 065 → 066 → 067 → 068 → 069 → code`,** and that is the
sequence production executed.

Earlier revisions of this section named only `066 → 067 → 068 → 069 → code`, and `docs/41` §9 named
only `068 → 069 → code`. Both were **wrong as rollout instructions**, because neither is the
sequence that actually executed. The production platform ceiling was `063` at the time
(applied `2026-08-17`) and is now `069`, and `ops/db_migrate.sh` applies **every**
`db/migrations/*.sql` not already recorded in `public.schema_migrations`, in filename order. An
operator running it against production therefore applies `064` first, whatever a portal document
says. A runbook that lists a shorter sequence does not make a shorter sequence happen; it only makes
the operator surprised.

| migration | role in this rollout |
|---|---|
| `064_public_runs_historical_reconciliation` | **Not portal work.** Platform-core provenance for retrospectively terminalizing an abandoned historical `public.runs` row (`docs/07` §5.8). It is in the chain because it sits below the required ceiling, and it *will* be applied by the portal rollout. It is listed here so that fact is expected rather than discovered. It is **not** optional: `db_migrate.sh` does not offer a way to skip it, and inventing one would be worse than applying it. |
| `065_database_export_job_cancellation` | **Required for the first release.** It widens the `database_export_jobs` status vocabulary to accept `'cancelled'`, which the shipped API writes (`api/main.py`, the `Anuluj` path). Without it the release's own export-cancellation feature fails against a `CHECK` constraint at runtime. It is additive and backward-safe — an old worker never writes the new status — so it is applied before the code, exactly like the rest. |
| `066_portal_account_preferences_and_saved_views` | `S13` preferences, saved views, column sets. |
| `067_portal_saved_object_structural_integrity` | `S13` correction of `066`. |
| `068_portal_generated_reports` | `S15` generated-report persistence. |
| `069_portal_generated_reports_integrity` | `S15` correction of `068`. |

Neither `064` nor `065` may be described as optional merely because it is not portal schema. "Not
portal schema" describes what a migration contains, not whether the rollout applies it.

### 6.2 Why the chain is verified as a chain

`066`/`067` are not declared in `db/schema_requirements.json`; the application degrades to pre-`S13`
behaviour on a confirmed-absent schema (`docs/37` §2.4, §9). `068`/`069` **are** declared, so a
release built from this tree refuses to activate against a platform database missing either of them
rather than rendering an empty Report Explorer library (`docs/41` §9). `068` alone does not satisfy
the requirement: a database without `069` still accepts a forged availability summary, a replayed
publication and an unsafe inline preview (`docs/41` §11).

The release-candidate verification proved the chain end to end against a disposable PostgreSQL 16: a
`≤063` baseline plus the `064…069` forward delta produces a schema **byte-identical** to a clean
full-chain build, re-applying the delta changes nothing, and release schema preflight refuses a
database stopped at `068`.

### 6.3 Rollout order

```
release schema preflight (expected to REFUSE before the migrations)
  → backup and verify it            (rollback is restore, not reverse DDL — docs/07 §1.11)
  → 064 → 065 → 066 → 067 → 068 → 069
  → release schema preflight        (must PASS)
  → code activation
  → health and portal readiness checks
```

Code activation happens only after the **complete** forward chain and a passing preflight. There is
no automated down migration.

That sequence was executed on explicit owner authorization and completed successfully; the ceiling
is now `069` and release `956dea3766b1` is active.

The rule that authorized it is unchanged and applies to every **future** rollout: applying
migrations, deploying code, restarting services, changing schedules or mutating production data is a
**separate operation requiring explicit owner authorization for that specific action**. Neither this
document, nor stage completion, nor a green release candidate constitutes that authorization.

---

## 6.4 Release provenance of the portal commits

Commit subjects do not reliably say where the portal landed. Recorded here because a reader looking
for migration `068` will not find it under a message that mentions it.

| commit | what it actually contains |
|---|---|
| `2756f9b` (on `origin/main`) | subject `docs(AGENTS): update guidelines for concurrent agent operations`, body asserting "No functional code changes were made". In fact a 217-file commit introducing `db/migrations/068_portal_generated_reports.sql`, eight `api/report_explorer/` modules, driver eco-dashboard assets, and `.claude/settings.local.json.before-emergency-recovery`. |
| `891106e` | `feat(portal): implement S15 Report Explorer…` — the remaining six `api/report_explorer/` modules, the routes, `api/main.py` wiring, CSS/JS, `docs/39`–`docs/41` |
| `e67ada0` | migration `069` and the first S15 review findings |
| `3fe571e` | schema-preflight coverage for `068`/`069` |
| `91bc63b` | the RP-18 active-dataset publication binding |

The history is shared and is **not** rewritten — no amend, no rebase, no force push. Provenance is
incomplete; verification is not. The release-candidate evidence was produced against the final tree
(clean-checkout import, full migration chain on disposable PostgreSQL 16, schema preflight,
authorization and delivery suites), so what the candidate does is established by testing the
candidate, not by trusting the commit messages that assembled it.

---

## 7. Re-entry for `S14` after the first release

When global search returns as a post-release update it re-enters as a normal stage: its own durable
`docs/NN`, its own acceptance evidence against `SH-11` and `PRODUCT_BEHAVIOR_CONTRACT.md` §1.5, and
its own review. Nothing about it is pre-built now. `S13` established the account-scoped CRUD pattern
`S14` may reuse but pre-builds none of its entities (`docs/37` §1).

---

## 8. What this document does not do

It writes no code, creates no migration, applies no migration, deploys nothing, restarts nothing and
pushes nothing. It edits no file inside the approved design handoff.
