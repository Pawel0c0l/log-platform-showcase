# Retention

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Any
> `PYTHONPATH="$PWD" python3 ops/runner.py …` command below is development /
> local / debug only: it executes the mutable working tree directly. Run
> retention in production through the installed wrapper
> (`/usr/local/bin/log-job-runner.sh <module> '<json>'`), which is the only
> supported production entrypoint once the release boundary is live. See
> `docs/07_operations.md` -> *Release boundary*.


## The global ceiling comes first

Everything in this document describes a **shorter** lifecycle that removes data
before the platform-wide ceiling is reached. The ceiling itself — **13 calendar
months**, the authoritative registry that states it once, the sweep that
enforces it on every store these shorter lifecycles do not reach, and the
backup-shadow limitation — is
`docs/42_platform_retention_and_schedule_governance.md`. Read that first if the
question is "how long may we keep X?"; read this one if the question is "how
does the prune decide what to delete?".

No number in this document was lengthened by that policy.

Two mechanics from that document apply to everything below and are not repeated
per section:

- **sweeps delete EARLY.** Every cutoff is `now + lead − 13 calendar months`,
  where the lead is the responsible schedule's guaranteed interval plus, for
  stores inside a backup set, the backup shadow. A periodic sweep that used the
  bare deadline would leave records alive past it (`docs/42` §3.1);
- **the ceiling is a maximum across every surviving copy**, backups included
  (`docs/42` §6).

## Platform retention contract

The authoritative implementation is `api/platform_prune.py`, exposed on the host by `ops/platform_prune.sh`. The repository service `ops/systemd/log-platform-prune.service` invokes:

```text
/usr/local/bin/log-platform-prune.sh --execute --days 60
```

The daily timer remains at 03:30 local system time with `Persistent=true`, `AccuracySec=1min` and no randomized delay. The systemd environment file supplies host-local database and MinIO coordinates; same-host retention does not depend on a LAN or Tailscale address. Direct host execution gives `MINIO_HOST_ENDPOINT` precedence, otherwise preserves a host-compatible `MINIO_ENDPOINT`, and maps only the exact Compose-internal default to the established loopback endpoint. The Compose API continues using Docker DNS.

The supported production validation is explicit dry-run:

```bash
ops/platform_prune.sh --dry-run --days 60
```

Dry-run and execution call the same planner. The cutoff is `now UTC - retention_days` and eligibility uses strict `< cutoff` boundaries. Dry-run opens a repeatable-read, read-only transaction, performs no database or MinIO mutation, creates no platform run, and returns only aggregate candidates and exclusion reasons under `PRUNE_DRY_RUN_SUCCEEDED`. Repeated dry-runs are idempotent.

## Eligibility and explicit exclusions

Platform prune is intentionally conservative:

- only terminal platform `runs`, logs belonging to terminal runs or no run, and unreferenced platform `artifacts` older than the cutoff may become candidates;
- `RUNNING` and other nonterminal runs and their rows are excluded, including stale historical `RUNNING` rows;
- an artifact is excluded when it is linked from Workflow B ingest, marked `workflow_b`, referenced by metadata/tags/virtual folders, owned by Database Explorer export retention, or still offered by an **available** Portal generated-report file (see below);
- unknown foreign-key references — or a reviewed reference whose delete semantics have drifted — unsupported storage backends, malformed storage identities, missing environment identity, invalid retention configuration, or dependency failure abort the whole operation;
- Workflow B ingest rows, raw/normalized/cleaned/Stage 3 report files, customer-business databases, portal configuration/audit, database-export lifecycle, and backup archives/manifests are outside this scope.

A session advisory lock prevents overlapping prune executions. A shared nonblocking lock on the coordinated backup lock file prevents prune from overlapping backup creation. No additional global Workflow B lock is required because Workflow B artifacts and active runs are excluded.

## Execution and consistency

Execution requires `--execute`; there is no default destructive mode. It re-attests the declared platform environment and connected database, uses a serializable PostgreSQL transaction, locks exact planned rows, and requires exact affected-row counts. MinIO objects are deleted before database rows. Any object-store failure rolls back the database transaction and retains database metadata for idempotent retry; PostgreSQL deletion commits only after every planned object deletion succeeds. Because S3 and PostgreSQL do not share a distributed transaction, an object deleted immediately before a later object-store failure can temporarily retain its database row; retry is safe and database lineage is never silently discarded.

The authenticated API endpoint `POST /maintenance/prune?days=60&dry_run=true` exposes the same read-only planner. The API rejects `dry_run=false` with HTTP 409 so API reachability cannot bypass host backup coordination. Safe output contains no object keys, filenames, client identifiers or report content.

Failure classifications include `PRUNE_HOST_MINIO_ENDPOINT_MISSING`, `PRUNE_HOST_MINIO_ENDPOINT_INVALID`, `PRUNE_ENVIRONMENT_IDENTITY_MISMATCH`, `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS`, `PRUNE_LOCKED`, `PRUNE_BACKUP_ACTIVE`, `PRUNE_DATABASE_UNAVAILABLE`, `PRUNE_DEPENDENCY_FAILED`, `PRUNE_OBJECT_STORE_DELETE_FAILED`, and `PRUNE_DATABASE_STATE_DRIFT`; all return non-zero from the host command.

## Portal generated reports and artifact retention

Migration `068` gives `portal_generated_report_files` an `artifact_id` reference to `public.artifacts`: the member row holds the report's domain identity, `artifacts` holds its bytes. It is the sixth and currently last entry in the prune's exact `EXPECTED_ARTIFACT_REFERENCES` allowlist — a seventh reference, from any future migration, still stops the prune with `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` until its retention semantics are reviewed and taught to the planner.

### What the reference contract pins

The allowlist compares whole foreign keys, not just their endpoints, because the endpoints do not describe what a delete does. For each of the six references the planner reads from `pg_constraint` and requires an exact match on:

| property | why the prune depends on it |
| --- | --- |
| referencing schema and table | who points at the artifact. Schema-qualified from `pg_namespace`, never from `regclass`, whose text output omits any schema on `search_path` and would therefore vary by role |
| referencing column | which column the delete acts on |
| referenced schema, table and column | `artifacts` may grow a second candidate key; a reference repointed at one is a different lifecycle under an identical name |
| delete action | what the server does to the referencing row. `CASCADE` destroys it, `SET NULL` preserves it, `RESTRICT` aborts the transaction after the MinIO objects are already gone |
| referencing-column nullability | whether `SET NULL` can execute at all. `SET NULL` over a `NOT NULL` column raises mid-delete instead of preserving history |
| referential-action trigger enablement | whether the declared action will actually fire. `confdeltype` is a declaration; the action is carried out by a trigger on the referenced table, and a superuser can disable it while leaving every other field unchanged — the delete would then leave a dangling reference instead of nulling it. The prune role is a superuser in this deployment, so this is not a hypothetical privilege |
| the session's `session_replication_role` | the same guarantee from the session side. `replica` suppresses *ordinary* triggers, and a foreign key's referential action is one, so every constraint reads as perfectly enforced — `confdeltype` unchanged, `tgenabled` still `'O'` — while `ON DELETE SET NULL` does nothing. The prune refuses any session not running as `origin`; it verifies rather than sets the value, because the setting is superuser-only and forcing it would merely break a least-privilege role |

A composite key to `artifacts` is refused outright rather than compared column by column. `ON UPDATE` is deliberately not pinned: the prune never updates an artifact's primary key, so no destructive behaviour depends on it.

Two further properties make the check bind to what the prune actually does rather than to a lookalike:

- **the schema is pinned before anything is read.** The planner names `public` relations unqualified, and the platform role's `search_path` begins with `"$user"`. `build_prune_plan` issues `SET LOCAL search_path = pg_catalog, public` before validating, so the contract cannot certify `public.artifacts` while the planner reads — and the delete path deletes — a shadow relation of the same name. Every relation outside `public` that this worker touches is already named schema-qualified;
- **the reviewed relations are locked before the contract is trusted.** Validation and planning are separate statements, so a migration committing between them could have repointed a reference after it was certified. The planner takes `ACCESS SHARE` on all six referencing relations and on `artifacts` first — the same mode its own `SELECT`s take moments later, so no lock ordering changes — and `ALTER TABLE ... DROP CONSTRAINT` needs `ACCESS EXCLUSIVE`, so it blocks until the prune transaction ends. A relation that does not exist is skipped, so an unmigrated database still fails as `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` rather than as a dependency error.

The six pinned references are:

| reference | delete action | referencing column |
| --- | --- | --- |
| `public.artifact_metadata_overrides.artifact_id` | `CASCADE` | `NOT NULL` |
| `public.artifact_tags.artifact_id` | `CASCADE` | `NOT NULL` |
| `public.artifact_virtual_folder_items.artifact_id` | `CASCADE` | `NOT NULL` |
| `public.database_export_jobs.artifact_id` | `SET NULL` | nullable |
| `ingest.raw_file.stage2_cleaned_artifact_id` | `SET NULL` | nullable |
| `public.portal_generated_report_files.artifact_id` | `SET NULL` | nullable |

The five pre-Portal rows are transcribed from migrations `024`, `026`, `043` and `048` and changed no behaviour — they already carried these actions before the contract could see them. For the Portal reference the pinning is load-bearing: the generated-report history-preservation guarantee below is exactly the `SET NULL`-over-nullable pair, so a catalog drift to `CASCADE`, to `RESTRICT`, or to a `NOT NULL` column stops the prune with `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` before anything is planned, even though the referencing `(table, column)` is unchanged.

The lifecycle rule is scoped by availability, not by the existence of the membership row:

- **an available member retains its artifact.** The artifact is excluded from planning under `artifact_generated_report_available`. This exclusion is the only protection that exists. The reference is `ON DELETE SET NULL`, and `068` additionally installs the `portal_generated_report_files_availability` trigger, which drops `is_available` to FALSE whenever the reference becomes NULL — deliberately, so object cleanup is never blocked. A prune that deleted the artifact of a live report would therefore not fail; it would silently convert that report to `Pliki wygasły`;
- **an unavailable or expired member does not retain its artifact.** Once `ReportPublication.expire_due_members` has swept the member to `is_available = FALSE`, the bytes are no longer offered and the artifact returns to the ordinary retention horizon;
- **the history row survives the deletion.** `artifact_id` is nullable and `SET NULL`, so the member keeps its filename, format, size, role and expiry after its bytes are gone. That is what makes the `Pliki wygasły` state and the history panel truthful.

Ordering therefore matters in one direction only: availability is dropped first, and object deletion follows. Deleting the object first is not refused by the database, so it must not be relied upon to be.

Reading `is_available` at plan time and deleting later is not a time-of-check/time-of-use window. The real (non-dry-run) plan holds `FOR UPDATE OF artifact` on every candidate, and inserting a member that references an artifact takes `FOR KEY SHARE` on that parent row; the two modes conflict, so a concurrent publication cannot bind a planned artifact — it blocks until the prune transaction ends and then fails its own foreign key. Availability also only ever moves `TRUE → FALSE` for an existing member: `expire_due_members` and `068`'s trigger both set it FALSE, and republication does not reactivate a row at all — it deletes the instance's members and inserts new ones.

One assumption behind that argument is **not** enforced by the schema: `_bind_members_to_artifacts` accepts any artifact of the right client and content type, with no age bound, so a generator could in principle bind an object already older than the retention horizon. Today that is unreachable — `ReportPublicationService.publish` has no production caller and production carries zero definitions, instances and members — and `test_generated_report_publication_has_no_production_caller_yet` fails the moment it stops being true. Before a generated-report publisher is wired up, confirm it can only bind objects it has just uploaded; if it cannot, the availability exclusion needs an age-independent guard.

## Portal retention scope

Platform `POST /maintenance/prune` does **not** delete portal configuration or audit tables. In particular, it does not prune `artifact_users`, `artifact_roles`, `artifact_user_roles`, `portal_clients`, `portal_user_clients`, report-folder assignments, database dataset catalog/assignments, portal groups, or `portal_audit_events`.

`portal_audit_events` is operational audit evidence for login/logout, denied access, admin changes, report activity, row browsing, exports, and group changes. Platform prune still does not touch it — but it is no longer unbounded: it is governed at the global 13-calendar-month ceiling by `ops/hard_retention.py` under policy `platform_db.public.portal_audit_events`, anchored on `created_at`. A shorter portal-audit horizon would be an ordinary shorter policy and needs an operator decision; a longer one needs an owner-approved override.

## Database Explorer async export retention

Asynchronous Database Explorer exports have a separate global retention policy from `/maintenance/prune`:

- completed background exports expire exactly **3 calendar days after completion** (`database_export_jobs.completed_at + 3 days`);
- the policy is global: no user-level override, no job-level override, and no portal UI control;
- after expiry, common artifact download/preview helpers reject the artifact based on `artifacts.expires_at` / `expired_at`, even if MinIO deletion has not yet succeeded;
- cleanup deletes the stored object idempotently, marks the Artifact Explorer row expired via `artifacts.expired_at`, and marks the queue row `status='expired'`;
- the `database_export_jobs` row remains as minimal status/audit history, with no downloadable result — retained until the global 13-month ceiling, at which point `ops/hard_retention.py` removes it and cascades its attempt-object ledger. A job still `queued`/`running`, or one whose `database_export_attempt_objects` row is still `cleanup_pending` with no recorded cleanup success, is protected and reported rather than deleted: that ledger row is the only record of an unpublished MinIO object, and removing it would orphan the object permanently;
- when migration 044 is present, the Reports `Database Exports` folder renders that remaining job history as non-downloadable lifecycle rows rather than stale artifact links.

Cleanup is implemented in `python -m ops.database_export_worker --cleanup-only` and in the continuous worker loop. The proposed hourly entry is `ops/systemd/proposed/database-export-cleanup.timer`. The same cleanup entry also retries `database_export_attempt_objects.state='cleanup_pending'` unpublished attempt-object deletion until `last_cleanup_success_at` is recorded, and keeps those ledger rows durable after success. Migration 044 does not change the 3-day policy or cleanup ledger behavior; it only lets the owner see queued/running/completed/failed/expired job lifecycle in the system-managed Reports folder. This policy applies only to async Database Explorer export artifacts (`workflow_name=database_explorer`, `stage_name=async_export`, `artifact_role=database_export`) and does not change retention for unrelated artifacts.

## suspected_bug incidents, occurrences and email outbox

Migration `052_suspected_bug_incidents_and_email_outbox.sql` adds three tables that `POST /maintenance/prune` does **not** touch: `suspected_bug_incidents`, `suspected_bug_occurrences` and `suspected_bug_email_outbox`. There is no automated retention job for them in this repo.

They are designed to survive log pruning. Every reference to `logs(id)` and `runs(run_id)` is `ON DELETE SET NULL`, so pruning old logs and runs succeeds unchanged and leaves the incident, its occurrence count and its delivery history intact — the occurrence row simply loses its `log_id` pointer. Deleting an incident cascades to its occurrences and outbox rows; nothing else cascades.

Growth is bounded by design rather than by the prune: occurrences accumulate one row per detection (which is why detections are grouped by fingerprint instead of by affected record), payloads are truncated to bounded sizes, and outbox rows are one per alert cycle, not one per occurrence.

All three are now additionally governed at the global 13-calendar-month ceiling by `ops/hard_retention.py`. The anchors are chosen so that live alerting is not disturbed:

- `suspected_bug_incidents` is anchored on **`last_seen_at`, not `first_seen_at`**. An incident that is still firing keeps its fingerprint — and therefore its duplicate-alert suppression — however old it is; only a fingerprint silent for the whole ceiling is removed, cascading its occurrences and outbox rows with it. This is what replaces the old "do not delete `open` incidents" rule: an open incident that is still recurring is never eligible, and one that has been silent for thirteen months is not suppressing anything worth keeping;
- `suspected_bug_occurrences` is swept in its own right on `occurred_at`, so a long-lived incident cannot carry occurrences older than the ceiling on its back;
- `suspected_bug_email_outbox` is swept on `created_at`, except rows still `pending` or `sending` — an undelivered alert is work in progress, not history.

## Workflow A i Workflow B — zakres retencji

- **Workflow B (`ingest`)**: rekordy w `ingest.imap_message` i `ingest.raw_file` **nie są** opisane w tej dokumentacji jako automatycznie usuwane przez `prune` — zachowanie zależy od przyszłych decyzji i ewentualnych osobnych procedur (**nie należy zakładać**, że prune czyści ingest, bez weryfikacji kodu).
- **Uprawnienia.** `db/client_business/052_retention_runtime_privileges.sql` nadaje runtime'owej roli klienta `SELECT, DELETE` (i nic więcej) na zarejestrowanych relacjach. Bez tej migracji 27 par (klient, tabela) zgłasza `INSUFFICIENT_PRIVILEGE` i **nie jest** objętych retencją — ta sama luka dotyczyła już `retention_purge`, tylko że 68 z 69 wierszy polityki jest wyłączonych, więc nikt jej nie zauważył. Batch usuwający nie używa `FOR UPDATE SKIP LOCKED`: PostgreSQL wymaga do tego `UPDATE`, a rola retencyjna nie ma powodu móc *nadpisywać* danych klienta.
- **`ingest.imap_message` i `ingest.raw_file` są teraz objęte globalnym pułapem 13 miesięcy** (`ops/hard_retention.py`). Anchor to `imap_message.fetched_at`: `raw_file` nie ma własnego timestampu, a jego FK jest `NOT NULL ON DELETE CASCADE`, więc wiadomość jest anchorem całej rodziny rekordów. `raw_file.duplicate_of_id` jest `NO ACTION`, więc stara wiadomość, na której pliki wskazuje młodszy duplikat, jest **odraczana i raportowana**, nie usuwana i nie przerywająca batcha. `prune` nadal ich nie dotyka.
- Pliki Workflow B na dysku nie są czyszczone przez `prune`, ale również podlegają pułapowi: raw/normalized report files pod `REPORTS_DATA_DIR` (na hoście `/home/logplatform/reports-data`) oraz cleaned CSV Stage 2 pod `/tmp/log-platform-stage2/cleaned` są zamiatane przez backend filesystemowy `ops/hard_retention.py` po `mtime`.
- Stage 1 artifact reconciliation relies on durable `raw_path` and `normalized_csv_path`. The 13-month horizon is more than an order of magnitude beyond any reconciliation window, and the ingest rows that reference these paths age out on the same cutoff — but a **shorter** cleanup for those files still must not be introduced until reconciliation eligibility, artifact-link validation, retention ordering and recovery behavior are explicitly designed together.
- The filesystem sweep never leaves its approved roots: absolute-path, depth and system-directory floors, an approved-prefix check on the resolved path, no descent into a symlinked directory, no unlink of a symlinked file, and containment re-proved on the resolved path immediately before each `unlink()`.
- **Workflow A (bazy klientów)**: platformowy `prune` nie czyści danych biznesowych klientów. Repo zawiera osobny worker `jobs.api.telematics.retention_purge`, który czyści tabele w bazach klientów według `workflow_a_control.client_table_retention`.

## Workflow A — `jobs.api.telematics.retention_purge`

Zakres:

- tabele zarejestrowane w `jobs/api/telematics/registry.py` i `workflow_a_control.table_registry`,
- enabled policies w `workflow_a_control.client_table_retention`; po migracji `017_*` policy rows przechowują `client_code` obok `client_id` dla audytu i filtrowania operatorskiego,
- bazy klientów wskazane przez `workflow_a_control.client_account`.

Zachowanie:

- default `dry_run=true`,
- cutoff liczony w Pythonie jako `now_utc - retention_days`, a następnie **domknięty globalnym pułapem**: efektywny cutoff to `max(now_utc - retention_days, hard_retention_cutoff(now_utc))`. Późniejszy cutoff usuwa *więcej*, więc krótsza polityka per-klient zachowuje swoją krótszą retencję bez zmian, a dłuższa niż 13 miesięcy nie jest w stanie nic zatrzymać ponad pułap. Dziś żadna włączona polityka nie przekracza 365 dni, więc nie zmienia to bieżącego zachowania — zmienia to, czego nie da się obejść edycją kolumny `retention_days`,
- schema/table/column walidowane przeciwko Python registry i interpolowane przez `psycopg.sql.Identifier`,
- log context zawiera `client_id` i `client_code` (jeśli klient ma ustawiony kod),
- deletion batchami po `ctid` z `batch_size` (default `5000`) i commit per batch,
- po successful non-dry-run aktualizuje `last_purge_run_at`, `last_purge_cutoff_ts`, `last_purge_deleted_count`.

Manual:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.retention_purge '{"dry_run":true,"batch_size":5000}'
```

Proponowany timer hostowy: `ops/systemd/proposed/log-job@retention-purge.{service,timer}`. **Uwaga operacyjna:** timer jest zainstalowany i włączony na hoście, ale jego `ExecStart` przekazuje `dry_run:true`, więc dziś nic nie usuwa. Niezależnie od tego globalny pułap obowiązuje **każdą** zarejestrowaną tabelę każdego włączonego klienta, bez względu na `client_table_retention.enabled` — ta flaga była jedyną rzeczą dzielącą te tabele od nieograniczonego wzrostu, a 68 z 69 wierszy polityki w produkcji jest wyłączonych.

`telematics_reports."Alpha_GPS_Baza_LOG"` (GPS Baza Log) to **jedyny zatwierdzony przez właściciela wyjątek** od pułapu 13 miesięcy kalendarzowych: decyzja z 2026-08-29 mówi, że ta relacja **nie ma retencji opartej na wieku**. Żaden rekord nie jest usuwany ani klasyfikowany jako przeterminowany z powodu wieku, żadna kolumna (`assignment_date` ani `imported_at`) nie jest kotwicą usuwania, import pełną wymianą normalnie odtwarza historyczne przydziały, a produkcyjny sweep planuje tu **zero** usunięć. Wyjątek jest zarejestrowany centralnie jako `Mode.OWNER_EXEMPT` z atrybucją (`OwnerExemption`) w `ops/retention_registry.py` — jest więc jawny i policzalny, a nie brakiem wpisu. Historia importów (`telematics_reports.alpha_gps_baza_log_import_runs`) pozostaje objęta pułapem, kotwiczona na `started_at`. Zmierzone 2026-08-29 rekordy starsze niż poprzedni cutoff (6 129 z 9 002 w `alpha_main`, najstarszy 2017-06-20) to **dane zachowywane zgodnie z decyzją**, nie zaległe czyszczenie. Szczegóły: `docs/42` §8.

Tabele klienckie **spoza** `registry.TABLES` (`eco_driving_weekly_email_send_log`, `eco_driving_monthly_email_send_log`, `eco_drivers_id_chart`, `eco_dashboard_delivery_operation`, `telematics_reports.report_207`, `telematics_reports.report_d105_2_ecodriving`, kopie `client_trips_legacy_backup_0NN`, staging V2) nie były objęte żadnym mechanizmem retencji przed wprowadzeniem rejestru; teraz są. Ich polityki i anchory są w `ops/retention_registry.py`.
