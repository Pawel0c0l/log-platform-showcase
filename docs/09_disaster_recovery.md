# Disaster Recovery

## Zakres backupu (platforma w repo)

W oparciu o `ops/backup.sh`:

- dump PostgreSQL do `backups/postgres_YYYYmmdd_HHMMSS.sql.gz`,
- backup danych MinIO:
  - jeśli istnieje katalog `miniodata/` w repo: `backups/minio_YYYYmmdd_HHMMSS.tar.gz`,
  - jeśli MinIO używa wolumenu Dockera: skrypt kopiuje `/data` z działającego kontenera `minio` i tworzy `backups/minio_YYYYmmdd_HHMMSS.tar.gz`.

Ten zakres obejmuje typowo instancję Postgres używaną przez **API platformy** (runy, logi, artefakty) oraz schemat **`ingest` (Workflow B)**, jeśli znajduje się w tej samej bazie. **Nie stanowi to automatycznie** pełnego backupu **głównych baz danych klientów** używanych przez docelowy **Workflow A** — jeśli są osobne instancje lub bazy, operator musi je uwzględnić we własnych procedurach DR.

Portal readiness depends on the same platform Postgres backup. A platform DB dump should preserve at least these portal/local-UI tables when present:

```text
artifact_users
artifact_roles
artifact_user_roles
artifact_role_permissions
portal_clients
portal_user_clients
portal_report_folders
portal_report_folder_users
portal_report_folder_groups
portal_database_datasets
portal_database_dataset_columns
portal_database_dataset_users
portal_database_dataset_groups
portal_audit_events
portal_groups
portal_group_users
portal_group_clients
```

After restore, run `PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py` and sign in as an active admin. If no active admin remains, use `scripts/bootstrap_portal_admin.py` to recreate one.

## Uruchomienie backupu

Jedynym autorytatywnym entrypointem dla uruchomień ręcznych i systemd jest:

```bash
cd /opt/log-platform
./ops/backup.sh create
```

Skrypt używa non-blocking `flock` na chronionym `backups/.backup.lock`; przegrany proces kończy się kodem 75 i nie tworzy plików. Jeden timestamp obejmuje dump PostgreSQL, staged read-only copy danych MinIO oraz manifest. Każdy output powstaje najpierw jako mode `0600` z suffixem `.partial`. PostgreSQL przechodzi `gzip -t` i pełny logical read, MinIO pełne `tar -tzf` i kontrolę liczby entries. Dopiero potem skrypt publikuje atomowo PostgreSQL, MinIO i na końcu `backup_<timestamp>.manifest.json`. Manifest jest commit markerem pary i zawiera bezpieczne identity, rozmiary, SHA-256, entry count oraz wyniki walidacji.

Przerwanie zostawia wyłącznie `.partial` albo `.failed`; nie są to recovery media. Automatyczna retencja przez `BACKUP_RETENTION_DAYS` nie usuwa już plików: usuwanie wymaga osobnego, przejrzanego procesu obejmującego tylko manifest-backed pairs. Historyczne backupy bez manifestu pozostają backward-compatible wyłącznie po ręcznej walidacji. Para `20260712_232326` jest jawnie sklasyfikowana jako nieważna dla restore, ponieważ powstała przed kontraktem manifestu i była walidowana podczas zapisu; nie usuwaj jej ani nie wybieraj automatycznie.

## Backup przed deployem / migracjami portalu

Before applying new platform migrations or restarting a production portal/API service, create and verify a manifest-backed pair:

```bash
cd /opt/log-platform
./ops/backup.sh create
./ops/backup.sh verify YYYYmmdd_HHMMSS
```

`verify` performs full archive traversal and manifest size/SHA-256 checks without printing database rows, object keys or contents. Confirm all three files share the timestamp and mode `0600`; never select a new-format pair whose manifest is absent.

The platform PostgreSQL dump includes portal/local-UI tables when they live in the standard platform DB. MinIO/object storage remains a separate backup artifact; keep both the DB dump and MinIO archive for a complete artifact/portal restore. Workflow A client business databases can be separate from the platform DB and must be covered by the operator's own backup procedure before client-business migrations or deploys that depend on those databases.

Artifact idempotency identities live in the platform `artifacts` table and canonical keyed blobs live in MinIO. Restore the platform DB and MinIO from a consistent backup pair. A retry can recover a keyed blob uploaded before a failed DB insert because its digest-based key is deterministic, but this is not a substitute for backing up both stores.

Before production Eco Driving Person email sends, back up the relevant client business database as well as the platform state. For BRAVO00016 weekly sends, the client DB backup must include `public.eco_person_weekly_email_send_log`, including the MIME preservation and Sent archive columns added by `db/client_business/041_eco_person_sent_archive_state.sql`. Those columns are the recovery source for archive-only Sent-folder retry after SMTP success; without them, operators cannot recreate the exact Sent copy without risking duplicate recipient delivery.

Before `043_eco_person_physical_person_identity.sql`, capture the target client database schema and verify that all seven isolated Eco Person tables are empty. The migration has no automatic down path and intentionally does not recover obsolete UUID configuration. Before aggregation/email activity, rollback is a restore of the reviewed pre-migration client-DB backup; after runtime rows or sends exist, require a coordinated full client-DB restore plan. A schema-only export is sufficient only for the reviewed empty local-development transition, not as a substitute for business-data backup after the new model is in use.

## Wymagane ENV dla backupu

- `POSTGRES_USER`
- `POSTGRES_DB`

Skrypt próbuje je odczytać z `docker compose config`, jeśli nie są ustawione w shellu.

## Uwagi dot. systemd backup

W repo jest:

- `ops/systemd/log-backup.service`
- `ops/systemd/log-backup.timer`

Aktualny `ExecStart` w `log-backup.service` uruchamia `ops/backup.sh create`, dokładnie ten sam command path co manualny run. Unit działa jako `logplatform`, ma `UMask=0077`, `Type=oneshot` i `TimeoutStartSec=4h`; timer zachowuje codzienny harmonogram 03:00 oraz `Persistent=true`.

MinIO jest kopiowane bez zatrzymania usługi do prywatnego staging directory, a tar czyta już niezmienny staging. Jest to ustanowiona non-disruptive praktyka repo i zakłada immutable artifact objects; nie jest filesystem snapshotem jednego punktu czasu dla równoległych mutacji. Nie uruchamiaj backupu podczas masowego uploadu/mutacji obiektów.

## Odtworzenie (high level)

1. Przywrócić środowisko i uruchomić stack `docker compose up -d`.
2. Odtworzyć dump PostgreSQL (platforma / ingest wg użytej bazy).
3. Odtworzyć dane MinIO (katalog lub wolumen).
4. Zweryfikować `GET /health` oraz kluczowe endpointy API.
5. Dla **Workflow A**: odtworzyć lub zsynchronizować **bazy klientów** zgodnie z procedurami poza tym repo, jeśli są niezależne od powyższego dumpu.

### Replaying the migration chain onto an EMPTY database is not a restore path

A restore replays a **dump**. Replaying `db/migrations/*.sql` against a blank PostgreSQL instance is
a different operation, and it does not complete on its own: two pieces of control-plane state are
**provisioned by the operator, never by a migration**, and the chain fails closed without them. This
is deliberate — a migration that invented them would be guessing at an environment's identity — but
it surprises anyone who assumes the migration files alone can bootstrap a platform.

| prerequisite | who creates it | when it must exist | what happens without it |
|---|---|---|---|
| `ops_control.environment_identity` marker row (`identity_key = 'primary'`, `database_role = 'platform'`) | operator, after `042_platform_environment_identity.sql` creates the empty table | before `047_artifact_upload_idempotency.sql` | `047` and every later guarded migration abort with `RAISE EXCEPTION 'platform environment identity guard is not provisioned'`. `042`'s own header states it "intentionally does not infer or insert local, staging, or production values". |
| the ALPHA00001 `trips_sync` row in `workflow_a_control.client_dataset_schedule` | operator onboarding, or the restored dump | before `060_workflow_a_daily_trips_lookback_l3.sql` | `060` aborts with `M2 target absent: no client_dataset_schedule row for ALPHA00001/trips_sync`. `060` is a **data** migration — it sets `lookback_days` on one existing production row (`docs/20` §14) — so it has nothing to update on an empty database. |

**This does not affect the documented production forward upgrade.** Production is already past both
points (platform ceiling `063`), so applying `064`–`069` needs neither step. The prerequisites matter
only for a from-scratch rebuild or a fresh development instance.

Verify them without exposing any secret — both checks read control-plane identity, not credentials:

```bash
set -a && source .env && set +a
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT identity_key, environment, database_role, database_name
     FROM ops_control.environment_identity WHERE identity_key = 'primary';"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT ca.client_code, s.dataset_name, s.frequency, s.lookback_days
     FROM workflow_a_control.client_dataset_schedule s
     JOIN workflow_a_control.client_account ca USING (client_id)
    WHERE ca.client_code = 'ALPHA00001' AND s.dataset_name = 'trips_sync';"
```

Exactly one row from each is the expected state. Do **not** add seed rows to the migrations to make
an empty replay convenient: the marker encodes which environment a database *is*, and a seeded guess
would let a development chain apply cleanly against a production identity it never verified.

### Backup coordination lock — one contract, three participants

`backups/.backup.lock` is the single advisory `flock` that serialises everything
touching the backup directory:

| Participant | Takes the lock | How |
|---|---|---|
| `ops/backup.sh` | yes, for the whole run | `exec 9>"$LOCK_FILE"; flock -n 9` |
| `ops/backup_retention.py` | yes, across discover → verify → plan → delete | `fcntl.flock(LOCK_EX \| LOCK_NB)` |
| restore (manual) | **must be wrapped by the operator** | see below |

Restore is documented command-by-command rather than scripted, so there is no
code path to take the lock on its behalf. Wrap the restore in it manually.

**The lock must be acquired BEFORE selection and verification, and held until the
last byte has been consumed.** Verifying first and locking afterwards leaves a
real window:

```
restore verifies set A            → A is VALID
                                    retention starts, takes the lock,
                                    A is expired and not an anchor → deleted
restore now takes the lock
restore replays postgres_A        → succeeds or half-succeeds
restore reaches minio_A           → gone. Inconsistent restore.
```

The whole critical section is therefore one command, holding **one file
descriptor** open across select → verify → consume → finish. Splitting it into a
verify step and a restore step is the defect, so the procedure is written so that
it cannot be split by accident:

```bash
cd /opt/log-platform
STAMP=YYYYmmdd_HHMMSS

# One flock, one fd, held for the entire critical section. Nothing between
# `flock` and the end of the heredoc runs without the lock.
flock -x backups/.backup.lock bash -s "$STAMP" <<'RESTORE'
set -euo pipefail
STAMP="$1"
cd /opt/log-platform

# 1. ATTEST + SELECT + VERIFY — under the lock, so nothing can delete the set
#    afterwards. `ops.verify_backup_set` attests THIS platform's identity itself
#    (ops_control.environment_identity) and pins the backup against it: there is
#    no flag to skip that, so the procedure cannot forget it. Full contract —
#    exact timestamp-derived members, environment/database/platform_uuid, mode,
#    size, sha256, and complete gzip/tar traversal.
#
#    `set -e` aborts the whole heredoc here on any failure, so a mismatch stops
#    BEFORE either PostgreSQL or MinIO is touched.
#
#    Run behind ops/run_with_environment_identity.py: attestation needs
#    LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID and the expected PostgreSQL
#    values, which live in the repository .env, not in the canonical identity
#    file. Invoked directly the verifier exits 1 with
#    EXPECTED_PLATFORM_IDENTITY_MISSING before verifying anything — safe, but it
#    would stop a legitimate restore for the wrong reason.
.venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.verify_backup_set "$STAMP"

# 2. CONSUME — still the same lock, and only reachable if step 1 exited 0.
docker compose up -d postgres minio
gzip -dc "backups/postgres_${STAMP}.sql.gz" | \
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

mkdir -p /tmp/log-platform-minio-restore
tar -C /tmp/log-platform-minio-restore -xzf "backups/minio_${STAMP}.tar.gz"
docker compose cp /tmp/log-platform-minio-restore/. minio:/data
RESTORE

# 3. Only now, outside the critical section, bring the API back up.
docker compose up -d api
curl -fsS http://127.0.0.1:8000/health
```

`set -euo pipefail` inside the heredoc means a failed verification aborts before
anything is consumed, and the lock is released by the shell exiting — there is no
path where verification is skipped but consumption proceeds.

### Restore identity invariant

**A backup may not be consumed unless it matches the authoritative target
identity: `environment` + `database` + `platform_uuid`.**

Structural validity is not enough. A staging backup can be perfectly
self-contained — exact members, correct hashes, readable archives — and still be
the wrong thing to restore into production. Verifying without identity pins
reported exactly that backup as `status: VALID`.

* Identity is **attested**, never supplied: `ops.verify_backup_set` reads
  `ops_control.environment_identity` through the same path retention uses. It is
  not taken from the backup, from the newest file, from a typed-in UUID or from an
  unvalidated environment variable.
* **Failure semantics** — any mismatch, any failed clause, or an identity that
  cannot be attested at all, aborts the procedure *before* PostgreSQL or MinIO is
  touched. Exit 1 = a set failed; exit 2 = the target identity is unknown, which
  is itself a refusal, not a warning.
* `repository_commit` is **not** pinned. It records the commit the backup was
  taken at, so requiring it to equal current HEAD would make every backup older
  than the last commit unrestorable. It is provenance, not deployment identity.

**Bare-metal rebuild caveat.** Attestation reads the identity marker from the
target platform database. Restoring into an existing, already-identified platform
— the ordinary case — works as written. Restoring into a *freshly created* empty
database has no marker yet, so `ops.verify_backup_set` exits 2 and the procedure
stops. That is deliberate: establish the target's environment identity first with
the existing provisioning/promotion tooling (`ops/promote_environment_identity.py`,
`ops/provision_local_environment_identity.py`), then restore. There is no bypass
flag, because a bypass is indistinguishable from the mistake it would enable.

Repo nie zawiera osobnego skryptu restore. Przed restore na istniejącym stacku
zrób dodatkową kopię aktualnych danych albo odtwarzaj do świeżych wolumenów.

## Platform prune recovery notes

### Backup shadow vs. the 13-month data-retention ceiling

A backup FILE is comfortably inside the platform ceiling — sets expire after 14
days. A backup's CONTENTS are the question that matters, and it is not answered
by pointing at the file's age: a 14-day-old full dump can contain a record whose
own age is thirteen months.

**Audited topology.** `ops/backup.sh` runs one `pg_dump` of the PLATFORM
database and tars MinIO `/data`. Client business databases, Cloudflare D1/R2,
`REPORTS_DATA_DIR` and the stage-2 scratch directory are in no
repository-controlled backup set at all — which is itself a disaster-recovery
observation worth acting on separately, and which is why only platform-Postgres
and MinIO stores carry a backup shadow.

**The guarantee.** `ops/backup.sh` takes full dumps with no per-record expiry
and no cryptographic-erasure key lifecycle, so a copy inside an archive cannot
be deleted individually. Compliance is therefore obtained at the SOURCE: stores
inside the backup set delete `PLATFORM_BACKUP_SET.shadow` (set retention plus
one expiry cycle, currently **15 days**) earlier than they otherwise would, on
top of the ordinary maintenance lead. The newest archive that can still contain
a record is one taken before that deletion, so every archive containing it has
expired by the record's 13-calendar-month deadline. Shorter live retention is
explicitly permitted by the policy; later deletion is not.

`ops.retention_registry.final_surviving_copy()` models the chain end to end and
`ops/tests_manual/test_retention_registry.py` asserts
`final_surviving_copy <= deadline` across a year of creation instants, together
with the counterfactual showing the guarantee fails without the shadow. See
`docs/42_platform_retention_and_schedule_governance.md` §6.

**Operational consequence for this document.** Changing `BACKUP_RETENTION_DAYS`
or the `backup-retention` cadence changes the retention lead of every
platform-database and MinIO store automatically — the value flows from
`ops/backup_retention.py` into the registry. Lengthening either shortens live
retention; do not change them without reading §6.

Platform prune never covers backup archives, manifests, Workflow B ingest/report files, database-export retention or customer-business databases. Its host command shares the coordinated backup lock and refuses to start while backup creation owns the exclusive lock. Preserve the latest verified PostgreSQL/MinIO pair before authorizing destructive retention.

PostgreSQL deletion is one serializable transaction committed only after all planned MinIO deletes succeed. If an object-store delete fails, the database transaction rolls back and metadata remains for retry. S3 and PostgreSQL have no distributed transaction: objects already deleted earlier in that failed attempt may temporarily retain database metadata, so recovery is to fix MinIO availability and retry the same idempotent planner—not to delete metadata manually. Environment identity drift, unknown artifact references or ambiguous storage identity must be corrected and independently reviewed before retry. After restore or runtime-environment correction, recreate only the Compose API and restart only `log-platform-api.service`; validate each with HTTP dry-run and the destructive-request 409 guard before authorizing any timer change.

## Workflow B w DR

Przywrócenie `ingest` i plików raportów na dysku (np. `REPORTS_DATA_DIR`) jest istotne, jeśli nadal polegasz na **ścieżce backupowej**; **Workflow B nie jest głównym modelem strategicznym**, ale dane historyczne mogą być potrzebne do audytu lub ręcznego dokończenia łańcucha.

Stage 2 cleaned-artifact recovery requires a consistent platform PostgreSQL + MinIO restore: `ingest.raw_file.stage2_cleaned_artifact_id` points at the canonical keyed artifact, while the deterministic object identity permits a compatible post-crash retry to return `reused`. A restored DB without its matching object storage (or vice versa) is an inconsistent state and must not be repaired by unkeyed re-upload.

Historical completed rows that predate the direct FK may be reviewed with the manual dry-run-first `ops/reconcile_stage2_cleaned_artifact_links.py`. Its canonical `0600` plan binds database/platform identity, candidate count, safe identifiers, artifact SHA-256/size and lineage fingerprints. Before any later `--execute`, take a coordinated verified Postgres+MinIO backup and preserve the reviewed plan/digest. Execution revalidates every row/object under a dedicated session advisory lock and one all-or-nothing transaction; rollback leaves every FK unchanged. Afterward verify row/status, artifact and object-store fingerprints independently. No timer or Workflow B orchestrator invokes this recovery tool.

Stage 1 reconciliation additionally depends on restored local Workflow B files referenced by `ingest.raw_file.raw_path` and `normalized_csv_path`, plus `stage1_normalized_artifact_metadata` from migration 049. Restore these paths with platform Postgres/MinIO before execute-mode reconciliation. Missing source produces a blocked result; multiple or incompatible legacy artifacts require operator review and are never overwritten or deleted automatically.

Stage 1/2/3 and parent Workflow B typed batch results are process-local orchestration facts. After restore, eligibility and retry decisions still come from restored ingest/artifact/control-plane and postprocessor migrated/error state; do not reconstruct a failed batch by treating logs as the source of truth. All 93 historical completed Stage 2 rows already have reconciled direct cleaned-artifact links. Restore migration 050 and its report-policy overrides together with the client default: Workflow B resolves override first, then client fallback. Its 06:00/20:00 Europe/Warsaw production timer remains uninstalled and disabled.

For manual rollback, first clear report overrides to `NULL`, then restore the prior client-level selector where required. Only after that, and only with explicit schema-rollback approval, drop `ck_report_type_client_load_policy_trip_metrics_source_override` and `trip_metrics_population_source_override`. Do not drop the column while active overrides remain.


## Manual Report 207 recovery backups

Before running `ops/recover_report_207_speed_violations.py --execute`, take and verify backups for every state store the selected scope can change:

- platform PostgreSQL and MinIO, because cleaned artifacts and Workflow B lineage are read from platform state;
- the target client business database resolved from `workflow_a_control.client_account`, because `telematics_reports.report_207` rows and `public.client_trips` speeding counters are changed there.

The repository `ops/backup.sh` covers the standard platform stack only. It does not automatically back up separate client business databases, so the operator must use the client DB backup procedure before acknowledging the recovery script's `BACKUP_CONFIRMED:<client_code>:<date_from>:<date_to>` token.

## Environment identity after restore or clone

Database dumps include `ops_control.environment_identity`. A restore intended to continue as the same logical production database may retain its reviewed identity, subject to the production recovery procedure. A production backup restored for local/dev or staging must not be used by guarded jobs until a deployment administrator intentionally reprovisions a new environment and UUID and updates the matching client expectation. A copied production marker combined with normal `local_dev` runtime declarations fails closed.

Do not let runtime jobs auto-correct restored markers. Local marker provisioning remains a separate `local_dev`-only operation. Classification of the same reviewed database installation from `local_dev` to `production` must use the official journalled promotion procedure below; clone provisioning and production restore identity still require separate reviewed recovery decisions and backup verification.

## Recovery from runtime identity provisioning or partial promotion

Provisioning and promotion have different recovery boundaries. Provisioning converges launch configuration while every database marker remains `local_dev`; promotion later changes database/control-plane/file identities and is journalled by migration 053. Never combine their recovery steps or manually edit a marker/runtime declaration.

### Provisioning failure

The provisioning tool creates a restricted external recovery copy before replacing each existing runtime file and uses file/directory fsync plus atomic rename. The canonical file, wrapper, helper, sudoers fragment and systemd drop-ins can therefore be inspected and reapplied deterministically. After failure while removing an old declaration or installing wrapper/systemd/Docker configuration:

Restore the read-only inspection boundary first if it is absent or damaged: verify the reviewed repository hashes, run `ops/install_runtime_identity_inspector.py` in dry-run mode against the exact host/release and verified checkpoint, approve its exact attestation separately, then install only the root-owned inspector and exact sudoers fragment. Verify `/usr/local/sbin/log-platform-runtime-identity-inspector` is `root:root` `0755`, `/etc/sudoers.d/log-platform-runtime-identity-inspector` is `root:root` `0440`, both hashes match the reviewed plan, and `visudo -cf` succeeds. Inspector restoration does not provision identity or authorize provisioning/promotion; those remain separate approvals.

Before regenerating, executing, resuming, or rolling back runtime identity work, restore a clean reviewed repository state through the normal commit/review workflow. Do not stash, reset, restore, or delete operator changes as a recovery shortcut. The clean-state guard blocks staged, unstaged, untracked, conflict, dirty-submodule, and active Git-operation state before any identity write. A new provisioning approval binds its exact HEAD. Recovery-v2 retains its original/current implementation contract. Resume-v2 alone permits a clean current HEAD that is identical to or a descendant of the original v5 execution HEAD and separately binds live resume implementation assets; sibling, unrelated, or reversed ancestry is refused. Resume plan version 3 additionally queries and binds the actual `origin` `refs/heads/main` SHA and normalized repository identity with `git ls-remote`; stale remote-tracking refs are never recovery evidence.

1. do not start/restart/recreate an affected consumer;
2. preserve the dry-run plan, exact attestation, verified checkpoint, and the external recovery evidence directory bound to the operation, plan SHA-256, repository HEAD, logical sources, and exact source/backup hashes;
3. compare repository and installed hashes and inspect `/etc/log-platform/environment-identity.env` without sourcing it;
4. rerun provisioning dry-run; if its plan is complete and the remaining actions are safe, obtain new approval for that exact plan;
5. run `systemctl daemon-reload` only through the provisioning execute path, then separately approve required restarts/recreation;
6. prove every running surface is `local_dev` and readiness no longer reports `RUNTIME_IDENTITY_NOT_PROVISIONED`, `RUNTIME_IDENTITY_MIXED`, or `RUNTIME_RELOAD_REQUIRED`.

A stale running systemd API or Docker API is not repaired by matching files: restart/recreate it and inspect its effective process/container environment. Do not restore a removed declaration to a secret-bearing file merely to make a stale process match; recover toward the single canonical source.

### Promotion failure

Migration 053 stores the immutable plan and completed steps. Its existing states remain `planned`, `in_progress`, `completed`, `failed`, and `rolled_back`; reload detail is represented by `current_step`/`completed_steps`: database identities converged, canonical file updated, `runtime_reload_required`, `runtime_processes_verified`, and final verification. There is still no global transaction across platform DB, client DBs, filesystem and processes.

New forward promotions require contract v5. The journal stores the complete v5 object and hash; v3/v4 forward execution is superseded. V5 binds the selected checkpoint checksum and restricted provisioning recovery root/directory/backup/evidence metadata and hashes plus sudo policy, implementation assets, historical recovery and explicit exclusions. Recovery-v2 separately revalidates the original historical v4 object and exact evidence without reinterpretation. Because live PID/InvocationID/container/Compose provenance is approval evidence, a normal runtime replacement invalidates an unused promotion approval and requires a fresh dry-run; it does not authorize altering recovery evidence.

Recovery order:

#### Known rollback gap for the current v5 reload-paused journal

Recovery-v2 currently accepts only a `failed` journal whose original contract/evidence shape matches its designed recovery inputs. It cannot process the existing v5 `in_progress` journal paused at `runtime_reload_required` with a non-null canonical runtime-file backup/hash set. That state has no implemented rollback contract: it must not be relabelled `failed`, have its backup fields cleared, or be forced through recovery-v2. Resume-v2 may only finalize it after every persistent and running surface is already proven at `production`; otherwise stop and design/review a separate rollback path. Production deployment of resume-v2, migration 054, production plan generation, and journal completion are distinct gated operations.
Resume-v2 is not an early-stage recovery contract. If the frozen journal suffix begins before `runtime_reload_required`, state-aware CLI routing returns `PRE_RUNTIME_FORWARD_RESUME_CONTRACT_REQUIRED` before resume-v2 approval validation. No generic resume-v1 or new pre-runtime forward-recovery executor is available; preserve the journal and design/review a separate contract.

Both finalization paths are now atomic, so one previously unrecoverable outcome can no longer occur. The original forward-v5 executor used to commit the `final_verification` step and the completion transition separately; an interruption between them left every step complete with `current_step=NULL` and `state='in_progress'`, which resume-v2, recovery-v2 and a new promotion all refuse. `journal_finalize_forward_v5` now performs both in one transaction under strict row predicates, and resume-v2 finalizes the same way, so an interrupted finalization leaves either the resumable pre-finalization state or a durable `completed` row. This narrows only the finalization window: the pre-`runtime_reload_required` limitation above is unchanged, and no broad early-stage forward-recovery contract exists. An interruption after a committed write is always reported with `writes_performed=true` and requires a read-only journal re-read rather than a blind rerun.


1. stop new manual/scheduled writes; do not modify UUIDs, markers, control-plane rows, identity files, units or containers by hand;
2. retain verified platform/client recovery media, checkpoint, dry-run JSON, promotion ID, plan hash, attestation hash, helper result and checksum-bound canonical-file backup;
3. inspect the journal, canonical file, installed helper/parser/consumer hashes, every database marker/control-plane row, and running systemd/Docker API effective value;
4. accept only source or target environment values with the exact immutable UUID/name/client identities;
5. for a post-convergence journal, first verify the complete migration-054 catalog/history/function/trigger/comment contract and the null resume-audit state of that exact target promotion; preserved audit evidence on unrelated completed or failed journals is intentionally excluded and does not block it; then generate a fresh read-only `--resume-plan`, separately approve only its exact `future_command` containing the plan hash and resume-v2 attestation, and never reuse retired resume-v1 or pre-version-3 command material;
6. if paused at `runtime_reload_required`, separately approve only the listed restart/recreate actions, verify target process identity, then resume final verification;
7. if rollback is required, first make running-process direction safe, generate the read-only rollback plan against the original checkpoint, and obtain separate approval.

Failure-specific behavior:

- a client step is durable only after its top-level transaction commits and a fresh read-only connection confirms marker/UUID/capability; journal history alone is not proof;
- a client commit or post-commit verification failure prevents that step from being appended;
- platform/control-plane transactions commit explicitly before their journal steps; advisory unlock never commits application work and unlock failures are not suppressed;
- helper failure before rename leaves canonical at source; if no promotion backup was created, recovery-v2 binds explicit null and must not invoke the helper when canonical already equals rollback target;
- helper success/journal interruption still requires target checksum plus the journalled exact backup path/checksum;
- resume reconciles every fresh surface against the journal and rejects unsupported divergence before writing; it recollects actual remote Git and the complete schema-054 contract before lock, under lock before the first write, and at each later non-journal boundary;
- systemd/Docker convergence remains separately approved after a successful forward promotion;
- helper/parser/installed-unit hash drift stops recovery; never bypass the hash gate.

Recovery-v2 rollback now preserves execution truth across resource-release failures. Its owned cleanup order is rollback write-connection close, advisory unlock when acquired, then read-only autocommit lock-connection close; each attempt is isolated and ordered cleanup evidence is secondary to the primary error. A raw interruption before mutation is `RECOVERY_V2_EXECUTION_INTERRUPTED`. A raw interruption after any rollback mutation starts is `FAILED_PROMOTION_RECOVERY_PARTIAL_STATE` with partial exit, completed/in-flight actions and journal/evidence commit ambiguity. Cleanup failure after an otherwise successful rollback is `RECOVERY_V2_CLEANUP_FAILED`; a durably observed `rolled_back` row is never regressed to `failed`, and known rollback-evidence path/hash data remains in the report.

Remote movement is `RESUME_REMOTE_HEAD_DRIFT`; migration-history, column, constraint, function, trigger, blocking-comment, or audit-value movement is `RESUME_SCHEMA_CONTRACT_DRIFT`. If either occurs after a journal progress write, stop with the preserved completed prefix, `writes_performed=true`, partial exit and reconciliation required. Do not classify it as journal drift and do not substitute the approved value for an unavailable current remote/schema observation.

The retained production plan SHA-256 `4a409a35ae96cb64d4c6027af0c04db5f2dfe58a3e9dcb6436f58f073dfbb5f5` is rejected because it lacks both complete approval bindings. It and every earlier resume plan are audit evidence only: do not approve or execute them. After the fix is independently reviewed and deployed, production requires a new plan generated from a reverified Git remote and complete schema contract.

For any of those classifications, freeze further writes and use `--inspect-promotions all --promotion-id '<PROMOTION_UUID>'` with the canonical runtime-file argument before considering another operation. Re-read database markers/control plane through fresh read-only connections, verify unchanged UUIDs, and verify any journalled rollback-evidence file/hash under the approved recovery root. Do not automatically rerun recovery, resume, or a second rollback. An ambiguous journal/evidence commit is resolved by inspection and a separately reviewed next plan, never by rewriting historical evidence.

For a failed mixed state, first generate `failed_environment_identity_recovery_plan_v2`. It binds original plan identity plus the current clean recovery implementation, failed journal, actual surfaces, runtime IDs/health, assets, evidence inputs, nullability and exclusions. Recovery commits each migration-045 client reversal from IDLE and verifies it after reconnect; commits platform marker and selected control-plane reversal explicitly; verifies full `local_dev` convergence and `PRODUCTION_PROMOTION_READY`; then commits/re-reads `rolled_back`. Only after journal verification does it atomically create mode-`0600` checksum-bound rollback evidence and record the evidence path/hash. Direct marker UPDATE, automatic retry, restart/recreation and UUID changes are not recovery contracts.

After successful recovery, a new forward approval is contract v5 only. Its immutable history section binds the rolled-back promotion ID, original v4 contract and plan hash, final `rolled_back`/null-step journal state, exact rollback-evidence path/hash/schema/metadata and journal-reference match. Historical `completed_steps` is evidence only; fresh durable state is authoritative. Rolled-back rows are excluded from active blocking but cannot be deleted, replaced, regressed or resumed without invalidating the v5 approval. Recovery-v2 still validates the original v4 plan and evidence exactly; it never upgrades or rewrites them as v5.

The v5 forward plan also binds the normalized effective helper sudo-policy fingerprint/structure, deterministic promotion implementation asset hashes and the complete sorted exclusions. A policy, source, evidence or exclusion change therefore requires a new dry-run rather than reuse of an approval. Forward v3/v4 execution is classified `PROMOTION_PLAN_CONTRACT_SUPERSEDED` before any journal write.

The recovery-v1 audit hash `9d417d3bb88451d1edb1bba4c67564ae0b3947a59cec93060bd25f7ad6278cf5` is diagnostic only and cannot authorize execution. The current failed promotion must be rolled back and verified before a new promotion plan is generated; never layer another promotion over the mixed state.

The platform `ops/backup.sh` manifest alone does not cover separate client databases. The promotion checkpoint must bind independently verified client recovery evidence. Runtime configuration and database markers must not be corrected manually after restore.

### Runtime identity recovery discovery

Provisioning evidence is stored outside the Git worktree under the repository-adjacent `log-platform-runtime-identity-recovery` root. Locate a rollback input through its `recovery-evidence.json`, then verify the evidence and named backup are regular non-symlink files, exactly mode `0600`, owned by the fixed `logplatform` service account, and match the recorded hashes before use. Every recovery-specific directory, including the recovery root, must be owned by that account and exactly mode `0700`. The contract is unchanged under sudo: root execution validates the approved service UID/GID rather than demanding root ownership, and matching trusted sudo-origin metadata does not relax any path check. Never copy a recovery artifact back into the checkout, print its contents, or restore a legacy identity assignment merely to satisfy a stale process.

A recovery-root mode mismatch blocks both new dry-runs and executes. Preserve all artifacts unchanged and use only a separately reviewed `ops/remediate_runtime_identity_recovery_permissions.py` dry-run. That plan binds the clean HEAD, host, current/target root mode, service UID/GID, and exact backup/evidence paths and hashes; execute may change only the root mode and then revalidate the full pair. It does not authorize systemd remediation, daemon reload, restart, Docker recreation, or recovery-content changes.

For the API precedence migration, preserve the obsolete `90-environment-identity.conf`, install and verify `zz-environment-identity.conf`, reload manager metadata, and prove the merged unit includes the canonical file after the host reset before retiring `90-...`. Reload and verify again after retirement. These are configuration-recovery steps only: service restart and Docker recreation remain separate approvals. Inactive prune and backup oneshots are `PER_INVOCATION_READY` after daemon reload; their timers do not need restart.
