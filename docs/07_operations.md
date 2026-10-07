# Operations

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the form
> `PYTHONPATH="$PWD" python3 ops/runner.py …` anywhere in this document are
> development / local / debug only — they execute the mutable working tree.
> The only supported production entrypoint for boundary-covered jobs is the
> installed wrapper `/usr/local/bin/log-job-runner.sh <module> '<json>'`.
> There is no production alternative. See *Release boundary* below.


## 1. Stack runtime

Start/restart:

```bash
cd /opt/log-platform
docker compose up -d
```

Current host inventory and the recommended safe port placement plan are recorded in
`ops/reports/service_inventory_and_port_plan.md`. Before changing the API service
layout, review that report: the Docker API may already own `127.0.0.1:8000`, and
the safer coexistence path is to run the host `log-platform-api.service` on
`127.0.0.1:8001` until a deliberate cutover is planned.

Status:

```bash
docker compose ps
```

Health:

```bash
curl -fsS http://127.0.0.1:8000/health
```

### 1.0.1 API import contract before a Compose restart

The Compose API contract is intentionally `./api:/app`, `working_dir: /app`, and
`uvicorn main:app`. Production modules must therefore support both repository
package imports (`import api.main`) and the top-level container import
(`cd /app && import main`) without `PYTHONPATH=/`, duplicate package loading, or
fallback after an unrelated `ImportError`.

Before restarting or recreating a live API after import/package changes, run the
fresh-process regression and the exact image/bind-mount probe:

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python ops/tests_manual/test_api_runtime_import_contract.py

docker compose -f docker-compose.yml run --no-deps --rm -T api \
  python -c "import main; assert main.app is not None"
```

Against the base definition this probe exercises the **image**, which is exactly
what production executes; it therefore needs the candidate image to be built
first. Add `-f docker-compose.dev.yml` if you want to probe the bind-mounted
working tree instead.

The disposable Compose probe publishes no host port, has no restart policy, and
must complete before `docker compose restart api`. For a route-heavy change,
also start a bounded disposable `uvicorn main:app --lifespan off` container on a
container-only port and verify `/health`; do not replace the live API for this
preflight.

### 1.1 Internal API — after FastAPI route / multipart `Form(...)` changes

The `api` container never picks up `api/main.py` edits by itself. If you add or change **`Form(...)`** fields on **`POST /artifacts/upload`** (or similar multipart routes), the **running** process may still use the **old** signature: clients can send new fields (e.g. `raw_file_id`), but the handler never binds them, so **`artifacts.raw_file_id`** stays `NULL` while `run_id` still works.

How you pick the change up depends on which Compose definition you are running:

- **production (base `docker-compose.yml`)** — application code comes from the image, so editing `api/` changes nothing until the image is rebuilt and the container recreated. `docker compose restart api` will **not** help.
- **development (`-f docker-compose.yml -f docker-compose.dev.yml`)** — the source bind is restored, but `uvicorn` still does not auto-reload, so a restart is required.

1. **Pick the change up**

   ```bash
   cd /opt/log-platform

   # development: bind-mounted source, restart is enough
   docker compose -f docker-compose.yml -f docker-compose.dev.yml restart api

   # production: rebuild the image, then recreate only the API service
   docker compose -f docker-compose.yml build api
   docker compose -f docker-compose.yml up -d --no-deps --force-recreate api
   ```

   The production form is a deployment. Treat it as such: it requires its own
   authorization, a retained rollback image and the smoke tests below.

2. **Verify live OpenAPI** (replace host/port if your `LOG_API_URL` differs; default bind is `127.0.0.1:8000`)

   ```bash
   curl -sS http://127.0.0.1:8000/openapi.json \
     | jq '.components.schemas.Body_upload_artifact_artifacts_upload_post'
   ```

   Without `jq`, you can narrow with Python:

   ```bash
   curl -sS http://127.0.0.1:8000/openapi.json \
     | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['components']['schemas']['Body_upload_artifact_artifacts_upload_post'], indent=2))"
   ```

3. **Confirm the multipart body schema lists the expected properties** under `properties`:

   - `file`
   - `kind`
   - `run_id`
   - `raw_file_id`
   - `workflow_name`
   - `stage_name`
   - `artifact_role`
   - `report_type`
   - `client_code`
   - `original_filename`
   - `display_filename`
   - `metadata_json`

4. **One small upload test** — use tokens from `.env` (`API_WRITE_TOKEN`). Pick a real `run_id` (e.g. latest `runs.run_id`) and a real `ingest.raw_file.id` so the API accepts `raw_file_id` (otherwise you get `400`). Response JSON should include **`raw_file_id`** when the field was applied.

   ```bash
   cd /opt/log-platform
   set -a && . ./.env && set +a
   RUN_ID="$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c 'SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1;')"
   RAW_ID="$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c 'SELECT id FROM ingest.raw_file ORDER BY created_at DESC NULLS LAST LIMIT 1;')"
   echo probe > /tmp/artifact_upload_probe.txt
   curl -sS -H "Authorization: Bearer $API_WRITE_TOKEN" \
     -F "kind=REPORT" -F "run_id=$RUN_ID" -F "raw_file_id=$RAW_ID" \
     -F "workflow_name=workflow_b" -F "stage_name=stage_2_clean" \
     -F "artifact_role=debug_sample" -F "report_type=unknown" \
     -F "client_code=CLIENT_A" \
     -F "original_filename=artifact_upload_probe.txt" \
     -F "file=@/tmp/artifact_upload_probe.txt;type=application/octet-stream" \
     "${LOG_API_URL:-http://127.0.0.1:8000}/artifacts/upload" | jq .
   ```

   If `RAW_ID` is empty (no ingest rows yet), skip this probe or insert a test row only in non-production environments.

5. **Verify in Postgres**

   ```sql
  SELECT artifact_id, run_id, raw_file_id, workflow_name, stage_name,
         artifact_role, report_type, display_filename, layout_version,
         client_code, storage_key, created_at
  FROM artifacts
   ORDER BY created_at DESC
   LIMIT 5;
   ```

6. **Verify business timezone behavior**

   App-created Postgres sessions should report `Europe/Warsaw`; stored `timestamptz` values remain timezone-aware and are not rewritten.

   ```sql
   SHOW TIME ZONE;
   SELECT
     '2026-05-18 10:00'::timestamp AT TIME ZONE 'Europe/Warsaw' AS summer_utc,
     '2026-01-18 10:00'::timestamp AT TIME ZONE 'Europe/Warsaw' AS winter_utc;
   ```

   Expected UTC instants are `2026-05-18 08:00:00+00` and `2026-01-18 09:00:00+00`.

### 1.2 Artifact storage layout

MinIO pozostaje fizycznym backing store dla plików, a Postgres `artifacts` jest indeksem metadanych. Nowe artefakty uploadowane przez `POST /artifacts/upload` używają `layout_version=2`; stare obiekty mogą pozostać w historycznym układzie (`layout_version=1`) i nadal są pobierane przez zapisany `storage_key`. `layout_version=1` oznacza stary fizyczny klucz obiektu; `layout_version=2` oznacza standardowy layout budowany przez API.

Before deploying keyed upload support, apply platform migration `047_artifact_upload_idempotency.sql`, then restart the API because its multipart signature changed. The migration is additive, requires the provisioned platform environment-identity marker, leaves historical rows unkeyed, and adds a partial unique index for non-null `(idempotency_scope, idempotency_key)`. Do not backfill historical artifacts. Keyed uploads use a separate digest-only `idempotent/v1/...` physical key while retaining artifact `layout_version=2` compatibility metadata. If object upload succeeds but the DB insert fails, retry the same identity: the deterministic location is safely reused and the request converges on one row. Back up Postgres and MinIO together before production deployment; this task does not apply the migration automatically.

Fizyczny klucz obiektu:

```text
{workflow_name}/{stage_name}/yyyy={YYYY}/mm={MM}/dd={DD}/run_id={run_id}/{optional_report_type_segment}/{artifact_role}/{display_filename}
```

Przykłady:

```text
workflow_b/stage_1_fetch/yyyy=2026/mm=05/dd=12/run_id=<uuid>/raw/unknown__20260512T133718Z__ac3866ec__raw.xls
workflow_b/stage_1_fetch/yyyy=2026/mm=05/dd=12/run_id=<uuid>/normalized/unknown__20260512T133718Z__ac3866ec__normalized.csv
workflow_b/stage_2_clean/yyyy=2026/mm=05/dd=12/run_id=<uuid>/report_type=report_207/cleaned/report_207__20260512T221144Z__ac3866ec__cleaned.csv
workflow_b/stage_2_clean/yyyy=2026/mm=05/dd=12/run_id=<uuid>/report_type=report_207/validation/report_207__20260512T221144Z__ac3866ec__validation.json
```

Stage names reserved by convention:

- `stage_1_fetch`
- `stage_2_clean`
- `stage_3_load`

Common artifact roles:

- `raw`
- `normalized`
- `cleaned`
- `validation`
- `schema_diff`
- `rejected_rows`
- `load_input`
- `load_result`
- `metadata`
- `debug_sample`

Display filename:

```text
{report_type_or_unknown}__{timestamp_utc}__{raw_file_short_id_or_na}__{artifact_role}.{ext}
```

`timestamp_utc` uses `YYYYMMDDTHHMMSSZ`; `raw_file_short_id` is the first 8 characters of `raw_file_id`, or `na`. `original_filename` is stored separately in DB metadata and is intentionally not embedded in `display_filename`.

Verify layout after a Stage 2 run:

```sql
SELECT artifact_id, workflow_name, stage_name, artifact_role, report_type,
       client_code, raw_file_id, display_filename, original_filename, layout_version, storage_key
FROM artifacts
WHERE layout_version = 2
ORDER BY created_at DESC
LIMIT 20;
```

### 1.3 Artifact Browser API

Artifact Browser API exposes discovery, detail, download, preview, controlled annotation endpoints, and virtual folder metadata for artifacts indexed in Postgres and stored in MinIO. It does not move objects, delete artifacts, add user accounts/RBAC for token calls, or migrate historical objects.

`client_code` is optional artifact metadata for future access control, virtual folders, and client-specific browsing. Existing rows can be `NULL`, and current Workflow B Stage 2 does not infer it because Stage 2 has no safe client-code source in `ingest.raw_file`.

Use the read token:

```bash
cd /opt/log-platform
set -a && . ./.env && set +a
```

List latest Workflow B Stage 2 cleaned artifacts:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts?workflow_name=workflow_b&stage_name=stage_2_clean&artifact_role=cleaned&limit=20" \
  | jq .
```

Search and paginate:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts?search=report_207&limit=50&offset=0&sort=created_at_desc" \
  | jq .
```

Multi-value exact filters use repeated query parameters as the canonical form. Comma-separated values are also accepted for convenience. Field-specific contains filters use `<field>_search` (for example `report_type_search=207`) and apply only when no exact values are selected for that field.

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts?workflow_name=workflow_b&workflow_name=workflow_a&report_type=report_207&sort=created_at_desc" \
  | python3 -m json.tool
```

Filter facets for UI dropdowns:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/facets" \
  | python3 -m json.tool
```

Update manual description/metadata. This writes `artifact_metadata_overrides`; it does not modify the object in MinIO and does not overwrite system `artifacts.metadata_json`.

```bash
ARTIFACT_ID="<artifact_id>"
curl -sS -X PATCH \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description":"Reviewed report 207 output","metadata_json":{"review_status":"ok"}}' \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/metadata" \
  | python3 -m json.tool
```

Add a tag:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tag":"reviewed"}' \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/tags" \
  | python3 -m json.tool
```

Delete a tag:

```bash
curl -sS -X DELETE \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/tags/reviewed"
```

Tags are canonical lowercase labels. They are trimmed, max 64 characters, and limited to `a-z`, `0-9`, `_`, `.`, `:`, `-`, starting with a letter or digit.

Get detail with run/raw-file lineage:

```bash
ARTIFACT_ID="<artifact_id>"
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID" \
  | jq .
```

Download:

```bash
curl -OJ -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/download"
```

Convenience lineage lists:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/runs/<run_id>/artifacts" \
  | jq .

curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/raw-files/<raw_file_id>/artifacts" \
  | jq .
```

Supported exact filters on `/artifact-browser/artifacts`: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `client_code`, `tag`, `run_id`, `raw_file_id`, `layout_version`, `file_ext`, `kind`, `content_type`, plus `date_from`, `date_to`, and global `search`. Multi-value exact filtering is supported for `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `file_ext`, `client_code`, `layout_version`, `kind`, and `tag`; tag filtering uses OR semantics. Contains filters use `<field>_search` for the same text-like fields, including IDs cast to text; exact values take precedence over `_search` for the same field. Supported sort values are allowlisted: `created_at_desc`, `created_at_asc`, `workflow_stage`, `report_type`, plus per-column asc/desc values for `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `client_code`, `file_ext`, `size_bytes`, `layout_version`, and `filename`. Invalid sort values are rejected instead of interpolated into SQL.

Virtual folders are logical navigation metadata only. **Manual** folders hold explicit artifact links in `artifact_virtual_folder_items`. **Smart** folders store filter criteria in `search_query_json` and list matching artifacts dynamically (same semantics as Artifact Browser filters); they do not insert rows into `artifact_virtual_folder_items` for matches, do not change MinIO keys, copy objects, remove objects, or replace the filter/search view. One artifact can be assigned to multiple manual folders. Deleting a folder deletes folder metadata, child folder records and manual folder-item relations only; it does not delete artifacts.

Create a root folder:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"folder_name":"Raporty FleetWeb","description":"FleetWeb report artifacts"}' \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders" \
  | python3 -m json.tool
```

Add an artifact to a folder:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"artifact_id":"<artifact_id>"}' \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>/artifacts" \
  | python3 -m json.tool
```

List a folder with child folders and assigned artifacts:

```bash
curl -sS \
  -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>?limit=50&offset=0&sort=created_at_desc" \
  | python3 -m json.tool
```

Remove an artifact from a folder without deleting the artifact:

```bash
curl -sS -X DELETE \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>/artifacts/<artifact_id>"
```

### Database Explorer async export worker

Large Database Explorer exports are generated by `ops/database_export_worker.py`, a local process outside Uvicorn/API request handling. The repository only provides code and proposed units; operators must apply migration `043_database_explorer_async_exports.sql` before queueing background exports. Migration `044_database_export_system_folders.sql` is required to enable the per-owner Reports system folder for those exports. Enable units deliberately in the target environment after the required migrations are applied.

Manual one-shot cycle (claim at most one queued job, recover stale leases, run cleanup):

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 -m ops.database_export_worker --once
```

Continuous local worker:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 -m ops.database_export_worker --loop --poll-seconds 30 --cleanup-interval-seconds 3600
```

Cleanup only (safe to run hourly):

```bash
PYTHONPATH="$PWD" python3 -m ops.database_export_worker --cleanup-only
```

Proposed units, not enabled by the repo:

- `ops/systemd/proposed/database-export-worker.service` — continuous worker, one global export at a time;
- `ops/systemd/proposed/database-export-cleanup.service`;
- `ops/systemd/proposed/database-export-cleanup.timer` — hourly cleanup if the continuous worker is not used for cleanup.

Migration 044 is additive: it creates `database_export_system_folders`, adds nullable `database_export_jobs.system_folder_id`, and adds indexes/foreign keys without changing direct exports or existing artifacts. Before 044 is applied, application schema probes keep the system-folder routes controlled: async queueing and the worker remain safe on the migration-043 schema, `/user/database/exports` uses the legacy owner-scoped job list, and `/user/reports/database-exports` falls back instead of raising a missing-column error. After 044, the first async queue action for a user idempotently provisions that user's single `Database Exports` folder.

Fresh host installation must keep Docker and host MinIO endpoints separate. The API
container uses Docker DNS (`MINIO_ENDPOINT=minio:9000`) and must continue to get
that value from the shared runtime configuration. Host systemd services cannot
resolve Docker's `minio` name, so only `database-export-worker.service` and
`database-export-cleanup.service` load a later host-only override file:

```text
/etc/log-platform/runtime.env
/etc/log-platform/database-export-minio-host.env
```

The override file must contain exactly the host-resolvable endpoint, without a
scheme or duplicated secrets:

```text
MINIO_ENDPOINT=127.0.0.1:9000
```

Do not put credentials, access keys, `MINIO_BUCKET`, `MINIO_SECURE`, or copied
runtime values in this file. Do not edit shared `/etc/log-platform/runtime.env`
to `127.0.0.1:9000`: inside the Docker API container, `127.0.0.1` is the API
container itself, not MinIO, so artifact upload/download would break.

Install order for a fresh local host:

```bash
cd /opt/log-platform
sudo install -d -m 0750 /etc/log-platform
sudo install -m 0600 /dev/null /etc/log-platform/database-export-minio-host.env
printf 'MINIO_ENDPOINT=127.0.0.1:9000\n' \
  | sudo tee /etc/log-platform/database-export-minio-host.env >/dev/null
sudo install -m 0644 ops/systemd/proposed/database-export-worker.service /etc/systemd/system/database-export-worker.service
sudo install -m 0644 ops/systemd/proposed/database-export-cleanup.service /etc/systemd/system/database-export-cleanup.service
sudo install -m 0644 ops/systemd/proposed/database-export-cleanup.timer /etc/systemd/system/database-export-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now database-export-worker.service
sudo systemctl enable --now database-export-cleanup.timer
```

The timer remains enabled for hourly cleanup; do not start
`database-export-cleanup.service` directly during installation unless you
intentionally want a one-shot cleanup run.

Safe verification of the active worker environment prints only the endpoint:

```bash
pid="$(systemctl show -p MainPID --value database-export-worker.service)"
sudo awk -v RS='\0' '/^MINIO_ENDPOINT=/{print}' "/proc/${pid}/environ"
systemctl is-enabled database-export-cleanup.timer
systemctl is-active database-export-cleanup.timer
```

Operational behavior:

- queue table: `database_export_jobs`;
- system-folder table after migration 044: `database_export_system_folders`, with stable identity from `(owner_user_id, system_key='database_exports')` and job linkage through `database_export_jobs.system_folder_id`;
- attempt-object cleanup ledger: `database_export_attempt_objects`;
- claiming: Postgres advisory transaction lock plus `FOR UPDATE SKIP LOCKED`; each claim writes a new `claim_token`, a unique `attempt_object_key`, and an `active` ledger row;
- lease: `lease_expires_at`, refreshed while rows are streamed only when the worker still holds the matching `claim_token`;
- stale policy: expired `running` jobs are requeued while `attempt_count < 3`; after that they fail with `WORKER_INTERRUPTED`; stale workers cannot overwrite a later claim's state or artifact linkage;
- unpublished attempts from stale/failure/fence-loss paths are marked `cleanup_pending`; cleanup retries object deletion idempotently until `last_cleanup_success_at` is recorded, then keeps the ledger row durable without selecting it again;
- graceful shutdown: `SIGTERM` / `SIGINT` wakes the idle loop promptly, prevents any new queue claim, and lets an active export stop only at safe checkpoints; a shutdown requested after upload marks the attempt object `cleanup_pending` and does not publish or falsely complete the job;
- output row cap: 1,000,000 data rows, excluding the header; no truncation;
- folder lifecycle: queued and running jobs are virtual rows only; completed jobs expose a download only through the real artifact; failed jobs show generic safe text; expired/deleted artifacts remain non-downloadable with no stale URL;
- retention: completed async exports expire exactly 3 calendar days after `completed_at`; download is blocked by metadata even if object deletion is retried.

Worker journal diagnostics are structured as single-line key/value events. Worker stderr/journald events use only these keys when the value is available: `event`, `job_id`, `stage`, `attempt`, `error_category`, `message`. Failed attempts emit exactly one `event=database_export_job_failed` line; `error_category` and `message` are static stage values such as `authorization_failed`, `query_failed`, `csv_generation_failed`, `xlsx_generation_failed`, `upload_failed`, `publication_failed`, `cleanup_failed`, or `unexpected_operation_failed`. The worker also logs safe `database_export_job_started`, `database_export_job_completed`, `database_export_job_fence_lost`, `database_export_mark_failed_error`, `database_export_cleanup_failed`, `database_export_cleanup_completed`, `database_export_stale_recovery_completed`, and `database_export_expiry_completed` events. Journal lines intentionally do not derive fields from raw exception messages, exception arguments, reprs, chained exceptions, tracebacks, request snapshots, filters, SQL, rows, row counts, artifact IDs, object keys, file paths, cookies, tokens, passwords, DSNs, credentials, environment values, or connection strings.

The worker uses existing platform DB and MinIO env (`POSTGRES_*`, `MINIO_*`) plus the portal client database mapping already configured for Database Explorer. No per-user or per-job retention override exists.

### 1.4 Local UI account management

Admin Portal Phase 2A exposes graphical management for local UI accounts:

```text
/admin/users
/admin/users/new
/admin/users/{user_id}
```

Only active users with `artifact_users.is_admin=true` can access these screens. They manage local UI sign-in for `/user`, `/admin`, and `/artifact-explorer`; they do not manage machine-level `API_READ_TOKEN` / `API_WRITE_TOKEN`.

Supported operations:

- create a local UI user,
- set display name, active status, and admin access,
- reset a password directly as an admin,
- assign/remove existing Artifact Explorer roles from `artifact_roles`,
- deactivate an account instead of deleting it.

Passwords are hashed by the API with the existing PBKDF2 helper. There is no self-registration, email-based password reset, or report-folder permission model in this phase. Customer/client assignment is handled separately by `/admin/client-access`. The UI blocks deactivating or demoting the last active admin.

### 1.5 Portal client access management

Admin Portal Phase 2B exposes graphical management for portal client access:

```text
/admin/client-access
/admin/client-access/clients/new
/admin/client-access/clients/{client_code}
/admin/client-access/users/{user_id}
/admin/groups
/admin/groups/{group_id}
```

Only active users with `artifact_users.is_admin=true` can access these screens. Operators can create and edit `portal_clients`, then assign active clients to local UI users through `portal_user_clients`. Client codes should match the `client_code` values used in artifact metadata and future client database mappings. The registry stores display names, active state and optional descriptions only; it must not store database credentials or other secrets.

Per-user client flags are:

- `can_view_database` - shows the assigned client on `/user/database`,
- `can_view_reports` - shows the assigned client on `/user/reports`,
- `can_export_database` - reserved for future controlled export behavior.

Portal client assignments also do not replace Artifact Explorer RBAC: technical artifact access remains controlled by `artifact_roles`, `artifact_user_roles`, and `artifact_role_permissions`. Portal groups can reuse the same client flags through `/admin/groups`, but they remain portal-only and additive. This phase does not enable arbitrary SQL, self-registration, or email password reset. Report-folder and dataset assignment are managed separately.

### 1.6 Portal report folder management

Admin Portal Phase 2C exposes customer-facing report folder management:

```text
/admin/report-folders
/admin/report-folders/new
/admin/report-folders/{folder_id}
/admin/report-folders/{folder_id}/preview
```

Operational flow:

1. Create or activate the client in `/admin/client-access`.
2. Assign the user report access to that client (`can_view_reports=true`).
3. Create a report folder for that client in `/admin/report-folders`.
4. Define only allowlisted artifact filters: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `file_ext`, `tag`, `search`, `date_from`, `date_to`. Do not enter `client_code` in filter JSON; the selected folder client is forced by the server.
5. Preview matching reports from the admin preview page. The preview table intentionally hides raw storage keys and technical lineage.
6. Assign users or groups to the folder. The UI/server rejects user assignment unless the user has effective report access to the folder client, and rejects group assignment unless the group has `can_view_reports=true` for the folder client.

Portal users see assigned folders at `/user/reports` and matching reports at `/user/reports/folders/{folder_id}`. Portal preview/download wrappers check direct-or-group folder assignment, active client/folder/group state, effective `can_view_reports`, folder `can_preview`/`can_download`, artifact filter match, and matching artifact `client_code`. This does not grant arbitrary artifact browsing and does not replace Artifact Explorer RBAC.

This phase does not enable arbitrary SQL, implicit access to all clients, self-registration, or email workflows.

### 1.7 Portal database catalog management

Admin Portal Phase 3A exposes graphical management for the Client Database Explorer catalog. Phase 3B adds read-only row browsing, and Phase 3C adds controlled CSV/XLSX export plus portal audit events:

```text
/admin/client-access/database
/admin/client-access/database/new
/admin/client-access/database/{dataset_id}
```

Operational flow:

1. Create or activate the client in `/admin/client-access`, and set the client `database_name` to the PostgreSQL **database name** of that client's business database (for example `client_code = ALPHA00001` → `database_name = alpha_main`). This is a bare allowlisted database name (`^[A-Za-z_][A-Za-z0-9_]{0,62}$`), never a DSN or credentials. The control-plane `logdb` (portal/runs/logs/artifacts) is separate and is not used as the dataset data source. Metadata discovery and row browsing/export connect to the mapped client database using the platform Postgres host/port/user/password with only the database name swapped, in a read-only session. The portal Postgres role needs, in each client database: `CONNECT` on the database, `USAGE` on the approved schemas, `SELECT` on the approved tables/views, and `information_schema` metadata visibility. The portal performs no auto-grants, DDL, schema mutation, or client DB writes. If `database_name` is missing, the dataset create page shows a specific "This client does not have a database name configured" diagnostic instead of a generic metadata error.
2. Assign the user database access to that client (`can_view_database=true`).
3. Register a dataset in `/admin/client-access/database` with customer-facing name, description and slug. Select the portal client from active `portal_clients`, then use `Refresh metadata` to progressively choose a visible non-system schema, table/view and date/time default column from that client's mapped database. Manual schema/table fallback is available only for single safe identifiers.
4. Add visible catalog columns. On the dataset detail page, prefer the guided physical-column picker when the registered table is reachable: it lists undiscovered columns from `information_schema.columns`, generates display labels, normalizes data types and can add multiple selected columns at once. Guided column saves validate selected names against that discovered physical-column allowlist, so real PostgreSQL column names with spaces, hyphens, uppercase letters or non-ASCII characters are supported. If no columns appear, read the admin diagnostic state: schema/table not found, metadata query failure, zero discoverable columns or all columns already cataloged. The manual single-column form remains available as fallback for simple identifiers. Column entries are portal metadata only; they do not create, drop or alter physical database columns.
5. Optionally mark one catalog column as the default date column. Guided selectors only list date/time-like columns; if a date column is not cataloged yet, add it first through the picker or manual form. Schema/table changes are blocked while catalog columns exist to avoid stale metadata pointing at a different physical table.
6. Assign users or groups to the dataset. The UI/server rejects user assignment unless the user has effective database access to the dataset client, and rejects group assignment unless the group has `can_view_database=true` for the dataset client. The dataset detail page shows an **Access diagnostics** panel (dataset status, client status, client database name, visible catalog columns, eligible user/group counts) and labels each assigned user `source: direct` and each assigned group `effective via group`. When no users/groups are eligible, the "Assign user"/"Assign group" controls now explain that eligible accounts are active accounts with database access to this dataset's client and link to `/admin/client-access` (users) or `/admin/groups` (groups) to grant that prerequisite first. This is an actionable diagnostic only; the eligibility rule and access checks are unchanged.
7. Review `/user/database` as the target user. It should show active dataset cards grouped by client.
8. Open a dataset card to `/user/database/datasets/{dataset_id}`. The row browser should show only approved visible columns.
9. Grant `can_export_rows=true` only for users or groups allowed to download filtered dataset rows. Direct and group dataset flags combine additively through OR semantics.
10. Use `/user/database/datasets/{dataset_id}/export?format=csv` or `format=xlsx` from the dataset page for controlled exports; review export events in `/admin/audit`.
11. To remove a dataset from the portal, use `Deactivate dataset` on `/admin/client-access/database/{dataset_id}`. This sets `is_active=false` only; it does not drop or alter the physical table, delete rows, remove assignments, or delete audit history. Inactive datasets are hidden from `/user/database` and denied for row browsing/export.

Troubleshooting — "I cannot add dataset visibility/access to users": dataset-user (and dataset-group) assignment is intentionally gated behind a client-level prerequisite. A user is only eligible when it is active and has effective `can_view_database` for the dataset's active client, granted **directly** in `portal_user_clients` or **via an active group** in `portal_group_clients`. If the "Assign user" list is empty, no account satisfies that prerequisite yet — assign client database access first from `/admin/client-access` (users) or `/admin/groups` (groups), then return to the dataset page. The effective dataset access shown to the user under `/user/database` is the additive OR of direct and group grants, and requires active user, active client, active dataset, effective `can_view_database`, and effective `can_view_rows`; `can_filter_rows`/`can_export_rows` gate filtering and export independently. Row browsing and export always connect to the client's mapped `database_name`, never `logdb`.

Safe identifier rule for `schema_name`, `table_name` and manual fallback identifiers:

```text
^[A-Za-z_][A-Za-z0-9_]{0,62}$
```

Guided physical-column saves use a stricter allowlist model instead of that regex: selected column names must already be present in `information_schema.columns` for the registered dataset table, then query rendering double-quotes the catalog-approved physical name. Do not enter SQL expressions, dotted names inside one field, comments, functions, operators, JSON paths or connection details. The guided admin selectors perform metadata-only inspection through `information_schema` for visible non-system schemas, tables/views and columns; regular users do not get schema/table/column discovery and the UI does not preview row values. Phase 3B row browsing is read-only and selects only catalog-approved visible columns. Sorting and filtering are limited to catalog-approved sortable/filterable columns, filter values are parameterized, date range uses only the configured default date column, and pagination is capped at 500 rows per page. Direct Phase 3C exports reuse that same safe query model, include only approved visible columns, enforce a fixed 20,000-row cap, and write `database_export_success`, `database_export_denied`, `database_export_validation_failed` or `database_export_failed` into `portal_audit_events`. Background Database Explorer exports use the same validated query state for explicit 20,001-through-1,000,000-row requests, queue `database_export_jobs`, and are generated by `ops/database_export_worker.py` with 3-day artifact retention; requests above 1,000,000 rows are rejected without queueing. Phase 3B/3C browsing/export do not mutate client database schemas and do not add INSERT/UPDATE/DELETE operations.

### 1.8 Portal group management

Admin Portal Phase 4B exposes reusable portal groups:

```text
/admin/groups
/admin/groups/new
/admin/groups/{group_id}
```

Operational flow:

1. Create a group with a clear operator-facing name and keep it active only while it should grant access.
2. Add active local UI users to the group.
3. Assign active clients to the group and set `can_view_database`, `can_view_reports` and `can_export_database` flags.
4. Assign the group to report folders only after the group has report access to that folder client.
5. Assign the group to database datasets only after the group has database access to that dataset client; set dataset flags `can_view_rows`, `can_filter_rows`, `can_export_rows` as needed.
6. Review a target user at `/admin/users/{user_id}` to see group memberships plus direct, group-derived and effective access counts.

Groups are portal-only. They do not create `portal_user_clients` rows, do not replace direct assignments, do not grant technical Artifact Explorer RBAC, do not change `artifact_roles`, do not grant arbitrary SQL and do not create implicit access to every client. Effective access is additive: direct assignments and active group assignments combine with OR semantics. Inactive groups do not grant client, report-folder or dataset access.

### 1.8.1 Eco Driving permission administration

Migration `051_portal_eco_driving_permissions.sql` is a deployment prerequisite. This implementation/commit does **not** apply it and does not create live grants. Use the following controlled procedure only after the normal backup, environment-identity and migration review for the target platform:

1. Confirm migration 051 is recorded in the target platform `public.schema_migrations` and that all six Eco columns exist with `NOT NULL DEFAULT FALSE`. Do not infer readiness from application code alone.
2. Sign in as an active portal administrator and open `/admin/client-access/eco-driving`. Admin management authority does not grant normal Eco Explorer data access.
3. Review the Users, Groups and Configured Eco Driving clients sections. A client without a registered provider remains configurable; registration never grants access automatically.
4. Open a user editor for a direct grant, or a group editor for a dynamic inherited grant. Review the direct, inherited and effective columns before changing anything.
5. Select only the required level: ranking; ranking + trip details; or ranking + trip details + reserved route permission. Route permission currently exposes no location/map data. Child grants add prerequisites automatically.
6. Save once. A stale page returns `409`; reload and review instead of resubmitting old state. A successful changed/no-change POST redirects back to the GET editor and cannot be repeated by refresh.
7. Review `/admin/audit` for exactly one `eco_driving_user_permissions_updated` or `eco_driving_group_permissions_updated` event when a change occurred. Verify subject, changed client codes, before/submitted/normalized states and normalization/cascade flags; a no-op has no update event.
8. Sign in as or perform an approved smoke test for the target user. Verify the user-facing Explorer link/data only for explicit effective grants (`direct OR active group`). An administrator without such a grant must remain denied as a normal Explorer user.
9. To revoke, uncheck ranking to clear ranking+details+routes, or uncheck trip details while retaining ranking to clear details+routes. Save, verify the cascade in audit, then confirm user-facing access is removed. Group revocation does not rewrite direct user grants.

The operation changes only platform `portal_user_clients`/`portal_group_clients` Eco flags and `portal_audit_events`. It does not write client-business databases, deploy migration 051, restart services, run jobs, send email, enable exports, expose routes/locations or create automatic grants.

### 1.9 Portal audit review

Admin Portal Phase 4A expands `/admin/audit` from a recent export list into a filtered operational audit view. Phase 4B adds portal group, group membership, group-client and report-folder/database-dataset group assignment events. Use it to review security-relevant portal activity: logins/logouts, denied portal/admin/report/database access, admin user-management, client access changes, group changes, report-folder changes, report preview/download, database catalog changes, row browsing and exports.

Supported filters are simple text/date inputs: `event_type`, `actor`, `client_code`, `dataset_id`, `report_folder_id`, `date_from`, `date_to`, plus `page` and `limit`. Date filters must be ISO date/datetime strings. Pagination defaults to 100 events and clamps `limit` to 500. This audit view is operational evidence, not a full SIEM or immutable retention system.

Audit metadata is sanitized before insert. Passwords, password hashes, tokens, API keys, secrets, authorization/cookie/session fields, DSNs/connection strings, SQL-looking keys and MinIO-looking fields are redacted. Operators should still avoid putting raw request bodies, row values, connection details or secrets into future audit metadata. Audit insert failures are non-blocking: the primary portal action continues and the failure is printed to server logs.

### 1.10 Portal deployment readiness

Use this checklist on a fresh deployment or after applying portal migrations:

1. Apply platform migrations from the repo root. The standard runner applies all `db/migrations/*.sql` lexically, including `033_portal_client_access.sql` through `037_portal_groups.sql`:

   ```bash
   set -a && . ./.env && set +a
   bash ops/db_migrate.sh
   ```

2. Set a stable UI session secret in the API environment before real users sign in:

   ```bash
   ARTIFACT_EXPLORER_SESSION_SECRET=<long random secret>
   ```

3. Bootstrap the first active admin with `scripts/bootstrap_portal_admin.py` as shown in the Artifact Explorer UI section. Prefer `--password-env`; unset the temporary password variable afterwards.

4. Restart the API after deployment if code or environment changed:

   ```bash
   docker compose restart api
   ```

5. Verify basic health and portal database readiness:

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py
   ```

   The readiness script checks the platform DB connection, all required portal tables, at least one active admin, and summary counts for users/clients/groups/folders/datasets/audit events. It exits non-zero when a critical item is missing and does not print secrets, DSNs, tokens, raw SQL, or row values.

6. In `/admin`, create operational objects in this order: users, groups, clients, report folders, database datasets, then direct/group assignments. Review `/admin/audit` after setup changes and after any customer-facing preview/download/export test.

7. Keep `/artifact-browser/*` bearer-token API clients separate from local UI users. Token API behavior remains machine-level and does not apply portal groups or Artifact Explorer RBAC.


### 1.11 Portal production service and reverse proxy

For a small always-on server/laptop deployment, the portal/API can run as a long-lived host service instead of relying only on the Compose `api` container. This is operational scaffolding only; it does not change portal permissions, Artifact Explorer RBAC, Artifact Browser token API behavior, schema, or job contracts.

Versioned examples:

- `ops/systemd/log-platform-api.service.example` — localhost-bound `uvicorn api.main:app` service with restart and basic systemd hardening.
- `ops/systemd/install_log_platform_api_service.sh` — safe installer that generates `/etc/systemd/system/log-platform-api.service` from host-specific paths and can run as `--dry-run`.
- `ops/nginx/log-platform.conf.example` — nginx proxy to `http://127.0.0.1:8000`, preserving `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`.

Adjust `User`, `Group`, `WorkingDirectory`, `EnvironmentFile` and `.venv` paths before installing. The example binds uvicorn to `127.0.0.1`; do not expose it directly on a public interface. Configure HTTPS termination in nginx/Caddy/another trusted proxy, or keep access behind VPN/local network controls.

Minimum API/portal env in the systemd `EnvironmentFile` is documented in `docs/02_infrastructure.md`. Use real secret values only on the host, for example `/etc/log-platform/api.env`, not in committed docs or example units. Required families are platform Postgres (`POSTGRES_*`), MinIO/artifact storage (`MINIO_*`), API bearer tokens (`API_READ_TOKEN`, `API_WRITE_TOKEN`), `ARTIFACT_EXPLORER_SESSION_SECRET`, `BUSINESS_TIMEZONE`, and `LOG_API_URL=http://127.0.0.1:8000` for local checks/jobs. There is no separate production-mode ENV variable in the current code.

The example file alone does not install a systemd unit. If:

```bash
sudo systemctl restart log-platform-api
```

returns `Unit log-platform-api.service not found`, install the unit first. Laptop-style install from the current checkout:

```bash
cd ~/ops/log-platform

bash ops/systemd/install_log_platform_api_service.sh \
  --user "$(whoami)" \
  --group "$(id -gn)" \
  --workdir "$PWD" \
  --env-file "$PWD/.env" \
  --venv "$PWD/.venv" \
  --enable \
  --restart
```

Use `--dry-run` first to print the generated unit and systemctl commands without writing `/etc/systemd/system/`:

```bash
bash ops/systemd/install_log_platform_api_service.sh \
  --user "$(whoami)" \
  --group "$(id -gn)" \
  --workdir "$PWD" \
  --env-file "$PWD/.env" \
  --venv "$PWD/.venv" \
  --enable \
  --restart \
  --dry-run
```

The helper reads no secret values and prints only the `EnvironmentFile` path. It runs `sudo` only for the actual unit install, `systemctl daemon-reload`, optional `enable`, and optional `restart`. Add `--yes` for non-interactive install after reviewing the dry-run output.

Install the reverse proxy example only after replacing placeholders and configuring TLS:

```bash
sudo cp ops/nginx/log-platform.conf.example /etc/nginx/sites-available/log-platform.conf
sudo ln -s /etc/nginx/sites-available/log-platform.conf /etc/nginx/sites-enabled/log-platform.conf
sudo nginx -t
sudo systemctl reload nginx
```

Post-deploy checklist:

1. Pull/update code and review `git diff --stat` for unexpected local changes.
2. Activate the venv: `source .venv/bin/activate`.
3. Install/update dependencies: `python -m pip install -r requirements-host.txt`. That one file is the
   complete native-host contract — it `-r`-includes `api/requirements.txt`, so the venv the systemd unit
   executes (`.venv/bin/uvicorn api.main:app`) gets the API's packages, `cryptography` among them, and the
   host jobs and `ops/` tools get theirs. The API's pins live once in `api/requirements.txt`. Never
   `pip install` a missing package by hand to make a start-up succeed: add it to the declaration instead,
   or the next host rebuilt from this document will not have it.
4. Take and verify a backup before migrations: `./ops/backup.sh`, then `gzip -t backups/postgres_YYYYmmdd_HHMMSS.sql.gz`; keep the matching MinIO archive.
5. Apply platform migrations and run readiness before restart:

   ```bash
   set -a && source .env && set +a
   source .venv/bin/activate
   ./ops/db_migrate.sh
   PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py
   ```

6. Bootstrap the first admin if needed: `PYTHONPATH="$PWD" python3 scripts/bootstrap_portal_admin.py --username admin --password-env PORTAL_ADMIN_PASSWORD`.
7. Run portal readiness: `PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py`.
8. Start or restart the service: `sudo systemctl restart log-platform-api.service`. On a release-boundary host this step is what makes an activated release live — the pointer move alone does not; see § "Release boundary".
9. Check service status and logs: `sudo systemctl status log-platform-api --no-pager` and `journalctl -u log-platform-api -n 100 --no-pager`. On a release-boundary host also confirm `ops/manage_release.py status` reports `pointer_matches_running_release: true`.
10. Verify health and routes: `curl -fsS http://127.0.0.1:8000/health`, open `/login` (canonical; `/artifact-explorer/login` remains a compatibility alias), confirm unauthenticated `/user` and `/admin` redirect to login, then sign in and review `/admin/audit`.

Follow logs during rollout:

```bash
journalctl -u log-platform-api -f
journalctl -u log-platform-api --since "1 hour ago"
```

For rollback, stop the service, restore the previous code revision or previous service/env file, run `sudo systemctl daemon-reload` when the unit changed, restart `log-platform-api.service`, then re-run `/health` and `ops/checks/check_portal_ready.py`. If migrations were applied and need data rollback, restore to a fresh platform DB/MinIO state from the pre-deploy backup; do not edit applied migration files in place.


### 1.12 Artifact Preview API (read-only)

Artifact Preview API adds JSON previews for common artifact file types. It does not implement PDF page rendering, tag/description editing, users/RBAC, virtual folder writes, or object migration.

Because preview support adds API image dependencies for spreadsheet parsing (`openpyxl`, `xlrd`), rebuild the API image after deploying this code:

```bash
cd /opt/log-platform
docker compose build api
docker compose up -d api
```

Supported previews:

- table: `csv`, `xls`, `xlsx`
- text: `txt`, `log`
- JSON: `json`
- PDF metadata: `pdf` (`pdf_inline`, no page rendering)

Preview limits:

- max object size loaded for preview: `25 MB`
- `rows_limit` default `1000`, max `5000`
- `text_chars_limit` default `20000`, max `100000`

CSV/table preview:

```bash
ARTIFACT_ID="<artifact_id>"
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/preview?rows_limit=100" \
  | python3 -m json.tool
```

XLSX sheet preview:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/preview?sheet_index=0&rows_limit=100" \
  | python3 -m json.tool
```

Text preview:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/preview?text_chars_limit=5000" \
  | python3 -m json.tool
```

PDF preview metadata:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/preview" \
  | python3 -m json.tool
```

Open a PDF/object inline through the download endpoint:

```bash
curl -OJ -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/download?disposition=inline"
```

### 1.13 Artifact Explorer UI

Artifact Explorer is the lightweight operator UI served by the same FastAPI app:

```text
http://127.0.0.1:8000/artifact-explorer
```

It uses the Artifact Browser API and Artifact Preview API helpers for list/filter/detail/preview/download, manual annotations, and virtual folder navigation. It can edit only artifact descriptions, manual metadata JSON, tags, and virtual folder metadata/memberships. There is no artifact delete, no file editing, no object migration, and no physical storage relayout.

Phase 6 adds local UI users and RBAC. Set a stable session secret before using the UI with real users:

```bash
ARTIFACT_EXPLORER_SESSION_SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
```

Store that value in the API container environment. If it is missing, the API uses a process-local development secret and all UI sessions are invalidated on restart.

Apply migrations before creating users:

```bash
bash ops/db_migrate.sh
```

Create or update the first admin user for Artifact Explorer, User Portal and Admin Portal:

```bash
export PORTAL_ADMIN_PASSWORD='<temporary strong password>'
PYTHONPATH="$PWD" python3 scripts/bootstrap_portal_admin.py \
  --username admin \
  --display-name "Portal Admin" \
  --password-env PORTAL_ADMIN_PASSWORD
unset PORTAL_ADMIN_PASSWORD
```

The script is idempotent: an existing `artifact_users.username` is updated to active admin and receives the new PBKDF2-HMAC-SHA256 password hash. It refuses empty passwords and does not print plaintext passwords. The older `scripts/create_artifact_admin.py` remains available for Artifact Explorer admin bootstrap and uses the same password hash format.

General user/role/permission management:

```bash
# User who can view/download only Workflow B Stage 2 cleaned artifacts.
python3 scripts/manage_artifact_rbac.py create-user --username stage2_reader
python3 scripts/manage_artifact_rbac.py create-role --role-name workflow_b_stage2_cleaned
python3 scripts/manage_artifact_rbac.py assign-role --username stage2_reader --role-name workflow_b_stage2_cleaned
python3 scripts/manage_artifact_rbac.py grant-permission \
  --role-name workflow_b_stage2_cleaned \
  --workflow-name workflow_b \
  --stage-name stage_2_clean \
  --artifact-role cleaned \
  --can-download

# User who can view only one client_code.
python3 scripts/manage_artifact_rbac.py create-user --username test_client_viewer
python3 scripts/manage_artifact_rbac.py create-role --role-name test_client_view
python3 scripts/manage_artifact_rbac.py assign-role --username test_client_viewer --role-name test_client_view
python3 scripts/manage_artifact_rbac.py grant-permission \
  --role-name test_client_view \
  --client-code TEST_CLIENT \
  --can-preview false

# Operator who can view/preview/edit annotations but cannot download.
python3 scripts/manage_artifact_rbac.py create-user --username annotation_operator
python3 scripts/manage_artifact_rbac.py create-role --role-name annotation_operator
python3 scripts/manage_artifact_rbac.py assign-role --username annotation_operator --role-name annotation_operator
python3 scripts/manage_artifact_rbac.py grant-permission \
  --role-name annotation_operator \
  --can-edit-annotations
```

Permission filter columns are allow-only exact matches. `NULL` means wildcard. Supported filters: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `client_code`, `file_ext`, `layout_version`, `tag`. Supported actions: view, preview, download, edit annotations. Admin users bypass artifact-level checks. Deny rules are not implemented.

The list page uses input-style searchable multi-select filters for `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `file_ext`, `client_code`, `kind`, `layout_version`, and `tag`, populated from `/artifact-browser/artifacts/facets`. Click or focus an input to open the facet dropdown, type to filter options, and select multiple values; selected exact values are shown comma-separated, e.g. `workflow_b, workflow_a`. If the operator types text without selecting an exact value, the form submits `<field>_search` and filters that field by case-insensitive contains. Existing URLs with repeated params such as `?workflow_name=workflow_b&workflow_name=workflow_a` remain compatible, and comma-separated exact values are also accepted.

The primary sort UX is the table header: click a sortable column title to toggle sorting while preserving current filters and pagination where possible. The active sorted column shows a single `↑` or `↓` indicator next to its title; inactive sortable columns show no indicator. Sortable headers cover Created, Workflow, Stage, Role, Report type, Client, Filename, Extension, Size, and Layout. The visible Extension column uses the stored `file_ext` value and displays `-` when it is missing. Detail pages show artifact metadata, lineage when available, annotation forms, a preview section, and download actions.

Virtual folder browsing is available at:

```text
/artifact-explorer/folders
/artifact-explorer/folders/<folder_id>
```

Folder pages show root/child folders, breadcrumbs, and artifacts assigned to the current folder. Folder names are visible to logged-in users for navigation, but artifact rows inside each folder are filtered by the existing artifact RBAC permissions. In Phase 7, folder management is admin-only (`artifact_users.is_admin=true`): create, rename, delete, add artifact to folder, and remove artifact from folder. Non-admin users can browse folders and visible artifacts only.

On artifact detail pages, admins can see manual memberships, smart folders that currently match (`membership_type`), and add/remove the artifact **only for manual folders** (dropdown lists full paths and excludes smart folders). Removing a membership does not delete the artifact. Deleting a virtual folder does not delete artifacts or MinIO objects.

The browser UI does not hardcode API tokens. It is server-rendered by FastAPI and uses same-origin UI routes protected by the session cookie:

```text
/artifact-explorer/artifacts/<artifact_id>/download
/artifact-explorer/artifacts/<artifact_id>/metadata
/artifact-explorer/artifacts/<artifact_id>/tags
```

The JSON APIs under `/artifact-browser/*` still require `Authorization: Bearer $API_READ_TOKEN` for reads and `Authorization: Bearer $API_WRITE_TOKEN` for annotation and virtual folder writes. Those token APIs remain machine-level access and do not apply per-user RBAC. The local UI uses server-side form routes and records `artifact_explorer:<username>` as the audit actor for annotation and folder writes.

After deploying route/template changes without auto-reload, restart the API service:

```bash
cd /opt/log-platform
docker compose up -d api
```

Verify OpenAPI still exposes backend endpoints:

```bash
curl -sS http://127.0.0.1:8000/openapi.json | python3 -m json.tool >/tmp/log-platform-openapi.json
```

List artifacts via API:

```bash
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts?limit=5" \
  | python3 -m json.tool
```

Preview a known artifact via API:

```bash
ARTIFACT_ID="<artifact_id>"
curl -sS -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/artifacts/$ARTIFACT_ID/preview" \
  | python3 -m json.tool
```

### 1.14 Legacy artifact metadata backfill

`ops/backfill_artifact_metadata.py` wzbogaca metadata starszych wierszy `artifacts`, gdy wartości można wywnioskować z wysoką pewnością. Skrypt:

- domyślnie działa jako dry-run,
- wymaga `--apply`, żeby zapisać zmiany,
- aktualizuje tylko kolumny w tabeli `artifacts`,
- nie przenosi, nie kopiuje i nie usuwa obiektów MinIO,
- jest idempotentny,
- zostawia `layout_version=1` dla starych fizycznych kluczy obiektów.

Dry-run:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py
```

Apply:

```bash
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py --apply --only-layout-version 1 --min-confidence high
```

Opcjonalne filtry:

```bash
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py --artifact-id <uuid>
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py --run-id <uuid>
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py --limit 100 --only-layout-version 1
```

Pola uzupełniane tylko przy wysokiej pewności: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `raw_file_id`, `file_ext`, `display_filename`, `original_filename`, a także marker `metadata_json.backfilled` z `backfill_source`, `backfill_confidence` i `backfill_fields`. Źródła wysokiej pewności obejmują `runs.source`, istniejące lub jednoznacznie dopasowane `ingest.raw_file`, `stage2_report_type`, deterministyczny suffix nazwy typu `__report_207.csv` i rozpoznany layout v2 w `storage_key`.

Skrypt celowo nie zgaduje `client_code`, nie zgaduje typu raportu z dowolnego tekstu w nazwie pliku i pomija wiersze, gdy stage/role/metadane są niejednoznaczne.

## 2. Migracje DB

Uruchomienie:

```bash
cd /opt/log-platform
bash ops/db_migrate.sh
```

Skrypt aplikuje `db/migrations/*.sql` w kolejności alfabetycznej i zapisuje stan w `public.schema_migrations`.

Aktualny zestaw migracji obejmuje m.in. schemat **`ingest` i kolumny Stage 2 dla Workflow B** (ścieżka backupowa) oraz control-plane Workflow A:

- `001_ingest_imap_fetch_reports.sql` … `005_ingest_raw_file_sha256_partial_unique.sql`
- `006_stage2_status.sql`
- `007_artifacts_raw_file_id.sql`
- `008_workflow_a_control_plane.sql`, `010_add_client_code.sql`
- `011_workflow_a_dataset_registry.sql`, `012_workflow_a_client_dataset_schedule.sql`, `013_workflow_a_client_table_retention.sql`
- `014_workflow_a_dispatcher_v1.sql`
- `015_workflow_a_v2_datasets.sql`, `016_workflow_a_disable_declared_v2_registry.sql` (`015` deklarowało V2 rows; `016` usuwa je z aktywnego registry, bo joby V2 nie istnieją jeszcze)
- `017_workflow_a_add_client_code_to_control_tables.sql` (denormalizuje `client_code` do schedule/history/retention control rows i backfilluje z `client_account`, gdy jest znany)
- `018_workflow_a_schedule_event_enrichment_mode.sql` (dodaje `client_dataset_schedule.event_enrichment_mode`, default `enabled`, CHECK `enabled|disabled`)
- `019_workflow_b_report_type_registry.sql` (dodaje `workflow_b_control.report_type_registry` jako read model typów raportów Stage 2; nie zmienia runtime detektora)
- `020_workflow_b_report_registry_detection_contract.sql` (dodaje Phase 1 maszynowo czytelnego kontraktu detekcji: `detection_rules_schema_version`, `column_types`, `multi_table`, `cleaner_entrypoint`; nie zmienia runtime detektora)
- `021_workflow_b_report_207_registry.sql` (dodaje `report_207` do control-plane registry; detekcja runtime pozostaje Pythonowa)
- `022_artifact_layout_metadata.sql` (dodaje metadata layoutu artefaktów i indeksy pod przyszły Artifact Explorer)
- `023_artifacts_client_code.sql` (dodaje opcjonalne `artifacts.client_code` oraz indeks pod filtrowanie per klient)
- `024_artifact_annotations.sql` (dodaje `artifact_metadata_overrides` i `artifact_tags` dla manualnych opisów/metadanych/tagów bez zmiany obiektów MinIO)
- `025_artifact_explorer_rbac.sql` (dodaje lokalnych użytkowników, role i allow-only permissions dla server-rendered Artifact Explorer UI)
- `026_artifact_virtual_folders.sql` (dodaje `artifact_virtual_folders` i `artifact_virtual_folder_items` dla metadata-only virtual folders; foldery i membershipy nie przenoszą ani nie usuwają obiektów MinIO)
- `027_artifact_smart_folders.sql` (dodaje `folder_type` + `search_query_json` dla smart / saved-search folderów; bez zmiany storage)
- `028_workflow_b_stage2_client_code_record_id.sql` (dodaje `report_type_registry.id_sync_column_name`, `report_type_registry.record_id_ingredients` oraz `ingest.raw_file.client_code` dla finalizacji Stage 2)
- `029_workflow_b_stage3_load.sql` (dodaje policy `workflow_b_control.report_type_client_load_policy` oraz pola `stage3_*` w `ingest.raw_file`)
- `030_workflow_b_alpha_gps_baza_log_registry.sql` (dodaje read-model registry i policy `data_overwrite=true` dla `ALPHA00001` / `Alpha_GPS_Baza_LOG`)
- `031_workflow_b_ensure_alpha_gps_baza_log_registry.sql` (idempotentnie naprawia/uzupełnia read-model registry i policy `data_overwrite=true` dla `ALPHA00001` / `Alpha_GPS_Baza_LOG`)
- `032_workflow_a_eco_driving_registry.sql` (dodaje Eco Driving dataset rows dla dispatchera, table registry rows oraz default-disabled schedule/retention rows dla istniejących klientów)
- `046_workflow_a_eco_person_registry.sql` (dodaje isolated Eco Driving Person dataset/table registry rows oraz default-disabled schedule/retention rows dla istniejących klientów; email schedules pozostają disabled-by-default)
- `033_portal_client_access.sql` (dodaje `portal_clients` i `portal_user_clients` dla portal-level client assignments)
- `034_portal_report_folders.sql` (dodaje `portal_report_folders` i `portal_report_folder_users` dla customer-facing Reports Explorer)
- `035_portal_database_catalog.sql` (dodaje `portal_database_datasets`, `portal_database_dataset_columns` i `portal_database_dataset_users` dla Client Database Explorer catalog; bez row browsing/export)
- `036_portal_audit_events.sql` (dodaje `portal_audit_events` dla portal export audit i przyszlych zdarzen security-sensitive)
- `037_portal_groups.sql` (dodaje `portal_groups`, `portal_group_users`, `portal_group_clients`, `portal_report_folder_groups` i `portal_database_dataset_groups` dla additive group-based portal permissions)
- `040_workflow_a_trip_metrics_population_source.sql` (dodaje `client_account.trip_metrics_population_source`, default `api_migration`, CHECK dla `api_migration|report_207_migration|d105_2_ecodriving_migration|disabled`)
- `050_workflow_b_report_postprocessor_selector_override.sql` (dodaje nullable, bez-defaultowego `report_type_client_load_policy.trip_metrics_population_source_override`; `NULL` dziedziczy client default; bez backfillu)
- `043_database_explorer_async_exports.sql` (dodaje kolejke `database_export_jobs`, ledger `database_export_attempt_objects` oraz owner/expiry metadata na `artifacts` dla async Database Explorer exports)
- `044_database_export_system_folders.sql` (dodaje stabilny per-owner system folder `Database Exports` i nullable `database_export_jobs.system_folder_id`; rollout jest addytywny, a aplikacja ma fallback, gdy ta migracja nie jest jeszcze zastosowana)
- `045_portal_database_quoted_column_names.sql` (relaksuje portal catalog CHECK constraints dla `portal_database_dataset_columns.column_name` i `portal_database_datasets.default_date_column`, aby guided Database Explorer mogl zapisac fizyczne nazwy kolumn wymagajace quoted identifiers; SQL safety pozostaje po stronie allowlisty aplikacji i cytowania identyfikatorow)

**Uwaga:** migracje te **nie** definiują samych w sobie Workflow A; główna baza biznesowa klienta pod A może być poza tym schematem.

Migracje baz biznesowych klientów Workflow A są osobne od `ops/db_migrate.sh`.
Dla istniejących klientów użyj:

```bash
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --apply
```

Migration `020_client_trips_final_schema.sql` przebudowuje bazowy
`client_trips`, zostawiając starą tabelę jako
`client_trips_legacy_backup_020` do rollbacku. Migration
`021_add_trip_mode_to_client_trips.sql` dodaje ordered kolumnę `trip_mode`
przez kolejny rebuild i zostawia `client_trips_legacy_backup_021`.

Dla emailowego Alpha GPS Workflow B zastosuj też client-business DDL `db/client_business/023_alpha_gps_baza_log_workflow_b.sql` do bazy `alpha_main`; tworzy `telematics_reports."Alpha_GPS_Baza_LOG"`. DDL `db/client_business/024_alpha00001_client_trips_dysponent_id.sql` dodaje nullable `public.client_trips."Dysponent_ID"` potrzebne przez ALPHA00001 postprocess enrichment, a `025_alpha00001_dysponent_id_batch_indexes.sql` dodaje pomocniczy indeks dla batch loop; standardowy runner client-business może dodać te obiekty globalnie, ale job działa wyłącznie dla `ALPHA00001`.

DDL `db/client_business/027_eco_driving_schema.sql` dodaje bazowe tabele wsparcia warstwy Eco Driving: `eco_trip_assignments`, `eco_driver_weekly_stats`, `eco_driver_monthly_stats`. DDL `db/client_business/028_eco_driving_periods_and_driver_chart.sql` dodaje `eco_drivers_id_chart`, view `public."Eco_Drivers_ID_Chart"`, audit wykluczenia prywatnych tripów oraz metadata dla cumulative month-to-date weekly ranking snapshots. DDL `db/client_business/029_eco_driving_nullable_scores.sql` pozwala zapisać `NULL` w punktach i score dla zerowego dystansu. DDL `db/client_business/030_eco_driving_trend_views.sql` dodaje query views `eco_driver_weekly_trends_view` i `eco_driver_monthly_trends_view` dla trend/progress reporting. DDL `db/client_business/031_eco_driving_validation_fields.sql` dodaje pola walidacji i `ecodriving_rating_type`, `db/client_business/032_eco_driving_weekly_email_notifications.sql` dodaje send log dla weekly email notifications, `db/client_business/033_eco_driving_weekly_email_send_log_grants.sql` dodaje send-log audit columns i granty `SELECT, INSERT, UPDATE` dla client DB usera, `db/client_business/034_eco_driving_rating_type_share_percent.sql` dodaje `ecodriving_rating_type_share_percent` do weekly/monthly stats i trend views, `db/client_business/035_eco_driving_monthly_email_notifications.sql` dodaje send log dla monthly email notifications, `db/client_business/036_eco_driving_round_per_100km_stats.sql` zaokrągla istniejące weekly/monthly `*_events_per_100km` do wartości całkowitych bez zmiany typów kolumn, a `db/client_business/037_eco_driving_score_from_rounded_per_100km.sql` przelicza istniejące punkty, `*_maxpoints_subtract`, `eco_driving_score_total`, walidacje, rating type i rating-type share percent z zaokrąglonych współczynników. DDL `db/client_business/044_eco_email_fail_closed_idempotency.sql` dodaje conflict-gated stable normal-send identity, ALPHA reservation-before-SMTP i oddzielne test/forced scopes; nie wolno stosować go bez zerowego wyniku read-only conflict preflight. Migracja obsługuje heterogeniczne schematy: każda z tabel driver/person weekly/monthly jest sprawdzana osobno, brak tabeli jest raportowany jako `ECO_EMAIL_TABLE_NOT_APPLICABLE`, brak wszystkich tabel Eco jest bezpiecznym no-op, a częściowo zastosowany kształt można uruchomić ponownie. Konflikt zgłasza model, tabelę, stabilny klucz podmiotu, okres, statusy, scopes, template types i row IDs, po czym cała transakcja jest wycofywana bez zmiany historii. Istniejące bazy dostają migrację przez standardowy runner `scripts/apply_client_business_migrations.py --apply`; nowy onboarding aplikuje pliki bez dodatkowych parametrów.

DDL `db/client_business/039_eco_person_driving_schema.sql` dodaje osobną rodzinę Eco Driving Person: `eco_person_people`, `eco_person_driver_mappings`, `eco_person_trip_assignments`, `eco_person_weekly_stats`, `eco_person_monthly_stats`, `eco_person_weekly_email_send_log`, `eco_person_monthly_email_send_log`, `eco_person_driver_mappings_view`, `eco_person_people_email_view` oraz trend views. Ta migracja nie modyfikuje tabel ALPHA00001 `eco_*`. DDL `db/client_business/041_eco_person_sent_archive_state.sql` dodaje addytywne kolumny do zachowania MIME i śledzenia Sent-folder archive statusu dla `eco_person_*_email_send_log`.
DDL `db/client_business/042_workflow_b_stage3_runtime_schema.sql` przenosi przygotowanie istniejących, zarejestrowanych tabel `telematics_reports`, indeksów `record_id`, pól trackingowych Report 207 i minimalnych grantów do adminowej ścieżki migracyjnej; recurring Stage 3 nie wykonuje tych operacji.

### BRAVO00016 Eco Person physical-person migration and import

Migration `043_eco_person_physical_person_identity.sql` is intentionally destructive to the obsolete UUID identity shape and therefore refuses to run unless all seven `eco_person_*` configuration/runtime tables are empty. Before applying it, attest `local_dev`, `logdb`, `telematics_main`, the control-plane client/database UUIDs, all five disabled schedules, and take a client-DB schema-only or full backup. Apply only the reviewed file so unrelated pending client migrations are not mixed in:

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/apply_client_business_migrations.py \
  --client-id 6018be20-5faa-41b6-89c9-fe2b54a8283e \
  --migration 043_eco_person_physical_person_identity.sql --list

PYTHONPATH="$PWD" .venv/bin/python scripts/apply_client_business_migrations.py \
  --client-id 6018be20-5faa-41b6-89c9-fe2b54a8283e \
  --migration 043_eco_person_physical_person_identity.sql --apply
```

The importer defaults to strict CP1250 and semicolon parsing. Always dry-run the exact reviewed absolute path first, review checksum/headers/raw rows/exact duplicates/logical rows/source collisions/physical groups/group conflicts/blocking errors, then use the same path for the atomic write:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_mapping_import \
  '{"client_id":"6018be20-5faa-41b6-89c9-fe2b54a8283e","csv_path":"/absolute/path/import eco_pesron_people bravo00016.csv","encoding":"cp1250","delimiter":";","dry_run":true}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_mapping_import \
  '{"client_id":"6018be20-5faa-41b6-89c9-fe2b54a8283e","csv_path":"/absolute/path/import eco_pesron_people bravo00016.csv","encoding":"cp1250","delimiter":";","dry_run":false,"apply":true}'
```

No-send verification requires all five `eco_person_driving_*` schedule rows to remain `enabled=false`, both send logs to remain empty, and no aggregation/email job invocation. Migration/import does not require a service restart, SMTP, or IMAP.

There is no automated down migration. Before any aggregation or email activity, rollback means restoring the reviewed pre-migration client-DB backup (or, with explicit approval, removing only the newly imported BRAVO00016 configuration and restoring the prior schema from the schema-only export). After runtime data or sent messages exist, use a full client-DB restore plan; do not replay obsolete UUID identities or improvise casts from textual aliases.

### Eco e-mail senders, SMTP acceptance and the Sent-folder copy

BRAVO00016 Eco Driving Person notifications — **weekly and monthly alike** — use the dedicated host ENV namespace `BRAVO_ECO_WEEKLY_EMAIL_*`. Do not export temporary aliases to `ECO_PERSON_EMAIL_*`; the disabled future schedule must be able to read the same namespace from the runtime environment file.

The namespace names a **sender mailbox, not a period** (`WEEKLY` in the variable names is historical). One account, one password and one Sent folder serve both reports, so the monthly job needs no duplicate credentials of its own. The only production monthly run to date (2026-08-12) failed twice on the absent `ECO_PERSON_EMAIL_*` namespace before it could proceed at all; that is the practice this consolidation removes. **Operator note:** the monthly mailer's `From` is now the documented BRAVO00016 identity below rather than the generic `Program Ecodriving <no-reply.ecodriving@example.invalid>` default. Monthly is not a declared scheduled dataset, so this takes effect only on the next explicitly invoked monthly send; check `from_header` in the run summary before authorizing it.

Operational sender requirements:

- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_USERNAME` and `BRAVO_ECO_WEEKLY_EMAIL_FROM_EMAIL` must both be `automations@example.invalid`;
- `BRAVO_ECO_WEEKLY_EMAIL_FROM_NAME` must be `Ecodriving Telematics`;
- blank `BRAVO_ECO_WEEKLY_EMAIL_REPLY_TO` means the header is omitted;
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_PASSWORD` and `BRAVO_ECO_WEEKLY_EMAIL_IMAP_PASSWORD` are protected secrets and must never be printed or committed;
- Sent-folder discovery should use IMAP `\Sent` special-use. Set `BRAVO_ECO_WEEKLY_EMAIL_IMAP_SENT_MAILBOX` only after verifying the mailbox listing.

#### What `sent` means, and where the copy is

All four Eco mailers (ALPHA weekly/monthly, BRAVO person weekly/monthly) behave identically here.

**`status='sent'` means: accepted by example.invalid SMTP for relay.** The relay answered the terminating dot with a `250`. It is **not** proof of recipient delivery: nothing downstream of example.invalid — the recipient's mail server, the mailbox, the reader — is observed anywhere in this platform. The evidence retained per recipient is:

- `smtp_message_id` — the `Message-ID` that was transmitted;
- `sent_at` — when acceptance was established;
- `provider_response` — the relay's own final reply, verbatim and prefixed by its reply code (e.g. `250 2.0.0 Ok: queued as ...`). When the reply is not captured it keeps the historical literal `SMTP accepted message without raising an exception`, which is a statement about the **library**, never about example.invalid. No server text is ever fabricated to fill this column;
- `metadata_json.smtp_acceptance.final_reply_observed` — `true` when the row quotes the relay, `false` when acceptance is established only by `smtplib` returning without raising. Read this before quoting `provider_response` at anyone;
- `metadata_json.smtp_acceptance` — `smtp_code`, `smtp_response`, `queue_id` (only if example.invalid emits one; it is never invented), `message_id`, `accepted_at` and which of the two evidence sources it came from.

The three SMTP outcomes stay distinct and unchanged: rejected (`failed`, retryable), accepted (`sent`), ambiguous (`pending` + the ambiguous marker, operator-only).

**After acceptance, the exact transmitted MIME message is appended to the sender mailbox's Sent folder** over IMAP and verified by `Message-ID`, so the mailbox shows what a manually sent message would show — same From, To, Subject, body and `Message-ID`, same bytes. The mailbox is the account's `\Sent` special-use folder, discovered per session, overridable with `{PREFIX}_IMAP_SENT_MAILBOX`.

Each sender **mailbox** — not each job — has one namespace, and the copy is filed in the Sent folder of the account the message was sent from:

| Mailer | SMTP + IMAP namespace | Sent copy today |
|---|---|---|
| BRAVO person weekly | `BRAVO_ECO_WEEKLY_EMAIL_*` | **yes** — configured; required, so a missing or half-written configuration stops the run before any customer mail |
| BRAVO person monthly | `BRAVO_ECO_WEEKLY_EMAIL_*` (same mailbox) | **yes** — inherits the same configuration |
| ALPHA weekly | `ECO_WEEKLY_EMAIL_*` | no — `ECO_WEEKLY_EMAIL_IMAP_*` is **not configured on this host**; runs report `sent_archive_enabled=false` and `sent_archive_skipped_count` |
| ALPHA monthly | `ECO_WEEKLY_EMAIL_*` (same mailbox) | no — same, and enabling it once enables both |
| any other person client | `ECO_PERSON_EMAIL_*` | only if that namespace's `_IMAP_*` values are supplied |

To give the two ALPHA mailers a Sent copy, supply `ECO_WEEKLY_EMAIL_IMAP_HOST`, `_IMAP_PORT`, `_IMAP_USERNAME`, `_IMAP_PASSWORD`, `_IMAP_USE_SSL` (plus optional `_IMAP_TIMEOUT_SECONDS` and `_IMAP_SENT_MAILBOX`) for the `ECO_WEEKLY_EMAIL_SMTP_USERNAME` mailbox. A partial configuration is an error, never a silent skip.

**A Sent-copy failure NEVER authorizes a resend.** The copy is a second, separate effect performed outside the SMTP failure path: once example.invalid has accepted the message the customer either has it or the relay owes it to them, and re-sending would produce a duplicate. A failed copy leaves `status='sent'` untouched, is counted as `sent_archive_failed_count`, is recorded against the row (`sent_archive_status='failed'` on the person send logs, `metadata_json.sent_folder_copy` on the driver ones) and logs an ERROR carrying `resend_authorized=false`.

If `sent_archive_status='failed'` while `status='sent'`, do not rerun the normal send and do not use `force_resend`. For the BRAVO weekly sender, retry only the archive step:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","period_start_date":"YYYY-MM-DD","period_end_date":"YYYY-MM-DD","archive_only":true}'
```

To restrict recovery to reviewed messages, pass `archive_message_ids` as a comma-separated string or JSON list. Archive-only mode never opens SMTP and uses the stored MIME bytes and original `Message-ID`.

## 3. Workflow B — Stage 1 run manualny (backup)

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.mail.fetch_reports '{"since_days":30}'
```

Stage 1 nadal zapisuje lokalne pliki pod `REPORTS_DATA_DIR` (`raw/` i `normalized/`) oraz wiersze `ingest.raw_file`. Dla nowo przetworzonych plików próbuje też addytywnie uploadować artefakty layoutu v2:

- `workflow_name=workflow_b`
- `stage_name=stage_1_fetch`
- `artifact_role=raw` dla pliku źródłowego
- `artifact_role=normalized` dla canonical CSV
- `report_type=unknown`
- `raw_file_id=<ingest.raw_file.id>`

Jeśli upload artefaktu się nie powiedzie, Stage 1 zachowuje dotychczasową lokalną persystencję i kończy run typed artifact-sync failure, dzięki czemu retry nie jest raportowany jako sukces.

Po wdrożeniu migracji `049_workflow_b_stage1_artifact_reconciliation.sql` Stage 1 używa keyed uploadów i przed IMAP wykonuje ograniczony reconciliation brakujących persisted artifacts. Artifact-sync failure kończy run typed wyjątkiem, ale nie cofa udanych ingest rows. Ręczna inspekcja bez IMAP i bez zapisu:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.mail.reconcile_report_artifacts '{"limit":50}'
```

Jawne wykonanie dla przejrzanych identyfikatorów:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.mail.reconcile_report_artifacts \
  '{"execute":true,"raw_file_ids":["<uuid>"],"roles":["raw","normalized"],"limit":10}'
```

Job nie czyta IMAP i nie ma timera. `execute=true` może uploadować wyłącznie brakujące role z istniejących `raw_path`/`normalized_csv_path`; lock loser niczego nie uploaduje. 409, wiele legacy artifacts albo niezgodne lineage wymagają działania operatora. Pełny Workflow B orchestrator jest zaimplementowany; proponowane unity 06:00/20:00 Europe/Warsaw są wersjonowane i walidowane, ale pozostają niezainstalowane.

Normalny Stage 1 zwraca `Stage1BatchResult`: pusta skrzynka, wszystkie wiadomości zdeduplikowane, expected `cancelEmail`/allowlist skips i brak wspieranych załączników są odrębnymi poprawnymi outcomes. Granica transakcji ingestu to jedna wiadomość IMAP: retryable fetch/download/persistence failure wycofuje `imap_message`, powiązane `raw_file` oraz nowo utworzone lokalne pliki wyłącznie tej wiadomości; wcześniejsze wiadomości pozostają committed. Download używa `.part`, dwóch prób, limitu rozmiaru i walidacji `Content-Length`, a finalną RAW ścieżkę publikuje atomowo dopiero po walidacji. Batch może zakończyć się `Stage1BatchError` z partial result i `retryable_work_remains=true`, jednocześnie udostępniając Stage 2 już committed sukcesy przez `downstream_stage2_work_may_exist`.

Artifact upload następuje po per-message commicie, ponieważ API weryfikuje widoczny `raw_file_id`. Nie jest to cichy sukces: upload failure ma typed retryable artifact outcome, trwały lokalny source pozostaje do reconciliation, a deterministyczny idempotency/object key naprawia również przypadek zapisu obiektu przed utratą metadata/response. Failure log zawiera IMAP UID, opaque message identity, nullable client/report/raw IDs, download host, exception type i sanityzowany detail, expected/received bytes, retry attempt, retryable classification, transaction scope i cleanup result; query stringi i sekrety nie są logowane.

Stage 1 akceptuje też załączniki `.xlsm`. Dla Alpha GPS plik wysyłany z VBA z `owner@example.invalid` jest zapisany jako raw artifact, a wszystkie niepuste arkusze workbooka są normalizowane do jednego CSV bez wykonywania makr (`openpyxl`, read-only/data-only). Domyślnie Stage 1 nie filtruje po odbiorcy, nadawcy ani temacie: po `SELECT <IMAP_MAILBOX>` szuka wiadomości przez `SINCE <date>`. Host może jawnie zawęzić fetch przez `IMAP_SENDER_FILTERS`, ale zmienna powinna pozostać pusta dla standardowego Workflow B.

## 4. Workflow B — Stage 2 run manualny (backup)

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage2.job_stage2 '{}'
```

Stage 2 wybiera eligible canonical CSV według trwałego stanu `ingest.raw_file`; nie skanuje domyślnie całego katalogu. Opcjonalnie: `'{"limit":20}'`, `'{"debug_detection":true}'`, `'{"raw_file_ids":["<uuid>"]}'`. `input_files`/`input_dir` są diagnostyczne i muszą jednoznacznie mapować się do persisted identity. Przed wdrożeniem kodu zastosuj migrację `048_workflow_b_stage2_batch_contract.sql`; nie wykonuje ona historycznego backfillu.

Completed `OK` z poprawnym cleaned artifactem jest pomijany. Session advisory lock serializuje przetwarzanie per raw-file; przegrany worker niczego nie uploaduje. Retry po przerwaniu używa scope `workflow_b.stage2.cleaned.v1`: zgodne bajty zwracają `reused`, a HTTP 409 oznacza niezgodny kontrakt i wymaga działania operatora, nie nowego losowego klucza. Historyczne `PENDING_REVIEW/stage2_exception` może być retryowane automatycznie; pozostałe niejednoznaczne historyczne reasons wymagają przeglądu. Parent orchestrator istnieje, ale jego produkcyjny harmonogram 06:00/20:00 Europe/Warsaw nie jest zainstalowany ani włączony.

Manualny audit historycznych direct links (domyślnie dry-run, bez platform runa):

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_stage2_cleaned_artifact_links.py \
  --plan /tmp/workflow_b_stage2_cleaned_links.json
```

Plan jest `0600`, deterministyczny i zawiera digest. Kandydat musi mieć `status=NORMALIZED`, `stage2_status=OK` i pusty FK; dokładnie jeden zgodny persisted cleaned artifact oraz niezmieniony obiekt MinIO są wymagane. Nie wolno wybierać po filename, czasie ani object key. Przyszły execute wymaga wcześniejszego skoordynowanego backupu Postgres+MinIO, osobno przejrzanego planu, dokładnego digest/count oraz braku aktywnego Workflow B. Dedykowany lock `workflow_b.stage2.historical_cleaned_link_reconciliation.v1` i jedna transakcja `FOR UPDATE` zapewniają all-or-nothing. Po execute operator ponownie sprawdza candidate count, FK/status fingerprint, artifact fingerprint i MinIO fingerprint. Narzędzie nie ma timera, nie zmienia statusów ani artefaktów, a orchestrator go nie wywołuje.

Weryfikacja Stage 2 w DB: `stage2_status`, `stage2_report_type` w `ingest.raw_file`. Testy manualne: `ops/tests_manual/` (normalizacja CSV, fetch_reports, stage2).

Finalizacja Stage 2 może wykrywać `client_code` i dodawać `record_id` do cleaned CSV. Operator konfiguruje to per typ raportu:

```sql
UPDATE workflow_b_control.report_type_registry
   SET id_sync_column_name = 'Nr rejestracyjny',
       record_id_ingredients = 'Data i czas,Nr rejestracyjny,Lokalizacja'
 WHERE report_type = 'report_207';
```

`id_sync_column_name` musi być dokładną nazwą kolumny z finalnego cleaned report. Stage 2 porównuje jej niepuste wartości z `registration`, `chassis_number` i `driver_name` w `client_trips` wszystkich włączonych baz klientów Workflow A. Dokładnie jeden dopasowany `client_code` jest zapisywany w `ingest.raw_file.client_code`; brak dopasowania zostawia `NULL`, a więcej niż jeden dopasowany klient kończy przetwarzanie pliku błędem. `record_id_ingredients` to przecinkowa lista kolumn cleaned report; z ich wartości Stage 2 generuje końcową kolumnę `record_id`.

Inspekcja rejestru typów raportów Workflow B:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/workflow_b_report_status.py --limit 10
```

Skrypt używa standardowych `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` i czyta tylko platformową bazę. Sekcje raportu:

- `Registry Summary` — aktualne wiersze `workflow_b_control.report_type_registry`: typ raportu, nazwa, `enabled`, status implementacji i moduł cleanera.
- `Stage 2 Status By Report Type` — liczba plików z metadanymi `stage2_*` według `stage2_report_type` i `stage2_status`.
- `Registered Report Types With Zero Successful Stage 2 Files` — zarejestrowane typy, dla których nie ma jeszcze pliku ze `stage2_status='OK'`.
- `Recent Pending/Failed Stage 2 Files` — ostatnie pliki wymagające uwagi: `PENDING_REVIEW`, przyszłe nie-OK statusy Stage 2 oraz raw files ze statusem `FAILED`.

Równoważne podstawowe zapytania operatorskie:

```sql
SELECT report_type, display_name, enabled, implementation_status,
       detection_rules_schema_version, multi_table, cleaner_module, cleaner_entrypoint
FROM workflow_b_control.report_type_registry
ORDER BY priority, report_type;
```

```sql
SELECT report_type, detection_rules_schema_version, detection_rules, column_types
FROM workflow_b_control.report_type_registry
WHERE enabled = true
ORDER BY report_type;
```

```sql
SELECT report_type, id_sync_column_name, record_id_ingredients
FROM workflow_b_control.report_type_registry
WHERE enabled = true
ORDER BY report_type;
```

Pola `detection_rules_schema_version`, `detection_rules`, `column_types`, `multi_table` i `cleaner_entrypoint` są kontraktem Phase 1 pod przyszłą DB-driven detekcję. Stage 2 nadal wykonuje detekcję i cleaning według Pythonowego `jobs.reports.stage2.registry`; runtime używa z DB tylko `id_sync_column_name` i `record_id_ingredients` do finalizacji cleaned report.

`report_207` jest obsługiwany przez Stage 2 jako Pythonowy typ raportu dla „207 Raport przekroczeń limitów prędkości drogowej”; odpowiadający wiersz DB służy operatorom do inspekcji i nie uruchamia DB-driven detekcji. Stage 3 może ładować finalized cleaned report tego typu do tabeli `telematics_reports.report_207` w bazie klienta, jeśli Stage 2 zapisał niepusty `client_code`.

`Alpha_GPS_Baza_LOG` jest obsługiwany przez Stage 2 jako Pythonowy typ raportu dla Alpha GPS XLSM. Detekcja szuka nagłówka `ID`, `Nr rejestracyjny`, `Data przydziału`, `RFID`, `PRYW`, `EDYS`, `OPTIMA`, `OTK`. Cleaning startuje od wiersza `ID`, `Nr rejestracyjny`, `Data przydziału`, `Nazwa Pliku csv`, kończy przed wierszem `ID`, `Data przydziału`, `PRYW stary`, `PRYW aktualny`, usuwa puste wiersze i zostawia tylko cztery kolumny outputu. Typ ma domyślny `client_code=ALPHA00001`, gdy ogólna finalizacja nie rozwiąże klienta.

## 4.1 Workflow B — Stage 3 run manualny (backup)

Przed uruchomieniem Stage 3 zastosuj migracje platformy, w szczególności `029_workflow_b_stage3_load.sql`, oraz adminową client-business migrację `042_workflow_b_stage3_runtime_schema.sql`, i upewnij się, że Stage 2 dla docelowych plików zakończył się `stage2_status='OK'`, zapisał niepusty `client_code` oraz utworzył cleaned artifact.

Dla `Alpha_GPS_Baza_LOG` wymagane są też `030_workflow_b_alpha_gps_baza_log_registry.sql` i naprawcza `031_workflow_b_ensure_alpha_gps_baza_log_registry.sql` w bazie platformowej oraz `023_alpha_gps_baza_log_workflow_b.sql` w bazie `alpha_main`.

Uruchomienie wszystkich pending plików:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{}'
```

Uruchomienie z limitem albo dla jednego pliku:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"limit":10}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"raw_file_id":"<uuid>"}'
```

Bezpieczna walidacja przed pierwszym produkcyjnym loadem:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"raw_file_id":"<uuid>","dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"limit":10,"dry_run":true}'
```

`dry_run=true` używa tej samej selekcji kandydatów i pobiera ten sam cleaned artifact Stage 2, ale nie tworzy schematu/tabel/kolumn/indeksów, nie insertuje, nie aktualizuje, nie kasuje ani nie czyści danych w bazie klienta i domyślnie nie zmienia `ingest.raw_file.stage3_status`. Walidacja sprawdza dostęp do artifactu, parsowanie CSV, bezpieczną nazwę tabeli, semantykę `record_id`, politykę `data_overwrite`, stan docelowego schematu/tabeli, kolumny do utworzenia/dodania, istniejący indeks unikalny i duplikaty istniejących `record_id`. Wynik jest wypisywany na stdout i widoczny w logach runa jako JSON-like context z licznikami `would_insert_rows`, `would_update_rows`, `would_skip_rows`, `would_reject_rows`, flagą `would_replace_table`, `warnings`, `errors` i `dry_run_status`.

Jeżeli operator chce zapisać wynik walidacji jako artifact platformowy, może dodać `persist_dry_run_result=true`. To nadal nie zapisuje danych biznesowych do bazy klienta, ale tworzy artifact `dry_run_result` w platformie.

Stage 3 zwraca `Stage3BatchResult`; dry-run outcomes są oddzielne od production `LOADED` i nigdy nie pojawiają się w `successful_load_identities`. Błędy per plik są akumulowane bez cofania innych udanych client-DB transactions, a końcowy `Stage3BatchError` udostępnia partial result. Environment identity, permission i global selection/connection failures są hard failures, nie no-work.

#### Local/dev single-file replay

Do lokalnej walidacji pojedynczego pliku bez IMAP, maili, schedulerów ani zewnętrznych wywołań sieciowych użyj `ops/checks/replay_workflow_b_file_to_stage3.py`. Skrypt jest narzędziem local/dev: Stage 1 normalizuje wskazany plik przez tę samą ścieżkę `_convert_to_canonical_csv`, Stage 2 używa normalnego Python registry/detektora/cleanera i konfiguracji `record_id_ingredients`, a opcjonalny Stage 3 load używa istniejącej logiki tabel docelowych i duplikatów `record_id`. Skrypt nie tworzy wpisów `ingest.raw_file` ani artifactów platformowych; służy do bezpiecznego odtworzenia lokalnej próbki w bazie klienta.

Dry-run bez zapisu do bazy klienta:

```bash
PYTHONPATH="$PWD" python3 ops/checks/replay_workflow_b_file_to_stage3.py \
  --client-code ALPHA00001 \
  --file "<local-report.xlsx>" \
  --report-type report_d105_2_ecodriving \
  --dry-run
```

Load Stage 3 jest dodatkowo chroniony połączonym guardem local/dev: jawna deklaracja runtime, zgodne markery platformy i bazy klienta, lokalne hosty oraz `WORKFLOW_B_LOCAL_REPLAY_ALLOW=1`. Brak lub mismatch tożsamości kończy komendę przed loadem:

```bash
WORKFLOW_B_LOCAL_REPLAY_ALLOW=1 PYTHONPATH="$PWD" \
  python3 ops/checks/replay_workflow_b_file_to_stage3.py \
  --client-code ALPHA00001 \
  --file "<local-report.xlsx>" \
  --report-type report_d105_2_ecodriving \
  --load-stage3
```

Przy `data_overwrite=false` istniejące `record_id` w tabeli docelowej są pomijane przez tę samą politykę co standardowy Stage 3. Jeśli `--report-type` jest podany i detekcja strukturalna zwróci inny typ, skrypt kończy się błędem przed Stage 3.

#### Uprawnienia Stage 3 w bazach klientów

Stage 3 wykonuje właściwy import zawsze jako `client_db_user` z `workflow_a_control.client_account`; nie uruchamia importu jako `POSTGRES_USER` i nie wykonuje recurring DDL/grantów. Konto admina PostgreSQL z `.env` (`POSTGRES_USER` / `POSTGRES_PASSWORD`) jest używane wyłącznie poza jobem, przez kontrolowaną ścieżkę migracji/grantów:

- rola współdzielona: `workflow_b_stage3_loader`,
- membership: `GRANT workflow_b_stage3_loader TO <client_db_user>`,
- dostęp do bazy: `GRANT CONNECT ON DATABASE <client_db_name> TO workflow_b_stage3_loader`,
- dostęp do przygotowanego schematu: `USAGE`, bez `CREATE`,
- tabele registered `telematics_reports` report targets: `SELECT, INSERT, UPDATE, DELETE` (`DELETE` jest wymagany przez istniejący tryb replace-all),
- `public.client_trips` dla Report 207: `SELECT, UPDATE`; brak wymagań do sekwencji.

PostgreSQL nie ma prostego natywnego mechanizmu `GRANT ON ALL FUTURE DATABASES`, dlatego dla istniejących klientów użyj skryptu operatorskiego, a dla nowych klientów robi to automatycznie onboarding Workflow A.

Wypisanie SQL dla wszystkich obecnych, włączonych klientów (bez zmian):

```bash
PYTHONPATH="$PWD" python3 ops/grant_workflow_b_stage3_permissions.py
```

Zastosowanie grantów dla wszystkich obecnych, włączonych klientów:

```bash
PYTHONPATH="$PWD" python3 ops/grant_workflow_b_stage3_permissions.py --apply
```

Zastosowanie grantów dla jednego klienta:

```bash
PYTHONPATH="$PWD" python3 ops/grant_workflow_b_stage3_permissions.py --client-code ALPHA00001 --apply
```

`--grant-existing-schema` może uzupełnić DML dla istniejącego schematu, ale przygotowanie kolumn i indeksów wykonuje wyłącznie migracja 042. Parametr joba `auto_grant_permissions=true` jest wycofany z runtime: Stage 3 i Report 207 odrzucają go deterministycznie jako non-retryable/operator-action-required.

Przed wdrożeniem kodu uruchom najpierw kontrolowaną migrację jako właściciel/admin (poniższe komendy są domyślnie dry-run/list i nie uruchamiają Workflow B):

```bash
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --list
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --client-name <CLIENT_NAME> --apply
```

Migracja `042_workflow_b_stage3_runtime_schema.sql` jest idempotentna: uzupełnia techniczne kolumny istniejących, zarejestrowanych tabel `telematics_reports`, tracking Report 207, liczniki `client_trips`, indeksy `record_id` i minimalne granty. Nie tworzy dynamicznej tabeli raportu ani raportowych kolumn biznesowych, gdy ich nie ma; taki target wymaga osobnej, kontrolowanej migracji przed loadem. Wykryte istniejące duplikaty `record_id` zatrzymują migrację zamiast usuwać lub przepisywać dane.

Migracja odbiera `workflow_b_stage3_loader` database/schema `CREATE`. Oddzielny legacy postprocessor D105.2 nadal zawiera opcjonalny runtime bootstrap i runtime DDL; przed migracją 042 przygotuj jego wymagane kolumny poza jobem albo pozostaw ten selector wyłączony. W przeciwnym razie brakujący obiekt zakończy D105.2 błędem zamiast zostać utworzony przez odziedziczone `CREATE`.

Po apply zweryfikuj read-only jako admin i jako skonfigurowany runtime role:

```sql
SELECT n.nspname, c.relname, owner.rolname AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles owner ON owner.oid = c.relowner
WHERE (n.nspname, c.relname) IN (('telematics_reports','report_207'),('public','client_trips'));

SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname='telematics_reports' AND tablename='report_207';

SELECT current_user,
       has_table_privilege(current_user, 'telematics_reports.report_207', 'SELECT,INSERT,UPDATE,DELETE') AS stage3_dml,
       has_table_privilege(current_user, 'public.client_trips', 'SELECT,UPDATE') AS report_207_postprocess_dml;
```

Kolejność deploymentu: backup klienta → review/list migracji → apply migracji 042 jako migration/admin owner → weryfikacja obiektów/grantów → deploy kodu → read-only Stage 3 dry-run. Rollback kodu nie wymaga usuwania addytywnych kolumn ani indeksów. Nie wykonuj automatycznego down migration; w razie rollbacku pozostaw przygotowany schemat i cofnij wyłącznie kod. Powrót do starego runtime-DDL wymagałby jawnego, osobno zatwierdzonego przywrócenia `CREATE` i nie jest zalecanym rollbackiem.

Stage 3 wybiera tylko wiersze `ingest.raw_file`, gdzie:

- `stage2_status = 'OK'`,
- `client_code` nie jest `NULL` ani pusty po trimowaniu,
- `stage2_report_type` nie jest `NULL` ani pusty po trimowaniu,
- istnieje pasujący cleaned artifact Stage 2 (`workflow_b` / `stage_2_clean` / `cleaned`, z literalnym albo zsanityzowanym `report_type`),
- `stage3_status` jest `NULL` albo pusty, albo wcześniejszy `OK` jest starszy niż bieżący output Stage 2 (`stage2_updated_at > stage3_finished_at`).

Domyślnie nie przetwarza ponownie tego samego outputu Stage 2 z `stage3_status='OK'`, `ERROR` albo `SKIPPED_NO_RECORD_ID`. Jeżeli Stage 2 zostanie uruchomiony ponownie dla tego samego `raw_file_id` i utworzy nowszy cleaned output, batch może ponownie wybrać ten wiersz. Targeted retry może użyć `force_reprocess=true` razem z `raw_file_id`, ale operator powinien najpierw rozumieć aktualny stan tabeli docelowej.

`force_reprocess` omija tylko completion/status eligibility dla jawnego `raw_file_id`; nie omija Stage 2 `OK` ani client/report routing. Idempotency zależy dalej od `record_id` i `data_overwrite`: skip/upsert chronią rekordowe targety, natomiast replace-all targety świadomie zastępują dane. Nie używaj force jako domyślnego parametru automatyzacji.

Parent job `jobs.reports.workflow_b.orchestrator` sekwencjonuje Stage 1/2/3 i konfiguracją wybiera bounded Report 207 postprocessing w jednym platformowym runie. Globalny advisory lock zapobiega równoległemu kompletnemu przebiegowi. Scheduled profile odrzuca force, path diagnostics, Stage 3 dry-run, raw-file filters i parametrowe selector overrides; selector pochodzi wyłącznie z control-plane policy. Typed partial stage failures pozwalają drenować bezpieczny persisted backlog, a unexpected failures zatrzymują downstream. Wszystkie 93 historyczne Stage 2 direct links są już zrekoncyliowane. Produkcyjny timer nadal nie jest zainstalowany ani włączony; docelowe godziny pozostają 06:00 i 20:00 Europe/Warsaw.

Konfiguracja polityki overwrite:

```sql
INSERT INTO workflow_b_control.report_type_client_load_policy
    (client_code, report_type, data_overwrite)
VALUES
    ('DELTA00001', 'report_207', false)
ON CONFLICT (client_code, report_type)
DO UPDATE SET data_overwrite = EXCLUDED.data_overwrite;
```

Brak wiersza policy oznacza `data_overwrite=false`. Stage 3 nie dopisuje policy automatycznie. Po udanym production loadzie parent wymaga jednak dokładnego wiersza policy do bezpiecznej selekcji postprocessora.

Po wdrożeniu migracji 050 nullable `trip_metrics_population_source_override` ma vocabulary `api_migration|report_207_migration|d105_2_ecodriving_migration|disabled`. `NULL`/pominięta kolumna dziedziczy `client_account.trip_metrics_population_source`; nie zmienia to Workflow A, routingu Stage 3 ani `data_overwrite`. Resolution jest override-first i raportuje origin. `disabled` zachowuje Stage 3, ale nie tworzy planu. `api_migration` oznacza brak Workflow B planu i nigdy nie uruchamia Workflow A. Report 207 wymaga `report_207_migration`; D105.2 jest rozpoznany, ale parent-unsupported; pozostałe niezgodności fail-closed.

Efekt w bazie klienta:

- schemat docelowy: `telematics_reports`,
- tabela docelowa: kanoniczny `stage2_report_type`, np. `report_207`,
- kolumny z cleaned report są zapisywane jako `TEXT`,
- techniczne kolumny zaczynają się od `_`: `_loaded_at`, `_raw_file_id`, `_source_artifact_id`, `_source_filename`, `_stage3_run_id`.

Jeżeli cleaned report ma użyteczne `record_id`, Stage 3 wymaga utworzonego przez migrację unikalnego indeksu dla niepustego `record_id`. Brak tabeli, kolumny albo indeksu daje typed `FAILED_SCHEMA_NOT_READY` (non-retryable, operator action required) i nie uruchamia DDL. Przy `data_overwrite=false` istniejące `record_id` są pomijane; przy `data_overwrite=true` są nadpisywane. Jeżeli `record_id` jest całkowicie pusty, `data_overwrite=false` kończy plik statusem `SKIPPED_NO_RECORD_ID`, a `data_overwrite=true` transakcyjnie zastępuje zawartość tabeli docelowej.

Dla `Alpha_GPS_Baza_LOG` Stage 3 używa specjalnego targetu `telematics_reports."Alpha_GPS_Baza_LOG"` zamiast generycznego `telematics_reports.<report_type>`. Load jest zawsze replace-all i transakcyjny: walidacja cleaned CSV, `DELETE FROM telematics_reports."Alpha_GPS_Baza_LOG"`, insert wszystkich wierszy i commit dopiero po sukcesie wszystkich insertów. Błąd walidacji lub insertu robi rollback i zostawia poprzedni snapshot tabeli.

Weryfikacja:

```sql
SELECT id, stage3_status, stage3_error,
       stage3_inserted_rows, stage3_updated_rows, stage3_skipped_rows,
       stage3_destination_schema, stage3_destination_table, stage3_data_overwrite
FROM ingest.raw_file
WHERE stage2_status = 'OK'
ORDER BY stage3_finished_at DESC NULLS LAST
LIMIT 20;
```

**Rozwój Workflow B jest wstrzymany** — powyższe pozostaje dla utrzymania i fallbacku.

### 4.2 Workflow B — `report_207` speeding post-processing (backup)

Po załadowaniu `report_207` przez Stage 3 do `telematics_reports.report_207` można uruchomić osobny job, który przenosi zdarzenia przekroczenia prędkości do zagregowanych liczników w `public.client_trips`.

Bezpieczny dry-run dla wszystkich włączonych klientów:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"dry_run":true}'
```

Dry-run dla jednego klienta i maksymalnie 100 kandydackich wierszy:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"client_code":"ALPHA00001","limit":100,"dry_run":true}'
```

Realne przetworzenie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"client_code":"ALPHA00001"}'
```

Przed uruchomieniem realnego przetwarzania ustaw klientowi `workflow_a_control.client_account.trip_metrics_population_source='report_207_migration'`. Jeżeli wartość jest inna (`api_migration`, `d105_2_ecodriving_migration` albo `disabled`), job pomija klienta przed połączeniem do bazy klienta i DML oraz raportuje `skip_reason=trip_metrics_population_source_mismatch`.

Job:

- pomija klienta, jeżeli `trip_metrics_population_source` nie jest `report_207_migration`,
- wymaga istniejących `telematics_reports.report_207`, kolumn trackingowych, liczników `public.client_trips` i indeksu `report_207__record_id_uidx`,
- przy brakującym obiekcie zwraca typed schema-readiness failure ze wskazaniem migracji 042; nie wykonuje `ALTER TABLE`, `CREATE INDEX` ani grantów,
- w dry-runie raportuje brakujące kolumny, planowane dopasowania i przyrosty, ale nie wykonuje DDL/DML.

Reguła dopasowania jest celowo ścisła:

```sql
report_207."Nr rejestracyjny" = client_trips.registration
AND (report_207."Data i czas"::timestamp AT TIME ZONE 'Europe/Warsaw') >= client_trips.start_timestamp
AND (report_207."Data i czas"::timestamp AT TIME ZONE 'Europe/Warsaw') <= client_trips.end_timestamp
```

`report_207."Data i czas"` is a FleetWeb local Polish timestamp without timezone. Do not compare it as UTC and do not add fixed offsets; use the IANA timezone `Europe/Warsaw` so DST is handled correctly.

Dopasowanie musi wskazać dokładnie jeden trip. Brak dopasowania zapisuje `NO_MATCHING_TRIP`, wiele dopasowań zapisuje `AMBIGUOUS_TRIP_MATCH`, a błędna prędkość, data albo rejestracja zapisuje odpowiednio `INVALID_SPEED`, `INVALID_TIMESTAMP` albo `INVALID_REGISTRATION`. Wiersze z błędem pozostają niemigrowane i domyślnie nie są ponawiane; retry wymaga `force_retry_errors=true`.

Buckety:

- `Prędkość > 140 AND Prędkość <= 160` → `client_trips.speeding_140_160_count`
- `Prędkość > 160 AND Prędkość <= 170` → `client_trips.speeding_160_170_count`
- `Prędkość > 170` → `client_trips.speeding_170_plus_count`

Weryfikacja w bazie klienta:

```sql
SELECT migrated_to_client_db, migrated_to_client_db_error, count(*)
FROM telematics_reports.report_207
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT registration, start_timestamp, end_timestamp,
       speeding_140_160_count, speeding_160_170_count, speeding_170_plus_count
FROM public.client_trips
WHERE speeding_140_160_count > 0
   OR speeding_160_170_count > 0
   OR speeding_170_plus_count > 0
ORDER BY start_timestamp DESC
LIMIT 20;
```


### 4.2.1 Workflow B — manual recovery after Report 207 identity fix (backup)

Use `ops/recover_report_207_speed_violations.py` only for reviewed recovery of Report 207 data loaded before commit `ae53716d89e2fdd9d57352a3c8217c30c5ab4ea6`. Do not run it as a scheduled job. Do not use it to delete base `client_trips` rows.

Dry-run example for a reviewed June scope:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/recover_report_207_speed_violations.py \
  --client-code BRAVO00016 \
  --date-from 2026-06-01 \
  --date-to 2026-06-30 \
  --raw-file-id 112b1b92-0000-0000-0000-000000000000 \
  --require-env-name production
```

If the platform artifact download path is unavailable but a reviewed cleaned CSV is present locally, pass it with explicit lineage:

```bash
PYTHONPATH="$PWD" python3 ops/recover_report_207_speed_violations.py \
  --client-code BRAVO00016 \
  --date-from 2026-06-01 \
  --date-to 2026-06-30 \
  --cleaned-csv 112b1b92-0000-0000-0000-000000000000=/path/to/report_207_cleaned.csv \
  --require-env-name production
```

Before any future execute run, take and verify backups for both the platform DB/MinIO and the target client business DB. The script prints the exact required confirmation tokens. Execute mode also requires environment identity attestation and, in production, the repository's production write confirmation. Example shape, not to be run until the dry-run and backups are reviewed:

```bash
# NOT RUN: future execute shape only
PYTHONPATH="$PWD" python3 ops/recover_report_207_speed_violations.py \
  --client-code BRAVO00016 \
  --date-from 2026-06-01 \
  --date-to 2026-06-30 \
  --raw-file-id 112b1b92-0000-0000-0000-000000000000 \
  --require-env-name production \
  --backup-confirmation-token BACKUP_CONFIRMED:BRAVO00016:2026-06-01:2026-06-30 \
  --counter-recalc-confirmation-token CLIENT_TRIPS_DATE_RECALC:BRAVO00016:2026-06-01:2026-06-30 \
  --production-write-confirmation '<exact token from environment_identity helper>' \
  --execute
```

The `telematics_reports.report_207` cleanup is scoped by raw/source/date where those identifiers are provided. `public.client_trips` has only aggregate speeding counters (`speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`) and no raw/source lineage, so the recovery clears and recalculates those counters for the requested client/date scope. Base trip records are not deleted.

### 4.3 Workflow B — D105.2 EcoDriving trip metrics post-processing (backup)

Ten flow dotyczy jednego specjalnego wariantu raportu D105.2, który zawiera metryki EcoDriving/event. Nie traktuj go jako dwóch raportów i nie opieraj detekcji na nazwie pliku ani tytule. Stage 2 rozpoznaje go strukturalnie po nagłówku z kolumnami: `Nr Rejestracyjny`, `Czas rozpoczęcia`, `Czas zakończenia`, `przekroczenia obr/min`, `> 140kmh`, `> 160kmh`, `> 170kmh`. Stage 3 ładuje cleaned CSV do `telematics_reports.report_d105_2_ecodriving`.

Bezpieczny dry-run migracji metryk:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration '{"dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration '{"client_code":"ALPHA00001","limit":100,"dry_run":true}'
```

Read-only validator przed write-mode:

```bash
PYTHONPATH="$PWD" python3 ops/checks/check_d105_2_ecodriving_trip_metrics.py --json
PYTHONPATH="$PWD" python3 ops/checks/check_d105_2_ecodriving_trip_metrics.py --client-code ALPHA00001 --strict
```

Przed realnym runem ustaw klientowi:

```sql
UPDATE workflow_a_control.client_account
SET trip_metrics_population_source = 'd105_2_ecodriving_migration'
WHERE client_code = 'ALPHA00001';
```

Przy innym selectorze (`api_migration`, `report_207_migration`, `disabled`) job pomija klienta przed auto-grantem, połączeniem do bazy klienta oraz jakimkolwiek DDL/DML i raportuje `skip_reason=trip_metrics_population_source_mismatch`.

Realny run:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration '{"client_code":"ALPHA00001"}'
```

Mapping jest bezpośredni i niekumulacyjny: `> 140kmh` → `speeding_140_160_count`, `> 160kmh` → `speeding_160_170_count`, `> 170kmh` → `speeding_170_plus_count`, `przekroczenia obr/min` → `overrev_events_count`. Job nie pisze `high_rpm_events_count`, bo Workflow A API rozróżnia HIGH_RPM i OVERREV jako osobne provider-labeled event metrics. Puste komórki metryk są błędem `INVALID_METRIC_COUNTS`, nie zerem.

Matching wymaga dokładnie jednego tripu po trimowanej rejestracji oraz równych timestampach start/end po parsowaniu raportu jako `Europe/Warsaw`. Nie ma fuzzy matchingu ani tolerancji; brak dopasowania zapisuje `NO_MATCHING_TRIP`, wiele dopasowań `AMBIGUOUS_TRIP_MATCH`, a błędy wejścia `INVALID_REGISTRATION`, `INVALID_TIMESTAMP`, `INVALID_METRIC_COUNTS`. Wiersze z `NO_MATCHING_TRIP` są retryable domyślnie, pozostałe błędy wymagają `force_retry_errors=true`. W pełni zerowe rows są oznaczane jako migrated bez inkrementowania tripu. Job nie wykonuje historycznego reconciliation istniejących liczników.

### 4.4 Workflow B — ALPHA00001 `Dysponent_ID` enrichment (backup)

`jobs.reports.workflow_b.orchestrator` uruchamia enrichment automatycznie tylko po udanym production Stage 3 loadzie dokładnego `(ALPHA00001, Alpha_GPS_Baza_LOG, telematics_reports.Alpha_GPS_Baza_LOG)`. Wpis jest statyczny w `jobs/reports/workflow_b/postprocessor_registry.py`; wartości z DB nie mogą wskazać dowolnego modułu. Trigger następuje po commicie source replace-all. Zakres zaczyna się w Warsaw-local dniu poprzedniego udanego loadu o innej dacie, a kończy na początku dnia bieżącego loadu (end exclusive). Enrichment ma oddzielne transakcje, więc jego failure nie rollbackuje source ingest/load; parent run pokazuje typed postprocessor failure.

### Kontrolowane odświeżenie źródła ALPHA do przeglądu backfillu

Scheduled Workflow B zachowuje postprocessor mode `execute`; schedule row i zwykłe parametry orchestratora nie przyjmują override `dry_run`. Manualny wyjątek jest zawężony do `ops/refresh_alpha00001_source_for_backfill.py`. Plan domyślny waliduje skonfigurowaną tożsamość bez połączenia IMAP i bez DML:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/refresh_alpha00001_source_for_backfill.py
```

Po wdrożeniu i ponownej read-only weryfikacji hosta, clean `main`, markerów DB, braku migracji oraz locka Workflow B, osobno zatwierdzone odświeżenie uruchamia się dokładnie tak:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/refresh_alpha00001_source_for_backfill.py \
  --execute-source-refresh \
  --attestation 'ALPHA00001/Alpha_GPS_Baza_LOG/telematics_reports.Alpha_GPS_Baza_LOG/postprocessor=dry_run'
```

`--execute-source-refresh` może użyć istniejącej ścieżki IMAP/normalizacji współdzielonego mailboxa, lecz przed Stage 2 wybiera wyłącznie raw IDs z tego runu o statycznym `report_key=gps_baza_start_skrypt`; po detekcji Stage 3 wymaga dokładnego ALPHA reportu i destination. Stage 3 source replace-all commit jest niezależną granicą transakcji. Dopiero z committed `successful_load_identity` powstaje jedyny statyczny ALPHA plan `dry_run`; Report 207 nie dziedziczy tego trybu. Wynik zawsze raportuje raw file ID, Workflow B run ID, cleaned artifact ID, destination, source rows/load time/max assignment date/age/overlap, enrichment metrics i `target_rows_modified`. Wymagane jest `target_rows_modified = 0`. Failure readiness nie cofa committed source i jest raportowany jako partial outcome; normalny postprocessor `execute` pozostaje outstanding. `NO_NEWER_SOURCE_AVAILABLE` i `ALREADY_LOADED` są typed idempotent outcomes; wcześniejszy failed refresh po committed Stage 3 pozwala ponowić wyłącznie dry-run. To polecenie nie uruchamia `ops/backfill_alpha00001_dysponent_id.py`, nie zmienia schedule i nie może przyjąć innego klienta/reportu/tabeli.

Powyższe polecenie wykonujące source refresh jest procedurą przyszłą — nie uruchamiaj go w etapie implementacyjnym ani bez osobnej zgody produkcyjnej. Backfill nadal wymaga własnego dry-runu i osobnej zgody na `--execute`.

Job atestuje platformę i `alpha_main`, wymaga jednego source `raw_file_id` / Workflow B run / cleaned artifact, sprawdza freshness (default 36 h), a przed każdym write batchem ponownie dowodzi tej samej source identity. Matching normalizuje rejestrację (trim, upper, usunięcie whitespace) i wybiera najnowszą `assignment_date <= Warsaw-local trip date`. Konkurencyjne różne ID na tej samej najnowszej dacie są pomijane jako ambiguity. Default nie nadpisuje niepustych `Dysponent_ID`; jawne `--overwrite-existing` jest ścieżką korekty. Legacy `force=true` jest blokowane.

Readiness obejmuje: non-private trips, `Driver_Restrictions`, potrzebę fallbacku, istniejące i planowane `Dysponent_ID`, brak assignmentu, ambiguity, konflikty, active driver-chart resolution, unmatched IDs oraz trip/distance coverage. Default minimum to 95% dla obu coverage (historyczny zdrowy okres przekraczał ten poziom); default ambiguity allowance to 25 i oba ustawienia są jawnie konfigurowalne. `sent`/email nie są częścią tego joba. Żaden Eco Driving generation ani send schedule nie może zostać włączony bez osobnej decyzji readiness.

#### Kontrolowany backfill od 2026-07-21

Default jest read-only. `--end-date` jest exclusive; brak endu rozwiązuje granicę jako day-after-latest-trip, a z `--require-fresh-source` jako minimum tej wartości i source-load day. Dry-run pokazuje source raw file/run, resolved period, candidates, planned updates, conflicts, ambiguities, unmatched IDs oraz predicted coverage. Exit codes: `0` ready/success, `2` invalid params, `3` identity failure, `4` readiness failure, `5` runtime/partial failure.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python ops/backfill_alpha00001_dysponent_id.py --client-code ALPHA00001 --start-date 2026-07-21
```

Preferowaną ścieżką pozostaje uruchomienie zatwierdzonego Workflow B source chain przed backfillem, bo tylko świeży snapshot daje aktualne przypisania. Jeżeli ambiguity przekracza jawny limit albo coverage spada poniżej threshold, nie wykonuj backfillu — uzgodnij dane źródłowe/politykę.

Świeżość źródła nie jest domyślnie egzekwowana ani w runie ręcznym, ani w automatycznym: enrichment obejmuje pełny żądany zakres na podstawie ostatniego zatwierdzonego snapshotu przypisań. Przypisania zmienione po tym snapshocie są nieznane i mogą zostać zapisane jako nieaktualne wartości. `--require-fresh-source` (param joba `require_fresh_source=true`) przywraca fail-closed: egzekwuje `--max-source-age-hours` i tnie okno do dnia załadowania snapshotu. Pozostałe bramki (identity load, timestamp z przyszłości, ambiguity, coverage) obowiązują zawsze, a bez `--overwrite-existing` uzupełniane są tylko puste `Dysponent_ID`.

Run na nieaktualnym źródle jest audytowalny bez blokowania: summary zawiera `enriched_beyond_source_boundary`, `source_boundary_exclusive`, `source_loaded_at`, `source_age_hours` i `source_business_date_max`, a job loguje WARNING, gdy okno wychodzi poza dzień załadowania snapshotu. Po odświeżeniu źródła powtórz zakres z `--overwrite-existing`, aby skorygować wartości zapisane ze starego snapshotu.

Przyszły execute, **nieuruchomiony w ramach implementacji**:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python ops/backfill_alpha00001_dysponent_id.py --client-code ALPHA00001 --start-date 2026-07-21 --execute
```

Opcje operacyjne: `--client-id` zamiast code, `--end-date YYYY-MM-DD` (exclusive), `--coverage-threshold`, `--distance-coverage-threshold`, `--max-ambiguities`, `--max-source-age-hours`, `--require-fresh-source`, `--batch-size`, `--max-batches`, `--limit`, `--overwrite-existing`. `--execute` commitowuje tylko udane batche; rerun pomija już poprawne wartości. Przy partial failure wcześniejsze batche pozostają committed i summary/exit code wymagają operator review. Nie ma automatycznego resume cursoru poza idempotentnym rerunem tego samego zakresu.

Dla pojedynczej zatwierdzonej naprawy provider-trip allowlist nadal używaj manual-only `jobs.reports.postprocess.job_alpha00001_dysponent_id_exact_enrichment` z dry-run i `expected_update_count`.

Nie dodano migracji: istniejące `023_alpha_gps_baza_log_workflow_b.sql`, `024_alpha00001_client_trips_dysponent_id.sql`, `025_alpha00001_dysponent_id_batch_indexes.sql` oraz aktualne indeksy pokrywają source provenance i access path. Nie dodano ani nie włączono schedule.

## 5. Workflow A — operacje

Operator uruchamia joby synchronizacji API → baza klienta:

- **Na żądanie**: `ops/runner.py` z modułem joba Workflow A i `params_json`.
- **Cyklicznie**: zaimplementowany `jobs.api.telematics.dispatcher` jako tick uruchamiany timerem hostowym; proponowane unity są w `ops/systemd/proposed/`.

Wyzwolenie **komendą z e‑mail** wymaga integracji poza tym repo (skrypt / reguła na hoście). Repo zawiera proponowane unity dla dispatchera i retention workera, ale operator musi je skopiować/włączyć na hoście.

### 5.1 Workflow A — Client Onboarding (Telematics)

Przed pierwszym uruchomieniem pipeline'u dla nowego klienta należy go onboardować: utworzyć bazę biznesową, zastosować DDL, wstawić rekord control-plane, zainicjalizować domyślne wpisy harmonogramu/retencji i zweryfikować dostęp.

#### Przegląd

Skrypt `scripts/onboard_workflow_a_client.py` jest **narzędziem tworzenia nowego klienta**. Nie jest
narzędziem naprawy, wznowienia ani wycofania klienta. Zanim cokolwiek zapisze — i zanim wykona
jakiekolwiek żądanie do providera — sprawdza, czy cel jest rzeczywistym stanem zerowym, i odmawia,
gdy nim nie jest:

| Kod odmowy | Kiedy |
|---|---|
| `ONBOARDING_REFUSED_CLIENT_EXISTS` | istnieje `client_account` o tej nazwie, kodzie lub nazwie bazy |
| `ONBOARDING_REFUSED_PROGRESS_STATE` | klient istnieje i wyszedł poza utworzenie: coverage, wiersz recovery, historia schedule'a albo platformowy run biznesowy |
| `ONBOARDING_REFUSED_PARTIAL_STATE` | brak `client_account`, ale istnieją wiersze control-plane z tym `client_code` — pozostałość po przerwanej próbie |
| `ONBOARDING_REFUSED_AMBIGUOUS_STATE` | tożsamość celu wskazuje na więcej niż jednego klienta |

Odmowa nie tworzy, nie zmienia i nie usuwa niczego, **nigdy** nie raportuje stanu
`CREATED_DISABLED_STRICT` dla istniejącego klienta i kieruje operatora do read-only diagnozy
(`ops/audit_telematics_cold_start.py`), do osobnej procedury wznowienia (§5.5) albo do kontrolowanego
wycofania. Ręczne kasowanie wierszy control-plane, żeby skrypt „przeszedł dalej”, jest zabronione.

Dla rzeczywiście nowego klienta skrypt wykonuje pełne onboardowanie:

- weryfikacja uwierzytelnienia do API Telematics (preflight)
- utworzenie bazy biznesowej klienta i użytkownika Postgres
- zastosowanie DDL (`020_client_trips_final_schema.sql`, `021_add_trip_mode_to_client_trips.sql`, `024_alpha00001_client_trips_dysponent_id.sql`, `025_alpha00001_dysponent_id_batch_indexes.sql`, `026_add_driver_restrictions_to_client_trips.sql`, `027_eco_driving_schema.sql`, `028_eco_driving_periods_and_driver_chart.sql`, `029_eco_driving_nullable_scores.sql`, `030_eco_driving_trend_views.sql`, `031_eco_driving_validation_fields.sql`, `032_eco_driving_weekly_email_notifications.sql`, `033_eco_driving_weekly_email_send_log_grants.sql`, `034_eco_driving_rating_type_share_percent.sql`, `035_eco_driving_monthly_email_notifications.sql`, `036_eco_driving_round_per_100km_stats.sql`, `037_eco_driving_score_from_rounded_per_100km.sql`, `039_eco_person_driving_schema.sql`, `040_eco_person_runtime_privileges.sql`, `041_eco_person_sent_archive_state.sql`, `043_eco_person_physical_person_identity.sql`, `044_eco_email_fail_closed_idempotency.sql`, `045_environment_identity_promotion_primitive.sql`, `046_eco_ranking_qualified_only.sql`, `012_*.sql`, `013_*.sql`, `014_*.sql`) bez tworzenia starego kształtu `client_trips`
- nadanie uprawnień (GRANT) na tabelach biznesowych
- bootstrap uprawnień `workflow_b_stage3_loader` dla backupowego Workflow B Stage 3 (`CONNECT` bez database/schema `CREATE`; przygotowanie schematu wykonuje migracja adminowa, a import Stage 3 nadal używa `client_db_user`)
- walidacja połączenia z bazą biznesową klienta
- **jedna transakcja control-plane**: wiersz `workflow_a_control.client_account`, dziesięć wierszy
  `client_dataset_schedule` i czternaście `client_table_retention` (wszystkie `enabled=false`,
  łącznie z Eco Driving), następnie **weryfikacja dokładnie tego nowo utworzonego stanu na tej samej
  transakcji, przed commitem**, i dopiero potem jeden commit. Nieudana weryfikacja wycofuje całość:
  zero nowych wierszy, każdy wcześniejszy wiersz bajt w bajt niezmieniony
- wydruk wymaganego następnego kroku maszyny stanów — read-only audytu cold-start
  (`ops/audit_telematics_cold_start.py`). Skrypt **nie** drukuje komendy pierwszego uruchomienia joba
  biznesowego: przy wyłączonym schedule'u taka komenda pomija pracę i i tak kończy się kodem `0`,
  co było źródłem pomyłki `ECHO00001`

Zasoby spoza tej transakcji — baza klienta i rola Postgres — nie mogą do niej należeć (leżą w innej
bazie, a `CREATE DATABASE` nie działa w transakcji), więc są **kompensowane**: skrypt usuwa wyłącznie
te, które sam utworzył w tym wywołaniu. Baza lub rola zastana jako istniejąca nie jest nigdy
usuwana.

#### Wymagania wstępne

- stack platformy uruchomiony (`docker compose up -d`)
- migracje platformy zastosowane (`bash ops/db_migrate.sh`) — w szczególności `008_workflow_a_control_plane.sql`, `010_add_client_code.sql`, `011_workflow_a_dataset_registry.sql`, `012_workflow_a_client_dataset_schedule.sql`, `013_workflow_a_client_table_retention.sql` oraz późniejsze migracje dispatcher/schedule do `046_workflow_a_eco_person_registry.sql`
- dostęp do Postgres z uprawnieniami `CREATE DATABASE` / `CREATE USER` (admin / superuser)
- poświadczenia Telematics API (username + hasło) dla nowego klienta
- środowisko Python z `psycopg`, `requests`, `PyYAML` (`.venv`)

#### Zmienne środowiskowe

Platforma (muszą być ustawione lub w `.env`):

- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `LOG_API_URL`, `API_READ_TOKEN`, `API_WRITE_TOKEN`

Per-klient (konwencja nazewnictwa oparta o `client_name` z YAML, zamienione na UPPER):

| Zmienna | Znaczenie |
|---------|-----------|
| `<CLIENT_NAME>_API_USERNAME` | Username do Telematics API (Basic Auth) |
| `<CLIENT_NAME>_API_KEY` | Hasło do Telematics API (Basic Auth) |
| `<CLIENT_NAME>_DB_USERNAME` | Użytkownik Postgres bazy biznesowej klienta |
| `<CLIENT_NAME>_DB_KEY` | Hasło do bazy biznesowej klienta |

Przykład dla klienta `DELTA`:

```bash
export DELTA_API_USERNAME='telematics_user@example.com'
export DELTA_API_KEY='secret_api_password'
export DELTA_DB_USERNAME='delta_dbuser'
export DELTA_DB_KEY='secret_db_password'
```

**Uwaga:** kolumny `*_secret_ref` w `workflow_a_control.client_account` przechowują **nazwy zmiennych env** (np. `DELTA_API_KEY`), nigdy wartości sekretów. Odpowiada to kontraktowi `jobs/api/telematics/secret_resolver.py`.

#### Konfiguracja YAML

Szablon: `scripts/templates/workflow_a_client.template.yaml`

```bash
cp scripts/templates/workflow_a_client.template.yaml scripts/delta.yaml
# Edytuj scripts/delta.yaml — wypełnij dane klienta (bez sekretów)
```

Pola w YAML (tylko konfiguracja, bez sekretów):

- `client_name` — nazwa klienta (prefiks env; bez spacji/znaków specjalnych)
- `client_code` — czytelny kod operatorski (np. `DELTA00001`); opcjonalny, unikalny
- `enabled` — `true`/`false`
- `provider_type` — `telematics_fleet` (v1)
- `provider_base_url` — bazowy URL API Telematics (bez trailing slash)
- `client_db_host`, `client_db_port`, `client_db_name`, `client_db_schema`
- `trip_metrics_population_source` — źródło prawdy dla metryk tripów w `public.client_trips`; wartości: `api_migration`, `report_207_migration`, `d105_2_ecodriving_migration`, `disabled`; default onboardingu to `api_migration`
- `speed_trigger_filter_text` — legacy pole konfiguracyjne; aktualne liczniki speeding są liczone z raw telemetry `/vehicles/events` tylko przy `trip_metrics_population_source=api_migration`, nie z `trigger_description`

#### Procedura onboardingu

**1. Dry-run** (bez zmian, walidacja konfiguracji):

```bash
cd /opt/log-platform

PYTHONPATH="$PWD" .venv/bin/python scripts/onboard_workflow_a_client.py \
  --config scripts/delta.yaml
```

**2. Apply** (tworzenie DB, DDL, grants, insert control-plane):

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/onboard_workflow_a_client.py \
  --config scripts/delta.yaml \
  --apply \
  --window-start-ts "2026-03-18T00:00:00Z" \
  --window-end-ts "2026-03-25T23:59:59Z"
```

Flagi pomijania (partial re-run): `--skip-db-create`, `--skip-ddl`, `--skip-grants`, `--skip-control-plane`, `--skip-provider-auth-check`.

#### Po onboardingu — konto NIE jest production-ready

Onboarding tworzy klienta w dokładnie jednym stanie: `CREATED_DISABLED_STRICT` —
`trips_pagination_mode = strict_meta` i schedule `trips_sync` `enabled = false`. Skrypt **nie może**
włączyć `trips_sync` przy tworzeniu (bramka `jobs/api/telematics/schedule_mutation_surfaces.py`), nie
uznaje się za ukończony na podstawie samego powstania wierszy — odczytuje zatwierdzony control-plane
i odmawia stanu niejednoznacznego (zduplikowany `client_code`, dwa schedule'e `trips_sync`, schedule
włączony, tryb inny niż `strict_meta`) — i drukuje maszynowo czytelną referencję stanu
(`telematics-onboarding-state/1`) dla narzędzi, które następują po nim.

> **Historyczne ostrzeżenie.** Do `2026-08-04` skrypt drukował tu gotową komendę
> `ops/runner.py jobs.api.telematics.sync_trips_and_speeding`. Przy wyłączonym schedule'u ta komenda
> **pomija pracę i kończy się kodem `0`**, co jest dokładnie tym mechanizmem, który wyprodukował
> fałszywe coverage `ECHO00001`. Komenda została usunięta; nie należy jej uruchamiać na tym etapie.

Wymagany następny krok to **read-only** audyt zero-state (`ZERO_STATE_VERIFIED`):

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/audit_telematics_cold_start.py \
  --client-code <CODE> --dataset trips_sync \
  --expected-environment production \
  --expected-platform-uuid <platform uuid>
```

Pełna maszyna stanów i operacyjna sekwencja dla nowego klienta: §5.5, „Procedura onboardingu
przyszłego klienta". Szczegóły okien i parametrów runu manualnego: sekcja 5.2 poniżej.

#### Preflight i bezpieczeństwo

- Preflight auth to **jedno** lekkie żądanie `GET /vehicles` z `limit=1&page=1`; sprawdza reachability i BasicAuth bez odpytywania ciężkiego `/trips` dla dużych flot.
- Timeout: 30 sekund.
- Można wyłączyć: `--skip-provider-auth-check`.

#### Typowe problemy przy onboardingu

| Objaw | Przyczyna | Rozwiązanie |
|-------|-----------|-------------|
| HTTP 401/403 na preflighcie | Błędny username lub hasło API | Sprawdź `<CLIENT_NAME>_API_USERNAME` i `<CLIENT_NAME>_API_KEY` |
| `SecretResolutionError: Missing secret for ref='...'` | `*_secret_ref` w DB zawiera wartość sekretu zamiast nazwy env-var | Zaktualizuj wiersz w `client_account`: wstaw nazwę zmiennej (np. `DELTA_API_KEY`), nie wartość |
| `column "client_code" ... does not exist` lub `column "trip_mode" ... does not exist` | Nie zastosowano finalnego DDL `020_client_trips_final_schema.sql` / `021_add_trip_mode_to_client_trips.sql` na bazie klienta | Uruchom ponownie z `--skip-db-create` (bez `--skip-ddl`) |
| `permission denied for table client_trips` | Brakujące granty na tabelach biznesowych | Uruchom ponownie z `--skip-db-create --skip-ddl` (bez `--skip-grants`) |
| `client_db_name already used by ...` | Inna konfiguracja klienta już wskazuje na tę bazę | Użyj innej nazwy DB lub sprawdź istniejący rekord |
| Błędny `provider_base_url` | Niewłaściwy URL API Telematics | Sprawdź URL w YAML; aktualny endpoint produkcyjny: `https://fleetapi-pl.telematics-provider.example/rest` |

#### Rollback przy błędzie

Skrypt w trybie `--apply` wykonuje **best-effort rollback** zasobów utworzonych w bieżącym uruchomieniu, jeśli którykolwiek krok zakończy się błędem:

- **Wiersz control-plane** (`workflow_a_control.client_account`) — usuwany, jeśli został wstawiony w tym uruchomieniu.
- **Baza danych klienta** — dropowana (z uprzednim zamknięciem połączeń), jeśli została utworzona w tym uruchomieniu.
- **Użytkownik DB klienta** — dropowany, jeśli został utworzony w tym uruchomieniu.

Rollback działa w kolejności odwrotnej (control-plane → DB → user) i jest **best-effort**: jeśli jeden krok cleanup nie powiedzie się, skrypt kontynuuje próby pozostałych kroków i wyświetla szczegółowe komunikaty.

Zasoby, które **istniały przed uruchomieniem** skryptu, nigdy nie są usuwane.

Pełne gwarancje transakcyjne (atomic all-or-nothing) nie są możliwe, ponieważ onboarding obejmuje wiele niezależnych połączeń i operacje `CREATE DATABASE` / `CREATE USER`, które nie podlegają transakcjom SQL. Skrypt informuje operatora o wyniku:

- **sukces** — wszystkie kroki zakończone pomyślnie
- **błąd + pełny rollback** — błąd w trakcie, ale cleanup udany
- **błąd + częściowy rollback** — cleanup niektórych zasobów się nie powiódł; wymagane ręczne czyszczenie


#### Kontrola źródła metryk tripów

`trip_metrics_population_source` jest per-client źródłem prawdy dla wspólnej grupy metryk `public.client_trips`: speeding buckety oraz HIGH_RPM/OVERREV. Zapobiega podwójnemu liczeniu tych samych metryk przez Workflow A API, Workflow B `report_207` i przyszły D105.2 EcoDriving loader.

Dozwolone wartości:

- `api_migration` — domyślne; `jobs.api.telematics.sync_trips_and_speeding` może pobierać `/vehicles/events` i pisać metryki.
- `report_207_migration` — API sync nadal synchronizuje tripy, ale pomija event-derived metryki; `job_report_207_speeding_migration` może pisać report_207 speeding buckety.
- `d105_2_ecodriving_migration` — API sync nadal synchronizuje tripy, ale pomija event-derived metryki; `job_d105_2_ecodriving_trip_metrics_migration` może pisać specjalny wariant D105.2 EcoDriving do trip metrics.
- `disabled` — obecne joby i przyszły D105.2 loader powinny pomijać metric writes; API może nadal synchronizować niemetryczne dane tripów.

Przed zmianą źródła uruchom read-only check:

```bash
PYTHONPATH="$PWD" python3 ops/checks/check_trip_metrics_population_source.py --json
PYTHONPATH="$PWD" python3 ops/checks/check_trip_metrics_population_source.py --client-code ALPHA00001 --strict
```

Zmiana źródła jest jawna i operatorska, np.:

```sql
UPDATE workflow_a_control.client_account
SET trip_metrics_population_source = 'report_207_migration'
WHERE client_code = 'ALPHA00001';
```

Rollout caveat: migracja `040` ustawia istniejące rows na `api_migration`, żeby nie zepsuć obecnego Workflow A API syncu. Klienci, którzy operacyjnie polegają na `report_207`, muszą zostać wykryci przez check i ręcznie przełączeni; repo nie konwertuje ich automatycznie.

### 5.2 Workflow A — Phase 2 manual run

Manualny run nadal jest podstawowym trybem diagnostycznym i on-demand. Ten sam job może być też uruchomiony przez `jobs.api.telematics.dispatcher`, jeśli włączysz schedule w DB i hostowy timer.

Moduł:

- `jobs.api.telematics.sync_trips_and_speeding`

Parametry (wymagane):

- `client_id`
- `window_start_ts` (ISO-8601)
- `window_end_ts` (ISO-8601)

Parametry opcjonalne istotne operacyjnie:

- `chunk_days` — rozmiar job-level chunków dla `GET /trips`; default `2`, maksimum `5`. Wartości `1`–`5` są poprawne, `0`/ujemne i wartości powyżej `5` kończą run błędem walidacji. Manualne dzielenie dużego backfillu na osobne wywołania nie jest wymagane, bo job robi to wewnętrznie także dla runów manualnych i dispatcher/scheduled.
- `event_enrichment_mode` — `enabled` albo `disabled`; działa tylko, gdy `trip_metrics_population_source=api_migration`. `disabled` pomija `/vehicles/events`, ale nadal pobiera `/trips` w chunkach i `/vehicles`; gdy selector nie jest `api_migration`, `/vehicles/events` i zapisy metryk tripów są pomijane niezależnie od tego parametru.

Ważne założenia środowiskowe (operator-managed):

- kontrol-plane konfig dla `client_id` jest ładowany z platformy: `workflow_a_control.client_account`
- `trip_metrics_population_source` musi być świadomie ustawiony: `api_migration` pozwala API jobowi pisać speeding/HIGH_RPM/OVERREV; `report_207_migration` i `d105_2_ecodriving_migration` zostawiają normalny sync tripów, ale blokują API event-derived metryki; `disabled` blokuje wszystkie obecne/future metric-writing źródła
- `provider_basic_auth_password_secret_ref` oraz `client_db_password_secret_ref` muszą odpowiadać na hoście:
- albo env-var o tej samej nazwie (np. `export TELEMATICS_FLEET_PASS=...`),
- albo `file:/path/to/secret.txt` (z którego job odczyta treść).

Przykład: `last_week` (UTC, na podstawie hosta):

```bash
cd /opt/log-platform

NOW_TS="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
START_TS="$(date -u -d '7 days ago' +'%Y-%m-%dT%H:%M:%SZ')"

PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.sync_trips_and_speeding "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "window_start_ts": "${START_TS}",
  "window_end_ts": "${NOW_TS}",
  "trigger": "MANUAL"
}
EOF
)"
```

Przykład: `last_month` (`now - 31 days`). Jeden manualny run wystarczy; `/trips` zostanie domyślnie podzielone na kolejne chunki po 2 dni:

```bash
cd /opt/log-platform

NOW_TS="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
START_TS="$(date -u -d '31 days ago' +'%Y-%m-%dT%H:%M:%SZ')"

PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.sync_trips_and_speeding "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "window_start_ts": "${START_TS}",
  "window_end_ts": "${NOW_TS}",
  "trigger": "MANUAL"
}
EOF
)"
```

Przykład: `last_month` bez pobierania `/vehicles/events` (tripy i inventory nadal są pobierane, liczniki OVERREV/HIGH_RPM oraz 140/160/170 są zapisane jako zera). Dla klienta o bardzo wysokim wolumenie można zmniejszyć `/trips` chunks do 1 dnia:

```bash
cd /opt/log-platform

NOW_TS="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
START_TS="$(date -u -d '31 days ago' +'%Y-%m-%dT%H:%M:%SZ')"

PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.sync_trips_and_speeding "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "window_start_ts": "${START_TS}",
  "window_end_ts": "${NOW_TS}",
  "trigger": "MANUAL",
  "event_enrichment_mode": "disabled",
  "chunk_days": 1
}
EOF
)"
```

Rekomendacje dla `chunk_days`:

- `2` — default i zwykły wybór; bezpieczniejszy globalny rozmiar dla klientów o wysokim wolumenie.
- `1` — użyj, jeśli `/trips` dalej timeoutuje albo przy awaryjnych backfillach z bardzo dużą liczbą tripów.
- `3`–`5` — używaj tylko dla klientów o niższym wolumenie albo celowanych backfilli, gdy operator akceptuje większy pojedynczy request. Telematics `/trips` może timeoutować dla klientów o wysokim wolumenie nawet wtedy, gdy całe okno mieści się w dokumentowanych limitach providera; mniejszy `chunk_days` zmniejsza rozmiar pojedynczego requestu bez zmiany zakresu całego runu.

#### Insert-only historical trip repair

For a proven historical ingestion gap, use the manual-only module `jobs.api.telematics.backfill_trips_insert_only`. Do not use normal `sync_trips_and_speeding` with `overwrite_existing=true` when event enrichment is disabled, because its conflict update can replace established event counters.

Required preflight:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.api.telematics.backfill_trips_insert_only \
  '{
    "client_id": "PUT_CLIENT_ID_HERE",
    "client_code": "PUT_CLIENT_CODE_HERE",
    "window_start_ts": "2026-05-20T02:00:00Z",
    "window_end_ts": "2026-05-28T02:00:00Z",
    "chunk_days": 2,
    "provider_trip_ids": [PUT_PROVEN_PROVIDER_TRIP_ID_HERE],
    "insert_only": true,
    "dry_run": true
  }'
```

For a repair with proven provider trip IDs, set `provider_trip_ids` to a non-empty unique allowlist. The provider request remains fleet-window scoped, but only allowlisted parsed trips can become candidates. Review `candidate_provider_trip_ids`, `rows_would_insert`, `rows_filtered_by_provider_trip_ids`, `rows_existing_skipped`, duplicate/rejection counters, provider request windows, registration/date distribution, and temporal overlap diagnostics. The logged DB host, name, and schema must identify the intended client database.

A real run is allowed only after reviewing that preflight and copying its exact `rows_would_insert` value into `expected_insert_count`:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.api.telematics.backfill_trips_insert_only \
  '{
    "client_id": "PUT_CLIENT_ID_HERE",
    "client_code": "PUT_CLIENT_CODE_HERE",
    "window_start_ts": "2026-05-20T02:00:00Z",
    "window_end_ts": "2026-05-28T02:00:00Z",
    "chunk_days": 2,
    "provider_trip_ids": [PUT_PROVEN_PROVIDER_TRIP_ID_HERE],
    "insert_only": true,
    "dry_run": false,
    "expected_insert_count": PUT_REVIEWED_COUNT_HERE
  }'
```

The real job uses only `ON CONFLICT ... DO NOTHING`. It does not update existing trips and does not call `/vehicles/events`. After insertion, verify the trip count and unchanged counters on pre-existing rows before retrying any report migration or rebuilding Eco Driving periods.

Weryfikacja w bazie biznesowej klienta (manual, po uruchomieniu):

- `client_trips`: upewnij się, że rekordy w oknie mają zaktualizowane kolumny `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count` oraz że pola `client_id` i `client_code` są zapisane spójnie; liczniki speeding pochodzą z fleet-wide `/vehicles/events`, nie z `/alerts/notifications`
- `client_trips`: `start_timestamp` i `end_timestamp` pozostają `timestamptz`; logi i user-facing konteksty joba pokazują dodatkowo wartości lokalne `*_local` w `Europe/Warsaw`
- `client_trips`: upewnij się także, że `high_rpm_events_count` i `overrev_events_count` są aktualizowane z provider-labeled fleet-wide `/vehicles/events`; job nie używa progów liczbowych `rpm`
- `client_trips`: finalna kolejność kolumn powinna odpowiadać `docs/05_jobs.md`; diagnostyka:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_client_trips_final_schema.py \
  --dsn "host=127.0.0.1 port=5432 dbname=<client_db> user=<user> password=<pw>"
```

- `client_trips`: `driver_tag_description` i `identification_tag_id` są wypełniane z `/trips`, jeśli provider zwraca te pola; `vehicle_name` i `vehicle_description` są wzbogacane z batch-safe `GET /vehicles` (`vehicle_name` oraz `client_vehicle_description`) przez lookup po `vehicle_id` z fallbackiem po znormalizowanej `registration`; brak dopasowania zostawia pola `NULL`
- `client_trips`: `trip_mode` jest wypełniane z `/trips.is_private` (`true` → `private`, `false` → `business`, brak/nieznana wartość → `NULL`); `sync_trips_and_speeding` pobiera `/trips` z `incl_private=true`, więc prywatne tripy są uwzględniane w runie
- Eco Driving schema: po zastosowaniu `027_eco_driving_schema.sql`, `028_eco_driving_periods_and_driver_chart.sql`, `029_eco_driving_nullable_scores.sql`, `030_eco_driving_trend_views.sql`, `031_eco_driving_validation_fields.sql`, `032_eco_driving_weekly_email_notifications.sql`, `033_eco_driving_weekly_email_send_log_grants.sql`, `034_eco_driving_rating_type_share_percent.sql`, `035_eco_driving_monthly_email_notifications.sql`, `036_eco_driving_round_per_100km_stats.sql` i `037_eco_driving_score_from_rounded_per_100km.sql` w bazie powinny istnieć `eco_trip_assignments`, `eco_driver_weekly_stats`, `eco_driver_monthly_stats`, `eco_drivers_id_chart`, `eco_driving_weekly_email_send_log`, `eco_driving_monthly_email_send_log`, view `public."Eco_Drivers_ID_Chart"` oraz views `eco_driver_weekly_trends_view` / `eco_driver_monthly_trends_view`. `eco_trip_assignments` ma audit prywatnych tripów (`driver_tag_description`, `is_private_trip`, `exclusion_reason`, `aggregation_included`), weekly stats używają cumulative month-to-date snapshots (`period_start_date`, `period_end_date`, `period_label`), a punkty/wynik mogą być `NULL` dla zerowego dystansu. Statyczna diagnostyka migracji:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_eco_driving_schema.py
PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_eco_driving_trend_views.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_runner_integration.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_aggregation_job.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_weekly_email_notifications.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_e2e.py
```

Przykładowa weryfikacja w bazie klienta:

```sql
SELECT driver_id, driver_name, email, ranking_included, is_active
FROM public.eco_drivers_id_chart
ORDER BY driver_id
LIMIT 20;

SELECT assignment_source, aggregation_included, exclusion_reason, COUNT(*)
FROM public.eco_trip_assignments
GROUP BY assignment_source, aggregation_included, exclusion_reason;
```

- `client_speeding_notifications`: tabela pozostaje dla danych historycznych/kompatybilności, ale aktualny `sync_trips_and_speeding` nie pobiera już `GET /alerts/notifications` do liczników HIGH_RPM / OVERREV

### 5.3 Workflow A — Eco Driving aggregation

Moduł:

- `jobs.ecodriving.job_eco_driving_aggregate`

Job czyta `public.client_trips`, zapisuje audit przypisania do `public.eco_trip_assignments`, agreguje cumulative month-to-date weekly reporting snapshots do `public.eco_driver_weekly_stats`, agreguje pełny miesiąc do `public.eco_driver_monthly_stats` i uzupełnia rankingi na podstawie `public.eco_drivers_id_chart`.

#### 5.3.0 Live rollout checklist (first controlled run on a real client DB)

Run these steps in order the first time Eco Driving aggregation is enabled
for a client. Every step is idempotent; abort at the first failure and read
the diagnostics it printed before continuing.

1. **Apply platform migrations** so the dispatcher registry, schedule
   defaults, and retention rows exist:

    ```bash
    cd /opt/log-platform
    bash ops/db_migrate.sh
    ```

    This applies `db/migrations/032_workflow_a_eco_driving_registry.sql`.
2. **Apply client-business migrations** to the existing client DB (or rely
   on the curated baseline that `scripts/onboard_workflow_a_client.py`
   already applies for new clients):

    ```bash
    PYTHONPATH="$PWD" .venv/bin/python scripts/apply_client_business_migrations.py --apply
    ```

    This applies the Eco Driving client-business migrations through `037_*` if missing, including `public.eco_driving_weekly_email_send_log`, `public.eco_driving_monthly_email_send_log`, client-user grants, the rating-type share reporting field, rounded stored per-100km rates, and rounded-rate scoring backfill.
3. **Verify schema in the client DB** — `eco_trip_assignments`,
   `eco_driver_weekly_stats`, `eco_driver_monthly_stats`,
   `eco_drivers_id_chart`, the compatibility view
   `public."Eco_Drivers_ID_Chart"`, and the trend views
   `eco_driver_weekly_trends_view` / `eco_driver_monthly_trends_view`:

    ```sql
    \dt public.eco_*
    \dv public.eco_*
    \dv public."Eco_Drivers_ID_Chart"
    ```
4. **Populate `eco_drivers_id_chart` before aggregation** with the known
   drivers and their `ranking_included` flag. The chart must contain at least
   one usable row for the client, and a non-empty source population must have
   at least one chart match. Individual missing rows remain valid and appear
   as `UNKNOWN_DRIVER`, but an empty chart fails with
   `DRIVER_CHART_NOT_LOADED` and an all-unmatched source fails with
   `DRIVER_CHART_NO_SOURCE_MATCHES`. See the SQL example below
   ("Populate/update `eco_drivers_id_chart`").
   Verify the selected client before continuing:

    ```sql
    SELECT count(*) AS driver_chart_rows
    FROM public.eco_drivers_id_chart
    WHERE client_id = '<CLIENT_ID>'
      AND driver_id IS NOT NULL
      AND btrim(driver_id) <> '';
    ```
5. **Dry-run the chosen month** to validate connectivity, periods, and
   counts without writing:

    ```bash
    PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
      '{"client_id":"<CLIENT_ID>","month":"YYYY-MM","dry_run":true}'
    ```
   Review `driver_chart_rows`, `source_driver_rows`, `matched_driver_rows`,
   `unmatched_driver_rows`, and `unmatched_driver_percentage`. Do not continue
   after either driver-chart precondition failure. The job validates all
   requested snapshot populations before `recalculate=true` can delete or
   replace an existing snapshot, and the transaction rolls back assignment
   work on failure.
6. **Selected-month full rebuild** with `recalculate=true` to seed clean
   weekly + monthly rows for the chosen month:

    ```bash
    PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
      '{"client_id":"<CLIENT_ID>","month":"YYYY-MM","include_weekly":true,"include_monthly":true,"recalculate":true}'
    ```
7. **Verify assignment audit counts** — see "Inspect skipped rows" and
   "Verify private-trip exclusion" SQL snippets below. Confirm
   `SKIPPED_NO_ID` and private rows are audited but excluded
   (`aggregation_included=false`).
8. **Verify `UNKNOWN_DRIVER`** rows — confirm only drivers absent from
   `eco_drivers_id_chart` show up; see the "Inspect `UNKNOWN_DRIVER`
   rows" snippet.
9. **Verify final weekly snapshot = monthly raw totals/kilometers** for a
   sample `assigned_id` — see "Verify that W2 includes W1 data and final
   W matches monthly raw totals". The final weekly row's
   `total_distance_meters`, `total_kilometers` and event sums must equal
   the corresponding monthly row.
10. **Enable the disabled schedule rows** (`enabled = false` by default
    after migration `032_*` / onboarding):

    ```sql
    UPDATE workflow_a_control.client_dataset_schedule
       SET enabled = true
     WHERE client_id = '<CLIENT_ID>'
       AND dataset_name IN (
         'eco_driving_weekly_snapshot',
         'eco_driving_month_end_weekly_snapshot',
         'eco_driving_monthly_aggregation'
       );
    ```
11. **Monitor the first scheduled run** in
    `workflow_a_control.client_schedule_run_history` and platform
    `logs` / `runs` until the first SUCCESS for each Eco Driving dataset
    arrives:

    ```sql
    SELECT dataset_name, status, scheduled_fire_ts, started_at, finished_at,
           error_summary
    FROM workflow_a_control.client_schedule_run_history
    WHERE client_id = '<CLIENT_ID>'
      AND dataset_name LIKE 'eco_driving_%'
    ORDER BY scheduled_fire_ts DESC
    LIMIT 10;
    ```

Do not skip step 5 (dry-run) on a fresh client; it is the safest way to
catch DSN / chart / period issues before any write.

Do not run weekly or monthly driver email notifications from an incomplete
snapshot. Email routing uses the persisted snapshot `ranking_included`, not
the current chart. Snapshot `NULL`, missing/non-positive rank or participant
count, missing rating type, and missing/out-of-range group percentage are
recorded per candidate as `status='failed'` with
`classification='INVALID_RANKING_SNAPSHOT'`; SMTP is not called. If a chart
was imported after aggregation, recalculate the reviewed snapshot first,
verify persisted rank/share values, and only then run a controlled dry-run or
test-recipient send.

Uruchomienie dla wybranego miesiąca:

```bash
cd /opt/log-platform

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "month": "2026-05"
}
EOF
)"
```

Uruchomienie dla jednego cumulative weekly reporting point. `period_start_date` musi być pierwszym dniem miesiąca, a `period_end_date` jest granicą raportu:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "period_start_date": "2026-05-01",
  "period_end_date": "2026-05-11",
  "include_monthly": false
}
EOF
)"
```

Weekly rows are cumulative month-to-date snapshots:

- W1: `month_start` → first weekly boundary.
- W2: `month_start` → second weekly boundary, including W1 data.
- Wn: `month_start` → nth weekly boundary.
- Final W: `month_start` → `next_month_start`; raw totals and kilometers should match the monthly row for the same filters.

Do not sum weekly Eco Driving rows to obtain monthly totals, because weekly rows are cumulative month-to-date snapshots. Monthly rows are calculated independently from `eco_trip_assignments`.

Rerun jednego `assigned_id` z usunięciem starych statystyk w wybranym zakresie przed upsertem:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "month": "2026-05",
  "assigned_id": "PUT_ECO_DRIVER_ID_HERE",
  "recalculate": true
}
EOF
)"
```

Dry-run wykonuje te same zapytania w transakcji i robi rollback:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"PUT_CLIENT_ID_HERE","month":"2026-05","dry_run":true}'
```

Manual selected-month full rebuild, including all cumulative weekly snapshots and the independent monthly row set:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "month": "2026-05",
  "include_weekly": true,
  "include_monthly": true,
  "recalculate": true
}
EOF
)"
```

Manual monthly-only rebuild:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "month": "2026-05",
  "include_weekly": false,
  "include_monthly": true,
  "recalculate": true
}
EOF
)"
```

Manual latest completed cumulative weekly snapshot resolver:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "mode": "previous_completed_weekly_snapshot",
  "include_weekly": true,
  "include_monthly": false
}
EOF
)"
```

Manual final-month weekly snapshot (previous-month start → current-month start). Use this on or shortly after day 1 of a new month to re-emit the closing cumulative snapshot for the month that just ended:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "mode": "final_month_weekly_snapshot",
  "include_weekly": true,
  "include_monthly": false
}
EOF
)"
```

Manual monthly aggregation resolver (previous full calendar month):

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate "$(cat <<EOF
{
  "client_id": "PUT_CLIENT_ID_HERE",
  "mode": "monthly_full_aggregation",
  "include_weekly": false,
  "include_monthly": true
}
EOF
)"
```

#### 5.3.1 Eco Driving notification email safety

Do not enable any Eco Driving email schedule before separate readiness approval. Apply `044_eco_email_fail_closed_idempotency.sql` only after its read-only conflict inspection returns zero unresolved normal-key groups. The migration aborts rather than deleting or choosing historical evidence.

All ALPHA driver and BRAVO person weekly/monthly senders default to `execution_mode=render_only`. Parameter omission performs validation/rendering without SMTP, IMAP, or a normal reservation. Real paths must be explicit:

- `test_send` requires one `test_recipient_email` and sends only there;
- `normal_send` uses real recipients and only an eligible closed period;
- `force_resend` requires `force_resend_reason`, preserves the original normal row, and records forced scope;
- `allow_unclosed_period_for_test=true` is limited to render/test scope.

Periods are half-open local Warsaw intervals: start inclusive, end exclusive, both at `Europe/Warsaw` midnight. They are date-closed at the exact exclusive-end instant. Automatic and explicit selections also require `min(stats.updated_at)` at or after that instant, and reject malformed, open, future, or prematurely generated snapshots (`SNAPSHOT_NOT_FINALIZED`).

Before migration or operational review, run the inspection read-only:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/inspect_eco_email_safety.py
```

It attests the expected environment identities, starts read-only transactions, prints normal-key conflict groups, and reports old/new period selection with `PERIOD_NOT_CLOSED`, `PERIOD_END_IN_FUTURE`, `SNAPSHOT_NOT_FINALIZED`, or boundary diagnostics. It performs no cleanup.

Safe render-only example:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>"}'
```

Explicit test-scope example:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"test_send","test_recipient_email":"test@example.com","limit":5}'
```

A normal or forced send may be considered only after migration, closed-period inspection, recipient/readiness approval, and explicit operator authorization. `status='pending'` is a committed reservation before SMTP; both `pending` and `sent` block another normal send under the stable subject/period identity. `status='sent'` means **accepted by example.invalid SMTP for relay**, not confirmed delivery (see "What `sent` means, and where the copy is"). A stale `pending` is not deleted or reclaimed: stop, determine whether SMTP accepted the original message, and reconcile the audit row through a separately reviewed procedure before retrying.

#### Driver Eco Dashboard links in Eco mail — opt-in and per-client gated

The dashboard link is **not** part of the ordinary Eco mailing path. All four mailing commands run legacy-only unless the invocation passes `--with-dashboard`, and even then dashboard mailing additionally requires per-client rollout permission. **Neither condition alone is sufficient.**

| | Result |
|---|---|
| any of the four commands, no flag | legacy e-mail, unchanged. No snapshot build, publication, R2 write, D1 state, capability, dashboard link, or dependency on publisher configuration |
| `--with-dashboard`, ALPHA00001 | **refused** — `ECO_DASHBOARD_MAILING_ROLLOUT_NOT_ENABLED`, before any publication, capability or SMTP |
| `--with-dashboard`, BRAVO00016 | **permitted** — rollout authorized by explicit owner decision. The run still has to ask for the dashboard; the permission sends nothing by itself |
| `--with-dashboard`, any other client | **refused** — undeclared clients are disabled |

Rollout permission is declared in `ops/eco_dashboard_mailing_rollout.json`. Enabling a client is one reviewed value change (`false` -> `true`) in that file and touches no mailing logic; it is a **separate, separately authorized decision** and is not implied by anything in this runbook. A wildcard entry is refused as a malformed declaration, so "all clients" cannot become the default. Do not enable a client by setting `ECO_DASHBOARD_BASE_URL` / `ECO_DASHBOARD_PUBLISHER_URL`: publisher configuration and mailing permission are deliberately independent.

A scheduled fire carries the option only for a (client, dataset) pair declared in `ops/eco_mailing_production_schedule.json`; every other fire builds `[python, ops/runner.py, <module>, <params_json>]` with no options, exactly as before. See *The BRAVO00016 scheduled production execution contract* below. All Eco schedule rows remain **disabled**. The `workers.dev` endpoint that currently exists is a temporary technical endpoint, not a mailing rollout. Full contract: `docs/28` §12.11.

#### Dashboard link lifetime and automatic retirement

A dashboard link lives as long as its reporting period justifies: **weekly grants 10 days, monthly grants 60**. There is no universal lifetime any more and the host holds no lifetime table — the single authority is `delivery/driver_eco_dashboard/worker/lib/capability_ttl.js`, which has no default and refuses any other period type. The host sends the period as the required `X-Publication-Period` control header on `/api/publish` and `/api/publish/recover`, sourced from the immutable `eco_dashboard_delivery_operation.period_type`; a missing, unknown or duplicated value is a `400` that creates no operation, grant or R2 object. Links stay period-scoped and snapshot-pinned, so publishing a newer period never revokes an older still-valid one and an old e-mail keeps showing its own report.

**Grants issued before 2026-08-28 keep their original 45-day validity.** The lifetime is decided when a grant is minted or rotated; nothing recomputes the expiry of a grant that already exists.

**Retirement is automatic and needs no operator action.** `EcoDashboardLinkService.close()` runs `DeliveryLedger.retire_expired_capabilities()` at the end of **every** dashboard-enabled Eco mailing run — all four jobs call `close()` from a `finally`. The sweep is unconditional in two ways that matter operationally: it does not require the run to have published anything (a quiet week with no candidates still retires), and it does not require an existing ledger connection (it opens its own, counted apart from `dashboard_ledger_connections_opened`, and avoids the publisher services so a missing machine credential cannot block it). Render-only rehearsals are excluded and open no connection. Retiring destroys the host's raw bearer once the grant has expired and moves the row to the terminal state `CAPABILITY_RETIRED`, keeping `capability_id`, `capability_digest`, `capability_expires_at`, `bearer_generation` and any bound submission identity as audit. It never touches a live grant, an unknown expiry, a leased row, `PROVIDER_AMBIGUOUS`, `OPERATOR_REQUIRED`, a submitted intent or an in-flight/accepted submission — so nothing here can clear an operator-required state on your behalf. Migration `051_eco_dashboard_capability_retirement.sql` is the prerequisite and is applied on all five enabled client business databases.

**What is not deleted.** Expired `eco_capability` rows in D1 are retained on purpose: they hold no secret and are what makes an expired link answer `LINK_EXPIRED` rather than look like a link that never existed. **No R2 snapshot is deleted by any of this** — report retention is a separate, still-undecided concern. `POST /api/publish/maintenance` compacts expired D1 *sessions* only; it needs the same machine credential as every other publisher route and answers `404` to everyone else.

#### The BRAVO00016 scheduled production execution contract

There is **one** canonical execution path. A scheduled fire, an operator's manual command and a future email-command trigger all become the same invocation of the same job, validated by the same `ops/runner.py` option contract. Only the source of the reporting period differs: the scheduler resolves it automatically, an operator may name it explicitly.

```
trigger -> ops/runner.py -> jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications
        -> candidate selection -> dashboard snapshot / publication / capability
        -> rendering -> SMTP -> Sent archive -> durable send log + run summary
```

**Where each part of the decision lives.** Nothing is duplicated, and no layer can override another:

| Decision | Authority | Effect if absent |
|---|---|---|
| whether a fire happens, its cadence, weekday, wall-clock time, business timezone | `workflow_a_control.client_dataset_schedule` (`enabled`, `frequency`, `day_of_week`, `run_time`, `timezone`) | no fire |
| what a fire *is* — the `execution_mode` | `ops/eco_mailing_production_schedule.json` | fire carries no `execution_mode`; the job's own default `render_only` applies — no mail, no dashboard |
| whether the mail may carry a dashboard | `ops/eco_dashboard_mailing_rollout.json` | no `--with-dashboard` option is added, and the job refuses one if it somehow arrived |
| which reporting period is sent | `jobs/ecodriving/email_safety.py` against the persisted weekly snapshots | `NO_ELIGIBLE_CLOSED_PERIOD`; nothing is sent |

`jobs/ecodriving/scheduled_mailing_contract.py` joins the first three and hands the dispatcher a typed invocation. The dispatcher names no option itself: it appends what the contract resolved and refuses anything outside the declared allowlist.

**Current declared state.** `BRAVO00016` / `eco_person_driving_weekly_email_notifications` -> `normal_send`, dashboard **on** (resolved from the rollout declaration). `ALPHA00001` and every other client and dataset -> undeclared, therefore render-only and dashboard-free. The monthly mailing is deliberately not declared.

**Period semantics (unchanged, now stated).** Weekly Eco Driving Person periods are cumulative month-to-date: `period_start_date` is always the first of the month, and `period_end_date` is one of the month's Mondays or the first of the next month, exclusive, at `Europe/Warsaw` midnight. For August 2026 those boundaries are `08-03` (W1), `08-10` (W2), `08-17` (W3), `08-24` (W4), `08-31` (W5) and `09-01` (the final partial segment). A scheduled run sends **the latest period that is both closed and finalized** at its effective instant, evaluated in `Europe/Warsaw`; `min(stats.updated_at)` must be at or after the exclusive end instant or the period is rejected as `SNAPSHOT_NOT_FINALIZED`. The selection, the instant it was evaluated at and every rejected candidate are written to the run summary under `period_selection`.

**Retry and failure matrix.** Encoded in the send log and the run-history row, not in a generic retry loop:

| Situation | Classification | What happens |
|---|---|---|
| the process failed before any candidate reached a remote effect | `SAFE_AUTOMATIC_RETRY` | the next scheduled fire re-runs; nothing external happened |
| a candidate was skipped, or reserved and left `pending` within the stale window | `SAFE_IDEMPOTENT_REENTRY` | a rerun for the same period re-derives the same identity and no-ops on `pending`/`sent` |
| candidate already `sent` for this period | `SAFE_IDEMPOTENT_REENTRY` | counted as `skipped_already_sent`; never resent |
| dashboard publication / link creation failed for a candidate | `MANUAL_RETRY` | that candidate is **not** sent without its link: `failed_before_smtp`, `dashboard_link_blocked`, other candidates continue, the shared transaction is intact. Re-run the period after fixing the publisher |
| SMTP definitively refused (including `PUBLISHER_EDGE_FORBIDDEN`-style definite refusals) | `MANUAL_RETRY` | row marked `failed`; no message was accepted, so a later run may reserve it again |
| SMTP outcome ambiguous | `OPERATOR_REQUIRED` / `NEVER_AUTO_RETRY` | row frozen `AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION`. No scheduler, runner, job, recovery loop, daemon restart or future trigger may resend — **including `force_resend`**. Only `ops/reconcile_eco_email_ambiguous_send.py` clears it |
| stale `pending` past the stale window | `OPERATOR_REQUIRED` | `STALE_PENDING_REQUIRES_RECONCILIATION`; establish what SMTP did before touching it |
| Sent archive (IMAP) failed after SMTP acceptance | `MANUAL_RETRY`, archive only | the send stays `sent` and is **never** resubmitted to SMTP. Recover with the `archive_only` path |

**Operator controls.** All read-only or already-existing; nothing here is made easier or broader:

```bash
# read-only: what a scheduled fire would resolve to, and where each half of
# that decision came from. Reads two declarations, no database, no network
PYTHONPATH="$PWD" .venv/bin/python ops/inspect_eco_mailing_schedule_contract.py

# read-only status: periods, finalization, conflicts, identities
PYTHONPATH="$PWD" .venv/bin/python ops/inspect_eco_email_safety.py --client-code BRAVO00016

# render-only rehearsal (no SMTP, no publication, no capability)
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<CLIENT_UUID>","execution_mode":"render_only"}'

# the exact scheduled invocation, run by hand
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}' --with-dashboard

# ambiguous-SMTP inspection and reconciliation (operator-required states)
PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_eco_email_ambiguous_send.py --help

# dashboard publication / delivery states left OPERATOR_REQUIRED
PYTHONPATH="$PWD" .venv/bin/python ops/recover_eco_dashboard_operator_required_delivery.py --help
```

`test_send` additionally requires `test_recipient_email`; `force_resend` additionally requires `force_resend_reason` and still refuses to act on an unresolved ambiguous row. Neither is schedulable: the declaration accepts only `render_only` and `normal_send`.

**Enabling the first production schedule** is a separate, separately authorized decision. It is one `UPDATE` of `enabled` on the `eco_person_driving_weekly_email_notifications` row for BRAVO00016, after the owner has confirmed the weekday and wall-clock time. `ops/activate_telematics_trips_schedule.py` cannot do it — it refuses every dataset but `trips_sync`.

**Observability.** A scheduled fire logs `scheduled_mailing_contract` (client, dataset, job module, `execution_mode`, `dashboard_enabled`, resolved runner options and the rollout source) before it launches, and the job's run summary carries `period_selection`, `candidates_count`, `rendered_count`, `sent_count`, `failed_count`, `failed_before_smtp_count`, `dashboard_link_blocked_count`, `smtp_attempt_count`, `smtp_accepted_count`, `smtp_failed_count`, `smtp_ambiguous_count`, `ambiguous_reconciliation_blocked_count` and the `sent_archive_*` counters.

#### Diagnosing a dashboard publisher refusal

The host publisher sends a fixed product identity on every request:

```
User-Agent: log-platform-eco-dashboard-publisher/1
```

It contains no host, client, driver or credential information and never varies. It is required: with `urllib`'s default `Python-urllib/3.x` the Cloudflare edge answers an empty **HTTP 403** (`error code: 1010`) before the Worker executes, so host publishing cannot work without it. Do not remove or override it.

Transport outcome codes an operator will see:

| Code | Meaning | Next action |
|---|---|---|
| `PUBLISHER_EDGE_FORBIDDEN` (HTTP 403) | Something **in front of** the publisher refused the request — Cloudflare browser-integrity/WAF/bot-management/access policy. The Worker never ran, so nothing was published. | Look at the **edge configuration**, not at the publisher, the ledger or the credential. Definite refusal: nothing to reconcile, and no automatic retry occurs. |
| `NOT_FOUND` (HTTP 404) | The boundary's deliberately ambiguous "unknown operation **or** unauthenticated" answer. | Check the credential and the operation identity. Not proof the operation does not exist. |
| `PROTOCOL_ERROR` and other definite refusals | The publisher answered something this host could not map. | Investigate the publisher response. |
| `TRANSPORT_OUTCOME_UNKNOWN` (timeouts, dropped connections, HTTP >= 500) | The request **may** have committed and the answer was lost. | The host state machine asks the publisher what happened. Never resend blindly. |

The Worker answers 400, 401, 404, 409 and 503 and never 403, so a 403 is by construction a boundary refusal. It is classified as a **definite** outcome and never as an ambiguous one, so it acquires none of the retry semantics reserved for transport ambiguity.

Scheduler integration uses the existing Workflow A dispatcher. Migration `032_workflow_a_eco_driving_registry.sql` and new onboarding create these disabled schedule rows:

| Dataset | Default schedule | Dispatcher mode |
|---|---|---|
| `eco_driving_weekly_snapshot` | Monday 03:00 Europe/Warsaw | `weekly_cumulative_snapshot` |
| `eco_driving_month_end_weekly_snapshot` | Day 1 03:30 Europe/Warsaw | `final_month_weekly_snapshot` |
| `eco_driving_monthly_aggregation` | Day 1 04:00 Europe/Warsaw | `monthly_full_aggregation` |

Enable Eco Driving schedules for a client:

```sql
UPDATE workflow_a_control.client_dataset_schedule
   SET enabled = true
 WHERE client_id = '<CLIENT_ID>'
   AND dataset_name IN (
     'eco_driving_weekly_snapshot',
     'eco_driving_month_end_weekly_snapshot',
     'eco_driving_monthly_aggregation'
   );
```

Inspect the generated schedule rows and the exact runner modes the dispatcher will use:

```sql
SELECT dataset_name, enabled, frequency, day_of_week, day_of_month,
       run_time, timezone, lookback_days
FROM workflow_a_control.client_dataset_schedule
WHERE client_id = '<CLIENT_ID>'
  AND dataset_name LIKE 'eco_driving_%'
ORDER BY dataset_name;
```

Populate/update ALPHA00001 `eco_drivers_id_chart` from an authoritative roster:

1. Copy `scripts/templates/alpha00001_eco_driver_chart_roster.template.csv` to an operator-controlled path and fill every `driver_name`, `ranking_included`, and `is_active` value from the authoritative source. Fill `email` when available. Do not derive identity from vehicle registration or assignment history.
2. Run the exact five-ID dry-run:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py \
  jobs.reports.postprocess.job_alpha00001_driver_chart_exact_import \
  '{"client_code":"ALPHA00001","input_path":"/absolute/path/alpha00001_driver_roster.csv","driver_ids":["82122","323715","553883","45488","359682"]}'
```

3. Review `rejected_rows`, `missing_allowlist_ids`, `extra_input_ids`, `proposed_insert_count`, `proposed_updates`, protected existing rows, and missing-email IDs. Do not continue while rejected rows remain.
4. Only after approval, repeat with `dry_run=false` and both exact reviewed counts. Keep `allow_updates=false` unless existing chart changes are explicitly approved. The sample `5`/`0` values below are valid only when the immediately preceding dry-run reports exactly five inserts and zero updates; otherwise use the reported counts:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py \
  jobs.reports.postprocess.job_alpha00001_driver_chart_exact_import \
  '{"client_code":"ALPHA00001","input_path":"/absolute/path/alpha00001_driver_roster.csv","driver_ids":["82122","323715","553883","45488","359682"],"dry_run":false,"expected_insert_count":5,"expected_update_count":0}'
```

5. Repeat the dry-run. An unchanged successful import must report zero proposed inserts and zero proposed updates. Rebuild an Eco Driving period separately only when the chart import was actually applied and the affected reporting period requires refreshed persisted ranking metadata.

For non-ALPHA clients, the existing generic direct SQL shape remains. Do not use it to bypass the guarded ALPHA00001 import job:

```sql
INSERT INTO public.eco_drivers_id_chart (
  client_id, driver_id, driver_name, email, ranking_included, is_active
) VALUES (
  '<CLIENT_ID>', 'ECO123', 'Jan Kowalski', 'jan.kowalski@example.com', true, true
)
ON CONFLICT (client_id, driver_id) DO UPDATE SET
  driver_name = EXCLUDED.driver_name,
  email = EXCLUDED.email,
  ranking_included = EXCLUDED.ranking_included,
  is_active = EXCLUDED.is_active,
  updated_at = now();
```

Verify private-trip exclusion:

```sql
SELECT provider_trip_id, assigned_id, driver_tag_description, is_private_trip,
       aggregation_included, exclusion_reason
FROM public.eco_trip_assignments
WHERE client_id = '<CLIENT_ID>'
  AND is_private_trip = true
ORDER BY trip_start_ts DESC
LIMIT 50;
```

Inspect skipped rows:

```sql
SELECT assignment_source, aggregation_included, exclusion_reason, COUNT(*)
FROM public.eco_trip_assignments
WHERE client_id = '<CLIENT_ID>'
GROUP BY assignment_source, aggregation_included, exclusion_reason
ORDER BY assignment_source, aggregation_included, exclusion_reason;
```

Inspect `UNKNOWN_DRIVER` rows:

```sql
SELECT assigned_id, period_label, total_kilometers, eco_driving_score_total
FROM public.eco_driver_weekly_stats
WHERE client_id = '<CLIENT_ID>'
  AND ranking_group = 'UNKNOWN_DRIVER'
ORDER BY period_start_date DESC, assigned_id;
```

Verify that W2 includes W1 data and final W matches monthly raw totals:

```sql
SELECT period_label, period_start_date, period_end_date,
       total_distance_meters, total_kilometers,
       overrev_events_count, eco_driving_score_total
FROM public.eco_driver_weekly_stats
WHERE client_id = '<CLIENT_ID>'
  AND assigned_id = '<ASSIGNED_ID>'
  AND month_start_date = DATE '2026-05-01'
ORDER BY period_sequence_in_month;

SELECT month_start_date, month_end_date,
       total_distance_meters, total_kilometers,
       overrev_events_count, eco_driving_score_total
FROM public.eco_driver_monthly_stats
WHERE client_id = '<CLIENT_ID>'
  AND assigned_id = '<ASSIGNED_ID>'
  AND month_start_date = DATE '2026-05-01';
```

Verify that the latest scheduled weekly run used `period_start_date = month_start_date`, not the incremental Monday:

```sql
SELECT period_label, period_start_date, month_start_date, period_end_date,
       total_kilometers, eco_driving_score_total
FROM public.eco_driver_weekly_stats
WHERE client_id = '<CLIENT_ID>'
ORDER BY period_end_date DESC, assigned_id
LIMIT 20;
```

Inspect weekly trend/progress view. `score_delta_abs` compares cumulative month-to-date scores between snapshots, and `kilometers_delta_abs` is the distance added since the previous snapshot:

```sql
SELECT period_label, period_start_date, period_end_date,
       total_kilometers, previous_snapshot_kilometers, kilometers_delta_abs,
       eco_driving_score_total, previous_snapshot_score,
       score_delta_abs, score_delta_pct,
       ranking_position, previous_ranking_position, ranking_position_delta
FROM public.eco_driver_weekly_trends_view
WHERE client_id = '<CLIENT_ID>'
  AND assigned_id = '<ASSIGNED_ID>'
  AND month_start_date = DATE '2026-05-01'
ORDER BY period_end_date;
```

Verify that W2 includes W1 data by comparing cumulative kilometers:

```sql
SELECT period_label, total_kilometers, kilometers_delta_abs
FROM public.eco_driver_weekly_trends_view
WHERE client_id = '<CLIENT_ID>'
  AND assigned_id = '<ASSIGNED_ID>'
  AND month_start_date = DATE '2026-05-01'
ORDER BY period_sequence_in_month;
```

Inspect monthly trends:

```sql
SELECT month_start_date, month_end_date,
       total_kilometers, previous_month_kilometers, kilometers_delta_abs,
       eco_driving_score_total, previous_month_score,
       score_delta_abs, rolling_3_month_avg_score,
       ranking_position, previous_ranking_position, ranking_position_delta
FROM public.eco_driver_monthly_trends_view
WHERE client_id = '<CLIENT_ID>'
  AND assigned_id = '<ASSIGNED_ID>'
ORDER BY month_start_date;
```

Inspect unranked unknown drivers in trend outputs:

```sql
SELECT assigned_id, period_label, total_kilometers, eco_driving_score_total,
       ranking_group, ranking_position, ranking_position_delta
FROM public.eco_driver_weekly_trends_view
WHERE client_id = '<CLIENT_ID>'
  AND ranking_group = 'UNKNOWN_DRIVER'
ORDER BY month_start_date DESC, period_end_date DESC, assigned_id
LIMIT 50;
```

Troubleshooting:

| Objaw | Najczęstsza przyczyna | Sprawdzenie / działanie |
|-------|------------------------|--------------------------|
| Manual run fails with `Missing required param: client_id` | Eco Driving job is client-scoped | Pass `{"client_id":"..."}`; dispatcher adds it from `client_dataset_schedule.client_id` |
| Dispatcher says no due Eco weekly snapshot | Schedule row disabled, wrong timezone/run_time, or the logical fire already exists in run history | Check `client_dataset_schedule` for `eco_driving_weekly_snapshot` and `client_schedule_run_history` for the latest `scheduled_fire_ts` |
| Wrong weekly bounds | Caller passed an explicit period that is not cumulative month-to-date, or schedule row points at the wrong Eco dataset | Use `mode=previous_completed_weekly_snapshot` or ensure `period_start_date` is the first day of the month |
| Wiele `SKIPPED_NO_ID` | Brak `"Driver_Restrictions"` i `"Dysponent_ID"` w `client_trips` | Sprawdź `SELECT COUNT(*) FROM public.client_trips WHERE NULLIF(btrim("Driver_Restrictions"), '') IS NULL AND NULLIF(btrim("Dysponent_ID"), '') IS NULL;` |
| Prywatne trasy znikają ze statystyk | `driver_tag_description` zawiera `pryw` case-insensitively | Sprawdź audit w `eco_trip_assignments` i source `driver_tag_description` |
| `ranking_group='UNKNOWN_DRIVER'` | Brak wiersza w `eco_drivers_id_chart` dla `assigned_id` | Dodaj/uzupełnij wiersz chart dla `(client_id, driver_id)` |
| `UNKNOWN_DRIVER` nie ma rankingu w trend view | Unknown drivers są zachowane, ale celowo nierankingowane | Oczekuj `ranking_position=NULL` i `ranking_position_delta=NULL`; uzupełnij `eco_drivers_id_chart`, jeśli driver ma być klasyfikowany |
| `qualification_status='NO_DISTANCE'` | Suma `trip_distance_meters` wynosi 0 | Zweryfikuj dystanse w `client_trips`; wynik Eco Driving będzie `NULL` |
| `qualification_status='LOW_DISTANCE'` | Dystans okresu jest >0 i <100 km | To poprawny status niskiego wolumenu; wiersz **nie bierze udziału w rankingu** — `ranking_group`, `ranking_position` i `ranking_total_participants` są `NULL` |
| `ranking_group IS NULL` | Wiersz nie jest `QUALIFIED` (`LOW_DISTANCE` lub `NO_DISTANCE`) | Oczekiwane: kwalifikacja ma pierwszeństwo przed chartem. `ranking_included` z chartu zostaje zachowany, ale nie działa aż do osiągnięcia progu 100 km. Nie mylić z `UNKNOWN_DRIVER`, który dotyczy wyłącznie wierszy `QUALIFIED` bez wpisu w charcie |
| Historyczny wiersz `LOW_DISTANCE` ma numeryczny `ranking_position` | Snapshot policzony przed wprowadzeniem kontraktu „tylko QUALIFIED w rankingu" | `046_eco_ranking_qualified_only.sql` niczego nie przelicza — poprawia wyłącznie strukturę. Korekta danych wymaga `recalculate=true`, co jest **operacją nieodwracalną**; patrz ostrzeżenie poniżej |
| Brak Eco Driving score | Zerowy dystans albo brak wymaganych rate values | Sprawdź `total_kilometers`, rate columns i migrację `029_eco_driving_nullable_scores.sql` |
| Rerun nie usuwa starego drivera bez nowych included trips | Domyślny run robi upsert istniejących wyników | Użyj `recalculate=true` dla miesiąca/okresu i opcjonalnie `assigned_id` |
| Miesięczne sumy są zawyżone | Operator zsumował cumulative weekly rows | Użyj `eco_driver_monthly_stats`; nie sumuj weekly snapshots |
| W2 ma większe `total_kilometers` niż W1 | Weekly rows są cumulative month-to-date snapshots | To oczekiwane: W2 zawiera W1 plus kolejne eligible tripy do drugiej granicy raportu |
| W1 nowego miesiąca wygląda jak reset | Nowy miesiąc zaczyna nowe cumulative month-to-date okno | To oczekiwane; trend view zachowuje chronologię po `assigned_id`, ale kilometraż W1 wraca do zakresu nowego miesiąca |
| Trend weekly nie sumuje się do monthly | Weekly rows są snapshotami, nie okresami do dodawania | Użyj `eco_driver_monthly_stats` albo `eco_driver_monthly_trends_view` dla miesięcznych totals |

Ostrzeżenie — przeliczanie okresów historycznych po wprowadzeniu kontraktu „tylko QUALIFIED w rankingu":

`recalculate=true` **usuwa** wiersze statystyk danego zakresu (`_delete_existing_stats`) i wylicza je od nowa. To operacja nieodwracalna i dotyczy nie tylko wierszy niezakwalifikowanych:

- w starym kontrakcie wiersz `LOW_DISTANCE` z `ranking_included=true` trafiał do `INCLUDED` i zajmował pozycję, zawyżając `ranking_total_participants`;
- po przeliczeniu **zmieniają się `ranking_position` i `ranking_total_participants` wszystkich pozostałych, zakwalifikowanych kierowców w tym okresie**;
- dokładnie te wartości zostały już wysłane w e-mailach, a tabele `*_email_send_log` przechowują `ranking_type`, `qualification_status` i `ranking_included`, ale **nie** `ranking_position` ani `ranking_total_participants`;
- po przeliczeniu nie istnieje więc żaden zapis pozycji, którą faktycznie zakomunikowano odbiorcy. Odtworzenie wymaga point-in-time restore bazy klienta.

Dlatego przed przeliczeniem okresu, dla którego istnieją wiersze `status='sent'`:

1. wykonaj i zweryfikuj kopię/snapshot dotkniętych wierszy statystyk;
2. uzyskaj jawną autoryzację dla konkretnego okresu — to nie jest rutynowe czyszczenie;
3. dopiero po przeliczeniu **wszystkich** starych okresów w danej tabeli uruchom `VALIDATE CONSTRAINT chk_<tabela>_ranking_requires_qualified`. Uwaga: `VALIDATE` działa na całej tabeli, nie na okresie — po przeliczeniu pojedynczego miesiąca nadal będzie zgłaszać naruszenie, dopóki istnieje choć jeden nieprzeliczony okres. Sukces walidacji jest dowodem, że w tabeli nie ma już rankowanego wiersza bez kwalifikacji.

Okresy policzone przed zmianą kontraktu mają `ranking_group IS NULL` nigdzie, więc raportowany `not_ranked_count` wynosi dla nich `0` — nie dlatego, że nie było kierowców poniżej progu, lecz dlatego, że trafiali oni wtedy do `INCLUDED`/`EXCLUDED`. Nie interpretuj `Not ranked = 0` dla starych okresów jako braku niezakwalifikowanych kierowców.

Uwaga o licznikach speeding:

- Liczenie jest na poziomie wiersza telemetrycznego: każdy wiersz z `/vehicles/events` z `speed >= 140`, który pasuje do rejestracji tripów i mieści się w tripie, liczy się jako osobne naruszenie.
- Job nie grupuje po timestampie i nie deduplikuje wierszy z tą samą rejestracją, timestampem ani prędkością; timestampy różniące się nawet o 1 sekundę także liczą się osobno.
- Buckety są zapisywane do `speeding_140_160_count` (`speed >= 140 AND speed < 160`), `speeding_160_170_count` (`speed >= 160 AND speed < 170`) i `speeding_170_plus_count` (`speed >= 170`).
- `/trips.max_speed` nie jest używany do dokładnego liczenia bucketów.
- `/vehicles/events` jest pobierane w adaptive chunkach czasowych z domyślnym startem `4h`, minimum `30m` i `limit<=1000`; eventy są filtrowane lokalnie po rejestracjach z tripów i `speed >= 140`.
- Po timeout/provider failure job zmniejsza chunk (`4h → 2h → 1h → 30m` przy domyślnych wartościach), po udanym mniejszym chunku kontynuuje tym rozmiarem do końca runu, a non-final granice requestów cofa o 1 sekundę, żeby nie podwajać eventów na inkluzywnych timestampach.
- Jeśli fleet-wide chunk nadal failuje na minimum przez timeout/retry exhaustion albo HTTP 500, a `TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK=true`, job uruchamia fallback per rejestracja dla **tego exact chunka**. Fallback używa `GET /vehicles/events` z query param `registration=<registration>`; nie używa per-path endpointów.
- Fallback jest all-or-nothing: dane są zwracane do dalszego liczenia dopiero po sukcesie wszystkich rejestracji. Jeśli jedna rejestracja failuje dla dużego fallback window, dzielone jest tylko to registration/window do `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES`. Nierozwiązany błąd kończy run przed DB upsert.
- `GET /trips` jest zawsze dzielone przez job na chunki (`chunk_days`, default `2`, cap `5`); dispatcher/scheduled runy dziedziczą ten default, więc bez jawnego parametru wysyłają requesty maks. 2-dniowe, a nawet przy override nie mogą wysłać jednego requestu `/trips` większego niż 5 dni.
- Twardy cap `max_pages=500` per adaptive chunk `/vehicles/events`, `TELEMATICS_PROVIDER_PAGE_LIMIT` dla standardowej paginacji (`/trips`, `/vehicles`), globalny provider safety budget, backoff retry (`5s`, potem `15s` przy domyślnym retry config) oraz rate limit zapobiegają nieograniczonej paginacji i burstom API; osiągnięcie page capu oznacza niekompletne event enrichment i fail przed DB upsert; `TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT`, `TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY`, `TELEMATICS_EVENTS_CHUNK_HOURS`, `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES`, `TELEMATICS_EVENTS_TIMEOUT_S`, `TELEMATICS_EVENTS_RATE_LIMIT_RPS` oraz `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_*` mogą nadpisać wartości dla `/vehicles/events`.
- Alternatywa operacyjna: `TELEMATICS_EVENTS_ENRICHMENT_MODE=audited_best_effort` albo parametr joba `{"event_enrichment_mode":"audited_best_effort"}`. Ten tryb zachowuje udane fleet/registration subwindows, zapisuje unresolved gaps jako audit evidence, uploaduje artefakt `VEHICLE_EVENTS_GAP_AUDIT` i wykonuje DB upsert z `event_enrichment_status=partial` oraz `complete_event_enrichment=false`. Używaj go tylko wtedy, gdy biznes akceptuje częściowe speeding/RPM counts z jawnym audytem gapów.
- Operator-safe disable: parametr joba `{"event_enrichment_mode":"disabled"}` pomija wszystkie requesty `/vehicles/events`, nie jest partial failure i zapisuje zera dla event-derived liczników (`high_rpm_events_count`, `overrev_events_count`, `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`). Nie używaj legacy `skip_vehicle_events=true` do tego celu; ten parametr pozostaje fatal przed DB upsert.
- Dla cyklicznych runów dispatchera ustaw `workflow_a_control.client_dataset_schedule.event_enrichment_mode='disabled'` dla row `dataset_name='trips_sync'`; dispatcher przekaże tę wartość jako parametr joba. Existing/default rows mają `enabled`, więc zachowują pobieranie `/vehicles/events`.

Uwaga o HIGH_RPM / OVERREV:

- Źródło liczników: provider-labeled fleet-wide `/vehicles/events`, ten sam fetch co dla speeding, ale bez filtra `speed >= 140`.
- Rozpoznawane etykiety obejmują m.in. `OVERREV_START`, `OVERREV_END`, `OVERREV`, `OVER_REV`, `OVER REV`, `HIGH_RPM_START`, `HIGH_RPM_END`, `HIGH_RPM`, `HIGH RPM`, `HIGH-RPM`.
- `*_START` oraz niesufiksowane etykiety liczą jako zdarzenie; `*_END` jest rozpoznawane i ignorowane, żeby nie podwajać jednego provider incidentu.
- Przypisanie do tripu używa `vehicle_id` albo znormalizowanej `registration` oraz `event_ts` w `[trip.start_timestamp, trip.end_timestamp]`.

### 5.2.1 Workflow B — ALPHA00001 Alpha GPS XLSM import

Kanoniczna ścieżka operacyjna:

1. VBA aktualizuje `GPS_baza_START_skrypt.xlsm` na laptopie Windows.
2. Outlook wysyła bieżący XLSM z `owner@example.invalid` do skrzynki Workflow B.
3. Stage 1 pobiera email i zapisuje raw XLSM oraz normalized CSV ze wszystkich niepustych arkuszy workbooka.
4. Stage 2 wykrywa `Alpha_GPS_Baza_LOG`, czyści sekcję outputu LOG i zapisuje cleaned artifact.
5. Stage 3 replace-all ładuje `alpha_main.telematics_reports."Alpha_GPS_Baza_LOG"`.

Manualny przebieg dla pojedynczego pliku po pobraniu emaila:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.mail.fetch_reports '{"since_days":7}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage2.job_stage2 '{"limit":20}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"raw_file_id":"<uuid>","dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"raw_file_id":"<uuid>"}'
```

Przed ręcznym Stage 3 nie zgaduj starego `raw_file_id`. Ustal aktualny lineage po nazwie załącznika:

```bash
PYTHONPATH="$PWD" python3 ops/diagnose_workflow_b_file_lineage.py \
  --filename GPS_baza_START_skrypt.xlsm \
  --report-type Alpha_GPS_Baza_LOG \
  --client-code ALPHA00001
```

Skrypt pokazuje wiersze `ingest.raw_file`, artifact rows dla każdego kandydata, czy normalized CSV zawiera sekcje `GPS_baza_START` / `LOG` / `Status_Prywatnosci`, dokładny wynik lookupu Stage 3 dla cleaned artifactu oraz status policy i tabeli docelowej.

Weryfikacja targetu:

```sql
SELECT source_sha256, count(*)
FROM telematics_reports."Alpha_GPS_Baza_LOG"
GROUP BY source_sha256;
```

### 5.2.1a Deprecated direct ALPHA00001 Alpha GPS XLSM import

Ten job importuje zapisany skoroszyt z laptopa operatora. Serwer nie uruchamia
VBA i nie modyfikuje pliku źródłowego; konsumuje tylko zsynchronizowany XLSM.
Jest zachowany tylko jako awaryjna/manualna kompatybilność starej ścieżki
`source_path`; nie jest kanonicznym flow operacyjnym.

Przed pierwszym uruchomieniem zastosuj client-business DDL do bazy Alpha
(`alpha_main`), tak jak inne migracje baz klientów:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --list
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --client-id <ALPHA00001_CLIENT_ID> --apply
```

Wymagany plik migracji: `db/client_business/022_alpha_gps_baza_log.sql`.
Tworzy:

- `telematics_reports."Alpha_GPS_Baza_LOG"` — target replace-all,
- `telematics_reports.alpha_gps_baza_log_import_runs` — historia importów i deduplikacja po udanym `source_sha256`.

Manualny dry-run, bez zmian w tabelach klienta:

```bash
cd /opt/log-platform
PYTHONPATH="$PWD" python3 ops/runner.py jobs.alpha.import_gps_baza_log_xlsm '{
  "source_path": "/home/logplatform/data/alpha/GPS_baza_START_skrypt.xlsm",
  "sheet_name": "LOG",
  "dry_run": true,
  "trigger": "MANUAL"
}'
```

Realny import:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.alpha.import_gps_baza_log_xlsm '{
  "source_path": "/home/logplatform/data/alpha/GPS_baza_START_skrypt.xlsm",
  "sheet_name": "LOG",
  "trigger": "MANUAL"
}'
```

Parametry operacyjne:

- `source_path` — wymagany path do zsynchronizowanego XLSM na serwerze,
- `sheet_name` — default `LOG`,
- `client_code` — default `ALPHA00001`; job znajduje `alpha_main` przez `workflow_a_control.client_account`,
- `force` — default `false`; przy `true` ponownie wykonuje replace-all dla tego samego `sha256`,
- `dry_run` — default `false`; waliduje workbook bez `DELETE`/`INSERT` i bez wpisu historii importu.

Zachowanie:

- jeśli ten sam `sha256` ma już udany import, realny run bez `force=true` kończy się skipem,
- przy realnym imporcie job najpierw tworzy `RUNNING` w historii, potem waliduje XLSM, a `DELETE` targetu, `INSERT` nowych wierszy i `SUCCESS` historii wykonuje w jednej transakcji,
- nazwa tabeli targetu jest mixed-case; w SQL zawsze cytuj ją jako `telematics_reports."Alpha_GPS_Baza_LOG"`,
- job uploaduje `import_summary` JSON i przy udanym realnym imporcie kopię XLSM jako artifact platformowy.

Weryfikacja w bazie klienta:

```sql
SELECT source_sha256, count(*)
FROM telematics_reports."Alpha_GPS_Baza_LOG"
GROUP BY source_sha256;

SELECT import_run_id, status, source_sha256, rows_loaded, started_at, finished_at, error_message
FROM telematics_reports.alpha_gps_baza_log_import_runs
ORDER BY started_at DESC
LIMIT 10;
```

Manualne testy bez DB:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/tests_manual/test_alpha_gps_baza_log_xlsm_import.py
```

### 5.4 Phase 2 — twarde limity bezpieczeństwa (Telematics HTTP)

**Cel:** zapobiec przypadkowej pętli żądań lub runaway pagination, które mogłyby zablokować lub wyczerpać quota/token API u klienta. Ochrona jest **w kodzie** (nie polega na „mniejszym oknie” jako głównym zabezpieczeniu).

**Domyślne limity** (nadpisywalne ENV — pełna tabela: `docs/02_infrastructure.md`):

- `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN` (default `500`)
- `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT` (default `300`)
- `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW` (default `80`)
- `TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW` (default `50`)
- `TELEMATICS_PROVIDER_MAX_RETRIES` (default `2`)
- `TELEMATICS_PROVIDER_TIMEOUT_S` (default `60`)
- `TELEMATICS_PROVIDER_PAGE_LIMIT` (default `1000`)
- `TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT` (default `1000`, capped at `1000`)
- `TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY` (default `500`, per adaptive chunk)
- `TELEMATICS_EVENTS_CHUNK_HOURS` (default `4`)
- `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES` (default `30`)
- `TELEMATICS_EVENTS_TIMEOUT_S` (default: `TELEMATICS_PROVIDER_TIMEOUT_S`)
- `TELEMATICS_EVENTS_RATE_LIMIT_RPS` (default `2.5`)
- `TELEMATICS_EVENTS_ENRICHMENT_MODE` (default effective mode `enabled` with strict fetch strategy; override paramem `event_enrichment_mode`; legacy values `strict` and `audited_best_effort` remain accepted)
- `TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK` (default `false`)
- `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS` (default `1.0`)
- `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS` (default `1500`)
- `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS` (default `1`)
- `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN` (default: existing global provider budget; set explicitly to allow larger ALPHA-style fallback)
- `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES` (default `5`)
- `TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES` (default `5`)
- `TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES` (default `5`)
- `TELEMATICS_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN` (default `1000`)
- `TELEMATICS_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH` (default `8`)

Rekomendowany start dla ALPHA (`client_id=9536f715-2fd0-4ffd-86ed-ba06f5490c5e`) przy znanym pojedynczym 5-min fleet-wide HTTP 500:

```bash
TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK=true
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS=1
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS=1500
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS=1
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN=2000
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES=5
```

Szacowanie czasu fallbacku: `registrations_count / fallback_rps`. Dla 1349 rejestracji to minimum ok. `22.5 min` przy `1 req/s` albo `11.25 min` przy `2 req/s`, bez czasu retry/subchunków/paginacji. Fallback działa tylko dla nieudanego chunka, nie dla całego dnia.

Tryb audited best-effort dla awaryjnej pracy ALPHA, jeśli strict nadal blokuje output przez provider 500 i operator akceptuje partial counts:

```bash
TELEMATICS_EVENTS_ENRICHMENT_MODE=audited_best_effort
TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK=true
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS=1
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS=1500
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS=1
TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN=2000
TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES=5
TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES=5
TELEMATICS_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN=1000
TELEMATICS_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH=8
```

W tym trybie raporty/trips są aktualizowane z dostępnymi eventami, ale każdy raport operacyjny musi pokazywać `event_enrichment_status=partial`, `complete_event_enrichment=false`, `event_gap_count`, affected registrations i `total_gap_duration_seconds`.

Monitorowanie postępu w logach platformy:

- `Phase start: trips fetch`, `Phase start: vehicle inventory fetch`, `Phase start: fleet events fetch`, `Phase start: DB upsert`
- `Fleet vehicle events adaptive chunk fetched` z `chunk_index`, `chunk_total_estimated`, `chunk_start_ts`, `chunk_end_ts`, `pages_fetched`, `records_fetched`, `source`
- `Registration fallback start for failed fleet vehicle-events chunk` z `registrations_count`, `fallback_rps`, `estimated_requests`, `estimated_min_duration_seconds`
- `Registration fallback progress` z `completed`, `total`, `percent`, `successful`, `failed_so_far`, `elapsed_seconds`, `estimated_remaining_seconds`, `current_registration`
- `Registration fallback subchunk progress` dla rejestracji dzielonych na mniejsze okna
- finalne summary z `fleet_chunks_successful`, `fallback_chunks_successful`, `fallback_requests`, `fallback_events_fetched`, `complete_event_enrichment=true`
- w `audited_best_effort`: `Audited best-effort vehicle-events gap recorded`, `Audited best-effort registration recovery start/progress/complete`, `Vehicle event gap audit artifact uploaded` oraz finalne pola `event_enrichment_status`, `event_gap_count`, `fleet_gap_count`, `registration_gap_count`, `affected_registrations_count`, `affected_registrations_sample`, `total_gap_duration_seconds`, `events_fetched_total`, `events_fetched_from_fleet`, `events_fetched_from_registration_fallback`, `speeding_rpm_counts_are_partial`

Artefakt gap audit:

- kind: `VEHICLE_EVENTS_GAP_AUDIT`
- nazwa pliku artefaktu: `vehicle_events_gap_audit.json`; lokalnie zapisywany pod `/tmp/vehicle_events_gap_audit_<run_id>/vehicle_events_gap_audit.json` (albo pod katalogiem z `LOG_PLATFORM_ARTIFACT_TMP_DIR`)
- format: JSON z `summary` oraz pełną listą `gaps`
- każdy gap zawiera `scope`, `registration`, `chunk_start_ts`, `chunk_end_ts`, `duration_seconds`, `endpoint`, `mode`, `failure_code`, `status_code`, `response_body_summary`, `attempts`, `split_depth`, `min_chunk_minutes`

Do diagnozy HTTP 422 z `/vehicles/events` użyj:

`PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/diagnose_telematics_vehicle_events.py --client-id <uuid>`

**Jak poznać safety-stop w logach platformy:**

- Poziom **ERROR**, wiadomość w stylu `Telematics provider safety stop: <abort_code>` lub logi `telematics_provider_*` z kontekstem.
- Pola kontekstu m.in.: `abort_code`, `phase` (`fetch_trips` / `fetch_vehicle_events` / `fetch_notifications`), `endpoint`, `sub_window`, `limit`, `total_requests`, `page`, `meta_current_page`, `meta_last_page`.

**Przykładowe `abort_code` (niepełna lista):** `MAX_REQUESTS_PER_RUN`, `MAX_REQUESTS_PER_ENDPOINT`, `MAX_REQUESTS_PER_SUBWINDOW`, `MAX_PAGES_PER_SUBWINDOW`, `HTTP_RETRY_EXHAUSTED`, `PAGINATION_LOOP`, `PAGINATION_MISMATCH`, `PAGINATION_NON_PROGRESS`, `MALFORMED_RESPONSE`, `MALFORMED_PAGINATION`, `INCONSISTENT_PAGINATION`, `HTTP_ERROR`.

**`abort_code` compatibility paginacji `/trips` (C7, wdrożone produkcyjnie `2026-08-03`).** Te kody może wyemitować wyłącznie run z `trips_pagination_mode = 'data_invariants_v1'` na endpoincie `/trips`; ścieżka `strict_meta` i pozostałe endpointy nie mogą ich wyprodukować. Ponieważ `BRAVO00016` jest jedynym klientem compatibility, wystąpienie któregokolwiek z tych kodów dla innego klienta jest incydentem. Produkcyjne recovery C11 z `2026-08-03` nie wyemitowało **żadnego** z nich. Kontekst jest sanityzowany: liczności, skrócone digesty tożsamości i skalary budżetów — nigdy surowe `trip_id`, rejestracje, adresy, współrzędne, payloady ani nagłówki autoryzacji.

| `abort_code` | Znaczenie | Pierwsza reakcja operatora |
|---|---|---|
| `PAGINATION_COMPAT_IDENTITY_MISSING` | Wiersz `/trips` bez użytecznego `trip_id` (`identity_defect=missing`) albo z `trip_id` bool/niecałkowitym/nieprzeliczalnym (`identity_defect=malformed`) | Wyłącz klienta do `strict_meta`, eskaluj do providera |
| `PAGINATION_COMPAT_DUPLICATE_IN_PAGE` | Duplikat tożsamości wewnątrz jednej strony | Wyłącz, eskaluj |
| `PAGINATION_COMPAT_PAGE_OVERLAP` | Tożsamość widziana już na **dowolnej** wcześniejszej stronie sub-okna | Wyłącz, eskaluj; podejrzenie mutacji po stronie providera |
| `PAGINATION_COMPAT_PAGE_REPEATED` | Powtórzony uporządkowany fingerprint (`repeat_kind=ordered_fingerprint`) albo ten sam zbiór tożsamości w innej kolejności (`repeat_kind=unordered_identity_set`) | Wyłącz, eskaluj |
| `PAGINATION_COMPAT_ROWS_EXCEED_LIMIT` | `len(data) > requested_limit` — kanał danych przestał respektować `limit` | Wyłącz, eskaluj |
| `PAGINATION_COMPAT_SHAPE_UNSTABLE` | `meta` zmieniło typ JSON między stronami sub-okna | Eskaluj |
| `PAGINATION_COMPAT_TOTAL_INVALID` | Obecny `total` nie jest nieujemnym integerem JSON | Eskaluj |
| `PAGINATION_COMPAT_TOTAL_UNSTABLE` | `total` różni się między stronami albo pojawia się/znika w trakcie sub-okna | Powtórz okno później; przy nawrotach rozszerz opóźnienie stabilizacji |
| `PAGINATION_COMPAT_TOTAL_EXCEEDED` | Zakumulowane unikalne wiersze przekraczają obecny `total` (w tym każdy wiersz przy `total = 0`) | Wyłącz, eskaluj |
| `PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED` | Terminacja na krótkiej/pustej stronie, ale `total` jest wyższy niż zakumulowane wiersze | **Zatrzymaj rollout** — hipoteza short-page jest fałszywa dla tego okna |
| `PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED` | Limit wierszy sub-okna (`TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW`) | Zawęź okno / zwiększ granularność chunków |
| `PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED` | Limit bajtów pojedynczej odpowiedzi (`scope=response`) albo całego sub-okna (`scope=sub_window`) | Zmniejsz `limit`, zbadaj wzrost payloadu |
| `PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED` | Limit czasu zegarowego sub-okna (`TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S`) | Powtórz poza szczytem |
| `PAGINATION_COMPAT_CONFIG_INVALID` | Błędna konfiguracja budżetów `TELEMATICS_PROVIDER_COMPAT_*`; walidowana **przed** pierwszym requestem, więc żadne żądanie nie zostało wysłane | Popraw ENV na hoście |

Kody `MAX_PAGES_PER_SUBWINDOW`, `MAX_REQUESTS_PER_*`, `PAGINATION_NON_PROGRESS`, `MALFORMED_RESPONSE`, `HTTP_ERROR` i `HTTP_RETRY_EXHAUSTED` zachowują dotychczasowe znaczenie i mogą wystąpić w obu trybach. `PAGINATION_COMPAT_WINDOW_INELIGIBLE` należy do C8 i **nie** jest jeszcze zaimplementowane.

Po safety-stop: run jest **FAILED**; **nie** wykonuj już kolejnych żądań do Telematics w tym uruchomieniu (job przerywa lokalnie).

### 5.4.1 Workflow A — `/trips` pagination diagnostic (operator tool)

**Purpose.** `ops/diagnose_telematics_trips_pagination.py` answers exactly one question about the `TELEMATICS_PAGINATION_PROVIDER_CONTRACT_CHANGED` incident: does the provider still apply the requested `page` parameter to the returned `data` while emitting contradictory `meta`? It compares page 1 against page 2 for one short window and reports a single classification. It is a diagnostic only — it is **not** a replacement for `jobs.api.telematics.sync_trips_and_speeding`, it never ingests anything, and it must not be used to work around a `PAGINATION_MISMATCH` safety stop. The production client keeps refusing the broken contract; that behavior is unchanged and must stay unchanged.

**What it never touches.** No `ops/runner.py`, no dispatcher, no schedule claim, no `run_context`, no platform run, no client-business write, no artifact upload, no email, no systemd. Its only outbound call is `GET /trips`. The one platform read it performs is a single read-only `SELECT` on `workflow_a_control.client_account` to resolve the provider base URL, username and password **reference**, and it happens only in live mode.

**Strict safety limits** (the tool refuses execution rather than bending any of them):

- method pinned to `GET`, endpoint pinned to `/trips`;
- client allowlist is `DELTA00001` only — every other code is rejected, with no fallback;
- exactly the requested pages are fetched, at most two, in the order given; there is no page loop and no inferred default page;
- a live run must request exactly two pages, otherwise the comparison is meaningless;
- `1 <= limit <= 1000`;
- window must be positive and at most **one hour**;
- no retry by default; `--allow-timeout-retry` permits at most **one** retry and only for a transport-level timeout — never for pagination or response-shape anomalies;
- request budget is at most 2 requests, or 3 when the single timeout retry fires, checked **before** every request;
- redirects are never followed; a cross-host `Location` is a hard stop (`REDIRECT_CROSS_HOST`);
- the response body is streamed under a hard byte cap (`--max-response-bytes`, default 32 MiB, ceiling 64 MiB);
- the evidence directory must live outside the repository working tree and must not already contain a bundle.

**Dry-run is the default.** Without `--allow-live-request` the tool stops before any socket is opened: it resolves no credentials, reads no control-plane row, and writes only the request plan. The classification in that mode is always `TELEMATICS_TRIPS_DIAGNOSTIC_DRY_RUN_READY`. Live execution requires the explicit `--allow-live-request` flag; there is no environment variable or config that can enable it implicitly.

**Result classifications** (exactly one per execution): `TELEMATICS_TRIPS_DIAGNOSTIC_DRY_RUN_READY`, `TELEMATICS_TRIPS_PAGES_DISTINCT_METADATA_BROKEN`, `TELEMATICS_TRIPS_PAGE_PARAMETER_IGNORED`, `TELEMATICS_TRIPS_PAGES_PARTIALLY_OVERLAP`, `TELEMATICS_TRIPS_PAGE_2_EMPTY`, `TELEMATICS_TRIPS_IDENTITY_CONTRACT_UNRESOLVED`, `TELEMATICS_TRIPS_PROVIDER_RESPONSE_MALFORMED`, `TELEMATICS_TRIPS_DIAGNOSTIC_TRANSPORT_FAILURE`, `TELEMATICS_TRIPS_DIAGNOSTIC_SAFETY_BLOCKED`.

**Evidence redaction guarantees.** The bundle (`request_plan.json`, `page_1_summary.json`, `page_2_summary.json`, `cross_page_comparison.json`, `diagnostic_summary.json`, `MANIFEST.txt`, `SHA256SUMS`) is written `0600` inside a `0700` directory and contains only sanitized data:

- no raw trip rows and no raw response body — response bytes are held in memory, parsed, summarized and dropped;
- no username, API key, `Authorization` header, cookie or credential-bearing URL; only the sanitized origin plus `/trips` is recorded, and any URI userinfo is stripped even if a base URL carried it;
- no registration, driver name, address, coordinates or per-trip timestamp;
- trip identity comes from the existing ingestion business key `provider_trip_id` (`int(row["trip_id"])`, the same value `client_trips` is keyed on) and is written **only** as an HMAC-SHA256 digest under a 32-byte salt generated per execution, kept in memory and never persisted. The same salt is used for both pages so page 1 and page 2 stay comparable inside one run, while digests are not reproducible across runs or by an outside observer;
- `SHA256SUMS` is `sha256sum -c` compatible and covers every other file including `MANIFEST.txt`.

**Example dry-run command** (safe; opens no connection):

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/diagnose_telematics_trips_pagination.py \
  --client-code DELTA00001 \
  --start-timestamp "2026-07-28 16:00:00" \
  --end-timestamp "2026-07-28 17:00:00" \
  --pages 1,2 \
  --limit 1000 \
  --output-dir /var/tmp/telematics-pagination-evidence/<UTC timestamp>
```

Review the printed plan and `request_plan.json`, then re-run the identical command with `--allow-live-request` appended. Live execution is a separate operator decision and consumes 2 provider requests (3 only if `--allow-timeout-retry` is set and a timeout actually occurs).

Focused tests (no network, no secrets):

```bash
PYTHONPATH="$PWD" python3 ops/tests_manual/test_telematics_trips_pagination_diagnostic.py
```

### 5.5 Workflow A — dispatcher

Dispatcher jest zaimplementowany jako standardowy job:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.dispatcher '{}'
```

Wymagania:

- migracje platformowe do `032_workflow_a_eco_driving_registry.sql` zastosowane przez `bash ops/db_migrate.sh`,
- enabled rows w `workflow_a_control.client_dataset_schedule`,
- po `017_workflow_a_add_client_code_to_control_tables.sql` wiersze schedule/history/retention mają denormalizowany `client_code`; migracja backfilluje go z `workflow_a_control.client_account`, gdy jest znany,
- po `018_workflow_a_schedule_event_enrichment_mode.sql` wiersze `client_dataset_schedule` mają `event_enrichment_mode NOT NULL DEFAULT 'enabled'`; dla `trips_sync` można ustawić `disabled`, żeby scheduled run pominął `/vehicles/events`,
- po `032_workflow_a_eco_driving_registry.sql` w registry istnieją Eco Driving dataset rows i default-disabled schedule rows dla `eco_driving_weekly_snapshot`, `eco_driving_month_end_weekly_snapshot`, `eco_driving_monthly_aggregation`,
- zgodność `dataset_name` / `job_module` z `jobs/api/telematics/registry.py`,
- środowisko procesu dispatchera zawiera `LOG_API_URL`, tokeny API oraz sekrety wymagane przez uruchamiane dataset joby.
- opcjonalnie `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES` (default `720`) albo parametr `{"stale_running_timeout_minutes": ...}` dla auto-fail starych `RUNNING` rows.

Przykład schedule dla `trips_sync` bez pobierania `/vehicles/events`:

```sql
UPDATE workflow_a_control.client_dataset_schedule
   SET enabled = true,
       run_time = '03:30',
       lookback_days = 2,
       event_enrichment_mode = 'disabled'
 WHERE client_id = '<uuid>'
   AND dataset_name = 'trips_sync';
```

Zachowanie:

- tick odpala najwyżej jeden due dataset,
- najpierw próbuje przejąć globalny Postgres advisory lock; jeśli lock jest zajęty, kończy tick bez błędu,
- przed sprawdzeniem aktywnych runów oznacza `RUNNING` rows starsze niż timeout jako `FAILED` z `error_summary='stale RUNNING auto-failed by dispatcher...'`,
- jeśli istnieje `client_schedule_run_history.status='RUNNING'`, tick kończy się bez uruchomienia nowego joba,
- due window jest liczone jako `scheduled_fire_ts - lookback_days` → `scheduled_fire_ts`,
- subprocess dostaje `trigger="SCHEDULED"` oraz `client_code`, jeśli klient ma ustawiony kod; dla `trips_sync` dostaje też `event_enrichment_mode` ze schedule row,
- dla Eco Driving datasetów dispatcher przekazuje `mode` oraz `include_weekly` / `include_monthly`: weekly snapshot używa latest completed cumulative boundary, month-end weekly snapshot używa `previous_month_start -> current_month_start`, monthly aggregation używa previous full calendar month,
- `platform_run_id` jest uzupełniany na podstawie `LOG_PLATFORM_RUN_ID_FILE` zapisanego przez runner subprocessu,
- oczekiwany tick bez claimable fire jest cichym sukcesem: nie tworzy technicznego `runs`, logów aplikacyjnych ani schedule-history; brak pięciominutowych no-op rows jest poprawny i nie oznacza, że timer przestał działać,
- skuteczny claim nadal tworzy pełny audit dispatchera i child joba, a planning/DB/claim/execution failure nadal tworzy widoczny `FAILED`/`ERROR`,
- systemd zapisuje własne wpisy start/stop oneshot w journald także dla cichego no-op ticka; te wpisy nie są tabelą `logs` platformy.

Proponowany timer:

```bash
sudo cp ops/systemd/proposed/log-job@dispatcher.service /etc/systemd/system/log-job@dispatcher.service
sudo cp ops/systemd/proposed/log-job@dispatcher.timer /etc/systemd/system/log-job@dispatcher.timer
sudo systemctl daemon-reload
sudo systemctl start log-job@dispatcher.service
sudo systemctl enable --now log-job@dispatcher.timer
```

Szczegóły i ograniczenia: `docs/10_scheduler_design.md` oraz `ops/systemd/proposed/log-job@dispatcher.README.md`.

#### 5.5.1 Triage: Telematics coverage gate, finalizacja C6 i bramka wyniku M3

**Kontekst — zaktualizowany `2026-08-14`.** Te kody może wyemitować wyłącznie scheduled fire `trips_sync` dla klienta z `trips_pagination_mode = 'data_invariants_v1'`. Runtime C6 oraz migracje `055`–`057` są wdrożone produkcyjnie, a sufit migracji to `057_workflow_a_trips_coverage_state.sql`.

> ~~Wcześniejszy zapis: „`BRAVO00016` jest **jedynym** klientem `data_invariants_v1`… jakiekolwiek wystąpienie tych kodów dla innego klienta jest incydentem do eskalacji.”~~ **Nieaktualne.** Był prawdziwy `2026-08-03`, gdy canary był uzbrojony i jeszcze niewykonany. Dziś `data_invariants_v1` jest normą, a nie wyjątkiem.

Stan bieżący: **czterej** klienci mają włączony `trips_sync` w trybie `data_invariants_v1` — `DELTA00001` (daily, `02:00 Europe/Warsaw`, `L = 7`), `ALPHA00001` (daily, `02:00 UTC`, `L = 3`), `FOXTROT00001` (daily, `02:00 UTC`, `L = 1`) i `BRAVO00016` (weekly, poniedziałek `02:00 Europe/Warsaw`, `L = 7`). `ECHO00001` pozostaje wyłączony (`enabled = false`) i jego coverage (`covered_through_source = manual_recovery`) nie może się przesuwać. Każdy z czterech ma wiersz coverage w `READY`, a trzej dzienni klienci przesuwają `W` codziennie. **Wystąpienie tych kodów dla dowolnego z czterech nie jest już samo w sobie anomalią konfiguracyjną** — należy je triagować normalnie, według tabel poniżej. Anomalią pozostaje: kod dla `ECHO00001`, kod dla klienta bez wiersza coverage, oraz **ten sam kod bramki wyniku u wielu klientów naraz**, co wskazuje na defekt współdzielonej ścieżki, a nie na pojedynczy nieudany run.

**Bramka wyniku M3 jest od `2026-08-13T10:59:21Z` żywym zachowaniem produkcyjnym** (release `fabaaa753f89`) i została potwierdzona `2026-08-14` na trzech naturalnych fire'ach. `returncode == 0` **nie wystarcza** do przesunięcia coverage: dispatcher musi dodatkowo przyjąć terminalny rekord procesu potomnego. Pełna specyfikacja bramki jest w sekcji „Bramka finalizacji coverage” niżej w tym dokumencie; kody odmowy w tabeli poniżej.

**Co oznaczają.** Gate reject występuje **przed** uruchomieniem joba. Dla bootstrap-required oraz istniejącego/malformed gapu coverage pozostaje bez zmian. Nowy rozłączny `READY` jest po durable claimie i przed launch atomowo utrwalany jako `GAP_DETECTED` razem z historią `FAILED`; `last_gap_detected_ts` i coverage `updated_at` dostają ten sam pełnosekundowy UTC timestamp. Żaden subprocess, sekret, provider ani client-business access nie występuje.
Dla wiersza ze statusem `GAP_DETECTED` sprawdź najpierw identity, obie pełnosekundowe aware granice i ich porządek/future-W, niepuste evidence oraz obecne aware `seeded_at` i niepusty `seeded_by`. Dopiero kompletna struktura oznacza poprawnie zapisany gap. Brak któregokolwiek elementu oznacza malformed state i kod bootstrap-required, nie gap.

| Kod | Znaczenie |
|---|---|
| `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` | Nie istnieje wiarygodny zweryfikowany przedział: brak wiersza; identity mismatch; `UNINITIALIZED`, `RESEED_REQUIRED`, nieznany/`NULL` status; malformed `READY`; albo malformed wiersz, którego status mówi `GAP_DETECTED`, lecz granice, evidence lub seed metadata nie spełniają wspólnych wymagań integralności. Literalny status gapu sam nie jest dowodem. |
| `TRIPS_COVERAGE_GAP_DETECTED` | Poprawny `READY` ma rozłączne wyliczone okno (`requires_gap_persistence=true`) albo istnieje **strukturalnie poprawny** wcześniej zapisany `GAP_DETECTED` (`requires_gap_persistence=false`). Tylko taki zapisany gap powtarza ten kod na każdym kolejnym due fire. |

| `TRIPS_COVERAGE_ADVANCE_CONFLICT` | Po udanym subprocessie retained claim snapshot nie zgadza się z zablokowanym coverage lub CAS nie zmienił dokładnie jednego wiersza. Transakcja coverage jest cofana; osobny `FAILED` jest dozwolony tylko dla nadal `RUNNING`. |
| `TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT` | Przed launch zmienił się snapshot/CAS nowego gapu; gap nie został utrwalony, subprocess nie ruszył. |
| `TRIPS_HISTORY_CLAIM_LOST` | Claim history zniknął lub nie jest już `RUNNING` (np. stale sweep/manual unblock). Coverage nie jest mutowane, terminalna historia nie jest nadpisywana i nie ma replay. |
| `TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED` | Commit na pewno nie doszedł albo reconciliation wykazało dokładną parę początkową; guarded `FAILED` dotyczy tylko świeżo potwierdzonego `RUNNING`. |
| `TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE` | Krytyczna niemożliwa para history/coverage. Wstrzymać compatibility schedule, zachować dowody i nie wykonywać ręcznego advancementu. |
| `TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE` | Krytyczny brak świeżego read-only reconciliation po niepewnym `COMMIT`. Nie zapisywać blind `FAILED`; wiersz może pozostać `RUNNING` do stale sweep lub osobno autoryzowanej rekonsyliacji. |

**Kody odmowy bramki wyniku M3.** Wszystkie oznaczają dokładnie to samo skutkiem: `returncode == 0`, ale terminalny rekord nie uprawnia do przesunięcia coverage, więc fire kończy się `FAILED`, `W` **zostaje bez zmian**, i **nie wykonano żadnego SQL-a na coverage** — decyzja zapada zanim finalizer otworzy transakcję. Różnią się tylko tym, co powiedział dowód. Kod trafia do `client_schedule_run_history.error_summary` oraz do wiersza `ERROR` w `public.logs` (`source = jobs.api.telematics.dispatcher`) z polami `outcome_refusal_code` i `outcome_refusal_reason`.

| Kod | Znaczenie |
|---|---|
| `EXECUTION_OUTCOME_ABSENT` | Proces biznesowy nie zapisał terminalnego rekordu albo zapisał pusty. Job „się udał”, ale nie zostawił dowodu. |
| `EXECUTION_OUTCOME_UNREADABLE` | Rekord istnieje, ale nie dało się go odczytać (błąd I/O lub nieoczekiwany wyjątek przy odczycie). |
| `EXECUTION_OUTCOME_MALFORMED` | Rekord nie jest poprawnym JSON-em albo nie przechodzi ścisłego parsowania/kontroli wewnętrznej spójności (np. twierdzi commit bez wejścia do providera). |
| `EXECUTION_OUTCOME_IDENTITY_MISMATCH` i pokrewne kody `EXECUTION_OUTCOME_*` z `verify_outcome` | Rekord jest poprawny, ale opisuje **inne** wykonanie: inny klient, schedule, dataset, okno, inny lub brakujący `platform_run_id`, albo niesie `recovery_run_id` przy scheduled fire (rekord manual-recovery podstawiony pod scheduled claim). Kody z kontraktu są przekazywane bez remapowania, żeby odmowa nazywała rzeczywistą przyczynę. |
| `TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE` | Rekord zweryfikował się, ale nie jest coverage-eligible — `skipped`, brak `provider_execution_entered`, brak `business_transaction_entered` albo transakcja nie jest `COMMITTED`. Reason wypisuje wszystkie cztery pola. |
| `TRIPS_OUTCOME_NOT_COLLECTED` | Fire, którego sukces mógłby przesunąć coverage, w ogóle nie zażądał rekordu. Ścieżka nieosiągalna z gałęzi compatibility; utrzymana jako fail-closed, żeby przyszła edycja nie mogła po cichu przesunąć `W`. |
| `TRIPS_OUTCOME_GATE_FAILED` | Defekt **samej bramki** — nie mówi nic o tym, co zrobił proces potomny, tylko że dispatcher nie zdołał rozstrzygnąć. Też fail-closed. Traktować jako błąd oprogramowania, nie jako problem danych. |

**Pierwsza reakcja na odmowę bramki wyniku** jest taka sama jak dla pozostałych kodów w tej sekcji i nie wprowadza nowej procedury: zebrać dowody read-only, nie ponawiać, nie przesuwać `W` ręcznie, nie przełączać klienta na `strict_meta`. Odmowa kosztuje **jeden** dzień postępu watermarku i jest samonaprawialna — następny zwykły fire ponownie obejmie utracone okno dzięki `min(base, W − O)`, aż do `R`. Eskalować, gdy: ten sam kod pojawia się u wielu klientów naraz; ten sam klient odmawia w kolejnych fire'ach (dryf w stronę `GAP_DETECTED` po przekroczeniu `R`); albo kod to `TRIPS_OUTCOME_GATE_FAILED`. Do czasu M11 nie ma alertu na zastój coverage — wykrycie opiera się na tym runbooku, nie na automacie.

**Gdzie szukać dowodu przy udanym runie.** Dispatcher zapisuje przyjęty wynik w kontekście linii `Job finished SUCCESS (rc=0)`:

```sql
-- wyłącznie read-only
SELECT ts, run_id, context->>'client_code'      AS client_code,
       context->>'execution_outcome'            AS execution_outcome,
       context->>'execution_outcome_upserted_count' AS upserted,
       context->>'coverage_advanced'            AS coverage_advanced,
       context->>'run_history_id'               AS run_history_id,
       context->>'platform_run_id'              AS platform_run_id
  FROM public.logs
 WHERE source = 'jobs.api.telematics.dispatcher'
   AND context ? 'execution_outcome'
 ORDER BY ts DESC
 LIMIT 20;
```

Wartości uprawniające do przesunięcia coverage to `EXECUTED_COMMITTED` oraz `EXECUTED_ZERO_ROWS_COMMITTED`. Brak takiego wiersza dla fire'a zakończonego `SUCCESS` byłby niespójnością do eskalacji. Sam rekord terminalny **nie jest trwały** — powstaje pod ścieżką tymczasową na czas jednego uruchomienia i znika po jego zakończeniu; trwałym śladem jest właśnie ten kontekst logu plus wiersze `client_schedule_run_history` i `public.runs`. Trwałe dowody per-request i per-subwindow (`provider_request_log`, `subwindow_complete`) to **M4 i są żywe od release'u `2782550f8efe`**, aktywnego `2026-08-14T15:27:58Z` (`docs/20` §22, domknięcie produkcyjne §22.14).

Niepewny `COMMIT` zawsze używa świeżego połączenia i jednego read-only repeatable-read snapshotu history+coverage. Dozwolone klasy to expected committed success/gap, exact original pair, terminal claim mismatch, atomic divergence albo reconciliation unavailable. C6 nigdy nie wnioskuje coverage z danych klienta i nie replayuje business joba.
**Inspekcja (wyłącznie read-only).**

```sql
-- ostatnie odrzucone fire'y i ich klasyfikacja
SELECT scheduled_fire_ts, status, error_summary,
       nominal_window_start_ts, nominal_window_end_ts,
       stabilization_delay_seconds, overlap_seconds, trips_pagination_mode
  FROM workflow_a_control.client_schedule_run_history
 WHERE schedule_id = '<schedule_uuid>'
 ORDER BY scheduled_fire_ts DESC
 LIMIT 20;

-- aktualny stan roszczenia coverage
SELECT schedule_id, client_code, dataset_name,
       coverage_start_ts, covered_through_ts, bootstrap_status,
       (bootstrap_evidence_ref IS NOT NULL) AS evidence_present,
       seeded_at, seeded_by, covered_through_source,
       last_gap_detected_ts, updated_at
  FROM workflow_a_control.client_dataset_coverage
 WHERE schedule_id = '<schedule_uuid>';
```

Powiązany `suspected_bug` (jeden incydent na warunek, kolejne fire'y to occurrences): `PYTHONPATH="$PWD" python3 ops/inspect_suspected_bugs.py --incident-code TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`.

**Czego nie robić.**

- Nie wymuszać retry ani re-fire tego samego `scheduled_fire_ts` — `UNIQUE (schedule_id, scheduled_fire_ts)` czyni fire jednorazowym, a terminalne wiersze historii są immutable.
- Nie przełączać klienta na `strict_meta` „żeby przeszło” — to ukrywa dziurę w danych zamiast ją zamknąć.
- Nie zakładać, nie seedować, nie edytować ani nie kasować wiersza `client_dataset_coverage`, w tym nie przestawiać `bootstrap_status` na `READY` i nie przesuwać `covered_through_ts`.
- Nie próbować „naprawić” gapu przez recovery/backfill bez osobnej autoryzacji.
- Dla malformed `GAP_DETECTED` nie wykonywać wymuszonego retry, fallbacku do strict ani ad-hoc repair. Status string nie jest dowodem; użyć wyłącznie read-only inspection i eskalować do osobno zrecenzowanego bootstrapu/reseedu/repair.

**Eskalacja.** Najpierw rozróżnij poprawnie zapisany gap od zniekształconego wiersza, którego status jedynie mówi `GAP_DETECTED`. Dla poprawnego gapu zinwentaryzuj dziurę i przekaż recover-or-exclude oraz późniejszy reseed do osobnej autoryzacji. Ta sekcja nie autoryzuje SQL-a mutującego, seed SQL, mode-flip, recovery ani bootstrapu. C6 jest wdrożone przy produkcyjnym suficie `057`. Od `2026-08-03` istnieje dokładnie jeden wiersz coverage (`BRAVO00016` / `trips_sync`, `READY`) i `BRAVO00016` jest jedynym klientem `data_invariants_v1`; pozostali czterej klienci są strict i niezbootstrapowani. Pierwszy **zaplanowany** compatibility fire nadal nie nastąpił — jedyne dotychczasowe produkcyjne wykonanie ścieżki compatibility to ręczne recovery C11 z `2026-08-03`, które zamknęło zaległy przedział i przesunęło `W` do `2026-08-03T00:00:00Z`. Zamknięcie zaległego przedziału ma dokładnie jedną zrecenzowaną ścieżkę — ręczne recovery C11 (§ „Ręczne recovery compatibility”), które wymaga wiersza `READY`, a więc nie dotyczy stanu `GAP_DETECTED` opisanego wyżej.

#### Narzędzia bootstrapu coverage (C10 / C10-W)

Do 2026-08-02 procedura bootstrapu z `docs/13_…` §13 nie miała żadnej implementacji, a jedyną
alternatywą był ad hoc SQL — zabroniony. Teraz istnieją dokładnie dwie zrecenzowane powierzchnie.
**Obie zostały uruchomione na produkcji dokładnie raz, dla `BRAVO00016` / `trips_sync`**: tabela
coverage ma dokładnie jeden wiersz. Tego samego dnia, już po bootstrapie, `BRAVO00016` został
włączony jako **jedyny** klient `data_invariants_v1` (canary); pozostali czterej klienci pozostają
`strict_meta` i niezbootstrapowani, a fleet-wide compatibility jest nadal zabronione.

**1. Audyt (Gate 1, wyłącznie read-only)** — `ops/audit_telematics_coverage_bootstrap.py`.
Nie ma i nie będzie miał przełącznika wykonania; połączenie jest otwierane jako read-only, więc
zapis odrzuca sam PostgreSQL. Zbiera konfigurację schedule/klienta, całą historię fire'ów,
**wyliczone brakujące fire'y** (jedyny sposób, by dziura bez wiersza stała się widoczna), terminalne
`FAILED`, evidenced intervals oraz istniejący wiersz coverage. Emituje kanoniczny bundle JSON
(`0600` w katalogu `0700` poza drzewem repozytorium) z SHA-256 liczonym nad postacią kanoniczną
z wyłączeniem samego pola `bundle_sha256`. Klasyfikacje: `COMPLETE_INTERVALS_INVENTORIED`,
`UNRESOLVED_GAPS_PRESENT`, `EXISTING_COVERAGE_PRESENT`, `AMBIGUOUS_SCHEDULE`,
`INSUFFICIENT_HISTORY_EVIDENCE`. **Narzędzie raportuje fakty i niczego nie rekomenduje** — nie
proponuje `A`, nie proponuje `W` i nigdy nie orzeka `READY`.

```bash
PYTHONPATH="$PWD" python3 ops/audit_telematics_coverage_bootstrap.py \
  --client-code <CODE> --dataset trips_sync \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629 \
  --output /var/lib/log-platform/coverage-bootstrap/<CODE>.json
```

**2. Writer (Gate 6 krok 5, dry-run first)** — `ops/bootstrap_telematics_trips_coverage.py`.
To jedyny autoryzowany `INSERT` coverage w repozytorium. Domyślnie wypisuje wyłącznie `DRY_RUN`;
zapis wymaga **jednocześnie** `--execute` oraz `--confirm-client-code` równego `--client-code`.
Przed jakąkolwiek transakcją zapisu przelicza kanoniczny SHA-256 bundla i wymaga zgodności z jego
własnym hashem i z `--evidence-sha256`, sprawdza wersję kontraktu semantyki bootstrapu, tożsamość
(environment, platform UUID, client, dataset, schedule), akceptowalną klasyfikację audytu oraz
ograniczony maksymalny wiek dowodu (14 dni).

`A` i `W` podaje **wyłącznie operator**. Narzędzie ich nie wnioskuje, nie poszerza, nie zawęża i nie
przesuwa. Wymaga `W >= A`, obu granic aware i pełnosekundowych, `W` nie w przyszłości, a każdy
nierozwiązany interwał z bundla przecinający `[A, W]` powoduje odmowę **z nazwaniem tego interwału**.
Dziura przed `A` i dziura po `W` nie blokują — zawężenie roszczenia przez późniejsze `A` jest
poprawną odpowiedzią na słaby dowód (`docs/13_…` §13.3).

Świeży preflight wykonuje się w obu trybach i **wszystkie jego bramki są zawężone do jednego
klienta docelowego**: identity, sufit migracji, dokładnie jedno konto klienta, dokładnie jeden
autorytatywny enabled schedule `trips_sync`, tryb celu dokładnie `strict_meta`, zero wierszy coverage
celu, zero wierszy recovery celu (i brak aktywnego recovery `PLANNED`/`RUNNING`), zero wierszy
historii `RUNNING` celu, zgodność `client_id` / `client_code` / dataset / schedule z bundlem i z
produkcją oraz brak driftu konfiguracji od czasu audytu. Zapis to jedna jawna transakcja: lock
schedule i client_account **celu**, ponowna weryfikacja, `INSERT` dokładnie jednego wiersza, odczyt
zwrotny i porównanie każdej wartości, dopiero potem `COMMIT`.

**Bootstrap jest operacją per klient — pierwszą i każdą kolejną.** Do `2026-08-03` writer wymagał
dodatkowo, by **cała flota** miała zero klientów `data_invariants_v1`. Było to założenie pierwszego
rolloutu i po zaakceptowaniu `BRAVO00016` jako produkcyjnego klienta compatibility blokowało ono
bootstrap każdego następnego klienta. Ta bramka została zastąpiona bramkami zawężonymi do celu:

- cel musi pozostać `strict_meta` — bootstrap zawsze poprzedza jego własne enablement
  (`docs/13_…` §13.6 kroki 5 i 6 są uporządkowane i nigdy nie odwracane);
- inni, wcześniej zatwierdzeni klienci compatibility są **dozwoleni**; istnienie `BRAVO00016` (ani
  żadnego kolejnego) nie blokuje celu;
- writer **nigdy** nie zmienia trybu paginacji — ani celu, ani żadnego innego klienta;
- stan innych klientów jest wyłącznie obserwowany: plan raportuje
  `non_target_compatibility_client_count` i posortowaną listę `client_code`, ale żaden wiersz
  nienależący do celu nie jest lockowany ani mutowany, a coverage i recovery innych klientów nie są
  liczone do bramek celu;
- każdy klient zachowuje własny inventory C10, własny bundel dowodowy, własne `A`/`W`, własną
  recenzję i własną autoryzację wykonania.

Plan (dry-run i execute) raportuje sanityzowane liczniki: `target_client_code`,
`target_pagination_mode`, `target_coverage_row_count`, `target_recovery_row_count`,
`target_active_recovery_count`, `target_running_history_row_count`,
`non_target_compatibility_client_count`, `rows_to_insert`, `database_writes_performed`,
`client_mode_changes = 0`, `schedule_changes = 0`, `history_mutations = 0`,
`recovery_rows_created = 0`, `non_target_coverage_rows_touched = 0`, `provider_requests = 0`,
`subprocesses_launched = 0`. Nie wypisuje sekretów, surowych wierszy biznesowych, ID tripów ani
danych osobowych.

**Status `ALPHA00001` (na `2026-08-03`).** Inventory C10 i bundel dowodowy dla `ALPHA00001` /
`trips_sync` są przygotowane (kanoniczny SHA-256
`80a85e48daeaa9412870a9894b5a4555a25a8809f937c3933e69061744c9dfcc`, klasyfikacja
`UNRESOLVED_GAPS_PRESENT`), a wcześniejszy dry-run został odrzucony **wyłącznie** przez usuniętą już
bramkę fleet-wide. Commit naprawczy `49ba6c02cbebec87ac3c22e9a605fcc249e1f28e` przeszedł
**niezależną recenzję** i został wypchnięty na `origin/main` `2026-08-03`; na tym samym bundlu
powtórzono dry-run, a następnie wykonano **dokładnie jeden** bootstrap `ALPHA00001`. Pełny wynik
rolloutu: sekcja „Produkcyjny rollout `ALPHA00001`” poniżej.

```bash
PYTHONPATH="$PWD" python3 ops/bootstrap_telematics_trips_coverage.py \
  --client-code <CODE> --dataset trips_sync \
  --evidence-file /var/lib/log-platform/coverage-bootstrap/<CODE>.json \
  --evidence-sha256 <64 hex> \
  --coverage-start-ts <A> --covered-through-ts <W> \
  --seeded-by <operator> --approval-ref <TICKET> \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
# dopiero po przeglądzie dry-runu dodać: --execute --confirm-client-code <CODE>
```

Stan początkowy jest kontraktowy i nie jest zgadywany: `bootstrap_status = 'READY'`,
`covered_through_source = 'bootstrap'`, `last_gap_detected_ts = NULL`, a `bootstrap_evidence_ref` to
bezpieczna referencja `telematics-coverage-bootstrap/1:sha256=<hash>:approval=<ticket>` — nigdy treść
bundla.

**Czego te narzędzia nie robią.** Nie włączają compatibility, nie zmieniają trybu klienta, nie
mutują schedule, nie tworzą wiersza historii, nie wykonują recovery ani backfillu, nie odpytują
providera i nie uruchamiają podprocesu. Writer nie ma `UPDATE`, `DELETE` ani upsertu i nigdy nie
nadpisuje istniejącego wiersza — wszystkie późniejsze mutacje coverage należą do finalizerów C6
(`docs/15_…`) oraz do finalizera sukcesu ręcznego recovery C11. Ad hoc produkcyjny SQL seedujący
coverage pozostaje zabroniony. **C11 (ręczne recovery compatibility) jest zaimplementowane
i przetestowane, ale nigdy nie wykonane produkcyjnie**, a migracja `058` nie jest zastosowana
produkcyjnie; jeśli dowód wykaże, że dziura musi zostać odzyskana, a nie wykluczona późniejszym `A`,
jej wykonanie pozostaje osobną, oddzielnie autoryzowaną bramką. Bootstrap execution i canary
compatibility enablement są odrębnymi operacjami i oba zostały wykonane `2026-08-03`.

**Weryfikacja deploymentu 2026-08-02.** Pierwszy naturalny tick po przywróceniu timera wystąpił o `23:06:22 CEST`, zakończył się `Result=success` / exit `0` i załadował zmigrowany schemat. Kolejny naturalny tick o `23:10:00 CEST` również zakończył się sukcesem. Żaden tick nie claimował schedule, nie uruchomił business joba ani requestu do providera i nie wyemitował kodu incydentu C6. Po tickach nadal było pięciu klientów `strict_meta`, zero klientów compatibility, zero wierszy coverage, zero seed/bootstrap/reseed/recovery, `303` wiersze historii i `0` w stanie `RUNNING`.

##### Wykonanie bootstrapu produkcyjnego BRAVO00016 (2026-08-03)

Pierwsze i jedyne dotychczasowe produkcyjne uruchomienie writera C10-W. Wykonano dokładnie jeden
`INSERT`; nie było `UPDATE`, `DELETE`, upsertu, retry ani drugiego uruchomienia.

| Pozycja | Wartość |
|---|---|
| Klient / dataset | `BRAVO00016` / `trips_sync` |
| `client_id` | `6018be20-5faa-41b6-89c9-fe2b54a8283e` |
| `schedule_id` | `1a3102bf-62d3-415c-8697-d5ee87bc415b` (jedyny enabled `trips_sync`) |
| `A` (`coverage_start_ts`) | `2026-07-01T00:00:00Z` |
| `W` (`covered_through_ts`) | `2026-07-27T00:00:00Z` |
| `bootstrap_status` | `READY` |
| `covered_through_source` | `bootstrap` |
| `last_gap_detected_ts` | `NULL` |
| `seeded_at` | `2026-08-03T08:07:48Z` (aware, UTC, pełnosekundowy) |
| `seeded_by` | `logplatform` |
| Evidence SHA-256 | `f399e9bfab3ec02f6923476d25b06be1ff59586be794ce41b70522023bb638df` |
| Approval reference | `TELEMATICS-C10-BRAVO00016-2026-08-03-W-2026-07-27` |
| `bootstrap_evidence_ref` | `telematics-coverage-bootstrap/1:sha256=<powyższy hash>:approval=<powyższy ticket>` |

`A` i `W` wybrał operator i zostały zatwierdzone przed uruchomieniem. Bundle dowodowy (klasyfikacja
`COMPLETE_INTERVALS_INVENTORIED`, zero `missing_or_unproven_intervals`) dowodzi ciągłego segmentu
`2026-06-29T00:00:00Z` – `2026-07-27T00:00:00Z`, w którym `[A, W]` zawiera się w całości. Żadne
recovery nie było wymagane i **C11 nie było potrzebne dla tego przedziału**.

**Nieudany run `2026-08-03` pozostaje poza roszczeniem.** Terminalny `FAILED` fire `2026-08-03
02:00 CEST` (`PAGINATION_MISMATCH` na `/trips`) obejmuje interwał `2026-07-27T00:00:00Z` –
`2026-08-03T00:00:00Z`, czyli dokładnie odcinek **po** `W`. `W` nie zostało z tego powodu ani
cofnięte, ani rozszerzone do `2026-08-03`: nieudany run zaczyna się w już udowodnionym punkcie
końcowym, a żaden interwał po `W` nie jest zasiewany.

**Weryfikacja po zapisie (wyłącznie read-only).** Nowa transakcja read-only potwierdziła: `1` wiersz
coverage łącznie, `1` dla `BRAVO00016` / `trips_sync`, `0` dla jakiegokolwiek innego klienta, oraz
zgodność co do instantu każdej przechowywanej wartości z zatwierdzonymi wejściami. Konfiguracja
klienta i schedule nie zmieniła się, writer nie utworzył wiersza historii (`308` wierszy przed i po),
`0` wierszy `RUNNING`, `0` incydentów C6.

**Bezczynność w trybie strict.** Po bootstrapie zaobserwowano jeden naturalny tick timera
(`2026-08-03 10:10:00 CEST`, `Result=success` / `ExecMainStatus=0`); timer pozostał `enabled` i
`active` i nie był ręcznie wyzwalany. Pełny snapshot stanu przed i po ticku jest identyczny: ten sam
odcisk wiersza coverage, `W` nadal `2026-07-27T00:00:00Z`, `bootstrap_status` nadal `READY`,
`last_gap_detected_ts` nadal `NULL`, pięciu klientów `strict_meta`, zero klientów compatibility.
Tick nie claimował fire'a, nie uruchomił business joba, nie wszedł w ścieżkę compatibility, nie
uruchomił finalizera C6 i nie wyemitował incydentu coverage — przy `strict_meta` dispatcher w ogóle
nie czyta `client_dataset_coverage`.

**Stan pozostały (na moment bootstrapu).** `BRAVO00016` był zbootstrapowany, ale nadal `strict_meta`.
Pozostali czterej klienci są niezbootstrapowani i strict. Włączenie canary compatibility dla
`BRAVO00016` (`docs/13_…` §13.6 krok 6) było wówczas odrębną, nieautoryzowaną bramką; wykonano je
później tego samego dnia — patrz sekcja poniżej.

##### Canary compatibility enablement BRAVO00016 (2026-08-03)

Pierwsze i jedyne dotychczasowe produkcyjne przejście `strict_meta` → `data_invariants_v1`.
Wykonano dokładnie jedną transakcję konfiguracyjną; nie było retry, drugiego uruchomienia ani
żadnej mutacji coverage. **Ta operacja uzbraja canary, ale go nie wykonuje** — nie uruchomiono
żadnego business joba, nie wywołano ręcznie dispatchera ani schedule, nie wykonano requestu do
providera, recovery ani backfillu.

| Pozycja | Wartość |
|---|---|
| Klient | `BRAVO00016` / `6018be20-5faa-41b6-89c9-fe2b54a8283e` |
| Schedule | `1a3102bf-62d3-415c-8697-d5ee87bc415b` (jedyny enabled `trips_sync`; weekly, `day_of_week=0`, `02:00`, `Europe/Warsaw`, `L = 7 d`) |
| Poprzedni tryb | `strict_meta` |
| Nowy tryb | `data_invariants_v1` |
| Affected rows | `1` |
| Klienci compatibility po zmianie | `1` (`BRAVO00016`) |
| Pozostali klienci | `ALPHA00001`, `FOXTROT00001`, `DELTA00001`, `ECHO00001` — nadal `strict_meta` |
| Stabilizacja `BRAVO00016` | `D = 10800 s`, `O = 3600 s`, `R = 2678400 s` (domyślne, niezmienione) |

**Zastosowany mechanizm.** Dedykowane narzędzie do zmiany `trips_pagination_mode` nie istnieje:
`jobs/trips_pagination_mode.py` to wyłącznie moduł stałych i walidatora, a
`ops/bootstrap_telematics_trips_coverage.py` jawnie **nie** zmienia trybu klienta. Użyto wyłącznie
udokumentowanego operatorskiego kontraktu `UPDATE` na `workflow_a_control.client_account`
(`docs/13_…` §13.6 krok 6 i §13.10 „Auditability”; lustro odwrotnego statementu z `docs/13_…`
§16.10 oraz `docs/14_…` §13 wiersz 4), zawężonego kanonicznym `client_id` i wymaganą wartością
bieżącą:

```sql
BEGIN;
UPDATE workflow_a_control.client_account
   SET trips_pagination_mode = 'data_invariants_v1'
 WHERE client_code           = 'BRAVO00016'
   AND client_id             = '6018be20-5faa-41b6-89c9-fe2b54a8283e'
   AND trips_pagination_mode = 'strict_meta';
-- affected-row count musi wynosić dokładnie 1; w przeciwnym razie ROLLBACK bez retry
COMMIT;
```

Transakcja egzekwowała w locie: dokładnie jeden zmieniony wiersz, dokładnie jeden klient
compatibility, zero pozostałych klientów poza `strict_meta` oraz niezmieniony odcisk wiersza
coverage. Read-back wykonano przed `COMMIT`. Żadna inna kolumna `client_account`, żaden schedule,
żaden wiersz coverage i żaden wiersz historii nie zostały dotknięte. Ad hoc wieloprzeznaczeniowy
SQL nie jest i nie był autoryzowany.

**Kolejność bramek `docs/13_…` §13.6 zachowana.** Krok 5 (coverage `READY`) wykonano
`2026-08-03 08:07:48Z`, krok 6 (mode flip) po nim, tego samego dnia. Tryb nie został włączony przy
`UNINITIALIZED`, `GAP_DETECTED` ani `RESEED_REQUIRED`.

**Odstępstwo od prowizorycznej rekomendacji.** `docs/14_…` §11.2 wskazywał prowizorycznie
`DELTA00001` jako pierwszego klienta, a §11.3 argumentował, że `BRAVO00016` powinien być ostatni.
Ta rekomendacja jest w dokumencie wprost oznaczona jako prowizoryczna do czasu ukończenia
inventory C10 — inventory ukończono dla `BRAVO00016` (`2026-08-03`, klasyfikacja
`COMPLETE_INTERVALS_INVENTORIED`). Wybór `BRAVO00016` jest zatem świadomą decyzją operatora, a nie
pominięciem bramki. Ryzyka z §11.3 (najciaśniejsza ciągłość, tygodniowa kadencja, nieodwracalna
zależność emailowa Eco Person, przedistniejąca jesienna dziura DST R5) pozostają aktualne i mają
znaczenie dopiero dla właściwego wykonania canary, nie dla samego uzbrojenia.

**Coverage bez zmian.** Wiersz `BRAVO00016` / `trips_sync` pozostał bajtowo identyczny przed i po
zmianie trybu oraz po naturalnym ticku: `A = 2026-07-01T00:00:00Z`, `W = 2026-07-27T00:00:00Z`,
`bootstrap_status = READY`, `covered_through_source = bootstrap`, `last_gap_detected_ts = NULL`,
`updated_at = 2026-08-03T08:07:48Z`, niezmieniona referencja evidence. Liczba wierszy coverage
nadal `1`; żaden inny klient nie ma wiersza.

**Weryfikacja po zapisie (nowe połączenie, wyłącznie read-only).** `1` klient compatibility i jest
nim wyłącznie `BRAVO00016`; czterej pozostali `strict_meta`; `308` wierszy historii przed i po
(zmiana konfiguracji nie tworzy wiersza historii); `0` wierszy `RUNNING`; `0` incydentów C6 i `0`
incydentów `TRIPS_*` w ogóle; schedule oraz konfiguracja stabilizacji bez zmian.

**Naturalny tick po włączeniu.** Timer nie był zatrzymywany, uruchamiany ani wyzwalany ręcznie i
pozostał `enabled` / `active`. Pierwszy naturalny tick po zmianie, `2026-08-03 10:40:00 CEST`,
zakończył się `Result=success` / `ExecMainStatus=0`. Loader dispatchera fail-closes na nieznanym
trybie i wartości stabilizacji spoza kontraktu, więc udany tick jest dowodem poprawnego
załadowania mieszanej floty. Tick nie claimował żadnego schedule, nie utworzył wiersza historii ani
wiersza `runs`, nie uruchomił podprocesu ani requestu do providera, nie uruchomił finalizera C6 i
nie wyemitował incydentu coverage. Żaden wiersz historii nie niesie evidence compatibility
(wszystkie pięć kolumn `NULL` w całej tabeli).

**Dlaczego nic nie zostało zaclaimowane.** Fire `2026-08-03 02:00 CEST` dla tego schedule istnieje
już jako terminalny `FAILED`, a `_claim_fire` używa
`ON CONFLICT (schedule_id, scheduled_fire_ts) DO NOTHING`, więc jest jednorazowy i nie może zostać
odtworzony ani powtórzony. Następny fire `BRAVO00016` przypada `2026-08-10 02:00 CEST`.

**Nieudany interwał `2026-08-03` pozostaje otwarty.** Odcinek `2026-07-27T00:00:00Z` –
`2026-08-03T00:00:00Z` leży w całości **po** `W` i nie jest objęty roszczeniem. Enablement go nie
zamknął, nie cofnął ani nie rozszerzył `W`. C11 nie było potrzebne dla zasianego przedziału
`[A, W]`; dla tego zaległego odcinka istnieje teraz zrecenzowana ścieżka — ręczne recovery C11
(§ „Ręczne recovery compatibility”) — której wykonanie wymaga zastosowania migracji `058`
i osobnej autoryzacji per okno i nie nastąpiło.

**Następna bramka.** Kontrolowane wykonanie canary — pierwszy rzeczywisty compatibility fire dla
`BRAVO00016` na interwale po `W` — jest **odrębną, osobno autoryzowaną** operacją i nie zostało tą
zmianą wykonane. Fleet-wide enablement pozostaje zabroniony (`docs/14_…` §14 N7); każdy kolejny
klient wymaga własnego inventory, przedziału, bundla i recenzji. Rollback trybu, gdyby był
potrzebny, wykonuje się wyłącznie zgodnie z `docs/13_…` §16.10 / `docs/14_…` §13 wiersz 4 — czyli
razem z ustawieniem `bootstrap_status = 'RESEED_REQUIRED'`, nigdy przez usunięcie wiersza coverage.

##### Ręczne recovery compatibility (C11) — `ops/recover_telematics_trips_window.py`

**Status: zaimplementowane, przetestowane, wdrożone i WYKONANE PRODUKCYJNIE DOKŁADNIE RAZ
(`2026-08-03`, `SUCCESS`).** Migracja `058_telematics_trips_manual_recovery.sql` **jest zastosowana na
produkcji** (`2026-08-03`); produkcyjny sufit migracji to `058_telematics_trips_manual_recovery.sql`.
Tabela `workflow_a_control.client_dataset_recovery_run` ma **dokładnie jeden** wiersz — canary
`BRAVO00016` opisany w sekcji „Produkcyjne wykonanie recovery C11”. Produkcyjne `W` dla
`BRAVO00016` wynosi po tej operacji `2026-08-03T00:00:00Z` ze źródłem `manual_recovery`, a nieudany
fire `2026-08-03` pozostaje **bajtowo niezmienionym** dowodem historycznym. Najpierw wykonano jeden
zatwierdzony dry-run, a następnie **jedno** osobno autoryzowane `--execute`; nie wykonano żadnego
automatycznego retry.

**Czym to jest.** Osobno identyfikowalna, jednorazowa operacja, która uruchamia **normalną**
produkcyjną synchronizację trips na jawnie autoryzowanym interwale UTC w trybie
`data_invariants_v1`, a dopiero po pełnym sukcesie biznesowym przesuwa `covered_through_ts` przez ten
sam niskopoziomowy CAS, którego używa zaplanowany finalizer C6
(`jobs/api/telematics/coverage_finalization.py`). Świadomie **nie** używa
`jobs.api.telematics.backfill_trips_insert_only`: celem jest zwalidowanie produkcyjnej ścieżki
compatibility, a nie tylko dosypanie brakujących wierszy.

**Czym to nie jest.** To **nie jest** zaplanowany fire. Narzędzie nigdy nie tworzy, nie edytuje, nie
usuwa, nie ponawia ani nie przeklasyfikowuje wiersza `client_schedule_run_history`, nigdy nie
odtwarza brakującego fire'a i nigdy nie dotyka `coverage_start_ts` ani `bootstrap_status`. Jego
tożsamość i dowody żyją w `workflow_a_control.client_dataset_recovery_run`.

Dry-run (domyślny — brak `--execute` to dry-run, nie ma flagi, o której można zapomnieć):

```bash
PYTHONPATH="$PWD" python3 ops/recover_telematics_trips_window.py \
  --client-code BRAVO00016 --dataset trips_sync \
  --window-start 2026-07-27T00:00:00Z \
  --window-end 2026-08-03T00:00:00Z \
  --expected-old-covered-through 2026-07-27T00:00:00Z \
  --reason "recover the 2026-08-03 PAGINATION_MISMATCH interval" \
  --approval-ref <TICKET> \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
# dopiero po przeglądzie planu dry-runu dodać:
#   --execute --confirm-client-code BRAVO00016
```

Dry-run wykonuje **zero** zapisów, **zero** requestów do providera i **nie uruchamia żadnego
podprocesu biznesowego** — jedyne podprocesy narzędzia to dwa lokalne, read-only polecenia `git`
wiążące plan z konkretnym commitem (plan raportuje `business_subprocesses_launched`).

**Bramki (każda odmowa jest stabilnym kodem `RECOVERY_REFUSED_*`).** Dokładnie jeden klient; dokładnie
jeden enabled authoritative schedule; tryb `data_invariants_v1`; dokładnie jeden wiersz coverage w
`READY`; `covered_through_ts == --expected-old-covered-through`; `--window-start` równe temu
watermarkowi; `--window-end` ściśle po `--window-start`; interwał nie większy niż
`trips_max_recovery_span_seconds`; `--window-end` nie w przyszłości i poza oknem stabilizacji `D`;
brak wiersza `RUNNING` w historii schedule; brak nieterminalnego recovery; brak już wykonanego
recovery o tej samej trójce klient/interwał/approval; czysty worktree i pełny 40-znakowy HEAD przy
`--execute`.

**Semantyka wykonania.**

| Etap | Zachowanie |
|---|---|
| 1 — claim | Jedna transakcja. Kolejność blokad: `client_account` → `client_dataset_schedule` → wiersz coverage → `client_dataset_recovery_run`. Wszystkie bramki powtarzane pod blokadą; wstawiany dokładnie jeden wiersz `RUNNING`. Żaden wiersz historii schedule nie powstaje. |
| 2 — biznes | Jedno uruchomienie `jobs.api.telematics.sync_trips_and_speeding` przez `ops/runner.py`, z literalnym oknem, `trips_pagination_mode=data_invariants_v1`, `trigger=MANUAL_RECOVERY`. Bez ponowień. |
| 3A — sukces | Nowa transakcja: blokada coverage, walidacja pełnego snapshotu claim-time, blokada wiersza recovery (`RUNNING`), CAS na oczekiwane stare `W`, `W → window_end_ts`, `covered_through_source='manual_recovery'`, `A` bez zmian, status nadal `READY`, recovery `SUCCESS` — atomowo. |
| 3B — porażka | Finalizer coverage **nie jest wołany**. Wiersz coverage pozostaje bajtowo identyczny (weryfikowane odciskiem), recovery `FAILED` z `RECOVERY_BUSINESS_FAILED` lub `RECOVERY_ORCHESTRATION_FAILED`. |
| 3C — konflikt finalizacji | Biznes się udał, ale CAS odmówił. Coverage **nie jest nadpisywane**, recovery `FINALIZATION_CONFLICT` z `TRIPS_COVERAGE_ADVANCE_CONFLICT`, na `stderr` trafia `RECOVERY_INCIDENT <kod>`. Wymagany osobny przegląd operatorski; narzędzie nigdy nie ponawia. |

**Recovery zablokowane w `RUNNING`.** Jeśli proces zginie między etapem 1 a etapem 3 (kill, restart
hosta, utrata połączenia), wiersz recovery zostaje `RUNNING`, a częściowy unikalny indeks
`uq_client_dataset_recovery_run_active` blokuje każde kolejne recovery dla tego schedule. To jest
zamierzone zachowanie fail-closed: narzędzie nigdy nie ponawia i nigdy samo nie domyka wiersza,
którego nie zaczęło. Triage jest **wyłącznie read-only**: sprawdzić `platform_run_id` w `runs` i
`logs`, ustalić, czy job biznesowy faktycznie się powiódł, i porównać bieżący odcisk coverage z
`initial_coverage_fingerprint` — muszą być równe, bo finalizer coverage w ogóle nie wystartował.
Domknięcie takiego wiersza jako `FAILED` wraz z klasyfikacją jest **osobno zrecenzowaną** decyzją
operatorską na tabeli dowodowej; nie jest to SQL na coverage i **nie wolno** przy tej okazji ruszyć
`covered_through_ts` ani `bootstrap_status`.

**Czego narzędzie nie robi.** Nie stosuje migracji, nie zmienia trybu klienta, nie zmienia schedule
ani parametrów stabilizacji, nie dotyka systemd, nie włącza kolejnych klientów, nie wykonuje ad hoc
SQL-a na coverage i nie implementuje C7. Ręczny `UPDATE` na `covered_through_ts` pozostaje
zabroniony — po C11 **dokładnie dwie** zrecenzowane powierzchnie mogą przesunąć `W`: finalizacja
sukcesu zaplanowanego fire'a i finalizacja sukcesu tego recovery.

**Uwaga wykonawcza przed pierwszym użyciem.** C7 (kompatybilna maszyna stanów paginacji) jest od
`2026-08-03` **zaimplementowane i wdrożone produkcyjnie**, razem z minimalną propagacją trybu:
`provider_client.py` rozgałęzia `/trips` na compatibility state machine, a `sync_trips_and_speeding.py`
przekazuje mu znormalizowany parametr `trips_pagination_mode`, który runner C11 już emituje.
Implementacja realizuje zaakceptowaną decyzję operatorską D5 Option B
(`docs/16_telematics_d5_total_policy_decision.md`). Runtime produkcyjny ładuje ten kod z commitu
`22db25e9cbc7ada60bf62c9a691957653d0c6cdd`, a migracja `058` jest zastosowana. Recovery na interwale
`2026-07-27` – `2026-08-03` jest zatem po raz pierwszy wykonywalne na ścieżce compatibility, a nie na
strict `PAGINATION_MISMATCH`. Nadal jednak wymaga **osobnej autoryzacji per okno**: zatwierdzony
dry-run nie jest zgodą na `--execute`.

##### Wdrożenie C7 i migracji 058 oraz zatwierdzony dry-run C11 (2026-08-03)

Pojedyncza kontrolowana operacja przygotowawcza. **Nie wykonano recovery, nie wywołano providera,
nie uruchomiono żadnej synchronizacji biznesowej.**

| Pozycja | Wartość |
|---|---|
| Runtime commit | `22db25e9cbc7ada60bf62c9a691957653d0c6cdd` (`main`, lokalny i `origin` identyczne) |
| Powierzchnia wdrożenia | **direct checkout** — `/usr/local/bin/log-job-runner.sh` robi `cd /opt/log-platform` i wykonuje `.venv/bin/python ops/runner.py`; brak zainstalowanego artefaktu i brak kopii w `site-packages` |
| Restart | **nie był wymagany i nie został wykonany** — `log-job@.service` jest `Type=oneshot`, więc każdy tick ładuje kod od nowa; timer pozostał `enabled` / `active` |
| Migracja | `058_telematics_trips_manual_recovery.sql` przez `bash ops/db_migrate.sh`, jedno zastosowanie, sufit `057` → `058`, `057` nietknięta |
| Tabela recovery | `workflow_a_control.client_dataset_recovery_run` utworzona, `0` wierszy, oba unikalne indeksy (`uq_..._approved_window`, `uq_..._active`) obecne, brak triggera mutującego coverage |
| `covered_through_source` | rozszerzone do `bootstrap`, `scheduled_run`, `operator`, `manual_recovery` |
| Naturalny tick po wdrożeniu | `2026-08-03 15:45:00 CEST`, `Result=success` / exit `0`; mieszana flota załadowana poprawnie; zero claimów, zero wierszy historii, zero requestów do providera, zero incydentów `TRIPS_*`. Potwierdzone ponownie na bezpośrednio zaobserwowanym ticku `2026-08-03 16:20:00 CEST` (patrz recenzja G-SM poniżej) |
| Dry-run C11 | exit `0`, `mode = DRY_RUN`, interwał `2026-07-27T00:00:00Z` – `2026-08-03T00:00:00Z` |
| Approval reference | `TELEMATICS-C11-BRAVO00016-2026-08-03-CANARY-1` |
| Plan dry-runu | `client_code=BRAVO00016`, `dataset_name=trips_sync`, `trips_pagination_mode=data_invariants_v1`, `expected_old_covered_through_ts=2026-07-27T00:00:00Z`, `bootstrap_status=READY`, `window_span_seconds=604800` (≤ `R = 2678400`), `planned_recovery_executions=1`, `business_subprocesses_launched=0`, `database_writes_performed=0`, `provider_requests=0`, `automatic_retries=0` |
| `initial_coverage_fingerprint` | `61bc47094b945de40e783823c6ce315ea01e1011b3a24f4a26f264346fb791ea` (`telematics-coverage-fingerprint/1`) |

**Stan niezmieniony przez całą operację.** `A = 2026-07-01T00:00:00Z`, `W = 2026-07-27T00:00:00Z`,
`bootstrap_status = READY`, `covered_through_source = bootstrap`, `last_gap_detected_ts = NULL`,
odcisk coverage identyczny przed migracją, po migracji, po ticku i po dry-runie. `308` wierszy
historii i `0` w stanie `RUNNING` przed i po. Nieudany fire `2026-08-03 02:00 CEST` pozostał
bajtowo niezmieniony (ten sam `run_history_id`, status, granice okna, timestampy i `error_summary`).
`BRAVO00016` pozostaje **jedynym** klientem `data_invariants_v1`; `ALPHA00001`, `FOXTROT00001`,
`DELTA00001` i `ECHO00001` pozostają `strict_meta`. Konfiguracja stabilizacji, schedule, jednostki
systemd i `.env` nie zostały zmienione.

**Wykonane.** Dokładnie jedna, **osobno autoryzowana** operacja `--execute` z parametrami
zatwierdzonego dry-runu (dodatkowo `--confirm-client-code BRAVO00016`) została uruchomiona
`2026-08-03` i zakończyła się `SUCCESS`; kompatybilność providera, commit biznesowy i przesunięcie
`W` do `2026-08-03T00:00:00Z` ze źródłem `manual_recovery` zostały zweryfikowane — patrz sekcja
„Produkcyjne wykonanie recovery C11 — BRAVO00016 canary” poniżej. **Rollout fleet-wide nadal nie
został wykonany** (`docs/14_…` §14 N7); każdy kolejny klient wymaga własnego inventory, przedziału,
bundla dowodowego i osobnej recenzji.

##### Recenzja G-SM aktywnego runtime'u C7 — **ZATWIERDZONA** (2026-08-03)

**Wynik: G-SM APPROVED.** Recenzję wykonała **świeża, niezależna sesja `Opus 5`**. Recenzent nie jest
`GPT 5.6 Terra` i **nie zgłasza się jako Terra**; model-specyficzna bramka tożsamości recenzenta
została wycofana decyzją operatorską na rzecz kontraktu procesowego: świeża sesja, recenzent nie jest
autorem C7, kod runtime czytany przed podsumowaniami projektowymi, brak edycji recenzowanego
runtime'u, niezależna weryfikacja własności bezpieczeństwa, prawdomówne raportowanie modelu.
Recenzent nie zmodyfikował żadnego pliku runtime, testu, migracji ani jednostki systemd.

| Pozycja | Wynik |
|---|---|
| Runtime HEAD produkcyjny | `2290acc3fce9fbe27ff9c8b5abbf19593bcb79e9` (`main`, lokalny i `origin` identyczne, worktree czysty) |
| Tożsamość bajtowa C7 | wszystkie recenzowane pliki runtime **bajtowo identyczne** z commitem `22db25e9cbc7ada60bf62c9a691957653d0c6cdd`; jedyna zmiana po C7 to dokumentacja (`docs/05_jobs.md`, `docs/07_operations.md`) — brak niezrecenzowanego driftu |
| Dowód direct-checkout | moduły importowane przez produkcyjny interpreter `/…/log-platform/.venv/bin/python` rozwiązują się do plików repozytorium; brak kopii w `site-packages`; brak restartu jednostki |
| `strict_meta` bez zmian | `_fetch_paginated`, `_parse_pagination_meta` i `_request_json` **bajtowo identyczne** sprzed C7; zmieniony wyłącznie `fetch_trips` (wybór gałęzi) |
| Zakres compatibility | wyłącznie `/trips`; `/vehicles`, `/drivers`, `/fuel`, `/vehicles/events` i `/notifications` pozostają strict |
| Terminacja | autorytatywna wyłącznie reguła short-page (łącznie z pustą stroną); pełna strona zawsze kontynuuje |
| Overlap | odrzucany wobec **pełnej historii** sub-okna, nie tylko poprzedniej strony |
| D5 Option B | brak `total` dozwolony; obecny `total` walidowany (bool/float/string/null/obiekt/tablica/ujemny odrzucane), stabilny między stronami, nigdy nieprzekroczony, uzgadniany do równości |
| Budżety | walidowane raz przed pierwszym requestem i przed każdym kolejnym; błędna konfiguracja kończy run `PAGINATION_COMPAT_CONFIG_INVALID` bez żądania do providera |
| Prywatność | logowane wyłącznie skrócone digesty HMAC i liczniki; brak surowych tożsamości, payloadów i soli |
| Propagacja trybu | dispatcher → sync → provider oraz C11 → runner → sync → provider; brak parametru i `null` rozwiązują się do `strict_meta`, wartość nieznana zawodzi zamknięcie przed konstrukcją klienta providera |
| Granica mutacji | `TelematicsProviderSafetyError` kończy run przed sekcją upsertu do bazy biznesowej i przed jedynym `conn.commit()` |
| Brak writera coverage w C7 | commit C7 nie dotyka `dispatcher.py`, `coverage_finalization.py` ani żadnej migracji |
| Advancement `W` | wyłącznie dwie zrecenzowane powierzchnie; CAS wiąże pełny retained snapshot + strict monotoniczność, nigdy nie zapisuje `coverage_start_ts` ani `bootstrap_status` |

**Testy (wszystkie exit `0`, sesje providera mockowane, dostęp sieciowy fatalny na poziomie importu):**
`test_telematics_trips_pagination_compat.py` (99 asercji, 0 błędów), `test_workflow_a_vehicle_events.py`,
`test_workflow_a_trip_chunking.py`, `test_telematics_trips_pagination_mode_config.py`,
`test_workflow_a_dispatcher.py`, `test_telematics_trips_recovery_workflow.py` oraz
`test_telematics_coverage_state_schema_postgres.py` na **jednorazowej** instancji PostgreSQL 16, usuniętej
po zakończeniu. `python -m py_compile` na sześciu plikach runtime — bez błędów.

**Weryfikacja produkcyjna wykonana w tej samej sesji.** Migracja `058` była już zastosowana
(`2026-08-03 15:42:47+02`), więc **nie została ponownie zastosowana** — zweryfikowano wyłącznie
odczytem: sufit `058`, tabela `client_dataset_recovery_run` z `0` wierszami, `29` kolumn, `PRIMARY KEY`,
dwa `FOREIGN KEY` (`ON DELETE RESTRICT`), `12` `CHECK`-ów, unikalność duplikatu okna
(`uq_client_dataset_recovery_run_approved_window`), częściowa unikalność aktywnego recovery
(`uq_client_dataset_recovery_run_active`), **brak triggerów** na tabeli recovery i na coverage,
`covered_through_source` dopuszczające `manual_recovery`. Zaobserwowano **naturalny** tick dispatchera
`2026-08-03 16:20:00 CEST` (`Result=success`, exit `0`) — bez ręcznego wyzwolenia; odcisk coverage,
`308` wierszy historii, `0` `RUNNING`, `0` wierszy recovery, tryby klientów i nieudany fire
`2026-08-03` pozostały identyczne przed i po. Wykonano **dokładnie jeden** dry-run C11 (bez
`--execute`, bez `--confirm-client-code`): exit `0`, `mode = DRY_RUN`, `sole_compatibility_client=true`,
`initial_coverage_fingerprint = 61bc47094b945de40e783823c6ce315ea01e1011b3a24f4a26f264346fb791ea`,
approval `TELEMATICS-C11-BRAVO00016-2026-08-03-CANARY-1`. **Zero** requestów do providera, **zero**
subprocesów biznesowych, **zero** zapisów do bazy, **zero** automatycznych retry. `W` pozostaje
`2026-07-27T00:00:00Z`, źródło `bootstrap`.

**Następny krok — wykonany.** Osobno autoryzowane, jednorazowe uruchomienie `--execute` odbyło się
`2026-08-03` i jest udokumentowane w sekcji poniżej.

##### Decyzja governance — niezależność recenzji jest procesowa (2026-08-03)

Operator **wycofał** wymóg, aby model recenzenta różnił się od modelu implementującego. Od
`2026-08-03` niezależność recenzji jest **procesowa**: świeża sesja recenzji, recenzent nie
modyfikuje implementacji w trakcie recenzji, runtime recenzowany niezależnie, recenzja i
implementacja pozostają osobnymi operacjami, a tożsamość modelu jest raportowana prawdomównie.
Odmienny model recenzenta jest **opcjonalny** i nie jest bramką blokującą. Zakończona recenzja G-SM
wykonana przez świeżą, niezależną sesję `Opus 5` jest **autorytatywna**; nie uruchomiono i nie
wymaga się kolejnej recenzji C7. Normatywna zmiana: `docs/14_…` §9 i §15.

| Pozycja | Wartość |
|---|---|
| Commit decyzji governance | `33e3be855393dc7630007fa824dab2378b9d6bf6` — `docs: make review independence process-based` |
| Zmienione pliki | `docs/14_telematics_trips_compatibility_implementation_plan.md`, `docs/05_jobs.md` (wyłącznie dokumentacja) |
| Skutek | bramka wykonania recovery C11 otwarta, warunkowana wyłącznie produkcyjnymi preconditions i autoryzacją operatorską per okno |

##### Produkcyjne wykonanie recovery C11 — BRAVO00016 canary — **SUCCESS** (2026-08-03)

**Dokładnie jedno** uruchomienie `--execute`. **Zero** automatycznych retry, zero ponowień, zero
ręcznego SQL-a na coverage, zero mutacji nieudanego fire'a, zero nowych wierszy historii schedule.

| Pozycja | Wartość |
|---|---|
| Approval reference | `TELEMATICS-C11-BRAVO00016-2026-08-03-CANARY-1` (ten sam co zatwierdzony dry-run) |
| `recovery_run_id` | `0e86fafa-ded3-4be4-8ec6-01dce344a0d1` |
| `platform_run_id` | `4088ac2e-2206-451f-8ac9-798084bcc856` |
| Interwał | `2026-07-27T00:00:00Z` – `2026-08-03T00:00:00Z` (`window_span_seconds = 604800` ≤ `R = 2678400`) |
| Repozytorium przy wykonaniu | `33e3be855393dc7630007fa824dab2378b9d6bf6`, worktree czysty; pliki runtime **bajtowo identyczne** z zrecenzowanym C7 `22db25e9cbc7ada60bf62c9a691957653d0c6cdd` |
| Start / koniec (UTC) | `2026-08-03T15:37:14Z` – `2026-08-03T15:37:47Z`; exit code `0`; `stderr` pusty (brak `RECOVERY_INCIDENT`) |
| Status recovery | `SUCCESS`, `error_classification = NULL` |
| Tryb | `data_invariants_v1`, `trigger = MANUAL_RECOVERY`, `business_subprocesses_launched = 1`, `automatic_retries = 0` |

**Wynik providera (compatibility state machine, ścieżka `/trips`).** Cztery sub-okna dwudniowe,
**po jednej stronie każde**, `4` requesty do `/trips` łącznie, `4` pobrane strony, `2774` zwrócone
wiersze (`990` / `923` / `695` / `166`). W **każdym** sub-oknie: `termination_reason = short_page`,
`total_reconciliation = exact`, `total_present = true` z `advisory_total` równym liczbie wierszy,
`overlap_count = 0` na każdej stronie oraz `unique_identity_count == returned_count` (brak duplikatu
tożsamości providera). Metadane providera pozostawały niespójne (`meta_per_page = 10`,
`meta_last_page` `99`/`93`/`70`/`17` przy `requested_limit = 1000`) — dokładnie ta niespójność
kończyła strict fire kodem `PAGINATION_MISMATCH`; w trybie compatibility są wyłącznie diagnostyczne.
**Zero** incydentów: brak `PAGINATION_MISMATCH`, brak jakiegokolwiek `PAGINATION_COMPAT_*`, brak
przekroczenia budżetu request/page/row/byte/elapsed, brak incydentu duplikatu tożsamości, brak
incydentu rekoncyliacji `total`, brak nowego `suspected_bug`. Log runu zawiera `67` wpisów `INFO`
i **zero** `WARNING` / `ERROR`.

**Wynik biznesowy.** Platform run `SUCCESS`; `2774` wiersze przygotowane i zupsertowane w jednej
transakcji (`db_commit_elapsed_seconds = 0.001`), `0` wierszy zniekształconych (`malformed_trip_id`,
`malformed_timestamp`, `other_parse_error` = `0`). Do `public.client_trips` trafiły `2773` wiersze
oznaczone tym `sync_run_id`, z `2773` unikalnymi `provider_trip_id` — **zero duplikatów tożsamości
providera w bazie**. Różnica `2774 → 2773` to jedna tożsamość zwrócona w dwóch sąsiednich job-level
chunkach, złożona idempotentnie przez `ON CONFLICT`; nie jest to duplikat wewnątrz sub-okna
compatibility (każde sub-okno raportuje `unique_identity_count == returned_count`). `2767` wierszy
mieści się w nominalnym przedziale, a `6` ma `start_timestamp` sprzed `2026-07-27T00:00:00Z`
(najstarszy `2026-07-26T20:24:15Z`) — to zastana semantyka filtra `/trips` providera, niezmieniona
przez C7. Zakres znaczników czasu zapisanych wierszy: `2026-07-26T20:24:15Z` –
`2026-08-02T21:35:05Z`; `66` przejazdów prywatnych. Liczniki speeding/HIGH_RPM/OVERREV pozostały
`0` **zgodnie z kontraktem joba**: `trip_metrics_population_source = report_207_migration`, więc
enrichment z `/vehicles/events` jest świadomie pomijany (`skip_reason =
trip_metrics_population_source_mismatch`) i te liczniki są zasilane migracją Report 207, nie tym
runem.

**Finalizacja coverage.** `rows_updated = 1`, `verified = true`, `coverage_start_ts_unchanged = true`.

| Pole | Przed | Po |
|---|---|---|
| `covered_through_ts` (`W`) | `2026-07-27T00:00:00Z` | `2026-08-03T00:00:00Z` |
| `covered_through_source` | `bootstrap` | `manual_recovery` |
| `coverage_start_ts` (`A`) | `2026-07-01T00:00:00Z` | `2026-07-01T00:00:00Z` (bez zmian) |
| `bootstrap_status` | `READY` | `READY` (bez zmian) |
| `last_gap_detected_ts` | `NULL` | `NULL` (bez zmian) |
| `bootstrap_evidence_ref` | `telematics-coverage-bootstrap/1:…` | bez zmian |
| Odcisk coverage | `61bc47094b945de40e783823c6ce315ea01e1011b3a24f4a26f264346fb791ea` | `389be64da83541b1e9d5812db7f589513af1b5f3a34d6b773bfd7d405102d578` |

Odcisk końcowy odczytany niezależnie z bazy jest **identyczny** z `final_coverage_fingerprint`
zapisanym w wierszu dowodowym recovery, a `initial_coverage_fingerprint` w tym wierszu jest
identyczny z odciskiem zatwierdzonego dry-runu.

**Niezmienność zaplanowanego fire'a i reszty stanu (weryfikacja read-only po wykonaniu).** Nieudany
fire `2026-08-03` (`run_history_id = 57054665-45a6-4144-814d-6fc979699900`) pozostał **bajtowo
niezmieniony** — ten sam status `FAILED`, granice okna, timestampy i `error_summary`; odcisk
`acd69cce7a7c5ebfe929eb51001f692a5b5e249e6394ce4611d85aff63c5648d` przed i po. `308` wierszy
historii przed i po, `0` w stanie `RUNNING`, `0` nowych wierszy historii
(`schedule_history_rows_created = 0`, `schedule_history_rows_modified = 0`). Dokładnie jeden wiersz
recovery, żaden w stanie `RUNNING`. `BRAVO00016` pozostaje **jedynym** klientem
`data_invariants_v1`; `ALPHA00001`, `FOXTROT00001`, `DELTA00001` i `ECHO00001` pozostają `strict_meta`.
Schedule (`weekly`, `02:00 Europe/Warsaw`, `lookback_days = 7`), wartości stabilizacji
(`D = 10800 s`, `O = 3600 s`, `R = 2678400 s`), jednostki systemd i `.env` bez zmian; timer
dispatchera pozostaje `enabled` / `active`.

**Następny krok.** Obserwować **naturalny** kolejny zaplanowany fire `BRAVO00016` na ścieżce
compatibility (pierwszy taki fire jeszcze nie nastąpił) i dopiero potem przygotowywać rollout
kolejnych klientów. **Rollout fleet-wide nie został wykonany**: każdy kolejny klient wymaga własnego
inventory C10, własnego przedziału `[A, W]`, własnego bundla dowodowego, osobnej recenzji i osobnej
zmiany konfiguracji.

##### Produkcyjny rollout `ALPHA00001` — bootstrap, enablement i recovery C11 — **SUCCESS** (2026-08-03)

Kontrolowany, per-klient rollout drugiego klienta compatibility. **Dokładnie jeden** bootstrap,
**dokładnie jedna** zmiana trybu i **dokładnie jedno** recovery `--execute`. **Zero** automatycznych
retry, zero ręcznego SQL-a na coverage, zero mutacji nieudanych fire'ów, zero odtworzonych
brakujących fire'ów, zero nowych wierszy historii schedule.

**1. Recenzja i push commita naprawczego.** Commit `49ba6c02cbebec87ac3c22e9a605fcc249e1f28e`
(rodzic `c14de68381c2b0f0b4a2591c08fdf3a92439d524`, `fix: allow per-client Telematics coverage
bootstrap`) zrecenzowano niezależnie **przed** poleganiem na raporcie implementacji: pełny diff,
`ops/bootstrap_telematics_trips_coverage.py`, nowe testy multi-client i zmienione testy writera.
Potwierdzono, że fleet-wide licznik compatibility przestał być bramką blokującą, a wszystkie bramki
są zawężone do celu (dokładnie jedno konto, dokładnie jeden enabled autorytatywny schedule
`trips_sync`, cel `strict_meta`, zero wierszy coverage celu, zero wierszy recovery celu w każdym
statusie, brak aktywnego recovery, brak `RUNNING` w historii celu, zgodność tożsamości z bundlem);
klienci compatibility spoza celu są wyłącznie obserwowani. Izolacja zapisu: jeden `INSERT`, brak
`UPDATE`/`DELETE`/`ON CONFLICT`, brak zapisu trybu, schedule, historii, recovery i danych
biznesowych; tożsamości zapisu pochodzą z wierszy rozwiązanych w preflighcie; wykonanie
re-weryfikuje stan celu pod `FOR UPDATE`, a równoległy insert przegrywa bez nadpisania zwycięzcy.
Żadna z zachowanych bramek (identity, sufit migracji, hash/świeżość/klasyfikacja dowodu, walidacja
przedziału, przecięcie z nierozwiązanymi lukami, `--execute`, potwierdzenie klienta, oczekiwanie
jednego insertu, weryfikacja po zapisie, zero retry) nie została osłabiona. Testy uruchomiono na
jednorazowym PostgreSQL 16 (usuniętym po recenzji), z fatalnym dostępem do sieci zewnętrznej:
`py_compile` writera oraz `test_telematics_coverage_bootstrap_multi_client_postgres.py`,
`test_telematics_coverage_bootstrap_writer_postgres.py`, `test_telematics_coverage_bootstrap_audit.py`,
`test_telematics_coverage_state_schema_postgres.py`, `test_telematics_trips_recovery_postgres.py`,
`test_telematics_trips_recovery_workflow.py` — wszystkie `PASS`. Push zwykły (bez `--force`):
`origin/main` `c14de683…` → `49ba6c02…`, po pushu `ahead 0 / behind 0`, worktree czysty.

**2. Dowód i bootstrap.**

| Pozycja | Wartość |
|---|---|
| Klient | `ALPHA00001` / `9536f715-2fd0-4ffd-86ed-ba06f5490c5e` / baza `alpha_main` |
| Schedule | `eb099f69-4876-4c7e-8f60-a2bad0c35b5b` (jedyny enabled `trips_sync`; `daily`, `02:00 UTC`, `lookback_days = 1`) |
| Bundel dowodowy | `alpha00001_trips_sync_c10_20260803T180527Z.json`, kanoniczny SHA-256 `80a85e48daeaa9412870a9894b5a4555a25a8809f937c3933e69061744c9dfcc`, klasyfikacja `UNRESOLVED_GAPS_PRESENT`, wygenerowany `2026-08-03T18:05:28Z` (w oknie świeżości), plik zwykły `0600`, nie-symlink |
| Approval bootstrapu | `TELEMATICS-C10-ALPHA00001-2026-08-PRODUCTION-REPORTING` |
| Zatwierdzone granice | `A = 2026-07-01T00:00:00Z`, `W = 2026-07-29T01:59:59Z` — żadna nierozwiązana luka z bundla nie przecina `[A, W]` (najbliższa, brakujący fire `2026-07-30`, zaczyna się `2026-07-29T02:00:00Z`) |
| Dry-run | exit `0`, `mode = DRY_RUN`, `rows_to_insert = 1`, `database_writes_performed = 0`, `target_pagination_mode = strict_meta`, `target_coverage_row_count = 0`, `target_recovery_row_count = 0`, `provider_requests = 0`, `subprocesses_launched = 0`, `client_mode_changes = 0`, `schedule_changes = 0`, `history_mutations = 0`, `non_target_compatibility_client_codes = ["BRAVO00016"]` |
| Wykonanie | `2026-08-03T19:09:44Z`, exit `0`, `affected_row_count = 1`, `transaction_result = COMMITTED`, zero `UPDATE`/`DELETE`, zero wierszy historii, zero wierszy recovery, zero requestów do providera, zero podprocesów |
| Stan zapisany | `A = 2026-07-01T00:00:00Z`, `W = 2026-07-29T01:59:59Z`, `bootstrap_status = READY`, `covered_through_source = bootstrap`, `last_gap_detected_ts = NULL`, `bootstrap_evidence_ref = telematics-coverage-bootstrap/1:sha256=80a85e48…:approval=TELEMATICS-C10-ALPHA00001-2026-08-PRODUCTION-REPORTING` |
| Odcisk coverage | `07a9f79a8a897b32731dff13e6244086111d7a147de913a83d26541a15c7b87b` (`telematics-coverage-fingerprint/1`), odtworzony niezależnie z zapisanego wiersza |

**3. Zmiana trybu `ALPHA00001`.** Wyłącznie udokumentowany wąski kontrakt operatorski `UPDATE` na
`workflow_a_control.client_account` (ten sam mechanizm co canary `BRAVO00016`), zawężony
kanonicznym `client_id`, `client_code` i wymaganą wartością bieżącą. Transakcja zweryfikowała przed
zapisem: dokładnie jeden wiersz klienta, tryb `strict_meta`, dokładnie jeden wiersz coverage `READY`
o zatwierdzonym odcisku, zero wierszy recovery `ALPHA`, zero wierszy `RUNNING` w historii `ALPHA`;
zablokowała wyłącznie wiersz `ALPHA` (`FOR UPDATE`), zmieniła **dokładnie jedną** wartość
(`trips_pagination_mode: strict_meta → data_invariants_v1`), wykonała read-back przed `COMMIT`
i skomitowała raz. `affected_rows = 1`. Odcisk coverage przed i po zmianie **identyczny**
(`07a9f79a…`), liczba wierszy historii bez zmian (`308`), wartości stabilizacji bez zmian
(`D = 10800 s`, `O = 3600 s`, `R = 2678400 s`). Żadna inna kolumna, żaden inny klient, żaden
schedule i żaden wiersz historii nie zostały dotknięte.

**4. Recovery C11.**

| Pozycja | Wartość |
|---|---|
| Approval reference | `TELEMATICS-C11-ALPHA00001-2026-08-PRODUCTION-REPORTING-1` (ten sam co zatwierdzony dry-run) |
| `recovery_run_id` | `f2cdeb56-9c5f-4768-80d2-7fbd4f384f39` |
| `platform_run_id` | `b59d6216-4872-4daa-8104-6ed2263ac89e` (`trigger = MANUAL_RECOVERY`, `actor = ops/recover_telematics_trips_window.py`) |
| Interwał | `2026-07-29T01:59:59Z` – `2026-08-03T02:00:00Z` (`window_span_seconds = 432001` ≤ `R = 2678400`) |
| Dry-run | exit `0`, `mode = DRY_RUN`, `planned_recovery_executions = 1`, `database_writes_performed = 0`, `provider_requests = 0`, `business_subprocesses_launched = 0`, `bootstrap_status = READY`, `pagination_mode = data_invariants_v1` |
| Repozytorium przy wykonaniu | `repository_head = 49ba6c02cbebec87ac3c22e9a605fcc249e1f28e`, worktree czysty |
| Start / koniec (UTC) | `2026-08-03T19:11:33Z` – `2026-08-03T19:15:38Z`; exit code `0`; brak `RECOVERY_INCIDENT` |
| Status recovery | `SUCCESS`, `automatic_retries = 0`, `business_subprocesses_launched = 1`, `business_returncode = 0` |
| Provider (compatibility) | `3` sub-okna (chunki 2-dniowe), `29` stron `/trips`, `36` requestów łącznie (`/trips` 30, `/drivers` 4, `/vehicles` 2); każde sub-okno zakończone `short_page`, `advisory_total` obecny i **równy** liczbie zebranych rekordów (`13236`, `11600`, `2824`); zero incydentów bezpieczeństwa. Jedno ostrzeżenie `telematics_provider_request_retryable` (`ReadTimeout`, próba 1/3) obsłużone przez ograniczony retry providera **wewnątrz** jednego wykonania — nie jest to retry operacji recovery |
| Transakcja biznesowa | `27660` wierszy pobranych, sparsowanych, przygotowanych i upsertowanych; `0` wierszy malformed (`trips_rows_malformed_trip_id`, `trips_rows_malformed_timestamp`, `trips_rows_other_parse_error`, `trips_rows_skipped_missing_registration` = `0`); `27649` odrębnych kluczy `(client_id, provider_trip_id)` niesie `sync_run_id` tego runu (różnica `11` to idempotentne złożenie powtórzeń z nakładki sub-okien przez `ON CONFLICT`); **jeden** commit klienta — wszystkie wiersze mają dokładnie jedną wartość `synced_at` (`2026-08-03T19:11:34.384512Z`), brak commitu częściowego |
| Finalizacja coverage | CAS zgodny z `expected_old_covered_through_ts = 2026-07-29T01:59:59Z`; `rows_updated = 1`, `verified = true`; `W` `2026-07-29T01:59:59Z` → `2026-08-03T02:00:00Z`, `covered_through_source = manual_recovery`, `A` bez zmian, `bootstrap_status = READY`, `last_gap_detected_ts = NULL`; odcisk końcowy `28d1642a718546fd9d248a28c3149a600267632a0e6ab91dcc0e0db4f3cb8d7f` |

**5. Weryfikacja po sukcesie (świeże transakcje read-only, `logdb` i `alpha_main`).** `ALPHA00001`
w trybie `data_invariants_v1`, dokładnie jeden wiersz coverage `READY` z `W = 2026-08-03T02:00:00Z`
i źródłem `manual_recovery`, dokładnie jeden wiersz recovery `ALPHA` w statusie `SUCCESS`, zero
wierszy `RUNNING` (`ALPHA` i globalnie). **Historia pozostała nienaruszona**: nieudane fire'y
`2026-08-01`, `2026-08-02` i `2026-08-03` bez zmian, brakujące fire'y `2026-07-30` i `2026-07-31`
nadal brakujące, łączna liczba wierszy historii `308` przed i po całym rollout'cie, zero wierszy
syntetycznych. Schedule i konfiguracja stabilizacji bez zmian; timer dispatchera `enabled` /
`active`. Agregaty biznesowe (`alpha_main.public.client_trips`, sanityzowane): `27638` wierszy
w oknie `[2026-07-29T01:59:59Z, 2026-08-03T02:00:00Z)`, `27638` odrębnych tożsamości providera,
`0` grup duplikatów, `0` wierszy z pustą tożsamością providera, `min(start_timestamp) =
2026-07-29T02:03:38Z`, `max(end_timestamp) = 2026-08-03T00:48:30Z`, `min(synced_at) =
max(synced_at) = 2026-08-03T19:11:34.384512Z`; wszystkie wiersze w oknie pochodzą z tego runu.

**Granica raportowania.** Raportowanie `ALPHA00001` oparte o `client_trips` jest zatwierdzone
**do `2026-08-03T02:00:00Z`** włącznie.

**Stan floty.** Klientami compatibility są **wyłącznie** `BRAVO00016` i `ALPHA00001`; `FOXTROT00001`,
`DELTA00001` i `ECHO00001` pozostają `strict_meta` i nie zostały w żaden sposób zmienione. Wiersz
coverage i wiersz recovery `BRAVO00016` pozostały bajtowo niezmienione.

**Następny krok.** Obserwować **naturalny** kolejny zaplanowany fire `ALPHA00001`
(`2026-08-04 02:00 UTC`) na ścieżce compatibility, a dopiero potem powtórzyć proces per klient dla
kolejnego priorytetowego konta. **Rollout fleet-wide pozostaje zabroniony.**

**Znana, nietknięta pozostałość.** W tabeli `runs` istnieje `5` wierszy `RUNNING` z maja i lipca
`2026` (dwa dotyczą `ALPHA00001`), bez odpowiadających im żywych procesów — to znany skutek braku
retry w `finish_run` (`CURRENT_TASK_CONTEXT.md` §5.5), a nie operacja w locie. Autorytatywna bramka
dispatchera (`client_schedule_run_history` w stanie `RUNNING`) wynosiła `0` przed każdym krokiem
i po nim. Wiersze te **nie zostały zmienione** w tym rollout'cie.

##### Pierwszy naturalny fire `ALPHA00001` compatibility — **BLOCKED WINDOW** (2026-08-04)

Audyt wykonano read-only na produkcji po terminalizacji docelowego fire'a. Repozytorium było na
`main`, lokalny i zdalny HEAD `536551a2751012745dcd4fddb9b8c6570c98873f`, ahead/behind `0/0`,
worktree czysty. Tożsamość platformy: `production`,
`52517750-7438-4558-8490-2736ae4cc629`; sufit migracji:
`058_telematics_trips_manual_recovery.sql`.

**Naturalne pochodzenie.** Timer był i pozostał `enabled` / `active`. Journal systemd pokazuje
naturalny start `log-job@dispatcher.service` o `2026-08-04T02:00:00Z` i poprawny koniec o
`02:02:30Z`. Dispatcher run `fda39742-fc54-4cae-8578-6f5cb34b573a` ma `trigger = SCHEDULED` i
`SUCCESS`. Dokładnie jeden authoritative history row:
`03f6b5a5-38a2-4327-8c5d-ff095857b979`, schedule
`eb099f69-4876-4c7e-8f60-a2bad0c35b5b`, fire `2026-08-04T02:00:00Z`, `SUCCESS`; został utworzony
o `02:00:00.323560Z` i połączony z dokładnie jednym business runem
`c36ea500-496e-4aa2-844f-26be3cf8c602`. Business run i jego params mają
`trigger = SCHEDULED`; nie ma recovery UUID ani drugiego business runu dla tego fire'a. Historyczne
terminalne wiersze nie zostały ponownie użyte.

**Bramka okna — `ALPHA_NATURAL_FIRE_BLOCKED_WINDOW`.** Claim-time evidence poprawnie zapisuje
`trips_pagination_mode = data_invariants_v1`, `D = 10800 s`, `O = 3600 s` oraz nominalny przedział
`2026-08-03T02:00:00Z` – `2026-08-04T02:00:00Z`. Autorytatywne
`window_start_ts` / `window_end_ts` oraz params business runu zapisują jednak rzeczywisty przedział
efektywny `2026-08-02T22:00:00Z` – `2026-08-03T23:00:00Z`. Nie jest to wymagany przez audyt
przedział `2026-08-03T02:00:00Z` – `2026-08-04T02:00:00Z`; claim nastąpił o nominalnym `02:00Z`,
nie po oczekiwanej eligibility około `05:00Z`. Dlatego natural-canary gate jest zablokowany mimo
technicznego `SUCCESS` providera, joba i finalizera.

**Sanityzowany wynik providera.** Jedno sub-okno `/trips`, `7` stron, `14` prób HTTP łącznie:
`/trips = 8`, `/drivers = 4`, `/vehicles = 2`. `/trips` zwrócił `6344` wiersze i `6344` unikalne
tożsamości; każda strona była ograniczona limitem, ostatnia zakończyła się `short_page`.
`advisory_total = 6344` był obecny, stabilny i uzgodniony dokładnie. Jeden `ReadTimeout` na
`/trips` (próba `1/3`) zakończył się ograniczonym retry wewnątrz provider clienta; nie było retry
operacji schedule. Zero `PAGINATION_MISMATCH`, `PAGINATION_COMPAT_*`, duplikatów wewnątrz strony,
cross-page overlapu, powtórzonych ordered/unordered fingerprintów i incydentów
page/request/row/byte/elapsed budget. Logi runu mają zero kluczy raw payload i provider-trip-ID,
zero error rows.

**Transakcja biznesowa.** Run `SUCCESS`: `6344` fetched, parsed, prepared i upserted; wszystkie
liczniki malformed/missing-registration wynoszą `0`. W bazie `alpha_main` ten `sync_run_id` ma
`6344` wiersze, `6344` odrębne tożsamości providera, `0` grup duplikatów i `0` pustych
tożsamości. Sanityzowany zakres timestampów zapisanych wierszy:
`min(start_timestamp) = 2026-08-02T16:40:02Z`,
`max(end_timestamp) = 2026-08-03T23:06:30Z`. Dokładnie jedna wartość `synced_at`
(`2026-08-04T02:00:00.674670Z`) i jeden transaction ID potwierdzają jeden commit bez częściowego
commitu.

**Finalizacja coverage.** Przed claimem `A = 2026-07-01T00:00:00Z`,
`W = 2026-08-03T02:00:00Z`, źródło `manual_recovery`, `READY`, gap `NULL`. Po udanym subprocessie
atomiczny finalizer C6 zapisał history `SUCCESS`, `coverage_advanced = true` i przesunął dokładnie
rzeczywiste `W` do `2026-08-03T23:00:00Z`, ze źródłem `scheduled_run`; `A`, `READY` i gap pozostały
bez zmian. History `SUCCESS` może zostać skomitowane przez tę ścieżkę dopiero po CAS wpływającym na
dokładnie jeden wiersz i pozytywnej weryfikacji read-back. Niezależna reprodukcja odcisku bieżącego
wiersza dała po obu implementacjach
`317b8454e9824d58d8920d6f04db1fcf37e5d33040090d179717da9d3df49f0f`.
Nie osiągnięto wymaganej przez audyt wartości `W = 2026-08-04T02:00:00Z`; nie wykonano ręcznego
przesunięcia.

**Nienaruszalność historii i recovery.** Recovery
`f2cdeb56-9c5f-4768-80d2-7fbd4f384f39` pozostaje `SUCCESS`, z tym samym platform runem, przedziałem,
`finished_at = updated_at = 2026-08-03T19:15:38Z` oraz odciskami początkowym/końcowym co po recovery.
Liczba recovery `ALPHA00001` pozostaje `1`; aktywnych recovery `0`. Brakujące fire'y `2026-07-30`
i `2026-07-31` nadal nie mają wierszy; fire'y `2026-08-01`, `2026-08-02` i `2026-08-03` nadal mają
po jednym niezmienionym `FAILED`. Nie zsyntetyzowano ani nie zmodyfikowano fire'a historycznego.

**Osobna znana luka — pięć stale general runs.** Żaden z poniższych runów nie ma żywego procesu,
aktywnego ani nawet powiązanego schedule-history row; żaden nie może blokować claimów dispatchera.
Korelacja nie pozwala wiarygodnie odtworzyć formalnego statusu terminalnego: wszystkie urwały logi
bez completion/error evidence i należy je traktować jako przerwane lub zabite, nie jako aktywne.

- `7970535a-cabe-4909-81b0-e109e63586c9` — `ALPHA00001`,
  `jobs.api.telematics.sync_trips_and_speeding`, start `2026-05-11T08:58:58.497745Z`;
- `37eeb114-a52c-4ab7-8281-3740971d0132` — `ALPHA00001`,
  `jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment`, start
  `2026-05-18T20:55:05.876945Z`;
- `8f7a1c56-24a2-4843-8e42-6e38d2be0194` — `BRAVO00016`,
  `jobs.api.telematics.sync_trips_and_speeding`, start `2026-07-07T10:12:21.716360Z`;
- `9231044d-8ef4-4dbf-8132-d6d3aedc6078` — `BRAVO00016`,
  `jobs.api.telematics.sync_trips_and_speeding`, start `2026-07-07T12:42:07.028154Z`;
- `612faf1d-917a-4f04-8a01-6f27d5057b90` — client association niewykazana w run/history,
  `jobs.reports.stage3.job_stage3`, start `2026-07-09T23:21:21.119867Z`.

To harmless observability debt i reporting/UI defect, nie operational blocker. Wymaga osobnego
repair tasku; ten audyt nie kończył, nie usuwał ani nie poprawiał tych wierszy.

**Stan końcowy i granica raportowania.** Dokładnie jeden terminalny history row dla target fire,
globalnie zero history `RUNNING`; tylko `BRAVO00016` i `ALPHA00001` są
`data_invariants_v1`, a `FOXTROT00001`, `DELTA00001`, `ECHO00001` pozostają `strict_meta`. Schedule
`daily / 02:00 UTC / lookback 1`, `D = 10800`, `O = 3600`, `R = 2678400` oraz timer pozostały bez
zmian. Granica raportowania nie zostaje zatwierdzona do oczekiwanego
`2026-08-04T02:00:00Z`; trwały stan coverage kończy się na rzeczywistym
`2026-08-03T23:00:00Z`, a rozbieżność kontraktu okna pozostaje blokującą bramką przed dalszym
rolloutem.

##### KOREKTA klasyfikacji pierwszego naturalnego fire'a `ALPHA00001` — **SUCCESS / STABILIZED WINDOW CONFIRMED** (2026-08-04)

Rekord `BLOCKED WINDOW` powyżej **pozostaje zachowany jako historyczny zapis audytu i nie jest
usuwany**, ale jego wynik jest **nieprawidłowy**. Osobny read-only audyt semantyki (repozytorium
`main`, lokalny i zdalny HEAD `f6896ac3d0003dd881a7b1f5148ef5ad21099397`, ahead/behind `0/0`,
worktree czysty; tożsamość platformy `production` /
`52517750-7438-4558-8490-2736ae4cc629`; sufit migracji `058_telematics_trips_manual_recovery.sql`)
klasyfikuje ten fire jako **`ALPHA_NATURAL_FIRE_SUCCESS_STABILIZED_WINDOW_CONFIRMED`**.

**Poprzedni wynik był błędem oczekiwania audytu, a nie awarią produkcji.** Poprzedni audyt
oczekiwał przedziału `2026-08-03T02:00:00Z` – `2026-08-04T02:00:00Z` oraz eligibility dopiero od
`2026-08-04T05:00:00Z`. To semantyka **opóźnionej eligibility** (`E_end = F`, claim od `F + D`),
której **nie implementuje kod i nie definiuje dokumentacja**. Żadnej takiej ścieżki nie ma w
repozytorium.

**Obowiązujący kontrakt: przesunięty cutoff.** Opóźnienie stabilizacji `D` przesuwa **efektywny
cutoff danych**, a nie nominalny moment odpalenia schedule'a:

```
F        = nominalny scheduled fire (dispatcher odpala, gdy F <= now_utc)
E_end    = F − D
base     = (F − L) − D − O
E_start  = max( min(base, W − O), E_end − R )
new_W    = max(W, E_end)
```

Dowód w kodzie:

| Element | Miejsce |
|---|---|
| Eligibility = nominalny czas fire'a (`fire_utc > now_utc` → niedue); brak przesunięcia o `D` | `jobs/api/telematics/dispatcher.py:428` (`evaluate_schedule`) |
| `E_end = F − D`, `base = (F − L) − D − O`, `candidate_start = min(base, W − O)`, `E_start = max(candidate_start, E_end − R)` | `jobs/api/telematics/coverage_windows.py:196-204` (`derive_effective_window`) |
| Claim zapisuje okno **efektywne** w `window_start_ts`/`window_end_ts`, a `[F−L, F]` w kolumnach nominalnych | `jobs/api/telematics/dispatcher.py:1916-1937` (`prepare_run`) |
| Job dostaje okno efektywne i **nigdy** go nie przelicza | `jobs/api/telematics/dispatcher.py:1687-1713` (`_build_job_params`) |
| Coverage awansuje do `E_end` (= `prepared.window_end`), nie do `F` | `jobs/api/telematics/dispatcher.py:1330-1354` (`_finalize_compat_success`) |
| Monotoniczny CAS z twardym warunkiem `covered_through_ts < new` | `jobs/api/telematics/coverage_finalization.py:409-545` (`advance_covered_through_cas`) |

Dokumentacja mówi to samo i **nie jest sprzeczna z kodem**:
`docs/13_telematics_trips_stabilization_windows.md` §4.1 (`effective_end = nominal_end −
stabilization_delay`), §4.2 (`E_end(n) = F_n − D`, `D` skraca się w warunku ciągłości), §5.2
(`E_end = F − D`, `E_start = min(base, W − O)` z capem `R`), §5.4 (`new_W = max(current_W, E_end)`)
oraz §14 („dispatcher odpala, gdy `F ≤ now_utc`, a `E_end = F − D ≤ now_utc − D`" — guard
eligibility jest **spełniony automatycznie**, bo okno jest przesunięte, a nie fire).

**Zgodność z rzeczywistym fire'em.** Dla `F = 2026-08-04T02:00:00Z`, `L = 1 d`, `D = 10800 s`,
`O = 3600 s`, `R = 2678400 s`, `W_old = 2026-08-03T02:00:00Z`:

| Wielkość | Formuła | Wartość | Produkcja |
|---|---|---|---|
| `E_end` | `F − D` | `2026-08-03T23:00:00Z` | `2026-08-03T23:00:00Z` ✔ |
| `base` | `(F − L) − D − O` | `2026-08-02T22:00:00Z` | — |
| `min(base, W − O)` | `min(22:00, 2026-08-03T01:00:00Z)` | `2026-08-02T22:00:00Z` | — |
| `E_start` | `max(…, E_end − R)` | `2026-08-02T22:00:00Z` | `2026-08-02T22:00:00Z` ✔ |
| `is_connected` | `E_start <= W + 1 s` | `true` | brak gap ✔ |
| `new_W` | `max(W, E_end)` | `2026-08-03T23:00:00Z` | `2026-08-03T23:00:00Z` ✔ |

Rewalidacja read-only (`logdb` i `alpha_main`, jawne `BEGIN TRANSACTION READ ONLY`) potwierdza:
dokładnie jeden naturalny history row `03f6b5a5-38a2-4327-8c5d-ff095857b979` (`SUCCESS`,
`trigger = SCHEDULED`, mode `data_invariants_v1`, `D = 10800`, `O = 3600`, nominalne
`2026-08-03T02:00:00Z` – `2026-08-04T02:00:00Z`, efektywne `2026-08-02T22:00:00Z` –
`2026-08-03T23:00:00Z`); jeden business run `c36ea500-496e-4aa2-844f-26be3cf8c602` (`SUCCESS`,
`trigger = SCHEDULED`, params z tym samym oknem efektywnym), bez powiązania z recovery; dispatcher
run `fda39742-fc54-4cae-8578-6f5cb34b573a` `SCHEDULED` / `SUCCESS`, timer `enabled` / `active`;
provider `7` stron, `advisory_total = 6344`, `accumulated_unique_rows = 6344`,
`total_reconciliation = exact`, `termination_reason = short_page`, zero incydentów budżetu i
paginacji; jedna zatwierdzona transakcja biznesowa (`6344` wierszy tego `sync_run_id`, `6344`
odrębnych tożsamości providera, `0` grup duplikatów, dokładnie jeden `synced_at`
`2026-08-04T02:00:00.674670Z`); coverage `A = 2026-07-01T00:00:00Z`,
`W = 2026-08-03T23:00:00Z`, `covered_through_source = scheduled_run`, `bootstrap_status = READY`,
`last_gap_detected_ts = NULL`, `updated_at = 2026-08-04T02:02:30Z` — CAS objął dokładnie jeden
wiersz (history `SUCCESS` jest w tej ścieżce commitowalne wyłącznie po `rows_updated = 1` i
pozytywnym read-backu), `W` przesunięte monotonicznie i **bez luki logicznej**: pobrany przedział
`[2026-08-02T22:00:00Z, 2026-08-03T23:00:00Z]` w całości pokrywa `(W_old, W_new]`. Overlap jest
**wyłącznie overlapem pobrania** — ponownie żąda danych sprzed starego `W`, nie zmniejsza i nie
duplikuje roszczenia coverage. Wiersz recovery `f2cdeb56-9c5f-4768-80d2-7fbd4f384f39` pozostaje
`SUCCESS` i niezmieniony (`finished_at = updated_at = 2026-08-03T19:15:38Z`), liczba recovery
`ALPHA00001` nadal `1`; brakujące fire'y `2026-07-30` i `2026-07-31` nadal brakujące, a
`2026-08-01`, `2026-08-02`, `2026-08-03` nadal po jednym niezmienionym `FAILED`. Globalnie zero
history `RUNNING`.

**Wnioski operacyjne.** Nie wystąpił defekt produkcyjny; nominalny fire `02:00 UTC` był poprawny;
`E_end = 23:00 UTC` był poprawny; `E_start = 22:00 UTC` był poprawny; awans `W` do
`2026-08-03T23:00:00Z` był poprawny. `ALPHA00001` **zaliczył** pierwszy naturalny canary
compatibility. Recovery, zmiana schedule'a, zmiana `D`/`O`/`R` ani zmiana runtime'u **nie są
wymagane**.

**Granica raportowania.** `W = 2026-08-03T23:00:00Z` UTC = `2026-08-04 01:00 CEST`
(Europe/Warsaw). Pełne raportowanie po lokalnej dobie kalendarzowej jest zatem zatwierdzone
**do `2026-08-03` Europe/Warsaw włącznie**. Nie wolno twierdzić kompletnego raportowania za
`4 sierpnia`, dopóki kolejny naturalny fire nie przesunie `W` poza koniec tej lokalnej doby.
Platforma z założenia **utrzymuje** skonfigurowany lag stabilizacji `D` i **nie** zbiega `W` do
nominalnego czasu fire'a — najnowsze `D` sekund jest zawsze celowo jeszcze nie zingestowane.

**Pięć stale `runs.status = RUNNING`** (`f49b6724…`, `ad928026…`, `458bda8b…`, `0f39ef1c…`,
`74bb721a…`) pozostaje osobnym długiem obserwowalności; audyt korekty ich nie kończył, nie usuwał
i nie poprawiał.

##### Rollout pozostałej floty Telematics — `DELTA00001`, `FOXTROT00001`, `ECHO00001` — **PARTIAL** (2026-08-04)

Klasyfikacja flotowa: **`TELEMATICS_REMAINING_FLEET_ROLLOUT_PARTIAL_SWEE_DISABLED_CONTRACT`**.
Klasyfikacje terminalne per klient: `DELTA00001_ROLLOUT_SUCCESS`, `FOXTROT00001_ROLLOUT_SUCCESS`,
`ECHO00001_ROLLOUT_BLOCKED_DISABLED_SCHEDULE_CONTRACT`.

Repozytorium przy starcie i przy każdej mutacji: `main`, lokalny i zdalny HEAD
`a27172f6829d5f3a63e2857fcc71a95f8e09f981`, ahead/behind `0/0`, worktree czysty. Tożsamość
platformy zweryfikowana świeżo w jawnej transakcji read-only: `production`,
`52517750-7438-4558-8490-2736ae4cc629`, rola `platform`, sufit migracji
`058_telematics_trips_manual_recovery.sql`. Zaakceptowana baza wyjściowa `BRAVO00016` / `ALPHA00001`
potwierdzona bez zmian przed startem. Klientów przetwarzano **ściśle sekwencyjnie**; następny
startował dopiero po zweryfikowanym stanie terminalnym poprzedniego.

**Zakres.** Zmieniono wyłącznie: `workflow_a_control.client_dataset_coverage` (dwa `INSERT`y
bootstrapu), `client_account.trips_pagination_mode` (dwie wąskie zmiany jednego pola),
`client_dataset_recovery_run` (dwa wiersze tożsamości/dowodów) oraz `public.client_trips`
w `delta_main` i `foxtrot_main`. Nie naprawiano żadnego innego workflow ani żadnej innej tabeli
w bazach biznesowych.

**1. `DELTA00001` — SUCCESS.**

| Pozycja | Wartość |
|---|---|
| Klient / baza | `DELTA00001` / `5f68d5db-6e2d-421d-8248-544640d3de9f` / `delta_main` (`public`) |
| Schedule | `a4ab0c52-0f4b-4670-89e9-210d1e051fce` — jedyny, `enabled`, `daily`, `02:00 Europe/Warsaw`, `L = 7 d`, `overwrite_existing = true`, `event_enrichment_mode = enabled`; `D = 10800 s`, `O = 3600 s`, `R = 2678400 s` |
| Granica bezpieczna | `F = 2026-08-04T00:00:00Z` (02:00 CEST), `E_end = F − D = 2026-08-03T21:00:00Z` |
| Inventory C10 | `72` udane interwały, `7` nierozwiązanych: `FAILED 2026-06-17` (przed `A`), brakujące fire'y `2026-07-30` i `2026-07-31`, `FAILED 2026-08-01`…`2026-08-04`. Wszystkie cztery świeże `FAILED` to strict `PAGINATION_MISMATCH` przerwany na `chunk 1/4` — **żaden nie zapisał danych biznesowych** (`0` wierszy w `delta_main` z ich `sync_run_id`) |
| Bundel dowodowy | `/home/logplatform/coverage-evidence/DELTA00001/delta00001_trips_sync_c10_20260804T090402Z.json`, plik zwykły `0600` w katalogu `0700`, wygenerowany `2026-08-04T09:04:02Z`, klasyfikacja `UNRESOLVED_GAPS_PRESENT`, kanoniczny SHA-256 `886420c092f40a0c2dd2f16e081d38909cb6c184d9b1ff3d584d4f19271ab365` (odtworzony niezależnie), SHA-256 pliku `26d8d1391b2474c95f20b64572dc2cc713ffc2810b4f93a540d9a82ac2318b5e` |
| Zatwierdzone granice | `A = 2026-07-01T00:00:00Z`, `W = 2026-07-22T23:59:59Z`. **Uwaga na 7-dniowy lookback**: pierwszy nierozwiązany interwał to brakujący fire `2026-07-30`, którego przedział to `[2026-07-23T00:00:00Z, 2026-07-30T00:00:00Z]`, więc `W` musi zatrzymać się sekundę przed `2026-07-23T00:00:00Z` — mimo że fire'y `2026-07-24`…`2026-07-29` są `SUCCESS`. Zawężenie roszczenia jest poprawną odpowiedzią; nie przesunięto `A` ani nie przekroczono luki |
| Approval bootstrapu | `TELEMATICS-C10-DELTA00001-2026-08-REMAINING-FLEET-ROLLOUT` |
| Dry-run / wykonanie | dry-run exit `0`, `rows_to_insert = 1`, `database_writes_performed = 0`; wykonanie `2026-08-04T09:06:03Z`, exit `0`, `affected_row_count = 1`, `transaction_result = COMMITTED`, zero `UPDATE`/`DELETE`, zero mutacji trybu, schedule, historii i recovery, zero requestów do providera, zero podprocesów |
| Odcisk po bootstrapie | `6a50343d00441ee93ed587115974d308215ea46fe52bac5c83185427d6bbc830` |
| Zmiana trybu | wąska transakcja na `client_account` zawężona `client_code` + `client_id` + wymaganą wartością bieżącą; bramki pod `FOR UPDATE`: dokładnie jeden wiersz klienta, tryb `strict_meta`, dokładnie jeden wiersz coverage `READY` o zatwierdzonym odcisku, zero wierszy recovery, zero `RUNNING` w historii. `trips_pagination_mode: strict_meta → data_invariants_v1`, `affected_rows = 1`, read-back przed `COMMIT`, odcisk coverage identyczny przed i po |
| Approval recovery | `TELEMATICS-C11-DELTA00001-2026-08-REMAINING-FLEET-ROLLOUT-1` |
| `recovery_run_id` | `3f617037-9d3d-4a4e-8858-bcf055e01ea6` |
| `platform_run_id` | `54c8f877-af7f-482f-8468-a0b050bc29bb` (`trigger = MANUAL_RECOVERY`, `actor = ops/recover_telematics_trips_window.py`, `SUCCESS`) |
| Interwał | `2026-07-22T23:59:59Z` – `2026-08-03T21:00:00Z`, `window_span_seconds = 1026001` ≤ `R = 2678400`, mieści się w jednym wykonaniu |
| Start / koniec | `2026-08-04T09:07:26Z` – `09:23:48Z`, exit `0`, brak `RECOVERY_INCIDENT`, `automatic_retries = 0`, `business_subprocesses_launched = 1`, `business_returncode = 0` |
| Provider (sanityzowany) | `6` sub-okien (chunki 2-dniowe), `9` stron `/trips`, `476` requestów łącznie (`/trips` 9, `/vehicles/events` 465, `/drivers` 1, `/vehicles` 1); każde sub-okno `termination_reason = short_page`, `advisory_total` obecny i `total_reconciliation = exact` we wszystkich sześciu; `0` incydentów paginacji i budżetu, `0` `abort_code`, **`0` ograniczonych retry HTTP**. Jedyne `WARNING` to stałe ostrzeżenie o jednostce prędkości |
| Transakcja biznesowa | `5302` wierszy pobranych, sparsowanych, przygotowanych i upsertowanych; wszystkie liczniki malformed/missing-registration `= 0`. W `delta_main` ten `sync_run_id` ma `5299` wierszy i `5299` odrębnych tożsamości providera (różnica `3` to idempotentne złożenie 1-sekundowych nakładek sub-okien przez `ON CONFLICT`), `0` grup duplikatów, `0` pustych tożsamości, **dokładnie jedna** wartość `synced_at` (`2026-08-04T09:07:27.410639Z`) — jeden commit, brak commitu częściowego. Wszystkie `5299` wierszy w oknie pochodzą z tego runu |
| Finalizacja coverage | CAS zgodny z `expected_old_covered_through_ts = 2026-07-22T23:59:59Z`; `rows_updated = 1`, `verified = true`; `W` → `2026-08-03T21:00:00Z`, `covered_through_source = manual_recovery`, `A` bez zmian, `READY`, gap `NULL`; odcisk końcowy `af0d361595857a8def8ce0a503ca87362df6515570f06faa56b43edf5d1c634a` |
| Granica raportowania | `W = 2026-08-03T21:00:00Z` UTC = `2026-08-03 23:00 CEST`. Ostatnia **pełna** doba Europe/Warsaw: **`2026-08-02`** (doba `2026-08-03` kończy się `2026-08-03T22:00:00Z`, czyli za `W`) |

**2. `FOXTROT00001` — SUCCESS.**

| Pozycja | Wartość |
|---|---|
| Klient / baza | `FOXTROT00001` / `f1c7f1ed-bcb2-4fba-8753-a951b2d952ad` / `foxtrot_main` (`public`) |
| Schedule | `9b124cb5-ffdc-4294-8e0e-a7f05f3bb41c` — jedyny, `enabled`, `daily`, `02:00 UTC`, `L = 1 d`, `overwrite_existing = true`, `event_enrichment_mode = enabled`; `D = 10800 s`, `O = 3600 s`, `R = 2678400 s` |
| Granica bezpieczna | `F = 2026-08-04T02:00:00Z`, `E_end = F − D = 2026-08-03T23:00:00Z` |
| Inventory C10 | `72` udane interwały, `7` nierozwiązanych — identyczny kształt jak `DELTA`: `FAILED 2026-06-17` (przed `A`), brakujące `2026-07-30`/`2026-07-31`, `FAILED 2026-08-01`…`2026-08-04`; `0` wierszy w `foxtrot_main` z `sync_run_id` nieudanych runów |
| Bundel dowodowy | `/home/logplatform/coverage-evidence/FOXTROT00001/foxtrot00001_trips_sync_c10_20260804T092602Z.json`, plik zwykły `0600` w katalogu `0700`, wygenerowany `2026-08-04T09:26:02Z`, klasyfikacja `UNRESOLVED_GAPS_PRESENT`, kanoniczny SHA-256 `c3181f9c7f1472e09b2756b5fc0fbc0cfa63465a93ddaf34a39aa13b294407f9` (odtworzony niezależnie) |
| Zatwierdzone granice | `A = 2026-07-01T00:00:00Z`, `W = 2026-07-29T01:59:59Z` — sekunda przed pierwszym nierozwiązanym interwałem `[2026-07-29T02:00:00Z, 2026-07-30T02:00:00Z]` |
| Approval bootstrapu | `TELEMATICS-C10-FOXTROT00001-2026-08-REMAINING-FLEET-ROLLOUT` |
| Dry-run / wykonanie | dry-run exit `0`, `rows_to_insert = 1`, `database_writes_performed = 0`; wykonanie `2026-08-04T09:26:20Z`, exit `0`, `affected_row_count = 1`, `transaction_result = COMMITTED`, zero `UPDATE`/`DELETE`, zero mutacji trybu, schedule, historii i recovery |
| Odcisk po bootstrapie | `4bba9f9e71f9ea4aef10f0789e29363880ba2c470d4c17cf0e33107bf988dd78` |
| Zmiana trybu | ten sam wąski kontrakt co `DELTA`; `affected_rows = 1`, read-back przed `COMMIT`, odcisk coverage identyczny przed i po |
| Approval recovery | `TELEMATICS-C11-FOXTROT00001-2026-08-REMAINING-FLEET-ROLLOUT-1` |
| `recovery_run_id` | `5eb4d561-d40d-46a4-800f-6e3e92c392fe` |
| `platform_run_id` | `d11dc1fd-fd1e-48c7-8a3d-8a30dea9cdf3` (`trigger = MANUAL_RECOVERY`, `SUCCESS`) |
| Interwał | `2026-07-29T01:59:59Z` – `2026-08-03T23:00:00Z`, `window_span_seconds = 507601` ≤ `R` |
| Start / koniec | `2026-08-04T09:27:09Z` – `10:02:28Z`, exit `0`, brak `RECOVERY_INCIDENT`, `automatic_retries = 0`, `business_subprocesses_launched = 1`, `business_returncode = 0` |
| Provider (sanityzowany) | `3` sub-okna, `13` requestów `/trips`, `897` requestów łącznie (`/trips` 13, `/vehicles/events` 882, `/drivers` 1, `/vehicles` 1); wszystkie trzy sub-okna `short_page`, `advisory_total` obecny, `total_reconciliation = exact`; `0` incydentów paginacji i budżetu, `0` `abort_code`. **Jedno ograniczone retry HTTP**: `telematics_provider_request_retryable`, `ConnectionError` / read timeout na `/vehicles/events`, próba `1/3`, backoff `5 s` — obsłużone przez provider client **wewnątrz jednego wykonania biznesowego**; **nie jest to retry operacji recovery** |
| Transakcja biznesowa | `11935` wierszy pobranych, sparsowanych, przygotowanych i upsertowanych; wszystkie liczniki malformed/missing-registration `= 0`. W `foxtrot_main` ten `sync_run_id` ma `11935` wierszy i `11935` odrębnych tożsamości providera, `0` grup duplikatów, `0` pustych tożsamości, **dokładnie jedna** wartość `synced_at` (`2026-08-04T09:27:09.599891Z`) — jeden commit. W oknie recovery leży `11931` wierszy i **wszystkie** pochodzą z tego runu (`0` z innych runów) |
| Finalizacja coverage | CAS zgodny z `expected_old_covered_through_ts = 2026-07-29T01:59:59Z`; `rows_updated = 1`, `verified = true`; `W` → `2026-08-03T23:00:00Z`, `covered_through_source = manual_recovery`, `A` bez zmian, `READY`, gap `NULL`; odcisk końcowy `9efa56b716d48ec01cad4cfd38369c01e9e7fdb80f14664e07a5e765fc547325` |
| Granica raportowania | `W = 2026-08-03T23:00:00Z` UTC = `2026-08-04 01:00 CEST`. Ostatnia **pełna** doba Europe/Warsaw: **`2026-08-03`** |

**3. `ECHO00001` — BLOCKED (`ECHO00001_ROLLOUT_BLOCKED_DISABLED_SCHEDULE_CONTRACT`).**

Żadnej mutacji produkcyjnej nie wykonano: `ECHO00001` pozostaje `strict_meta`, bez wiersza coverage,
bez wiersza recovery, z niezmienionym schedule'em. Ustalony stan żywy:

- dokładnie **jeden** autorytatywny schedule `trips_sync`
  (`60c80b85-f294-4a00-8e09-b6a3688af443`, `daily`, `02:00 UTC`, `L = 1 d`,
  `overwrite_existing = true`, `event_enrichment_mode = enabled`), **`enabled = false`** — wiersz
  istnieje i nie jest usunięty, `created_at = updated_at = 2026-04-29T08:20:49Z`;
- **brak** konkurencyjnego schedule'a `trips_sync`, enabled czy disabled; `client_schedule_legacy`
  nie ma wierszy dla tego klienta;
- **zero** wierszy `client_schedule_run_history` — dla `trips_sync` i dla każdego innego datasetu,
  po `client_code` i po `client_id`;
- **zero** runów platformowych odwołujących się do tego klienta;
- **zero** wierszy w `echogallery_main.public.client_trips`.

Blokują dwa niezależne warunki dozwolonego przypadku SWEE:

1. **Nie istnieje historyczny dowód, który dałoby się skorelować z tym schedule'em.** Nie ma ani
   jednego fire'a, runu ani wiersza biznesowego, więc nie da się wyprowadzić żadnego dowiedzionego,
   zreconciliowanego interwału, a zatem żadnego defensywnego `W`.
2. **Zrecenzowane narzędzia nie mogą bezpiecznie działać na tym stanie.** Wszystkie trzy wymagają
   dokładnie jednego **enabled** autorytatywnego schedule'a i odmawiają **przed** jakimkolwiek
   zapisem: read-only audyt C10 zwrócił `AUDIT_REFUSED AMBIGUOUS_SCHEDULE: trips_sync resolves to 0
   enabled schedules` (exit `4`, **żaden bundel nie powstał**), a writer C10-W
   (`BOOTSTRAP_REFUSED_PREFLIGHT`, `_count_target_enabled_schedules`) i recovery C11
   (`ops/recover_telematics_trips_window.py:724`) mają tę samą bramkę.

W chwili tamtego rolloutu nie istniał zatwierdzony kontrakt bootstrapu na wyłączonym schedule'u ani
zrecenzowana procedura aktywacji. Autoryzowana kolejność operatorska umieszcza aktywację
schedule'a **po** udanym bootstrapie, zmianie trybu, recovery i zweryfikowanym `READY` — a ówczesne
narzędzia czyniły ten stan nieosiągalnym. Schedule'a **nie włączono** nawet tymczasowo, wyłącznie po
to, by zaspokoić preflight narzędzia.

**Ten brak został uzupełniony osobnym zadaniem implementacyjnym** — patrz „Ścieżka cold start"
poniżej i `docs/13_…` §13.6a. `ECHO00001` **pozostaje niezmieniony** (`strict_meta`, schedule
`enabled = false`, zero coverage, zero recovery, zero historii, zero runów, zero wierszy
`client_trips`) do czasu niezależnej recenzji i osobno autoryzowanego rolloutu.

**4. Weryfikacja flotowa (świeże transakcje read-only, `logdb` + bazy biznesowe).**

| Klient | Tryb | Coverage `A` → `W` | Źródło | Recovery |
|---|---|---|---|---|
| `BRAVO00016` | `data_invariants_v1` | `2026-07-01T00:00:00Z` → `2026-08-03T00:00:00Z` | `manual_recovery` | `616ef560-…` `SUCCESS` |
| `ALPHA00001` | `data_invariants_v1` | `2026-07-01T00:00:00Z` → `2026-08-03T23:00:00Z` | `scheduled_run` | `42ab4f32-…` `SUCCESS` |
| `DELTA00001` | `data_invariants_v1` | `2026-07-01T00:00:00Z` → `2026-08-03T21:00:00Z` | `manual_recovery` | `9a0f5253-…` `SUCCESS` |
| `FOXTROT00001` | `data_invariants_v1` | `2026-07-01T00:00:00Z` → `2026-08-03T23:00:00Z` | `manual_recovery` | `7be531f9-…` `SUCCESS` |
| `ECHO00001` | `strict_meta` | brak wiersza | — | brak wiersza |

Wszystkie cztery wiersze coverage są `READY` z `last_gap_detected_ts = NULL`. Aktywnych recovery
`0`. Historia schedule: `312` wierszy przed i po całym rollout'cie, globalnie `0` w stanie
`RUNNING`. Historia `DELTA00001` i `FOXTROT00001` po rollout'cie nadal `72` `SUCCESS` + `5` `FAILED`
każda — **nieudane fire'y pozostały bajtowo niezmienione**, brakujące `2026-07-30` i `2026-07-31`
nadal nie mają wierszy, nie zsyntetyzowano ani nie przeklasyfikowano żadnego fire'a. Wiersze
coverage i recovery `BRAVO00016` oraz `ALPHA00001` pozostały niezmienione (`updated_at`
odpowiednio `2026-08-03T15:37:47Z` i `2026-08-04T02:02:30Z`, odciski `389be64d…` i `317b8454…`).
Schedule'y, `D = 10800`, `O = 3600`, `R = 2678400` i timer dispatchera (`enabled` / `active`) bez
zmian. Bazy biznesowe niebędące celem nietknięte: `telematics_main` `max(synced_at) =
2026-08-03T15:37:15Z`, `alpha_main` `2026-08-04T02:00:00Z`, `echogallery_main` `0` wierszy.

**Pięć stale `runs.status = RUNNING`** pozostaje osobnym długiem obserwowalności — ten rollout ich
nie kończył, nie usuwał i nie poprawiał. Autorytatywna bramka dispatchera
(`client_schedule_run_history` w stanie `RUNNING`) wynosiła `0` przed każdym krokiem i po nim.

**Znana luka poboczna (nie naprawiana tutaj).** `DELTA00001`, `FOXTROT00001` i `ECHO00001` mają
`client_db_environment` i `client_db_identity_id` `NULL` w control-plane, a ich bazy biznesowe nie
mają tabeli `ops_control.environment_identity` (klientowska migracja `038_environment_identity.sql`
nie została do nich zastosowana). Tożsamość bazy jest **jednoznaczna** (jedno konto → jedna nazwa
bazy → jedna żywa baza na jednym hoście), ale **nieatestowana**. Żadne z narzędzi tej ścieżki
(audyt C10, writer C10-W, recovery C11, `sync_trips_and_speeding`) nie odczytuje tych pól, więc nie
było to bramką. Uzupełnienie atestacji tożsamości baz biznesowych dla tych trzech klientów jest
**osobnym zadaniem hardeningowym**.

##### Ścieżka przyszła — `strict_meta` nie jest produkcyjnym trybem pracy

Obowiązuje od `2026-08-04` dla wszystkich kolejnych kont Telematics:

- **`strict_meta` nie jest zatwierdzonym produkcyjnym trybem pracy aktywnego schedule'a `/trips`.**
  Może pozostać wyłącznie jako fail-closed stan onboardingowy lub diagnostyczny **przed**
  bootstrapem coverage.
- **Aktywny produkcyjny schedule `trips_sync` nie może być trwale sparowany ze `strict_meta`.** To
  właśnie ta para wyprodukowała serie `PAGINATION_MISMATCH` z `2026-08-01`…`2026-08-04`.
- Nowy klient Telematics musi przejść, w tej kolejności: weryfikację tożsamości klienta i bazy →
  inventory C10 → bundel dowodowy → bootstrap coverage → zmianę trybu na `data_invariants_v1` →
  kontrolowane recovery, gdy jest wymagane → weryfikację granicy raportowania → aktywację
  schedule'a.
- **Żaden zbiorczy, flotowy update trybu nie jest dozwolony.** Każdy klient zachowuje własny
  inventory, własny bundel dowodowy, własne `A`/`W`, własną recenzję, własną autoryzację wykonania
  i własną granicę rollbacku.
- Runy zaplanowane po rollout'cie działają w semantyce **przesuniętego cutoffu**: `E_end = F − D`,
  `new_W = max(W, E_end)`. Platforma celowo utrzymuje lag `D` i **nie** zbiega `W` do nominalnego
  `F`; najnowsze `D` sekund jest zawsze jeszcze nie zingestowane.

**To jest zapis procedury, nie mechanizm.** Technicznego wymuszenia tej reguły dla nowych kont
**nadal nie ma**: `scripts/onboard_workflow_a_client.py` tworzy konto ze `strict_meta`
i default-disabled schedule'ami, a żadne ograniczenie bazodanowe nie zabrania pary
„enabled `trips_sync` + `strict_meta`". Pozostaje to **osobną luką hardeningową**; to zadanie
świadomie nie tworzyło nowego defaultu bazodanowego ani migracji.

##### Ścieżka cold start — nowy klient, który nigdy nie wykonał żadnego runu

Zaimplementowana `2026-08-04` jako osobna, wąska ścieżka. **Nie osłabia kontraktu C10.** Spec:
`docs/13_telematics_trips_stabilization_windows.md` §13.6a.

**Różnica wobec historycznego bootstrapu C10.** C10 (`docs/13_…` §13.2–§13.6) jest oparty na
**dowodach**: inwentaryzuje udowodnione interwały i pozwala operatorowi wybrać `A`/`W` wewnątrz
tego materiału. Klient świeżo onboardowany nie może w to wejść i **słusznie** jest odrzucany
dwukrotnie: jego schedule jest `disabled` (audyt C10 zwraca `AMBIGUOUS_SCHEDULE`, writer
`BOOTSTRAP_REFUSED_PREFLIGHT`), a pusta historia klasyfikuje się jako
`INSUFFICIENT_HISTORY_EVIDENCE`, której writer C10 nie akceptuje. Cold start odpowiada na inne,
węższe pytanie: **czy ten klient jest dowodliwie pusty?**

**Klient z jakąkolwiek historią wykonań — udaną czy nieudaną — nie wchodzi na tę ścieżkę.** Każde
z czterech narzędzi odmawia i kieruje operatora z powrotem do C10.

**Wymagany stan wejściowy (`EMPTY_DISABLED_STRICT`), wszystkie warunki łącznie:**

- dokładnie jedno konto klienta o tym `client_code`;
- tryb dokładnie `strict_meta`;
- dokładnie **jeden** wiersz schedule `trips_sync` (brak konkurencyjnego, enabled ani disabled);
- ten schedule `enabled = false`;
- **zero** wierszy `client_schedule_run_history` celu, dla każdego statusu;
- **zero** runów platformowych (`public.runs`) odwołujących się do celu;
- **zero** wierszy coverage celu;
- **zero** wierszy recovery celu;
- **zero** wierszy w `public.client_trips` bazy biznesowej klienta;
- brak żywego procesu celu i brak obcej transakcji trzymającej lock silniejszy niż
  `AccessShareLock` na relacjach control-plane;
- zgodne environment, platform UUID i jednoznaczna tożsamość bazy biznesowej;
- sufit migracji `058_telematics_trips_manual_recovery.sql`.

**Sekwencja (każde przejście osobno bramkowane, idempotentne albo bezpiecznie odmawiające):**

```
EMPTY_DISABLED_STRICT → COLD_START_EVIDENCE_READY → BASELINE_COVERAGE_READY
→ COMPATIBILITY_MODE_READY → RECOVERY_CHAIN_IN_PROGRESS → RECOVERY_CHAIN_COMPLETE
→ SCHEDULE_ENABLED → NORMAL_SCHEDULED_OPERATION
```

**1. Dowód zero-state (read-only)** — `ops/audit_telematics_cold_start.py`. Brak przełącznika
wykonania; oba połączenia (platforma i baza biznesowa) otwierane read-only. Zero requestów do
providera, zero podprocesów biznesowych (jedyny podproces to lokalne `git rev-parse HEAD`), zero
zapisów. Bundle: plik zwykły `0600` w katalogu `0700` poza drzewem repozytorium, otwierany
`O_NOFOLLOW`, symlink pliku i katalogu odrzucany. Klasyfikacja: dokładnie jedna —
**`COLD_START_ZERO_STATE_CONFIRMED`**. `UNRESOLVED_GAPS_PRESENT` **nie jest** reużywane dla
klienta, który nigdy nie działał: brak historii to brak dziur, a twierdzenie przeciwne byłoby
fałszywym zdaniem o produkcji.

```bash
PYTHONPATH="$PWD" python3 ops/audit_telematics_cold_start.py \
  --client-code <CODE> --dataset trips_sync \
  --expected-schedule-id <SCHEDULE_UUID> \
  --desired-managed-start <ISO INSTANT> \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629 \
  --output /var/lib/log-platform/cold-start/<CODE>.json
```

Bundle zawiera: environment, platform UUID, HEAD repo, `client_code`/`client_id`, nazwę bazy
biznesowej i dostępne runtime dowody jej tożsamości, dataset, `schedule_id`, `enabled`, komplet
parametrów schedule'a, tryb paginacji, wszystkie liczniki zero-state, wynik detekcji żywego
procesu, znaczniki czasu bazy użyte do świeżości, `desired_managed_start_ts`, najpóźniejszą
bezpieczną granicę shifted-cutoff (`now − D`), kanoniczny SHA-256, czas wygenerowania i wersję
semantyki. Nie zawiera sekretów, DSN-ów, payloadów providera ani danych osobowych.

**2. Baseline coverage (dry-run first)** — `ops/bootstrap_telematics_cold_start_coverage.py`. Wstawia
dokładnie **jeden** wiersz coverage o **zerowej szerokości**: `A == W == zatwierdzony pierwszy
zarządzany instant`. Brak `UPDATE`, `DELETE`, `ON CONFLICT` i ścieżki naprawczej. Nie zmienia
trybu, nie zmienia schedule'a, nie tworzy wiersza historii ani recovery, nie dotyka danych
biznesowych, nie odpytuje providera, nie uruchamia podprocesu.

```bash
PYTHONPATH="$PWD" python3 ops/bootstrap_telematics_cold_start_coverage.py \
  --client-code <CODE> --dataset trips_sync \
  --evidence-file /var/lib/log-platform/cold-start/<CODE>.json \
  --evidence-sha256 <64 hex> \
  --expected-schedule-id <SCHEDULE_UUID> --confirm-schedule-disabled \
  --coverage-start-ts <T> --initial-covered-through-ts <T> \
  --seeded-by <operator> --approval-ref <TICKET> \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
# dopiero po przeglądzie dry-runu dodać: --execute --confirm-client-code <CODE>
```

Stan początkowy: `bootstrap_status = 'READY'`, `covered_through_source = 'bootstrap'`,
`last_gap_detected_ts = NULL`, a `bootstrap_evidence_ref` to
`telematics-cold-start-bootstrap/1:sha256=<hash>:approval=<ticket>:managed-start=<ISO>` — prefiks jest
**nośnikiem kontraktu**, nie kosmetyką: bramka C11 cold-start dopasowuje dokładnie ten prefiks.

*Semantyka baseline'u.* `[A, W]` to domknięty przedział **zweryfikowanego** pokrycia. Przy `A == W`
jest zdegenerowany: obejmuje zero czasu, więc twierdzi, że **żaden** przedział o niezerowej
długości nie został pokryty. Nie może zawyżyć historii, bo w przedziale zerowej szerokości nie ma
czego zawyżać. Migracja `057` dopuszcza `A <= W` w obu `CHECK`-ach, a `derive_effective_window`
wymaga tylko `A <= W` — **żadna migracja nie jest potrzebna**. Recovery zaczyna dokładnie w `W`
(nigdy `W + 1 s`), więc suma `[T, T] ∪ [T, E] = [T, E]` jest ciągła i nie powstaje dziura
jednosekundowa; potem `E_start = min(base, W − O) <= W`, więc kolejne fire'y są `is_connected`
z konstrukcji. **`READY` nie znaczy „gotowy do raportowania"** — to słownik stanu coverage
wymagany przez schemat, żeby wiersz w ogóle niósł granice. Plan narzędzia raportuje
`reporting_ready = false` i `covered_interval_seconds = 0`.

**3. Zmiana trybu** — bez zmian: udokumentowana wąska transakcja operatorska na `client_account`
(sekcja „Canary compatibility enablement" powyżej), zawężona `client_code` + `client_id` +
wymaganą wartością bieżącą, `affected_rows = 1`, read-back przed `COMMIT`.

**4. Recovery przy wyłączonym schedule'u — łańcuch jednego lub więcej okien** —
`ops/recover_telematics_trips_window.py` z jawną bramką
`--allow-disabled-schedule-for-cold-start`. **Bez tej flagi zachowanie jest niezmienione**:
disabled schedule nadal jest odrzucany (`RECOVERY_REFUSED_TARGET`), a podanie którejkolwiek opcji
cold-start bez flagi jest odmową, nie cichym no-opem.

*Dlaczego łańcuch.* Zakres cold startu dłuższy niż `trips_max_recovery_span_seconds` (`R`) klienta
**nie mieści się w jednym recovery**, a podnoszenie `R` zniszczyłoby limit chroniący kwotę
providera. Recovery jest więc ciągiem okien:

```
baseline W → okno 1 SUCCESS → W1 → okno 2 SUCCESS → W2 → … → zatwierdzone końcowe W → aktywacja
```

**Cold start jednookienny to łańcuch długości jeden** i pozostaje w pełni wspierany.

*Podział jest deterministyczny.* `ops/telematics_cold_start_chain.plan_recovery_windows` dzieli
`[start, final_end]` na kolejne okna o długości co najwyżej `R`: pierwsze zaczyna się dokładnie w
`start`, każde następne dokładnie tam, gdzie skończyło się poprzednie (nigdy `+ 1 s`, więc na żadnej
granicy wewnętrznej nie powstaje dziura), okna się nie nakładają, a ostatnie kończy się dokładnie w
`final_end`. Zakres niebędący wielokrotnością `R` kończy się jednym krótszym oknem, nie poszerzoną
granicą. Granice muszą być pełnosekundowymi instantami UTC — nic nie jest zaokrąglane;
`final_end <= start` i `R <= 0` są odrzucane. Narzędzie przelicza podział od **bieżącego** `W` przy
każdym wywołaniu.

*Tożsamość łańcucha bez migracji.* Migracja `058` nie ma kolumny łańcucha i **żadna nie jest
dodawana**. Tożsamość to strukturalna kompozycja istniejącego `approval_ref`:

```
--cold-start-chain-ref   TELEMATICS-COLD-START-ECHO00001-2026-08
--approval-ref okna      TELEMATICS-COLD-START-ECHO00001-2026-08-W01
                         TELEMATICS-COLD-START-ECHO00001-2026-08-W02
```

Chain ref sam nie może kończyć się segmentem `-W<NN>`, więc segment okna jest rozstrzygalny bez
kontekstu: okno jednego łańcucha nigdy nie może zostać pomylone z oknem innego ani z samym chain
refem. Każde okno zachowuje własny unikalny `approval_ref`, więc
`uq_client_dataset_recovery_run_approved_window` zachowuje pełne znaczenie. `reason` **nie** niesie
tożsamości — to swobodny tekst bez kontraktu strukturalnego.

*Który kontrakt obowiązuje, decyduje stan, nie flaga.* Cel bez żadnego wiersza zadeklarowanego
łańcucha to **pierwsze okno** i musi spełnić wszystkie pierwotne bramki zero-state bez zmian:

- dokładnie jeden schedule celu i jest `disabled`, zgodny z `--expected-schedule-id`;
- `--confirm-schedule-disabled`;
- tryb celu `data_invariants_v1`;
- dokładnie jeden wiersz coverage `READY` utworzony z zaakceptowanego dowodu cold-start
  (prefiks `telematics-cold-start-bootstrap/1`), `covered_through_source = 'bootstrap'`, `A == W`;
- `--expected-coverage-fingerprint` zgodny z zapisanym wierszem;
- `--expected-old-covered-through` równy baseline'owi i `--window-start` równy `W`;
- zero wierszy historii, zero wierszy recovery, zero runów platformowych celu;
- `--approval-ref` musi być `W01` zadeklarowanego łańcucha;
- `--window-end` równy `--approved-shifted-cutoff-boundary` i równy deterministycznej granicy
  następnego okna, mieszczący się w `now − D` i w `R`;
- `--execute` plus `--confirm-client-code`.

Cel, który **już** niesie wiersze tego łańcucha, to **kontynuacja** i musi spełnić wszystkie naraz:

- schedule nadal `disabled`, tryb nadal `data_invariants_v1`;
- dokładnie jeden wiersz coverage `READY`, `covered_through_source = 'manual_recovery'`, `A` nadal
  pierwotnym baseline'em cold startu, `A < W`, odcisk bieżącego coverage zgodny;
- **każdy** wiersz recovery celu należy do tego łańcucha — obcy wiersz odmawia;
- **każdy** wiersz łańcucha jest `SUCCESS`; wiersz `FAILED`, `FINALIZATION_CONFLICT`, `PLANNED` lub
  `RUNNING` odmawia i **nigdy nie jest pomijany** tylko dlatego, że poproszono o kolejne okno;
- ordinale dokładnie `1..N` bez dziur i duplikatów, każdy wiersz z dokładnie tym klientem, schedulem
  i datasetem, okno 1 startuje w `A`, każde kolejne tam, gdzie skończyło się poprzednie, a ostatnie
  kończy dokładnie w bieżącym `W`;
- każde okno łańcucha wskazuje dokładnie jeden istniejący run platformowy o statusie `SUCCESS`,
  żadne dwa okna nie dzielą runu i żaden run celu nie jest poza łańcuchem;
- nadal zero wierszy historii schedule'a i brak żywego procesu celu;
- żądane okno to następny ordinal, startuje dokładnie w `W`, kończy dokładnie na deterministycznej
  następnej granicy, mieści się w `R` i ma własny unikalny `--approval-ref`;
- dry-run nadal domyślny, brak automatycznego retry.

**Jedno wywołanie planuje i wykonuje dokładnie jedno okno.** Nie ma polecenia iterującego po całym
planie; każde okno to osobne zatwierdzenie i osobne wykonanie. Schedule pozostaje `disabled` przez
**cały** łańcuch i **nie staje się claimable przez dispatchera** —
`dispatcher._load_enabled_schedules` selektuje `WHERE cds.enabled = true AND ca.enabled = true`.
Zachowanie recovery dla schedule'ów `enabled` jest bez zmian.

**5. Aktywacja schedule'a** — `ops/activate_telematics_trips_schedule.py`, dry-run first. **Nie
wymaga dokładnie jednego wiersza recovery.** Wymaga, żeby cały stan recovery celu był dokładnie
jednym nazwanym łańcuchem `N` udanych, stykających się okien: tryb `data_invariants_v1`; dokładnie
jeden wiersz coverage `READY`; `covered_through_source = 'manual_recovery'`; `W` równe jawnie
zatwierdzonej końcowej granicy łańcucha (`--approved-final-chain-boundary` musi równać się
`--expected-covered-through`); odcisk coverage zgodny; `--cold-start-chain-ref` podany jawnie;
`--expected-successful-window-count` równy faktycznemu `N`; wszystkie wiersze łańcucha `SUCCESS`;
okno 1 startuje w `A`, kolejne stykają się, ostatnie kończy w `W`; jeden run platformowy `SUCCESS`
na okno, bez duplikatów i bez runów spoza łańcucha; brak obcego wiersza recovery; brak aktywnego
recovery; **zero** wierszy historii schedule'a celu; brak konkurencyjnego schedule'a; schedule
aktualnie `disabled`; `--execute` plus `--confirm-client-code`.

Wykonanie: jedna transakcja, lock `client_account` → `client_dataset_schedule`, `UPDATE` dokładnie
jednego wiersza i dokładnie jednego pola `enabled false → true` (nawet `updated_at` nie jest
ruszane), weryfikacja po zapisie porównująca **wszystkie pozostałe kolumny** bajtowo z pre-image
spod tego samego locka, read-back na niezależnym połączeniu read-only. Równoległa aktywacja, która
już się zatwierdziła, jest raportowana jako `ALREADY_ENABLED` z `database_writes_performed = 0` —
bez drugiego zapisu.

Wykonanie każdego okna łańcucha osobno (dry-run first za każdym razem):

```bash
PYTHONPATH="$PWD" python3 ops/recover_telematics_trips_window.py \
  --client-code <CODE> --dataset trips_sync \
  --allow-disabled-schedule-for-cold-start --confirm-schedule-disabled \
  --cold-start-chain-ref TELEMATICS-COLD-START-<CODE>-<YYYY-MM> \
  --approval-ref TELEMATICS-COLD-START-<CODE>-<YYYY-MM>-W<NN> \
  --window-start <bieżące W> \
  --window-end <deterministyczna granica tego okna> \
  --approved-shifted-cutoff-boundary <ta sama granica> \
  --approved-final-chain-boundary <zatwierdzona granica końcowa łańcucha> \
  --expected-old-covered-through <bieżące W> \
  --expected-schedule-id <SCHEDULE_UUID> \
  --expected-coverage-fingerprint <64 hex bieżącego coverage> \
  --reason "cold-start chain window <NN>" \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
# dopiero po przeglądzie dry-runu dodać: --execute --confirm-client-code <CODE>
```

Dry-run raportuje `cold_start_chain_ref`, `cold_start_window_ordinal`, ukończone okna,
`cold_start_chain_remaining_plan` i `cold_start_chain_total_windows` — plan do recenzji, nigdy
autoryzację.

```bash
PYTHONPATH="$PWD" python3 ops/activate_telematics_trips_schedule.py \
  --client-code <CODE> --dataset trips_sync \
  --expected-schedule-id <SCHEDULE_UUID> \
  --expected-covered-through <W po ostatnim oknie> \
  --expected-coverage-fingerprint <64 hex> \
  --cold-start-chain-ref TELEMATICS-COLD-START-<CODE>-<YYYY-MM> \
  --expected-successful-window-count <N> \
  --approved-final-chain-boundary <W po ostatnim oknie> \
  --approval-ref <TICKET> \
  --expected-environment production \
  --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629
# dopiero po przeglądzie dry-runu dodać: --execute --confirm-client-code <CODE>
```

**Zachowanie przy awarii i rollback.**

- Przed aktywacją każda awaria zostawia schedule `disabled`, więc **żadna zaplanowana praca
  dispatchera nie staje się claimable**. Nie jest potrzebna żadna akcja operatora, żeby ten stan
  utrzymać.
- Jeśli recovery zawiedzie po zmianie trybu: terminalny dowód recovery (`FAILED` albo
  `FINALIZATION_CONFLICT` z klasyfikacją) jest **zachowywany**, coverage nie przesuwa się (`W`
  bajtowo bez zmian, potwierdzane odciskiem), schedule pozostaje `disabled`, **nie ma
  automatycznego retry** i **wiersz baseline nie jest automatycznie usuwany**.
- Jeśli zawiedzie **późniejsze okno łańcucha**: wszystkie wcześniejsze udane wiersze recovery i
  wiersz terminalny nieudanego okna są **zachowywane**, `W` zostaje na ostatnim pomyślnie
  sfinalizowanym oknie, schedule pozostaje `disabled`, aktywacja **odmawia**, a kontynuacja łańcucha
  jest odmawiana (`RECOVERY_REFUSED_COLD_START_CHAIN`) dopóki nieudany wiersz istnieje. Nieudany
  wiersz **nigdy nie jest cicho pomijany** przez poproszenie o kolejne okno; wznowienie wymaga nowej
  jawnej decyzji operatora i nowego `--approval-ref`.
- Cofnięcie trybu do `strict_meta` w tym stanie jest **opcjonalne, nie wymagane**, dopóki schedule
  pozostaje `disabled`: przy wyłączonym schedule'u dispatcher w ogóle nie czyta coverage ani nie
  wchodzi w ścieżkę compatibility, więc para „`data_invariants_v1` + disabled schedule" jest
  bezczynna. Cofnięcie jest wskazane wyłącznie, jeśli klient ma pozostać w tym stanie dłużej niż
  jedna sesja operacyjna — wtedy obowiązuje reguła „`strict_meta` tylko jako stan onboardingowy
  przed bootstrapem". Destrukcyjny rollback usuwający dowody jest zabroniony.
- Ponowna próba po awarii wymaga **nowej autoryzacji** i nowego `--approval-ref`; ten sam
  klient × schedule × interwał × approval jest odrzucany przez
  `uq_client_dataset_recovery_run_approved_window`.

**Czego ta ścieżka nigdy nie robi.** Nie syntetyzuje historii schedule'a, nie tworzy fikcyjnego
udanego runu, nie wymyśla historycznego interwału, nie używa `A − 1 s`, **nie włącza schedule'a
tymczasowo, żeby zaspokoić preflight**, i nie twierdzi gotowości raportowej przed recovery.

**Testy.** `ops/tests_manual/test_telematics_cold_start_chain.py` (czysty, bez bazy: podział okien,
tożsamość łańcucha, ciągłość, korespondencja runów), `test_telematics_cold_start_audit.py`,
`test_telematics_cold_start_bootstrap_postgres.py`, `test_telematics_cold_start_recovery_postgres.py`,
`test_telematics_schedule_activation_postgres.py` — disposable PostgreSQL 16, zero requestów do
providera (dostęp sieciowy spoza loopbacku jest w tych testach fatalny), zero prawdziwych
podprocesów biznesowych. Do zestawu należy także
`test_telematics_coverage_state_schema_postgres.py`, który utrzymuje kontrakt słownikowy C5 dla tych
modułów (patrz niżej).

**Rejestracja w kontrakcie słownikowym C5 (wykonana).**
`ops/tests_manual/test_telematics_coverage_state_schema_postgres.py` był czerwony od commitu
wprowadzającego cold start (`373e017`) aż do commitu rejestrującego: jego allowlista
`C5_ALLOWED_IDENTIFIERS_BY_MODULE` nie rejestrowała nowych modułów, więc ich legalne użycie słownika
coverage było zgłaszane jako `unauthorized coverage vocabulary`. **Korekta wcześniejszego zapisu:**
zestaw okien łańcucha jednak *dodał* czwartego offendera — `ops/telematics_cold_start_chain.py` nazywa
`covered_through_ts` — więc naruszeń było cztery moduły, nie trzy.

Wszystkie cztery moduły zostały niezależnie przejrzane i **jawnie zarejestrowane** pod tym samym
kontraktem deny-by-default, każdy z **dokładnym** zestawem identyfikatorów (nie wspólnym,
poszerzonym zbiorem) — pominięcia są celowe i są częścią kontraktu:

| Moduł | Dozwolony słownik | Powierzchnia zapisu coverage |
|---|---|---|
| `ops/audit_telematics_cold_start.py` | `client_dataset_coverage`, `coverage_start_ts`, `covered_through_ts` | brak — wyłącznie `SELECT`; `bootstrap_status` jest **zabroniony**, bo odczyt statusu zakładałby istnienie wiersza coverage, czemu przeczy bramka zero-state |
| `ops/bootstrap_telematics_cold_start_coverage.py` | j.w. + `bootstrap_status`, `bootstrap_evidence_ref`, `covered_through_source`, `last_gap_detected_ts` | dokładnie jeden `INSERT` w jednej nazwanej funkcji `_insert_baseline_row`, w tym samym zatwierdzonym kształcie co writer C10; `UPDATE`, `DELETE`, `ON CONFLICT` i lock coverage pozostają zabronione; `nominal_window_*` zabronione (to dowód claim-time historii, której to narzędzie nie pisze) |
| `ops/activate_telematics_trips_schedule.py` | `coverage_start_ts`, `covered_through_ts`, `bootstrap_status`, `covered_through_source` | brak — czyta przez `coverage_finalization.read_coverage_row`; **nie wolno mu nazwać tabeli coverage**, dokładnie jak manualnemu CLI recovery |
| `ops/telematics_cold_start_chain.py` | `covered_through_ts` | brak — moduł czysty, bez połączenia i bez SQL |

Kontrakt nie został poszerzony: dokładnie **dwa** moduły mogą trzymać `INSERT` coverage (historyczny
writer C10 i writer cold startu), każdy w jednej nazwanej funkcji i jednym zatwierdzonym kształcie,
a nazwa funkcji jednego nie jest autoryzowana w drugim. Lock i `UPDATE` coverage pozostają wyłącznie
w finalizerze C6 i we współdzielonym module C11. Rejestracja jest po **dokładnej ścieżce** pliku —
nie istnieje forma katalogowa ani wildcard, a odkrywanie nadal enumeruje cały śledzony przez Git
zbiór plików `.py`, więc każdy nowy, niezarejestrowany moduł używający tego samego słownika nadal
jest odrzucany. Testy negatywne dowodzące tego wszystkiego (w tym że dodanie mutacji coverage do
narzędzia read-only oraz `UPDATE`/`DELETE` do writera cold startu nadal odmawia) żyją w tej samej
zawężanej suicie, nie w równoległej słabszej.

##### Rollout produkcyjny `ECHO00001` z `2026-08-04` — NIEUDANY, zatrzymany na W01

**Klasyfikacja: `SWEE_COLD_START_W01_FAILED`. `ECHO00001` NIE jest onboardowany, NIE jest
reporting-ready i jego schedule pozostaje `enabled = false`.** Recenzowany stack trzech commitów
(`373e017`, `3a95a10`, `3f57293`) został wypchnięty na `origin/main` i jest niezmieniony. Rollout
zatrzymano po pierwszym oknie recovery, przed W02 i przed aktywacją.

**Wykonane kroki (w kolejności).**

| Krok | Wynik |
|---|---|
| Dowód cold-start | `COLD_START_ZERO_STATE_CONFIRMED`, hash kanoniczny `227811108eadaa79b7f2622f735b7b073c91dc7d00038319a19ca30394364279`, plik `/home/logplatform/coverage-evidence/ECHO00001/echo00001_trips_sync_coldstart_20260804T174156Z.json` (`0600` w katalogu `0700`), zatwierdzona granica końcowa `2026-08-04T14:41:56Z` |
| Plan okien | dokładnie dwa: `W01 2026-07-01T00:00:00Z → 2026-08-01T00:00:00Z`, `W02 2026-08-01T00:00:00Z → 2026-08-04T14:41:56Z` |
| Baseline bootstrap | jeden `INSERT`, `A == W == 2026-07-01T00:00:00Z`, `READY`, `source = bootstrap`, odcisk `fc6d47ea1bc089b4c3cbc2bc5a5843e1de61dcb97ad575bc05b876a3a5a01475` |
| Zmiana trybu | wąska transakcja jednego wiersza: `strict_meta → data_invariants_v1`, `affected_rows = 1`, odcisk coverage niezmieniony |
| Recovery W01 | `recovery_run_id = 30491719-32cb-4fd7-88d6-481362f8900b`, `platform_run_id = 2733f673-745c-46a9-8e51-5b513b0edc6b`, status `SUCCESS` — **ale run biznesowy nie pobrał niczego** |
| Recovery W02 | **nie wykonany** |
| Aktywacja | **nie wykonana** |

**Defekt (BLOCKER, nie naprawiany w tym zadaniu).**
`jobs/api/telematics/sync_trips_and_speeding.py` ma własną bramkę:

```python
if not schedule.enabled:
    client.log("INFO", ..., "Dataset schedule disabled; skipping run.", ...)
    return
```

Ścieżka cold start **wymaga** wyłączonego schedule'a przez cały łańcuch, więc każde okno recovery
cold startu trafia w ten wczesny `return`. Job kończy się kodem `0`, run platformowy dostaje status
`SUCCESS`, a `ops/recover_telematics_trips_window.py` traktuje `returncode == 0` jako sukces biznesowy
i przesuwa `covered_through_ts` przez CAS C6. **Efekt: `W` przesunięto o miesiąc przy zerowym
pobraniu danych.** Run `403d970f` trwał `0,09 s`, wykonał **0 requestów do providera**, **0 zapisów
biznesowych** i zapisał pięć wierszy logu, z których czwarty to `Dataset schedule disabled; skipping
run.`

To jest **fałszywe roszczenie coverage w produkcji**: wiersz `ECHO00001` deklaruje zweryfikowany
przedział `[2026-07-01T00:00:00Z, 2026-08-01T00:00:00Z]`, podczas gdy
`echogallery_main.public.client_trips` ma `0` wierszy.

**Dlaczego suita testowa tego nie złapała.** `ops/tests_manual/test_telematics_cold_start_recovery_postgres.py`
podmienia `rc.launch_sync` na dublerów `FakeLaunch` / `ChainLaunch`, które zwracają `returncode = 0`
i rejestrują run platformowy `SUCCESS`. Prawdziwy moduł `sync_trips_and_speeding` nigdy nie jest
uruchamiany przeciwko wyłączonemu schedule'owi, więc bramka `schedule.enabled` nie jest pokryta
przez żaden test cold startu. Wszystkie 17 wymaganych suit przechodzi z kodem `0` i nadal
przechodziłoby po naprawie.

**Stan zachowany (odczyt read-only, `2026-08-04T17:46:21Z`).**

| Obiekt | Wartość |
|---|---|
| `trips_pagination_mode` | `data_invariants_v1` (zmieniony, nie cofnięty) |
| Coverage | jeden wiersz `READY`, `A = 2026-07-01T00:00:00Z`, `W = 2026-08-01T00:00:00Z`, `source = manual_recovery`, `last_gap_detected_ts = NULL`, odcisk `a22c8cc5d927b37669c3456c0403bdbc496751e746237ede1e264f775a620614` |
| Recovery | jeden wiersz `SUCCESS`, `approval_ref = TELEMATICS-COLD-START-ECHO00001-2026-08-W01` |
| Runy platformowe | jeden, `SUCCESS`, no-op |
| Schedule | `enabled = false`, `updated_at` nadal `2026-04-29T08:20:49Z` — **nietknięty** |
| Historia schedule'a | `0` wierszy — żadnego syntetycznego fire'a |
| `client_trips` | `0` wierszy |

**Dlaczego ten stan jest bezpieczny do pozostawienia.** `dispatcher._load_enabled_schedules`
selektuje `WHERE cds.enabled = true`, więc wyłączony schedule jest dla dispatchera niewidoczny.
Fałszywe `W` nie może zostać użyte przez żaden scheduled fire, dopóki schedule pozostaje wyłączony.
**Nie wolno aktywować `ECHO00001`, dopóki defekt nie zostanie naprawiony i dopóki fałszywy przedział
nie zostanie rozliczony osobną, recenzowaną bramką.**

**Czego nie zrobiono i dlaczego.** Nie ponowiono recovery (zakaz automatycznego ponowienia i brak
autoryzacji na trzecie wykonanie), nie wykonano W02, nie aktywowano schedule'a, nie usunięto ani nie
poprawiono ręcznie wiersza coverage ani wiersza recovery (zakaz ręcznego SQL na coverage i zakaz
mutowania CAS), nie cofnięto zmiany trybu (nieautoryzowana), nie edytowano kodu runtime.

**Wymagane następne bramki (osobno autoryzowane).**

1. ~~Naprawa defektu~~ — **wykonana `2026-08-04`**, patrz „Utwardzenie ścieżki onboardingu" poniżej.
2. Rozliczenie fałszywego `W` dla `ECHO00001` (reseed baseline'u albo `GAP_DETECTED`) — nie da się
   tego zrobić żadnym z obecnych narzędzi bez nowej bramki. **Nadal otwarte.**
3. ~~Pokrycie testowe uruchamiające prawdziwy `sync_trips_and_speeding` przeciwko wyłączonemu
   schedule'owi~~ — **wykonane**, `ops/tests_manual/test_telematics_recovery_execution_path_postgres.py`.
4. Dopiero potem ewentualny ponowny rollout `ECHO00001`. **Nadal otwarte i nieautoryzowane.**

##### Utwardzenie ścieżki onboardingu — `2026-08-04`

Naprawa jest **generyczna** i dotyczy każdego przyszłego klienta produkcyjnego. `ECHO00001` jest
z niej jawnie wyłączony i pozostaje w kwarantannie (patrz „Kwarantanna `ECHO00001`" na końcu tej
podsekcji).

**Przyczyna źródłowa.** Wyłączony schedule powodował, że job biznesowy zwracał sukces bez wykonania
pracy (`Dataset schedule disabled; skipping run.`, kod wyjścia `0`), a
`ops/recover_telematics_trips_window.py` traktował `returncode == 0` jako wystarczający dowód sukcesu
biznesowego i przesuwał coverage mimo zera requestów providera, zera stron, zera transakcji
biznesowych, zera wierszy przygotowanych i zera wierszy zapisanych.

**Normatywna maszyna stanów onboardingu.** Klient **nie może pominąć stanu**, a samo istnienie
wiersza coverage **nie oznacza** gotowości raportowej:

```
CREATED_DISABLED_STRICT → ZERO_STATE_VERIFIED → BASELINE_CREATED
→ COMPATIBILITY_MODE_SET → RECOVERY_EXECUTED_AND_COMMITTED → COVERAGE_VERIFIED
→ SCHEDULE_ACTIVATED → FIRST_NATURAL_FIRE_VERIFIED → PRODUCTION_READY
```

**Trzy warstwy naprawy** (wymagane łącznie; kontrakt normatywny: `docs/13_…` §13.6a.0):

1. **Jawna autoryzacja manual-recovery** (`jobs/api/telematics/manual_recovery_authority.py`) —
   koniunkcja parametrów joba, atestacji uruchomienia w
   `TELEMATICS_MANUAL_RECOVERY_AUTHORITY` i trwałego wiersza recovery `RUNNING` o zgodnych
   tożsamościach i oknie, przy wciąż wyłączonym schedule'u. Sama flaga logiczna ani bezpośrednie
   wywołanie joba nie wystarczają. Dispatcher nie może jej ustawić ani odziedziczyć.

   **Poziom zaufania — dokładnie.** Atestacja jest *atestacją operacyjną i zabezpieczeniem przed
   przypadkowym użyciem*, nie granicą bezpieczeństwa i nie uwierzytelnieniem. Nie zawiera sekretu
   ani wartości losowej; wszystkie jej pola są stałymi modułu lub identyfikatorami podawanymi przez
   operatora, więc proces lokalny działający jako **ten sam użytkownik systemowy** i znający te
   identyfikatory może zbudować równoważną atestację. Nazwa launchera jest kontrolą spójności, a nie
   dowodem pochodzenia wywołania. Nie zakładać ochrony przed złośliwym procesem tego samego
   użytkownika, operatorem świadomie budującym atestację, przejęciem konta usługowego ani przejęciem
   poświadczeń bazy platformowej. Realną bramką autoryzacyjną jest **trwały wiersz recovery
   `RUNNING`** w control-plane, a nie atestacja; wraz z izolacją dispatchera to właśnie te dwa
   elementy wykluczają przypadkowe uruchomienie. Wiersz recovery **nie** jest klaimowany w osobnym,
   wcześniejszym kroku operatora — `ops/recover_telematics_trips_window.py` klaimuje go i uruchamia
   proces potomny w tym samym wywołaniu: jedna transakcja wstawia dokładnie jeden wiersz `RUNNING`
   po ponownej ewaluacji bramek pod blokadami klaimu, potem budowana jest atestacja, potem startuje
   proces potomny, a na końcu odczytywany jest wynik terminalny i wykonywana finalizacja. Operator
   dostarcza wcześniej **zatwierdzenie** (klient, dataset, okno, oczekiwany watermark, powód,
   `approval_ref`); dry-run jest domyślny i zatrzymuje się przed klaimem.
2. **Strukturalny wynik terminalny** (`jobs/api/telematics/execution_outcome.py`) — dokładnie jeden
   ściśle parsowany rekord JSON na proces biznesowy, pod ścieżką z
   `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE`.
3. **Bramka finalizacji coverage** — `returncode = 0` **oraz** `EXECUTED_COMMITTED` /
   `EXECUTED_ZERO_ROWS_COMMITTED` **oraz** `provider_execution_entered = true` **oraz**
   `business_transaction_entered = true` **oraz** transakcja `COMMITTED` **oraz** `skipped = false`
   **oraz** zgodność tożsamości i okna **oraz** obecna po obu stronach i dokładnie równa tożsamość
   runu platformowego. Wejście do providera, wejście do transakcji i commit to trzy **niezależne**
   stwierdzenia; rekord twierdzący commit bez wejścia do providera jest odrzucany jako wewnętrznie
   sprzeczny przy parsowaniu.

**Zapamiętać:**

- `returncode == 0` **nie wystarcza**;
- praca **pominięta nigdy** nie przesuwa coverage;
- praca **zacommitowana z zerem wierszy może** przesunąć coverage;
- dla wyniku mogącego przesunąć coverage brakujący, pusty lub zniekształcony `platform_run_id`
  jest **odmową**, a nie „brakiem czego porównywać”; dla rekordów pominięcia i `FAILED` pole
  pozostaje opcjonalne. To samo wymaganie obowiązuje każde okno łańcucha przy aktywacji.

**Rozszerzenie M3 — ta sama bramka obowiązuje teraz scheduled fire'y.** Powyższy opis powstał dla
powierzchni manual recovery. Od release'u `fabaaa753f89` (aktywny `2026-08-13T10:59:21Z`) ten sam
kontrakt wyniku obowiązuje **scheduled** compatibility fire `trips_sync`: dispatcher przyznaje
procesowi potomnemu świeżą, per-uruchomieniową ścieżkę rekordu, a
`_require_coverage_eligible_outcome` musi go przyjąć, zanim `_finalize_compat_success` w ogóle
zostanie wywołany. Różnica względem recovery jest jedna i celowa: scheduled claim wymaga, by
`recovery_run_id` był **nieobecny**, co odrzuca rekord manual-recovery podstawiony pod scheduled
fire — i symetrycznie `evaluate_execution_evidence` odrzuca brak `recovery_run_id`, więc bramka
recovery nie może odziedziczyć odczytu ze scheduled fire'a. Triage kodów odmowy i zapytania
inspekcyjne: § 5.5.1. Potwierdzone produkcyjnie `2026-08-14` na trzech naturalnych fire'ach
(`DELTA00001`, `ALPHA00001`, `FOXTROT00001`) — wszystkie `EXECUTED_COMMITTED`, zero odmów.

**Zapobieganie aktywnemu `strict_meta`.** `jobs/api/telematics/schedule_mutation_surfaces.py` jest
rejestrem **deny-by-default**: każda ścieżka tworząca lub włączająca schedule musi być w nim
zarejestrowana, aktywacja `trips_sync` przy `strict_meta` jest odmawiana
(`SCHEDULE_ACTIVATION_REFUSED_STRICT_META`), a `trips_sync` musi być tworzony wyłączony. Rejestr ma
**trzy klasy mutacji** i trzy zarejestrowane powierzchnie:

| klasa | powierzchnie |
|---|---|
| `CREATION_SURFACES` | `scripts/onboard_workflow_a_client.py`, `ops/manage_telematics_reconciliation_schedule.py` |
| `ACTIVATION_SURFACES` | `ops/activate_telematics_trips_schedule.py`, `ops/manage_telematics_reconciliation_schedule.py` |
| `DEACTIVATION_SURFACES` | `ops/manage_telematics_reconciliation_schedule.py` — **wyłącznie** |

Klasa dezaktywacji jest osobna celowo: `ops/activate_telematics_trips_schedule.py` **nie** ma prawa
niczego wyłączać, więc **żadna zarejestrowana powierzchnia nie może wyłączyć schedule'a bazowego
(`DAILY`)** — to jedyna mutacja, której model coverage nie wykrywa. Odwracalna jest wyłącznie kadencja
rekoncyliacji (M6/M7): `ops/manage_telematics_reconciliation_schedule.py disable` przywraca wiersz
`WEEKLY_RECONCILIATION` do `enabled = false`, **nie kasuje** wiersza ani jego historii wykonań,
nie dotyka wiersza `DAILY`, jest idempotentna dla wiersza już wyłączonego i — jak `register`
i `enable` — wymaga `--execute`, `--approval-ref`, `--confirm-client-code`, `--expected-environment`
i `--expected-platform-uuid`. Wyłączenie w trakcie trwającego runu dotyczy **wyłącznie przyszłych
odpaleń**: dyspozytor czyta `enabled` raz, na początku ticku, więc run już zaklaimowany kończy się
normalnie. Test odmowy skanuje repozytorium i nie przepuszcza nowej, niezarejestrowanej powierzchni
mutacji. **Migracja nie była potrzebna** — enforcement aplikacyjny pokrywa wszystkie wspierane
ścieżki mutacji.

##### Procedura onboardingu przyszłego klienta

Każdy krok jest osobną bramką; kolejność jest obowiązkowa i żadnego stanu nie wolno pominąć.

| # | Stan | Narzędzie / warunek bramki |
|---|---|---|
| 1 | `CREATED_DISABLED_STRICT` | `scripts/onboard_workflow_a_client.py --apply`; wynik: `strict_meta` + `trips_sync enabled = false`, jawnie **nie** production-ready, emitowana referencja stanu |
| 2 | `ZERO_STATE_VERIFIED` | `ops/audit_telematics_cold_start.py` (read-only); wymagane `COLD_START_ZERO_STATE_CONFIRMED` |
| 3 | `BASELINE_CREATED` | `ops/bootstrap_telematics_cold_start_coverage.py`; dokładnie jeden wiersz o zerowej szerokości, `A == W` |
| 4 | `COMPATIBILITY_MODE_SET` | wąska, jednowierszowa transakcja `strict_meta → data_invariants_v1` (§13.6 krok 6) |
| 5 | `RECOVERY_EXECUTED_AND_COMMITTED` | `ops/recover_telematics_trips_window.py --allow-disabled-schedule-for-cold-start …`, jedno zatwierdzone okno na wywołanie; wymagany strukturalny wynik `EXECUTED_COMMITTED` lub `EXECUTED_ZERO_ROWS_COMMITTED` z transakcją `COMMITTED` |
| 6 | `COVERAGE_VERIFIED` | odczyt coverage **oraz** bazy biznesowej klienta; `W` musi odpowiadać rzeczywiście pobranym danym |
| 7 | `SCHEDULE_ACTIVATED` | `ops/activate_telematics_trips_schedule.py`; wymaga `data_invariants_v1`, zweryfikowanego coverage ze źródła `manual_recovery`, strukturalnego dowodu wykonania dla **każdego** okna łańcucha, braku aktywnego/nieudanego/niejednoznacznego recovery, dokładnego oczekiwanego `W` i odcisku, oraz wyłączonego schedule'a; zmienia dokładnie jedno pole |
| 8 | `FIRST_NATURAL_FIRE_VERIFIED` | pierwszy naturalny fire dispatchera zakończony `SUCCESS` z przesunięciem `W` ze źródła `scheduled_run` |
| 9 | `PRODUCTION_READY` | dopiero teraz klient jest oznaczany jako produkcyjny i raportowy |

Dry-run jest domyślny dla każdego narzędzia zapisującego; `--execute` zawsze wymaga dodatkowego
`--confirm-client-code`. Nic nie jest ponawiane automatycznie: nieudane okno zostawia dowody,
wyłączony schedule i niezmieniony coverage, i wymaga osobnej decyzji operatora.

**Uwaga o wierszach recovery sprzed `2026-08-04`.** Nie niosą one strukturalnego dowodu wykonania,
więc aktywacja zbudowana na nich jest odmawiana (`ACTIVATION_REFUSED_EXECUTION_PROOF`). Czterej
aktywni klienci (`BRAVO00016`, `ALPHA00001`, `DELTA00001`, `FOXTROT00001`) mają już włączone schedule'e
i nie wymagają aktywacji, więc zmiana ich nie dotyczy.

##### Kwarantanna `ECHO00001`

`ECHO00001` jest **poza zakresem** utwardzenia i nie jest kryterium jego ukończenia. Utrzymywany
stan, niezmieniony: schedule `enabled = false`, tryb `data_invariants_v1` bez zmian, brak recovery
W02, brak aktywacji, wiersz coverage i wiersz recovery nietknięte, brak wierszy historii schedule'a.
**Istniejące fałszywe coverage `SWEE` pozostaje jawnie nieważne i nie wolno go używać do
raportowania.**

Pozostałe opcje dla `ECHO00001` — żadna nie jest wykonywana w ramach utwardzenia:

1. pozostawić w kwarantannie bezterminowo;
2. wykonać później niezależnie zrecenzowaną naprawę;
3. wykonać później kontrolowaną dekomisję i czysty ponowny onboarding.

### 5.6 Workflow A — stale / stuck dispatcher RUNNING rows

Dispatcher automatycznie oznacza jako `FAILED` wiersze `RUNNING` starsze niż `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES` (default `720` minut). Jeśli trzeba odblokować ręcznie przed timeoutem, operator może oznaczyć row jako failed:

```sql
UPDATE workflow_a_control.client_schedule_run_history
   SET status='FAILED',
       finished_at=now(),
       error_summary='manual unblock: dispatcher killed'
 WHERE status='RUNNING';
```

Jeżeli taki stale sweep lub manual unblock wygra z późnym finalizerem compatibility, C6 zwraca `TRIPS_HISTORY_CLAIM_LOST`, cofa transakcję i nie wykonuje coverage mutation ani terminal-history overwrite.

### 5.6a Execution watchdog — terminalne statusy `public.runs`

`ops/execution_watchdog.py` traktuje jako terminalne wszystkie trzy finalne statusy:

`SUCCESS`, `FAILED`, `CANCELED`

`RUNNING` jest jedynym statusem nieterminalnym, więc **tylko `RUNNING` może zestarzeć się do
`STALE`**. Wcześniej watchdog rozpoznawał wyłącznie `SUCCESS` i `FAILED`, więc run w stanie
`CANCELED` po przekroczeniu `stale_grace_minutes` generował incydent „execution stuck" dla
egzekucji, która była już rozstrzygnięta. Luka była niewidoczna, bo nic nie zapisywało
`CANCELED`; `ops/reconcile_historical_run.py` jest jej pierwszym realnym writerem.

`CANCELED` zwraca `EXPECTED_FAILED` (nie alertuje) z własnym tytułem — w odróżnieniu od
`FAILED` nie ma awarii, którą ścieżka jobu miałaby ponownie zgłosić.

Wokabularz jest zduplikowany lokalnie w watchdogu (jednostka systemd nie może ciągnąć
zależności FastAPI ani boto3), a test
`test_terminal_status_vocabulary_matches_the_platform` pilnuje zgodności z `api/main.py`
i `api/platform_prune.py`.

### 5.6b Execution watchdog — tożsamość subjectu Workflow A a `run_type`

Od migracji `062_*` tożsamością harmonogramu jest `uq_client_dataset_schedule
UNIQUE (client_id, dataset_name, run_type)`, więc jeden dataset klienta może mieć
kilka ról jednocześnie (`DAILY`, `WEEKLY_RECONCILIATION`, `MONTHLY_RECONCILIATION`).
Watchdog budował `subject_key` wyłącznie z `(client_code, dataset_name)`, więc role
siostrzane trafiały do **jednego** wiersza `ops_control.watchdog_observation`.

To nie była kosmetyczna kolizja. W obrębie jednego skanu:

* werdykt jednej roli nadpisywał werdykt drugiej;
* druga rola odczytywała cudzy werdykt jako `previous`;
* `observation_count` rósł raz na kolizję, nie raz na podmiot;
* `WEEKLY OK` mogło zamknąć incydent otwarty przez `DAILY MISSING` w tym samym przebiegu;
* wynik zależał od kolejności wierszy, bo zapytanie nie miało pełnego tiebreaku.

**Kontrakt tożsamości** (`ops/execution_watchdog.py::workflow_a_subject_key`):

| rola | `subject_key` |
| --- | --- |
| `DAILY` | `workflow_a:{client}:{dataset_name}` |
| każda inna | `workflow_a:{client}:{dataset_name}:{run_type}` |

`{client}` to `client_code`, a gdy kodu **nie ma** (`NULL`) — `client_id`. `client_code`
jest **nullowalne z założenia** (migracja `017_*`: „for clients that intentionally do not
have client_code, NULL remains allowed"), a indeks unikalny nie ogranicza powtórzonych
`NULL`-i, więc sam kod nie jest tożsamością klienta: dwóch klientów bez kodu dzielących
dataset kolidowałoby dokładnie tak samo jak dwie role. Każdy harmonogram, który ma kod,
zachowuje ten kod — fallback jest osiągalny wyłącznie dla klienta, który i tak nie miał
w tym kluczu tożsamości. W produkcji wszystkie pięć kont ma kod, więc żaden istniejący
klucz się nie zmienia.

Brak kodu jest sprawdzany jako `is None`, a **nie** jako falsy. `client_code` to
nieograniczony `TEXT` bez `CHECK`-a na niepustość, więc `''` jest wartością *obecną* i
klucz `workflow_a::{dataset_name}` może już istnieć w bazie; podstawienie pod niego
`client_id` przesunęłoby żywy klucz `DAILY` dokładnie tak, jak zrobiłoby to
dokwalifikowanie roli — czyli złamałoby jedyny niepodlegający negocjacji niezmiennik tej
poprawki. Pusty kod współdzielony przez dwóch klientów jest zwykłą kolizją: zgłasza ją
`report_subject_key_collisions()`, a klucze zostają nietknięte.

Ponieważ `client_code` jest nieograniczonym `TEXT`, unikalności nie da się wyprowadzić
z samego schematu: nic nie zabrania nadać jednemu klientowi kodu równego UUID-owi innego
klienta. To konfiguracja patologiczna, nie przypadkowa (w produkcji żadne konto nie jest
bez kodu), więc `report_subject_key_collisions()` **wykrywa i zgłasza**, a nie naprawia:
jeżeli dwa harmonogramy wyliczą ten sam `subject_key`, na stderr trafia
`watchdog_subject_key_collision` z obydwoma `schedule_id`, a klucze zostają bez zmian.

Dokwalifikowanie kolidującej pary przez `schedule_id` wyglądałoby jak naprawa i byłoby
gorsze: klucz zależałby wtedy od tego, **jakie inne wiersze istnieją**, więc poprawienie
albo usunięcie jednego harmonogramu po cichu przesunęłoby tożsamość drugiego i osierociło
historię obserwacji oraz otwarte fingerprinty zebrane pod kluczem dokwalifikowanym.
`subject_key` musi być faktem o jednym harmonogramie. Lekarstwem jest nadanie klientom
różnych kodów — to działanie operatora, a nie zgadywanka watchdoga. W produkcyjnym
dry-runie 54 podmioty Workflow A dają 54 różne klucze i zero kolizji.

`DAILY` zachowuje historyczny klucz **bajt w bajt**. Cała żywa historia obserwacji,
epoka `eligible_since` i otwarte fingerprinty incydentów są adresowane dokładnie tym
ciągiem, a `SuspectedBugEvent.fingerprint_identity()` hashuje `subject_key` — kwalifikowanie
klucza `DAILY` osierociłoby ten stan i otworzyło każdy incydent na nowo pod inną tożsamością.
Role nie-`DAILY` nie mają historii do zachowania, więc noszą kwalifikator.

Kształty nie mogą się aliasować: `dataset_name` jest ograniczone do `^[a-z][a-z0-9_]*$`
(migracja `011_*`), a każdy `run_type` z `ck_client_dataset_schedule_run_type` jest
wielkimi literami, więc żadna legalna nazwa datasetu nie zapisze się jak rola.

`run_type` jest projektowany przez `load_workflow_a_subjects` (bez niego loader szukał
epoki *rodzeństwa*) i trafia do `detail`, które `fingerprint_identity()` pomija — czyli
identyfikuje wiersz obserwacji dla operatora, nie zmieniając żadnego istniejącego
fingerprintu. Enumeracja jest sortowana pełną tożsamością
(`client_code, dataset_name, run_type, schedule_id`); po rozdzieleniu kluczy kolejność
jest już tylko kwestią czytelności, a nie poprawności.

**Stan produkcyjny w chwili poprawki.** Pięć wierszy `WEEKLY_RECONCILIATION` jest
zarejestrowanych i wyłączonych (M6 nie jest włączone), ale sama rejestracja wystarczyła,
by kolizja była realna: `ops_control.watchdog_observation` zawierało wyłącznie klucze
niekwalifikowane, a `workflow_a:ALPHA00001:trips_sync` i `workflow_a:ECHO00001:trips_sync`
były ostatnio zapisane przez wyłączony harmonogram `WEEKLY_RECONCILIATION` (werdykt
`DISABLED` maskujący włączony `DAILY`). Żaden klucz nie-`DAILY` nie istnieje, więc
poprawka **nie wymaga migracji stanu** — pierwszy skan po wdrożeniu odda klucz `DAILY`
harmonogramowi `DAILY` i utworzy osobny klucz dla roli tygodniowej.

Jeden efekt uboczny jest oczekiwany i samonaprawialny: wiersze przejęte przez rolę
tygodniową mają `eligible = false`, więc pierwszy skan po wdrożeniu widzi krawędź
`false → true` i ustawia świeże `eligible_since`. Do najbliższego strzału ten podmiot
raportuje `NOT_YET_EXPECTED` zamiast wstecznie oczekiwać strzałów sprzed poprawki.
Nie gubi to incydentu — w chwili poprawki nie było ani jednego otwartego incydentu
watchdoga — a kolejny strzał jest monitorowany normalnie.

### 5.7 Workflow A — client-business retention worker

To jest osobny mechanizm od platformowego `/maintenance/prune`. Worker czyści wiersze w bazach biznesowych klientów według `workflow_a_control.client_table_retention`.
Po migracji `017_*` policy rows przechowują także `client_code` obok `client_id`; kod jest używany w log contextach dla łatwiejszego filtrowania operatorskiego.

Dry-run:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.retention_purge '{"dry_run":true,"batch_size":5000}'
```

Apply:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.retention_purge '{"dry_run":false,"batch_size":5000}'
```

Parametry filtrujące:

- `client_id`
- `table_name`
- `max_batches`

Proponowany timer czyta parametry z `/etc/log-platform/retention-purge.params.json`:

```bash
sudo tee /etc/log-platform/retention-purge.params.json >/dev/null <<'JSON'
{"dry_run": true, "batch_size": 5000}
JSON

sudo cp ops/systemd/proposed/log-job@retention-purge.service /etc/systemd/system/log-job@retention-purge.service
sudo cp ops/systemd/proposed/log-job@retention-purge.timer /etc/systemd/system/log-job@retention-purge.timer
sudo systemctl daemon-reload
sudo systemctl start log-job@retention-purge.service
```

Zacznij od `dry_run=true`; dopiero po weryfikacji logów i polityk zmień param file na `dry_run=false`.

### 5.8 Platform core — historical reconciliation of an abandoned `public.runs` row

**To nie jest to samo co §5.6.** §5.6 dotyczy `workflow_a_control.client_schedule_run_history`
i ma automatyczny stale reaper w dispatcherze. `public.runs` **nie ma żadnego reapera**:
`ops/execution_watchdog.py` jest expectation-driven, więc ad hoc uruchomienie ręczne nigdy nie
staje się jego subjectem, a jedyny normalny writer — `PATCH /runs/{run_id}` — zawsze stempluje
`ended_at = now()`.

Dlatego run, którego proces zginął przed terminalizacją, zostaje `RUNNING` na zawsze, a użycie
zwykłego endpointu miesiące później zapisałoby wielotygodniową egzekucję, która nigdy nie miała
miejsca.

#### Czym to jest, a czym nie jest

| | |
|---|---|
| **Jest** | administracyjnym domknięciem **jednego imiennie wskazanego** wiersza w rejestrze |
| **Nie jest** | replayem ani retry — nie odtwarza żadnej pracy biznesowej |
| **Nie jest** | bulk cleanupem — nie istnieje ścieżka „domknij wszystkie stale runy” |
| **Nie jest** | zamiennikiem normalnej finalizacji — kontrakt P1-I (`run_context`, `PATCH /runs/{run_id}`) pozostaje nietknięty |

#### Kontrakt

- **Jawna tożsamość.** `--run-id` przyjmuje dokładnie jeden UUID. Brak globów, list i słowa `all`.
- **Jawny status terminalny.** Operator podaje `--status` z `FAILED` albo `CANCELED`. Nic nie jest
  domyślne ani wnioskowane: wiek nie implikuje `FAILED`, a cisza nie implikuje `CANCELED`.
  **`SUCCESS` jest strukturalnie wykluczony** — rekoncyliacja stwierdza, że run nie domknął się
  sam, więc nie może fabrykować nieobserwowanego wyniku biznesowego.
- **Prawdziwy `ended_at`.** Operator podaje `--ended-at` jako historyczny znacznik ISO-8601
  **z jawnym offsetem strefy**. Narzędzie nigdy nie stempluje czasu rekoncyliacji jako czasu
  zakończenia egzekucji. Walidowane: `ended_at >= started_at` oraz `ended_at <= teraz`. Timestamp
  naiwny jest odrzucany, a nie lokalizowany.
- **Trwała proweniencja.** Każda rekoncyliacja zapisuje wiersz w `ops_control.run_reconciliation`
  (migracja `064_*`): `run_id`, wybrany status, podany `historical_ended_at`, `reconciled_at`,
  `actor`, `reason`, `approval_ref`, `repository_head` i opcjonalny `evidence_ref`.
  **Obecność wiersza jest jedynym trwałym sygnałem**, że status terminalny został ustawiony
  administracyjnie, a nie zaobserwowany przez job:

  ```sql
  SELECT r.run_id, r.status, (rc.run_id IS NOT NULL) AS reconciled
    FROM public.runs r
    LEFT JOIN ops_control.run_reconciliation rc USING (run_id);
  ```

- **Fail-closed CAS.** `UPDATE` wymaga w swoim `WHERE` `status = 'RUNNING' AND ended_at IS NULL`,
  więc wiersz domknięty w międzyczasie przez innego writera przegrywa CAS i zostaje nietknięty.
  `UPDATE` i `INSERT` proweniencji dzielą jedną transakcję. `run_id` jest kluczem głównym tabeli
  proweniencji, więc druga rekoncyliacja tego samego runu jest odrzucana przez bazę.
- **Dry-run domyślnie.** Bez `--execute` narzędzie tylko czyta i drukuje proponowaną mutację.

#### Użycie

Podgląd kandydata (read-only; podpowiada obronny `ended_at` na podstawie ostatniego logu):

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_historical_run.py inspect \
    --run-id 612faf1d-917a-4f04-8a01-6f27d5057b90
```

Dry-run:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/reconcile_historical_run.py reconcile \
    --run-id 612faf1d-917a-4f04-8a01-6f27d5057b90 \
    --status FAILED \
    --ended-at '2026-07-10T01:21:21.260753+02:00' \
    --actor 'imie.nazwisko' \
    --reason 'proces przerwany przed finalizacją; superseded przez 49b404e4' \
    --approval-ref 'OPS-2026-08-18-run-ledger'
```

Wykonanie — dopisz `--execute`.

#### Granica autoryzacji

Uruchomienie z `--execute` przeciwko produkcji jest **mutacją danych produkcyjnych** i wymaga
jawnej, bieżącej autoryzacji użytkownika (AGENTS.md §7). Samo istnienie narzędzia, przygotowany
plan ani zielony dry-run nie stanowią takiej autoryzacji. Nieudana mutacja nie jest automatycznie
ponawiana.

#### Skutki dla platform prune

Run, do którego istnieje wiersz w `ops_control.run_reconciliation`, jest **zatrzymywany**
przez `api/platform_prune.py` z powodem `run_retained_reference` — tej samej klasy co run
posiadający logi lub artefakty.

To nie jest kosmetyka. FK proweniencji jest `ON DELETE RESTRICT`, a `_delete_objects`
(MinIO) wykonuje się **przed** transakcją bazodanową. Gdyby zrekoncyliowany run trafił do
`plan.run_ids`, `DELETE FROM runs` zostałby odrzucony przez FK, transakcja SQL cofnęłaby
**wszystkie** usunięcia wierszy w tej partii, a obiekty w MinIO byłyby już skasowane.
Wykluczenie następuje więc na etapie budowy planu, zanim cokolwiek zostanie usunięte.

Zakres celowo wąski: **artefakty i logi zrekoncyliowanego runu nadal podlegają normalnej
retencji** i mogą zostać usunięte. Wiersz proweniencji jest samowystarczalnym dowodem
(status, historyczny koniec, `actor`, `reason`, `approval_ref`, `evidence_ref`) i przeżywa
niezależnie. Rekoncyliacja nie ustanawia nowej, szerokiej polityki retencji.

Środowisko bez migracji `064` prunuje normalnie: `build_prune_plan` sprawdza obecność tabeli
przez `to_regclass` i raportuje `run_reconciliation_absent`, dokładnie jak robi to już
horyzont `provider_request_log` (migracja 061).

Testy deterministyczne: `ops/tests_manual/test_public_runs_historical_reconciliation.py`
(część PostgreSQL wymaga jednorazowej, wyrzucalnej bazy — nigdy `logdb`) oraz
`ops/tests_manual/test_platform_prune.py`.

## 6. Repo vs host

### Platform prune — authoritative host operation

Platform retention is implemented by `api/platform_prune.py` and the thin `ops/platform_prune.sh` entrypoint. It is separate from Workflow A client-business retention and from all Workflow B file retention. The API route is read-only only; `dry_run=false` returns 409.

Safe validation:

```bash
cd /opt/log-platform
ops/platform_prune.sh --dry-run --days 60
```

Systemd loads `/etc/log-platform-host.env`, runs as `logplatform` in the repository working directory and invokes the installed byte-identical wrapper with `--execute --days 60`. Direct host execution selects `MINIO_HOST_ENDPOINT` when declared; otherwise it preserves a host-compatible `MINIO_ENDPOINT` and maps only the exact Compose-internal default to the established loopback endpoint. The command attests environment/database identity, requires a healthy MinIO bucket, takes a shared nonblocking backup lock and a session advisory prune lock, and exits non-zero on every blocked or failed condition. It never logs storage keys or row identifiers. Dry-run reports only aggregate candidate/exclusion counts and zero mutations.

Install after saving protected rollback copies and verifying no prune is active:

```bash
sudo install -o root -g root -m 0755 ops/platform_prune.sh /usr/local/bin/log-platform-prune.sh
sudo install -o root -g root -m 0644 ops/systemd/log-platform-prune.service /etc/systemd/system/log-platform-prune.service
sudo install -o root -g root -m 0644 ops/systemd/log-platform-prune.timer /etc/systemd/system/log-platform-prune.timer
sudo systemctl daemon-reload
systemd-analyze verify ops/systemd/log-platform-prune.service ops/systemd/log-platform-prune.timer
sudo systemctl enable --now log-platform-prune.timer
```

Do not start `log-platform-prune.service` for validation because its installed contract is destructive. Validate with the explicit `--dry-run` command. After changing Compose identity declarations, recreate only the API service with `docker compose -f docker-compose.yml up -d --no-deps --force-recreate api`; restart the host API separately with `sudo systemctl restart log-platform-api.service`. Verify HTTP `dry_run=false` returns 409 and `dry_run=true` succeeds on each restarted API. The schedule is daily 03:30 local system time, persistent, one-minute accuracy and zero randomized delay. Full eligibility, exclusions and cross-store consistency behavior are in `docs/08_retention.md`.

### Release boundary — dev tree vs release vs active runtime

**Status: `ACTIVE`. Production executes the pinned release, not the development
working tree.** The cutover was performed on **2026-08-11**; M1 of
`docs/20_telematics_ingestion_permanent_repair_plan.md` is complete.

**THE POINTER AND THE RUNNING PROCESS ARE TWO FACTS.** Activation moves
`current`. It restarts nothing. The long-running services resolve `current` when
they start and hold that release until restarted, so between an activation and
the restart the pointer names one release and the services execute another. The
three job surfaces do not share this property — `log-job-runner.sh` resolves
`current` per invocation, so jobs adopt a new release on their next tick with no
restart at all.

This means a release can be simultaneously live for jobs and not live for the
API. Read `running_release_id` from `ops/manage_release.py status`; it is
observed from the live process's working directory, which the kernel pins to the
directory the process actually started in, so a later pointer move cannot
rewrite it.

#### Procedure — adopting an activated release on the long-running services

Run this whenever `status` reports `pointer_matches_running_release: false`, and
as step 2 of every activation. It is the routine second half of activation, not
an incident response.

*Before adopting — what a file-level verification does not cover.* A release is
verified at activation time by byte comparison against its commit, which proves
the tree and nothing about whether anything can start from it. A release that
has never executed has never had that tested. Four read-only checks:

1. `ops/manage_release.py verify --release <current_release_id>` → `verified: true`;
2. runtime links resolve and the interpreter is executable (the 2026-08-20
   outage class: a link-less release verifies cleanly and then crash-loops);
3. every `.py` in the release tree compiles;
4. `api.main`, `ops.database_export_worker` and any changed job module import
   from the release tree under its own interpreter:

       cd <release_root>/releases/<release_id>
       PYTHONPATH="$PWD" ./.venv/bin/python -c \
         "import api.main, ops.database_export_worker"

Point 4 is the one that matters, because it is the only check that executes the
release rather than reading it.

*Adopting.*

    # 1. Establish the pre-state.
    ops/manage_release.py status
    systemctl show log-platform-api.service database-export-worker.service \
      --property=MainPID --value

    # 2. Adopt. Both services together: they are one release boundary.
    sudo systemctl restart log-platform-api.service database-export-worker.service

    # 3. Verify what is RUNNING — not the symlink.
    ops/manage_release.py status          # pointer_matches_running_release: true
    systemctl show log-platform-api.service --property=MainPID --value
    readlink /proc/<new MainPID>/cwd      # must name the release `current` names

    # 4. Health and smoke.
    curl -fsS http://127.0.0.1:8001/health
    journalctl -u log-platform-api -n 100 --no-pager

Step 3 reads the NEW MainPID's `cwd`. Reading the symlink instead reports
success unconditionally, and is exactly the check that missed the 2026-08-25
drift for two days.

*Expected interruption.* A restart of the API is a brief connection refusal on
`127.0.0.1:8001` while uvicorn rebinds — nginx proxies to it and does not retry,
so in-flight requests fail. Seconds, not minutes. The export worker is a loop
with no inbound socket; restarting it interrupts at most one in-flight export,
which is re-picked. Choose a quiet window; there is no drain step.

*Rollback, if the adopted release misbehaves.*

    ops/manage_release.py rollback --execute
    sudo systemctl restart log-platform-api.service database-export-worker.service
    ops/manage_release.py status

`rollback` FAILS OPEN by design (`_assess` in `ops/manage_release.py` explains
why: an unknown verdict must never be the reason recovery does not happen). It
is also an activation, so afterwards `previous` names the release just escaped
— see `repair-previous` below before attempting a second hop. Note that rollback
has the same two-step shape: the pointer move alone changes nothing that is
already running.

#### Record — the 2026-08-25 → 2026-08-27 release drift

Closed. Kept because it is the only observed instance of this failure mode, and
because it is why the tooling reports a running release at all.

The `92c53ece27da` activation of **2026-08-25 13:30 UTC** moved `current` and,
as designed, restarted nothing. Nobody restarted the long-running services
afterwards, so `log-platform-api.service` and `database-export-worker.service`
continued executing `023e6452fd5b` — the previous release — for two days, while
the three job surfaces had already adopted `92c53ece27da` on their next tick.

Nothing reported it. `ops/manage_release.py status` described only files and
symlinks, so it printed `production_executes_release_root: true` beside
`remedy: null` — a field about the wrapper's configuration, read as a statement
about the runtime. That is the defect the running-release observation now
closes; the drift itself was the symptom.

**The impact was nil, and that was luck rather than design.** The delta between
the two releases touched `jobs/`, tests, documentation and a comment-only
wrapper header. `git diff 023e6452fd5b 92c53ece27da -- api/` is empty, so no
module either long-running service imports differed between them: their
executable surface was byte-identical, which is what makes the
`RESTART_NOT_REQUIRED` classification recorded at activation substantively
right. That property is a fact about this particular pair of releases and must
not be generalised — the next activation that touches `api/` would have been
silently not-live in precisely the same way, with the same clean status output.

**Remediated 2026-08-27 12:32 CEST** by the operator under explicit
authorization, using the procedure above: both services restarted together, both
came up on `92c53ece27da` (PIDs 1729732 and 1729753), `/health` 200 on
`127.0.0.1:8001` and through nginx, and `journalctl` recorded nothing at warning
or above since the restart. `status` reported
`pointer_matches_running_release: false` before and `true` after — the drift
detection confirmed in both directions against production rather than a
fixture.

| Fact | Value |
|---|---|
| Installed wrapper | `/usr/local/bin/log-job-runner.sh`, release variant, `root:root` `0755`, byte-identical to the tracked source (`ops/systemd/proposed/log-job-runner.release.sh`); `status` reports `wrapper_up_to_date: true` after the authorized 2026-08-25 refresh reinstall (which absorbed the comment-only header fix; executable behaviour unchanged) |
| Installed wrapper SHA-256 | `e6ab8204152c8b393b568b0998f65673235a5e9fa59d3e00448cd0943095fe59` (reinstalled and verified 2026-08-25) |
| Active release (`current`) | **Read it: `ops/manage_release.py status` → `current_release_id`.** No SHA is recorded here. The previous version of this row named one, in the same sentence that warned against trusting a SHA in a document, and it was wrong within two days |
| Running release | **Read it: same command → `running_release_id`.** This is observed from the live process and is NOT the same fact as the pointer. `pointer_matches_running_release` says whether they agree; `null` means it could not be observed, which is not agreement |
| Rollback target (`previous`) | **Read it: same command → `previous_release_id`.** |
| Cutover fence | `STATE=VERIFIED` |
| Host prerequisites | complete (both, see below) |

Read this together with the scope paragraph further down: the three job
execution surfaces go through `log-job-runner.sh`, and since 2026-08-20 the
long-running services (`log-platform-api.service`,
`database-export-worker.service`, `database-export-cleanup.service`) are pinned
through `log-ops-runner.sh`. `.env` / `.venv` remain shared symlinks into the
development tree, and a few host-bound units remain unpinned (see the scope
table). The cutover *steps* are retained at the end of this subsection as the
procedure of record for a future boundary installation, not as pending work.

Three things must never be used as evidence for one another:

| Concept | Path | Property |
|---|---|---|
| **Development repository** | `/opt/log-platform` | mutable, routinely dirty; never a release payload |
| **Release tree** | `/opt/log-platform-release/releases/<release_id>` | the byte content of one commit, sealed read-only, no `.git` |
| **Active runtime** | whatever `BASE_DIR` the installed `/usr/local/bin/log-job-runner.sh` names | the only thing that decides what actually runs |

Historically all three were the same directory, so an uncommitted edit became
production behaviour at the next 5-minute dispatcher tick with no promotion
point and no rollback target (`docs/20` §1.9).

**Layout.** Under the release root: `releases/<release_id>/` (one directory per
commit), `meta/<release_id>.json` (provenance, deliberately outside the source
tree so the tree stays byte-identical to the commit), `current` and `previous`
symlinks, `activations.log` (append-only journal), `state/pycache` (mutable
bytecode cache). `release_id` is the first 12 hex characters of the commit, so
preparing the same commit twice converges instead of accumulating copies.

**Materialization** is `git archive <sha>` extracted into a staging directory,
verified, then `rename(2)`d into place. This deviates deliberately from
`docs/20` §5, which proposed a worktree checked out to a tag: a worktree keeps a
`.git` link into the development repository, stays writable, and promotes by
`git checkout` **in place**, so a dispatcher tick landing mid-checkout would
execute a half-updated tree. Extraction plus an atomic pointer swap makes a
partially constructed release unobservable. The `docs/20` §5 *properties* are
preserved; only the mechanism differs.

**Mutable runtime resources are linked, never copied.** `.env` (secrets) and
`.venv` (build artifact) belong to neither the commit nor the release. They are
declared symlinks whose targets are recorded in provenance and checked by
verification. Both currently point back into the development tree — this is the
"explicitly shared" option of `docs/20` §5.5, chosen rather than defaulted.
Consequence to keep in mind: **a code rollback does not roll back the venv.** If
a release ever needs different dependencies, give it its own venv at preparation
time instead of relying on the shared one.

The release tree is sealed read-only **including its directories**, because
running a release is itself a write attempt: CPython would otherwise create
`__pycache__` next to every imported module and the release would fail its own
verification from the first execution onwards. The release wrapper sets
`PYTHONPYCACHEPREFIX` to `state/pycache` so nothing is lost to the seal.

**Verification is recomputed, never cached, and does not need the source
repository.** Provenance carries the full `path -> (mode, blob sha)` manifest,
and `verify` hashes the actual bytes against it — content, path set, executable
bits, plus the declared runtime links. A stored "verified" flag would be exactly
the stale trust anchor this boundary exists to remove; a stored *manifest* is
the opposite, because every entry is re-derived from the release bytes on each
check. Keeping it local is what stops a rollback target from expiring: a release
stays verifiable after its commit becomes unreachable through a rebase, a
deleted branch or `git gc`. While the commit *is* reachable the manifest is also
cross-checked against `git ls-tree`, so a doctored manifest is caught;
`verified_against_source_repository` reports which of the two applied.

Note what verification does **not** do: it is not continuous. It runs at
preparation and before every activation, but nothing re-checks the active
release afterwards, and the wrapper only asserts that `current` resolves to a
release directory. A periodic `verify` routed through the existing `OnFailure`
alert path is follow-up work.

> **KNOWN GAP — `verified: true` does not mean the release can boot.**
> Observed in production on 2026-08-20. `prepare` was run without `--env-file`
> and `--venv`, so the release was materialized with `runtime_links: {}` and no
> `.venv` / `.env` symlinks. `prepare` reported `verified: true`, a standalone
> `verify` reported `verified: true`, and `activate` verified again and swapped
> the pointer — all three passed, because verification checks that the
> **declared** runtime links resolve, and none were declared. The refusal
> `RELEASE_RUNTIME_RESOURCE_MISSING` only fires at *launch*, from the wrapper,
> so the defect surfaced as `log-platform-api.service` crash-looping and a 502
> from nginx roughly 90 seconds long, ending at rollback.
>
> Until this is closed, treat these as a manual gate before every activation:
> `runtime_links` in the `prepare` output must be non-empty, and
> `releases/<id>/.venv/bin/python` must exist and be executable.
>
> Two secondary findings from the same incident, both still open:
>
> * **Rollback retargets `previous` at the bad release.** Rollback is itself an
>   activation, so it swaps the pointers: after rolling back off a broken
>   release, `previous` names that broken release. The next `rollback --execute`
>   would promote something known not to boot. There is currently no way to
>   clear it — `remove` refuses a pointer-referenced release
>   (`RELEASE_POINTER_INVALID`) and `prepare` refuses to converge one whose
>   runtime links differ (`RELEASE_ALREADY_EXISTS_MISMATCH`), so a correctly
>   prepared release of the *same commit* cannot replace it. The only exit is to
>   activate a different, good release, after which the broken one becomes
>   unreferenced and removable.
> * **The assessment answers for the CALLING process, not the service user.**
>   `os.access(..., X_OK)` in `assess_release_bootability` reports whether the
>   process running the CLI can execute the interpreter. On this host that is the
>   same identity the service runs as (`logplatform` in both cases,
>   owning the release root and `.venv`), so the answer is correct today — but by
>   coincidence of deployment, not by construction. Running the CLI under `sudo`,
>   or a host whose unit declares a different `User=`, breaks it, and the failure
>   is a false NEGATIVE: a `.venv` traversable by root but not by the service user
>   passes the gate and then fails at launch. **Run release commands as the
>   service user.** Documented rather than enforced by owner decision on
>   2026-08-20 — asserting it would add another refusal path to a mechanism that
>   had just been corrected for refusing too eagerly.
> * **The seal limits this race, but the POINTERS are not sealed.** A runtime
>   link cannot be removed from inside a release tree without unsealing it first,
>   because the seal covers directories too — so the readlink race is unlikely
>   there. The `current` and `previous` symlinks are NOT sealed, which makes
>   `_pointer_target` the genuinely exposed site. Useful when calibrating how
>   much any link-race hardening is worth: aim it at the pointers, not the trees.
> * A candidate fix is for `prepare` to default `--env-file` / `--venv` to
>   `DEFAULT_RUNTIME_LINK_NAMES` rather than silently producing a link-less
>   release, and/or for `verify` to refuse a release whose declared link set is
>   empty. Owner: whoever holds the release-boundary tooling. Not changed here
>   deliberately — deployment tooling should not be modified on the same pass as
>   a production deployment.

### Release-tooling backlog — known, reproduced, deliberately NOT fixed

Found by two independent reviews on 2026-08-20 and reproduced end to end. They
were left unfixed by owner decision: they are **latent** conditions on paths that
run rarely, while every change to this tooling is live for operators the moment
it merges — the tool executes from the canonical tree, with no deploy step to
absorb a mistake. Rollback's behaviour changed three times that day, and twice
a change intended to improve the emergency path made it worse before being
caught. The marginal risk of another change stopped clearly beating the risk of
the condition it would fix.

Read this section before changing anything here, and prefer fixing one item with
its own verification over a sweep.

**THE LAST-RESORT ESCAPE, since several items below can block every command.**
`current` and `previous` are ordinary symlinks. If no `manage_release.py`
subcommand will move a pointer, an operator can move it by hand:

    ls -l  /opt/log-platform-release/current
    ln -sfn releases/<release_id> /opt/log-platform-release/current.new \
      && mv -Tf /opt/log-platform-release/current.new \
                /opt/log-platform-release/current
    sudo systemctl restart log-platform-api.service

This bypasses verification, the schema preflight and the activation journal, so
the release is NOT proven to be its commit and the move is not recorded. Use it
only when a documented command has refused and the refusal is one of the items
below; then run `ops/manage_release.py status` and reconcile. Writing `current`
by hand while a job is starting is also unsynchronised with the management lock.
It is a fire escape, not a door.

**R2 — the dispatch guard asserts something it cannot know.** `main()`'s
unexpected-error report says "Nothing was reported as done that was not done; the
state on disk is whatever it was before the failure." That is FALSE.
`_activate_release_locked` performs two pointer swaps and then appends the
journal; a failure between them — or in `_append_activation` on ENOSPC — leaves
`previous == current`, the poisoned state, while the report says
`executed: false`. Reproduced: pointers `(A, B)` before, `(A, A)` after, with a
clean "nothing changed" message. **Anyone trusting that message will not run
`status`, which is the one thing that would show them the truth.** Fixing it
means either the guard reporting the state as UNKNOWN and directing the operator
to `status`, or making the swap pair recoverable.

**R3 — dry run and execute disagree.** `activate`'s dry run is written so a
rehearsal cannot report a clean result the real command would refuse. `rollback`
and `repair-previous` do not hold that. `previous == current` is checked inside
`rollback_release`, reached only under `--execute`, so the dry run returns 0 with
`warning: null`; `repair-previous`'s dry run returns 0 where `--execute` can die
in `verify_release`; and `activate`'s dry run reports a `SchemaPreflightError`
while `rollback` has no equivalent in either branch. Note the interaction: the
state R2 creates is exactly the state R3's dry run fails to warn about.

**R4 — every real rollback requires the platform DB and every enabled client DB.**
The highest-consequence item here. `cmd_rollback` calls `rollback_release`
without injecting a schema preflight, so the real `verify_schema_prerequisites`
runs and opens the platform connection **unconditionally**, before and regardless
of whether the release declares any requirement. A `SchemaPreflightError` is a
plain `RuntimeError`, so it lands in the broad catch as `UNEXPECTED_ERROR` exit 3
with `next:` pointing the operator at a permission or filesystem fault on
`.env` / `.venv` — **the wrong thing entirely**.

Why this matters more than its severity rating suggests: a platform-database
outage is a plausible *reason* to be rolling back, including one caused by the
release being rolled back from. So the fault that motivates the rollback can be
the fault that blocks it, with misleading guidance and no dry-run warning. This
is the same shape as the missing-shared-resource defect fixed in `ef5a154`, in a
dependency nobody framed as part of "bootability", which is why five rounds of
fixes never touched it. **Until it is fixed, the escape above is the answer.**
A fix would need to decide whether a release declaring no schema requirements
should reach the database at all.

**R5 — `--assume-timers-stopped` has two meanings.** It is the resumption signal
for the cutover's unknown-verdict gate, and it is *also* the documented first-run
remedy for `CUTOVER_NO_ACTIVE_CONSUMER_TIMERS` — see the cutover runbook below,
which tells an operator to pass it when every consumer timer is already inactive.
An operator who stops timers by hand before a planned window, hits that refusal
and follows the printed remedy is therefore on a **first run with production
healthy** and has silently waived the unknown-verdict gate for the target *and*
the predecessor — the predecessor gate existing specifically to stop the tool
installing a fallback that cannot start. The two mentions must not be read
independently. A fix needs a distinct signal for resumption, or a fence state
that survives the documented clear-and-re-run.

**R6 — an absent `current` yields a factually false explanation.**
`diagnose_runtime_fault` keys failing paths by defect *reason*, so
`release_not_found` can never intersect `interpreter_not_executable` or
`env_missing`. A missing comparison release therefore always produces an empty
intersection and the conclusion `RELEASE_SPECIFIC`, and `rollback` refuses saying
"the release currently serving does NOT fail on those same paths" — when there is
no serving release at all and the fault is in fact shared. It blocks, misinforms,
and points at `repair-previous`, which cannot help. A fix should treat an
unresolvable comparison as `INDETERMINATE`, which already proceeds.

**R7 — partial overlap reports SHARED and drops the rest.** The shared test is
`if shared:` — any intersection — and the report emits only `shared_paths`. A
fallback failing on both a shared `.venv` and its own `.env`, compared against a
release failing only on the `.venv`, is reported as SHARED with the `.env` fault
discarded. The operator repairs the shared path, is told no pointer move will
help, and the fallback still cannot start — from a fault the tool had in hand.
`mine` minus `shared` is the missing set.

### Why these survived — the test-suite shape

The most transferable finding, and the reason four of the above outlived five
rounds of fixes.

**There was no test in which a rollback SUCCEEDS.** Until `ef5a154` the only
`rollback --execute` in the suite expected a refusal, and every "this case
proceeds" assertion was about a **dry run's exit code** — a branch that never
reaches the pointer, and the branch where blocking code is easiest to get past.
The behaviour those tests claimed to protect was unreachable in the execute path
and the suite could not have shown it.

The suite had also *recorded* the obstacle — a docstring explaining that a
successful activation could not be driven in a temporary release root — and then
accepted it rather than treating it as the thing to solve. It is solvable: the
schema preflight is imported lazily inside `_activate_release_locked` and can be
substituted, which is how section 12 now drives a rollback to completion and
asserts **the pointer moved** rather than an exit code.

The fixtures also varied only in **how a release is broken**, never in how the
release **root** or the **command sequence** is broken. Nothing exercised a
missing release directory, a dangling pointer, `previous == current`, a failure
between the two pointer swaps, or a link target that is *absent* rather than
mode-broken. R1, R2, R3 and R6 all live in that unvaried dimension. `chmod 0644`
on `bin/python` keeps the file present, which is the one break shape verification
tolerates — so every shared-fault test passed while the real fault, a removed
venv directory, blocked every command.

When adding coverage here, vary the release root and the command sequence, and
assert the pointer, not the exit code.

### Publication risk — this tooling exists only on this host

As of 2026-08-20 `main` was 26 commits ahead of `origin/main`, and all of the
operator-live release tooling described in this document is among the unpushed
commits. The owner declined to publish, with the facts stated, on four separate
occasions; that is a recorded decision, not an oversight. The consequence to hold
onto: if this machine is lost, the deployed *identity* is recoverable only for
the one commit published as tag `deployed/2026-08-20-c880cc1`, and the tooling
that operates the release boundary is recoverable not at all.

```bash
cd /opt/log-platform

# What is prepared, what is pointed at, and what production actually executes.
ops/manage_release.py status

# Prepare a release from an explicit commit (idempotent; never activates).
ops/manage_release.py prepare --commit <full-sha> \
  --env-file /opt/log-platform/.env \
  --venv     /opt/log-platform/.venv

# Re-verify at any time; read-only with respect to production.
ops/manage_release.py verify --release <release_id>

# Promotion and rollback default to a dry run; --execute moves the pointer.
# ACTIVATION IS TWO STEPS. Moving the pointer does not change what is running:
# the long-running services resolve `current` at start and hold it. Step 2 is
# not optional and not "later" — until it runs, production serves the previous
# release. `activate --execute` exits NON-ZERO and reports
# ACTIVATED_NOT_YET_LIVE precisely so this cannot be missed.
ops/manage_release.py activate --release <release_id> --execute        # 1. pointer
sudo systemctl restart log-platform-api.service database-export-worker.service   # 2. adopt

# 3. VERIFY WHAT IS RUNNING, not what the symlink says. `running_release_id` is
#    read from the live process; `pointer_matches_running_release` must be true.
ops/manage_release.py status

# Rollback is the same shape: pointer, then restart.
ops/manage_release.py rollback --execute
sudo systemctl restart log-platform-api.service database-export-worker.service

# Aim `previous` somewhere else. Never touches `current`. Dry run by default.
ops/manage_release.py repair-previous --release <release_id> --execute
```

**`repair-previous` — the way out of a poisoned fallback.** Rollback is an
activation, so it swaps the pointers: after rolling off a release that would not
start, `previous` names that release. The fallback is then the thing you just
escaped, and neither documented exit works — `remove` refuses a
pointer-referenced release (`RELEASE_POINTER_INVALID`) and `prepare` refuses to
converge a release whose runtime links differ (`RELEASE_ALREADY_EXISTS_MISMATCH`),
so a correct rebuild of the same commit cannot replace it. Production sat in
exactly that state for about forty minutes on 2026-08-20.

`repair-previous` points `previous` at a different release. It **never writes
`current`**, unconditionally — whatever is serving keeps serving, so the worst
outcome of a mistake is a rollback target that is not the one you meant, never a
change to what is running. It refuses a target equal to `current` (that state
makes rollback unperformable), refuses an unknown release, verifies the target
against its commit inside the management lock, reports the target's own
bootability, and prints both the current and the intended `previous` before it
will act. Afterwards the displaced release is unreferenced and `remove` works
again.

    ops/manage_release.py list                                   # choose a known-good release
    ops/manage_release.py repair-previous --release <id>         # dry run: shows before and after
    ops/manage_release.py repair-previous --release <id> --execute
    ops/manage_release.py rollback --execute

**Before reaching for it, check whether the fault is shared.** Every release
declares the same `.env` and `.venv`, both in the development tree (see *Mutable
runtime resources are linked, never copied* above). So a venv rebuild or an
interrupted `pip install` makes **every** release fail the launcher's
preconditions at once, and no pointer move helps — `repair-previous` will succeed
and change nothing. `rollback` and `activate` now report
`shared_runtime_fault` when the release they are considering and the one
currently serving fail on the same resolved path; when that appears, repair the
named path instead. The wrapper's preconditions are the whole test:

    [[ -x <release>/.venv/bin/python ]]   &&   [[ -e <release>/.env ]]

`activate` verifies before it swaps, so an unverifiable release cannot become
current; it records the outgoing release in `previous` and never deletes it, so
rollback is a pointer move rather than a rebuild. Rollback is itself an
activation, so it is reversible. `remove` refuses to delete anything `current`
or `previous` names — releases are sealed, so this is also the only correct way
to delete one (`rm -rf` fails on the seal).

Every refusal carries a stable classification: `RELEASE_COMMIT_UNRESOLVED`,
`RELEASE_SOURCE_NOT_A_COMMIT`, `RELEASE_ROOT_INVALID`,
`RELEASE_ALREADY_EXISTS_MISMATCH`, `RELEASE_CONTENT_MISMATCH`,
`RELEASE_RUNTIME_RESOURCE_MISSING`, `RELEASE_RUNTIME_LINK_MISMATCH`,
`RELEASE_METADATA_INVALID`, `RELEASE_POINTER_INVALID`,
`RELEASE_INCOMPLETE_MATERIALIZATION`, `RELEASE_NOT_FOUND`.

**Source isolation, stated precisely.** After cutover, editing tracked or
untracked Python, SQL or documentation in
`/opt/log-platform` does **not** change what the
scheduled Telematics path executes: the dispatcher, its child jobs, Workflow B and
retention all run from the pinned release until another explicit promotion. That
is a guarantee about *source*, not about the environment — `.venv` and `.env`
remain shared symlinks into the development tree, so a `pip install` or an
`.env` edit still reaches production immediately, and a code rollback does not
roll back dependencies. Treat those two as configuration and dependency
management, documented deliberately rather than claimed to be immutable.

### Host prerequisites — both applied (2026-08-11)

Both were separate, separately authorized production changes, and both are now
complete; `ops/cutover_execute.py` no longer refuses on them. They are recorded
here as the procedure of record — a future host installing this boundary from
scratch still has to satisfy both before cutting over.

**A. Retention consumer.** Correction to an earlier statement in this document:
the installed `log-job@retention-purge.service` does **not** bypass the wrapper.
Its base unit names `/usr/bin/python3 ops/runner.py`, but an `override.conf`
redirects `ExecStart` to `/usr/local/bin/log-retention-purge.sh`, a two-line
shim that `exec`s the wrapper — verified with
`systemctl show -p ExecStart --value log-job@retention-purge.service`. It
therefore already takes the execution barrier and the fence, and the transaction
resolves that one level of indirection rather than flagging it.

What remains worth doing, and why it is a prerequisite rather than an
emergency: the *repository* unit and the installed base unit disagree, so a
future reinstall from the repository would reintroduce a real bypass. Install
`ops/systemd/proposed/log-job@retention-purge.service`, remove the now-redundant
`override.conf`, `systemctl daemon-reload`, verify the effective `ExecStart`
still reaches `/usr/local/bin/log-job-runner.sh`, and leave the timer's
active/enabled state unchanged.

**B. Barrier-capable development bootstrap wrapper.** The gate is not byte-equality
alone — the installed wrapper must also *contain* the three contracts
(`flock --shared`, the `CUTOVER_FENCE` check, `WRAPPER_VARIANT`), so checking
out an older copy of the repository wrapper cannot satisfy it. Install
`ops/systemd/proposed/log-job-runner.sh` from the verified candidate over
`/usr/local/bin/log-job-runner.sh`, under the canonical wrapper-install lock,
after confirming the current file is the expected historical development
wrapper. Verify exact SHA-256, `root:root`, mode `0755`, and that
`ops/manage_release.py status` still reports `development_tree`. It continues to
execute the development tree — this prerequisite adds the barrier, the fence
check and the safe re-exec, and changes nothing about which source runs.

**Scope — the boundary does not pin everything.** Two launchers pin code to the
active release, and everything else still names the development tree by absolute
path and keeps executing it against the same database.

| Launcher | Pinned units | Purpose |
|---|---|---|
| `/usr/local/bin/log-job-runner.sh` | `log-job@dispatcher.service` (every 5 min), `log-workflow-b.service` (06:00/20:00), `log-retention-purge.sh` (Sun 05:30) | jobs — opens a `public.runs` lifecycle, reads `jobs/config/<module>.json`, takes params |
| `/usr/local/bin/log-ops-runner.sh` | *installed and active:* `suspected-bug-email-worker.service` (every 5 min), `execution-watchdog.service` (every 15 min), and since 2026-08-20 `log-platform-api.service`, `database-export-worker.service`, `database-export-cleanup.service` | non-job processes — `python -m <module>`, no run row, no params (the API unit runs `uvicorn`) |

Still host-bound: `disk-space-monitor`, `backup-retention`, `log-backup`,
`log-platform-prune` and `log-platform-unit-failure@`.

`log-platform-api.service` and `database-export-worker.service` are the two the
Portal V1 rollout exposed; their release-bound units were installed on
2026-08-20 — see *Long-running services and the release boundary* below.

**Why a second launcher rather than one.** `log-job-runner.sh` is a *job* entry
point: it execs `ops/runner.py`, which opens a `public.runs` lifecycle through
`run_context`. The alert worker and the watchdog are not jobs — routing them
through it to obtain release resolution would buy the correct import path at the
cost of writing run rows into the very dataset the watchdog exists to read.
`log-ops-runner.sh` resolves the same `current` pointer, applies the same
release-shape and `PYTHONPYCACHEPREFIX` guards, and then simply `exec`s
`python -m <module>`. It deliberately takes neither the execution-quiescence
barrier nor the cutover fence: both govern whether *jobs* may run, and an
observer that is silenced by the same switch as the thing it observes is not an
observer. For the `Type=simple` services the barrier omission is a requirement
rather than a preference — the shared lock is held for the launched process's
whole lifetime, so a daemon that took it would block every future cutover
permanently. Its exit codes (`90` pointer invalid, `91` runtime resource
missing, `92` the release does not carry an entrypoint the unit declared) stay
clear of `3`, which `ops/suspected_bug_email_worker.py` owns for "an alert
reached `dead_letter`".

### Long-running services and the release boundary

```
RELEASE_BOUNDARY_HARDENING_DEPLOYED
```

**Deployed as an authorized production operation on 2026-08-20 and verified
read-only.** The installed `log-platform-api.service` and
`database-export-worker.service` execute through
`/usr/local/bin/log-ops-runner.sh` with
`WorkingDirectory=/opt/log-platform-release`, and
their processes run from `<release root>/current` **resolved at start**. Which
release that is, right now, is read from `ops/manage_release.py status`
(`running_release_id`); it is not recorded here, because it changes at every
restart and a document cannot keep it true.

*On `RESTART_NOT_REQUIRED`.* An activation may be classified that way when the
executable/import surface of both services is byte-identical between the
outgoing and incoming releases — when `git diff <outgoing> <incoming> -- api/`
is empty and the delta touches only job code, tests, documentation or comments.
The classification is then substantively correct: restarting would load the same
bytes.

**It is a statement about one pair of releases, never a standing property, and
it is not a licence to leave the services behind the pointer.** A service on the
`previous` tree under that classification is serving identical code, but it is
still not executing the active release, and the next activation that touches
`api/` makes the difference real. Verify the classification per activation
against the actual diff, and prefer restarting anyway — the cost is seconds.
The 2026-08-25 activation was classified this way, correctly, and the services
then stayed on the previous release for two days with every tool reporting
health; see § "Record — the 2026-08-25 → 2026-08-27 release drift".

The paragraphs below describe the model and, historically, the defect it closed.

**The defect (HISTORICAL — closed by the 2026-08-20 installation).** The
previously installed `log-platform-api.service` and
`database-export-worker.service` name
`/opt/log-platform` as `WorkingDirectory`, export
`PYTHONPATH` pointing at it, and exec that checkout's own interpreter. The
release pointer therefore does not decide what either process runs: `current`
can move to a new release and both keep serving whatever the development
worktree contains at their next restart. The bytes currently served happen to
match the deployed release. That is a coincidence of timing — nobody edited the
tree between the release being cut and the services being restarted — not a
property of the system, and it does not survive the next unrelated restart after
the next unrelated edit.

**The installed model.** Three units execute through
`/usr/local/bin/log-ops-runner.sh`, the launcher the alerting sidecars already
use:

| Unit | ExecStart |
|---|---|
| `ops/systemd/proposed/log-platform-api.service` | `log-ops-runner.sh uvicorn api.main:app --host 127.0.0.1 --port 8001` |
| `ops/systemd/proposed/database-export-worker.service` | `log-ops-runner.sh ops.database_export_worker --loop --poll-seconds 30 --cleanup-interval-seconds 3600` |
| `ops/systemd/proposed/database-export-cleanup.service` | `log-ops-runner.sh ops.database_export_worker --cleanup-only` |

The launcher resolves `<release root>/current` **once, at process start**, refuses
anything that is not a real release directory, refuses a release that does not
carry the entrypoints the unit declares in
`Environment=OPS_RUNNER_REQUIRE_RELEASE_FILE=`, then `cd`s to the resolved path
and sets `PYTHONPATH` to it — set, never prepended, because `ops` is a namespace
package and a path spanning both trees would allow a silent mixed-version
import. `EnvironmentFile=/etc/log-platform/runtime.env` exports a `PYTHONPATH`
naming the development tree and `EnvironmentFile=` beats `Environment=`, which is
why the units carry no `Environment=PYTHONPATH=` of their own and why the
launcher is the only place the assignment can win.

The cleanup unit is included because it runs the same module as the worker: a
cleanup pass on a different build would expire artifacts under one version's
rules that another version's publication rules promised.

**Contract now in force.**

* `current` selects the code. Nothing else does.
* A running process keeps the release it loaded. There is no live code swapping,
  by design.
* **Activation requires a restart of both services to take effect**:
  `pointer activation → restart log-platform-api.service and
  database-export-worker.service → health/smoke`.
* **Rollback is the same shape**: `ops/manage_release.py rollback --execute`
  → restart both → health/smoke. The pointer move alone changes nothing for a
  process already running.
* Editing or committing in the development worktree does not change what either
  service executes after an unrelated restart.
* A missing, dangling, non-release or structurally incomplete `current` fails the
  unit (exit `90`/`91`/`92`) rather than falling back to the development tree.
  For `log-platform-api.service`, `Restart=on-failure` means a genuinely broken
  release surfaces as a restart loop with exit `92` in the journal, which is the
  signal to fix the pointer, not the code.

**Environment and dependencies are deliberately NOT release-versioned.**
`.env` and `.venv` stay symlinks from each release into the development tree, and
host configuration stays in `/etc/log-platform-host.env`,
`/etc/log-platform/runtime.env`,
`/etc/log-platform/database-export-minio-host.env` and
`/etc/log-platform/environment-identity.env`. So:

```
CODE_ROLLBACK_SCOPE                = python source in the release tree
RUNTIME_ENVIRONMENT_ROLLBACK_SCOPE = nothing — venv, .env and /etc are shared and
                                     survive a rollback unchanged
```

A code rollback does not roll back dependencies. That is the existing contract
(see *Mutable runtime resources are linked, never copied* above), preserved
intentionally; if a release ever needs different dependencies, give it its own
venv at preparation time rather than relying on the shared one.

**The old bootstrap installer now refuses this host.**
`ops/systemd/install_log_platform_api_service.sh` generates a checkout-bound unit
— it is what produced the current defect — so it exits `1` with
`RELEASE_BOUNDARY_PRESENT` when `<release root>/current` exists.
`--dry-run` still works for inspection; `--allow-development-tree-binding`
overrides deliberately. `ops/systemd/log-platform-api.service.example` carries the
same warning and names the authoritative unit.

**Minimum production operation to activate the hardening** (not performed; needs
explicit authorization):

```bash
cd /opt/log-platform

# 0. Confirm what is active, and that the release carries the entrypoints.
ops/manage_release.py status
ls -l /opt/log-platform-release/current/api/main.py \
      /opt/log-platform-release/current/ops/database_export_worker.py \
      /opt/log-platform-release/current/.env

# 1. Validate the candidates without installing them.
systemd-analyze verify ops/systemd/proposed/log-platform-api.service \
                       ops/systemd/proposed/database-export-worker.service \
                       ops/systemd/proposed/database-export-cleanup.service

# 2. Install the launcher (if it has drifted) and the units.
sudo install -m 0755 -o root -g root \
  ops/systemd/proposed/log-ops-runner.sh /usr/local/bin/log-ops-runner.sh
sudo install -m 0644 ops/systemd/proposed/log-platform-api.service \
  /etc/systemd/system/log-platform-api.service
sudo install -m 0644 ops/systemd/proposed/database-export-worker.service \
  /etc/systemd/system/database-export-worker.service
sudo install -m 0644 ops/systemd/proposed/database-export-cleanup.service \
  /etc/systemd/system/database-export-cleanup.service

# 3. Reload, then restart in a controlled order.
sudo systemctl daemon-reload
sudo systemctl restart database-export-worker.service
sudo systemctl restart log-platform-api.service

# 4. Confirm which release each process actually loaded.
journalctl -u log-platform-api.service -n 20 --no-pager | grep log-ops-runner
journalctl -u database-export-worker.service -n 20 --no-pager | grep log-ops-runner
systemctl status log-platform-api.service database-export-worker.service --no-pager

# 5. Health and smoke.
PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py
```

Existing `/etc/systemd/system/log-platform-api.service.d/` drop-ins remain
compatible: `override.conf` resets the `EnvironmentFile=` list and re-adds
`/etc/log-platform-host.env`, `zz-environment-identity.conf` re-adds the identity
file, and the installed unit declares the same two — so the effective set is
unchanged. Confirm it with
`systemctl show log-platform-api.service -p EnvironmentFiles` before and after.

**Rollback of the hardening itself** is the same install step with the previous
unit files, or `systemctl revert` if no other drop-in state has changed, followed
by `daemon-reload` and the same two restarts. Because the hardening changes only
where code is read from, a service that fails to start under it can be returned
to the previous behaviour without touching the release pointer.

Deterministic evidence for all of the above, runnable without privilege:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/tests_manual/test_service_release_binding.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/tests_manual/test_systemd_unit_contract.py
```

**`log-platform-unit-failure@.service` is host-bound on purpose.** It fires from
PID 1 after some other unit has already failed. A broken or half-promoted
`current` fails every release-bound unit at once — exactly the fault an operator
most needs to hear about — and a release-bound handler could not start to report
it. The version boundary that creates is bounded and tested, not accidental: the
adapter imports four names from repository code (`INCIDENT_UNIT_FAILURE`,
`is_self_alerting`, `report_operational_failure`, `utcnow`, all from
`ops.operational_alert`), touches no outbox, watchdog or fingerprint internals,
and persists through a contract set by migrations `052`/`059`.
`ops/tests_manual/test_release_runtime_isolation.py` asserts that surface exists
and stays call-compatible in both the baseline and the candidate release.

Two consequences are worth stating rather than leaving to be discovered:

* **Operator-run Telematics tools stay unpinned.** `ops/recover_telematics_trips_window.py`,
  `ops/activate_telematics_trips_schedule.py`, `ops/bootstrap_telematics_trips_coverage.py`,
  `ops/bootstrap_telematics_cold_start_coverage.py`, `ops/backfill_alpha00001_dysponent_id.py`
  and `ops/db_migrate.sh` are invoked by hand from the development tree and
  mutate production Telematics data. They are dry-run-first and review-gated, so
  they are not an automatic bypass, but after cutover they can write production
  data using code the scheduled path is not running. Treat "which commit
  produced this coverage watermark" as an open question when using them, and
  prefer running them from a tree whose HEAD matches the active release.
* **`execution-watchdog` now agrees with the dispatcher by construction.**
  It computes expected fire times with `latest_scheduled_fire_local` from
  `jobs.api.telematics.dispatcher`. While it ran from the development tree it could
  disagree with the release-bound dispatcher — including on *uncommitted* edits —
  producing false missing-run incidents or suppressing real ones before the
  change was ever promoted. Since `log-ops-runner.sh`, both resolve the same
  release, so the two can only disagree if one release ships an inconsistency.
  The same change makes `ops/watchdog_expectations.json` release-supplied:
  activating a release is now sufficient to change the expectation set.

**Canonical production entrypoint.** **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION** —
every `PYTHONPATH="$PWD" python3 ops/runner.py …` command in this document and in
`docs/05_jobs.md`, `docs/08_retention.md` and the Telematics plan documents is
development / local / debug only. `/usr/local/bin/log-job-runner.sh` is the
only supported way to run a boundary-covered job in production — the dispatcher,
Workflow A datasets, Workflow B stages and orchestrator, retention purge, Report
207 postprocessors and Eco Driving jobs. It is the single place that decides
which source tree production runs from, and it selects the project virtualenv and
canonical environment identity. The `PYTHONPATH="$PWD" python3 ops/runner.py …`
form throughout `docs/05_jobs.md` is **development / local / debug only**: it
executes the mutable working tree directly, which after cutover means running
unpromoted code against production data. Repository unit templates for
wrapper-consuming jobs invoke the installed wrapper for the same reason, and
`ops/tests_manual/test_release_boundary.py` asserts it, so a template cannot
quietly regress to a direct `ops/runner.py` invocation.

**Interaction with environment-identity provisioning — read before cutover.**
`ops/provision_runtime_environment_identity.py` installs
`ops/systemd/proposed/log-job-runner.sh` (the *development* wrapper) to
`/usr/local/bin/log-job-runner.sh`, and `ops/runtime_identity_readiness.py`
asserts the installed file matches it. Left alone, a routine identity
re-provision after cutover would overwrite the release wrapper and silently put
production back on the mutable development tree, while every release pointer
still looked healthy.

Provisioning now fails closed on **every** installed wrapper that is not
conclusively the development one. Only two states are replaceable — no wrapper
at all, and the development wrapper. A current release wrapper and a release
wrapper from an *earlier commit* both refuse with `RELEASE_BOUNDARY_ACTIVE`; an
unfamiliar script refuses with `INSTALLED_WRAPPER_UNRECOGNIZED`; an unreadable
one refuses with `INSTALLED_WRAPPER_INDETERMINATE`. Recognising the historical
case matters: any later commit that touches the release wrapper changes its
bytes, so hash equality alone would report a perfectly valid older boundary as
"unknown" — and an operator told "unknown" reinstalls the development wrapper.
The remedy for a stale boundary is to **promote a release**, never to
re-provision. `ops/runtime_identity_readiness.py` reports this as a
`release_boundary` block (`installed_wrapper_variant`,
`provisioning_may_replace`, `refusal_classification`) so the state is stated
rather than inferred from a hash mismatch.

**Updating the development wrapper after cutover.** Because only an absent or
conclusively-development wrapper may be provisioned over, editing
`ops/systemd/proposed/log-job-runner.sh` makes the installed (previous)
development wrapper `unrecognized`, and provisioning then refuses. That is the
correct direction, but it means there is no in-place update path: to change the
development wrapper on a host that is *not* cut over, remove
`/usr/local/bin/log-job-runner.sh` first and re-provision. Every tick fails
while it is absent, so stop the three consumer timers for the duration exactly
as in the cutover procedure. On a host that *is* cut over, do not do this at
all — promote a release instead.

**The cutover fence — a gate that outlives the process.** The execution barrier
is a file descriptor: when the cutover releases it, including during an unwind,
every queued job is free to run. That is wrong in exactly the case that matters
— the wrapper was replaced, the install then raised, and nobody knows whether
the installed wrapper is safe. The barrier answers "may I start right now"; the
fence at `<release_root>/state/cutover-state.txt` answers "is production
execution permitted at all", and it survives the process that wrote it.

| State | Meaning | Jobs may run |
|---|---|---|
| absent | no cutover has ever recorded state (pre-cutover normal) | yes |
| `ALLOWED` | explicitly re-enabled by a recovery | yes |
| `IN_PROGRESS` | a cutover owns the critical section | no |
| `BLOCKED_UNCERTAIN` | wrapper may have been replaced and was not verified | no |
| `VERIFIED` | cutover completed and verified | yes |
| unreadable / malformed | indeterminate | no |

Both wrappers read it **after** taking the shared barrier and **before**
resolving any tree, exiting 5 if it does not permit execution. The file is
`KEY=VALUE` so the wrappers can parse it with `sed` — they must decide whether
Python may run before running Python — and writes are `rename`-atomic, so a
reader never sees half a state. It lives under the release root rather than
`/run` deliberately: a reboot must not convert `BLOCKED_UNCERTAIN` into "fine
now". The cutover writes `IN_PROGRESS` before the pointer sequence,
`BLOCKED_UNCERTAIN` in the unwind — which runs **while the barrier is still
held**, so a queued job finds the fence closed rather than a free lock — and
`VERIFIED` only after every post-install assertion passes. Every unwind path
reconciles the fence it wrote: `ALLOWED` when the wrapper is provably untouched,
`VERIFIED` when it was already verified, `BLOCKED_UNCERTAIN` otherwise. Timers
are never restarted while the fence refuses execution, because that would halt
every job while reporting the scheduler restored.

**Recovery from an unsafe fence is deliberate.** A cutover process ending clears
nothing — the fence records that the outcome was never established, and the
process exiting says nothing about the installed wrapper. `ops/manage_cutover_fence.py
recovery` gathers the four facts that must be established first (installed
wrapper identity, both pointers, candidate integrity, timer state); re-enabling
execution is then an explicit `allow --execute --reason '<why>'`, and the reason
is written into the fence for the next reader.

**Wrapper self-refresh is not environment-controlled.** Each wrapper declares
`WRAPPER_VARIANT` and, once it holds the barrier, compares it with the copy now
on disk, re-execing if they differ. There is no environment marker: a
caller-supplied variable used to be able to suppress the refresh, which is
exactly the bypass that would keep the old inode running the mutable tree.
Termination is structural — after the re-exec the executing wrapper *is* the
file on disk, so the comparison no longer differs.

**Three locks, always taken in one order.** The boundary uses three distinct
lock domains, and the cutover is the only participant that holds more than one:

| Lock | Path | Held by | Protects |
|---|---|---|---|
| Execution barrier | `/run/lock/log-platform-execution.lock` | every job (**shared**), cutover (**exclusive**) | no new job starts while a cutover owns its critical section |
| Release management | `<release_root>/.manage.lock` | `prepare` / `activate` / `rollback` / `remove` | pointer and journal consistency |
| Wrapper install | `/run/lock/log-platform-wrapper-install.lock` | cutover, identity provisioning | writes to `/usr/local/bin/log-job-runner.sh` |

Acquisition order is **execution barrier → release management → wrapper
install**, never the reverse. The release-management lock is now held across the
pointer sequence, the wrapper installation *and* the final verification, ending
only once the fence is marked `VERIFIED`: releasing it earlier let a concurrent
activation move `current` between the verification and production resuming, so
the cutover would report a target it was no longer delivering. Jobs take only the barrier, provisioning only the
wrapper-install lock, and release management only its own, so no cycle exists.
Neither production path is configurable through the environment: both paths are
constants, because two processes inheriting different values would take
*different* locks and both report success — silently reopening the race the
locks exist to close.

**A job blocked on the barrier re-execs into the new wrapper.** `mv -f` is a
rename, so a shell already executing the old wrapper keeps reading the *old
inode*: without a check it would resume after the barrier is released and run
the pre-cutover script text — resolving the development tree *after* the cutover
reported success. Each wrapper therefore declares `WRAPPER_VARIANT` and, once it
holds the barrier, compares that against the copy now on disk, re-execing itself
once if they differ. The barrier descriptor is not close-on-exec, so it survives.

**Consumers that do not enter through the wrapper are refused.** The barrier
lives in the wrapper, so a unit whose installed `ExecStart` runs
`python ops/runner.py` directly takes no barrier at all and can start during the
exclusive window. The transaction reads each discovered consumer's effective
`ExecStart` and refuses with `CUTOVER_CONSUMER_BYPASSES_WRAPPER` if any bypasses
it. **The installed `log-job@retention-purge.service` currently does exactly
this** — its `ExecStart` is `/usr/bin/python3 ops/runner.py …` against the
development tree, and it has no `OnFailure` drop-in. Install the repository
version of that unit before cutting over, or the transaction will (correctly)
refuse.

**The execution barrier is what makes quiescence real.** Stopping the timers
does not stop a manual `systemctl start log-job@dispatcher.service` or a direct
invocation of the wrapper. Both wrappers — development and release — therefore
take the **shared** side of the barrier *before* resolving which tree to run,
and hold it for the job's lifetime. The cutover takes the **exclusive** side
across the final gate, the pointer sequence, the replacement and the
verification. Consequences: a cutover waits out jobs already running; no new job
can start inside the window; and a job that tries during the window blocks and
then resolves the **new** release rather than the old tree. Shared locks stack,
so when no cutover is running the barrier costs ordinary production nothing. The
kernel releases it on process death.

**Host-level wrapper serialization.** Exactly two operations replace
`/usr/local/bin/log-job-runner.sh`: this cutover, and identity provisioning.
They share an `flock` on `/run/lock/log-platform-wrapper-install.lock`, and both
re-read the installed wrapper's identity *inside* that lock immediately before
writing. Without it the two race: provisioning plans, sees the development
wrapper, decides it is replaceable; a cutover installs the release wrapper; and
provisioning then executes its stale plan and puts the development wrapper back
on top — production returns to the mutable tree with every release pointer still
looking healthy. Holding a lock is not sufficient on its own, which is why the
decision is re-taken rather than carried forward from planning.

**Release management is serialized.** Every mutating operation — `prepare`,
`activate`, `rollback`, `remove` — takes an exclusive `flock` on
`<release_root>/.manage.lock` for its whole critical section. Individually atomic
renames are not sufficient: activation is a sequence (read `current`, write
`previous`, write `current`, append the journal), so two concurrent operators
could otherwise both read the same outgoing release and record a predecessor that
was never current, sending a later rollback to the wrong code. A blocked command
waits and then refuses with `RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS` rather
than proceeding. Ownership is tracked per release root **and per thread**: a
second thread in the same process queues exactly as a second process does,
rather than mistaking another thread's lock for its own re-entrant nesting. The kernel releases
the lock when the holding process dies, so a killed command cannot wedge future
management. Read-only `verify`, `status` and `list` never take the lock and stay
available during a promotion.

**Production cutover — not authorized as part of the boundary work.** The
mechanism above changes nothing while the installed wrapper is the development
one; `ops/manage_release.py status` decides this by SHA-256, not by reading
`BASE_DIR` (the release wrapper's value is an unexpanded shell variable), and
reports `production_wrapper_variant` plus `wrapper_executes_release_root`.

That field was called `production_executes_release_root` until 2026-08-27. It
only ever measured the installed **wrapper's configuration**, but its name was
read as a statement about the runtime, and on a host where the pointer had moved
and nobody had restarted, `status` printed `true` next to `remedy: null` while
production served the previous release. The field now says which of the two it
is, and `running_release_id` sits beside it carrying the other.

**Quiescence ordering is the part that is easy to get wrong.** Checking that a
service is inactive and *then* stopping its timer is a race: a tick can start in
between, and stopping a timer never terminates a run that has already begun. The
required order is therefore **stop every wrapper-consuming timer first, then
check services and advisory locks, then check again immediately before
replacement** — a long job can finish, or a `Persistent=true` catch-up tick can
fire, between the two checks.

Three timers can launch work through the wrapper, not one: the dispatcher, the
Workflow B orchestrator, and the retention purge (which reaches the wrapper
through `/usr/local/bin/log-retention-purge.sh` via an `override.conf`
redirect). All three must be stopped.

`ops/cutover_preflight.py` encodes this and is strictly read-only — it stops and
installs nothing, it only refuses to bless an unsafe moment. Exit `0` = safe now,
`1` = a blocker, `2` = the check could not be completed (never read as safe).

Preconditions, all of them:

- **host prerequisite A** applied: the installed `log-job@retention-purge.service`
  routes through the wrapper (see below) — the transaction refuses otherwise;
- **host prerequisite B** applied: the installed wrapper is the *exact* reviewed
  barrier-capable development bootstrap wrapper. A wrapper from an earlier
  commit is refused: it predates the execution barrier and the fence, so
  quiescence cannot be enforced;
- the cutover fence is in a permitting state (no unresolved earlier attempt);
- candidate prepared and verified. `current` does **not** need to point at it:
  the transaction performs the M0 → target pointer sequence itself, inside the
  quiesced window;
- the release being promoted contains
  `ops/systemd/proposed/log-job-runner.release.sh` — step 4 installs the wrapper
  *from the release*, so a release cut before the boundary existed cannot be
  promoted onto it;
- a window of roughly **07:00–19:00, not Sunday**: 02:00–06:00 is the ingestion
  band (`docs/20` §5.6), 06:00 and 20:00 are Workflow B, and the retention purge runs
  `Sun 03:30 UTC` — 04:30 or 05:30 local depending on DST, so avoid Sunday
  mornings entirely rather than trusting a local-time figure — and it is
  destructive;
- the release being promoted contains no `db/migrations/` change. Pointer
  rollback reverts code only, so a release carrying a migration is **not**
  pointer-rollback-safe and needs its own forward plan.

```bash
# 0. Verify the candidate and see the whole plan. Read-only; safe at any time.
.venv/bin/python ops/cutover_execute.py \
  --release <FINAL_RELEASE_ID> --previous 7518947f47ea

# 1. Execute the transaction. It stops the discovered wrapper-consumer timers,
#    gates on preflight, sequences the pointers, installs the wrapper under the
#    host lock, verifies everything, and only then restarts exactly the timers
#    that were running beforehand. Any failed gate aborts; nothing downstream
#    of it runs.
sudo -v   # cache credentials: the transaction shells out to sudo for install
.venv/bin/python ops/cutover_execute.py \
  --release <FINAL_RELEASE_ID> --previous 7518947f47ea --execute

# 2. REQUIRED, time-boxed: confirm production actually executed the release.
#    Exit 0 from the transaction proves the wrapper is installed and verified and
#    the timers were told to start — not that any job has run from the release.
#    Within 10 minutes (two dispatcher ticks) the journal must show the wrapper
#    naming the promoted release. If it does not, roll back.
ops/manage_release.py status          # production_wrapper_variant must be "release"
journalctl -u 'log-job@dispatcher.service' --since '-10min' --no-pager | grep log-job-runner
# expect: log-job-runner release=<FINAL_RELEASE_ID> base_dir=<...>/releases/<id>

# 3. Update the wrapper SHA-256 recorded in docs/14 (§ evidence table and the
#    stage-2 acceptance row) and docs/15 §1. Until then those documents assert a
#    hash that is deliberately no longer true.
```

**Why a program and not a list of shell steps.** The earlier runbook told the
operator to write `preflight || { echo 'ABORT'; }`. That construct *succeeds* —
a brace group's exit status is the last command's, and `echo` always succeeds —
so a failed gate silently allowed the installation to proceed. A gate that shell
semantics can turn into a pass is not a gate. `ops/cutover_execute.py` performs
the sequence itself; every step either passes or raises, and installation is
unreachable except through all of its predecessors. `--execute` is required for
any mutation, and the default run only reports the plan.

The transaction, in order:

1. verify the target release **and** the M0 predecessor release;
2. resolve the wrapper to install by **immutable release path**
   (`releases/<id>/ops/systemd/proposed/log-job-runner.release.sh`), never
   through `current`, which moves during the procedure;
3. capture which wrapper-consumer timers are currently active;
4. stop exactly those timers — nothing new can start after this point;
5. preflight (`post-timer-stop`): services quiescent, both advisory-lock domains
   free, consumer discovery complete;
6. take the **exclusive execution barrier** — this waits out any job still
   running through the wrapper, and from here no new one can start;
7. preflight again (`pre-replacement`), now meaningful because nothing new can
   begin between this gate and the replacement;
8. sequence the pointers **M0 → target**, so `previous` ends as the M0 release;
9. take the host wrapper-install lock, re-read the installed wrapper's identity
   inside it, back up the outgoing wrapper outside `PATH`, enter
   `WRAPPER_REPLACEMENT_ATTEMPTED`, install atomically;
10. verify **while the barrier is still held and before any timer restarts**:
    wrapper SHA-256 equals the wrapper from that exact release, owner `root`,
    group `root`, mode `0755`, the bytes match the promoted release's own
    wrapper, the declared release root matches, `current` = target,
    `previous` = M0, target release still verifies;
11. release the wrapper-install lock, then the execution barrier;
12. restart exactly the timers captured in step 3 — never one that was already
    stopped — and confirm each is active.

**Failure semantics — uncertainty fails closed.** The transaction tracks an
explicit state, not a "did we install yet" flag, and enters
`WRAPPER_REPLACEMENT_ATTEMPTED` **before** invoking the replacement. That
ordering is the safety property: a replacement that succeeds and then raises — a
signal after `rename`, a failure in a later step — must not be mistaken for
"nothing was installed". From that state onwards the unwind restarts nothing
unless the installed wrapper is proven byte-identical to the original; anything
else is reported as `WRAPPER_STATE_MAY_HAVE_CHANGED`, the timers stay stopped,
and the error carries both digests plus explicit recovery commands. A visible
outage is better than production running an unverified wrapper.

Before the replacement phase, a failure restores exactly the captured timer
state and leaves the wrapper untouched.

**The scheduler baseline survives a failed attempt.** A failure after the
replacement leaves the timers stopped on purpose. If the recovery is to re-run,
a naive re-capture would then record every timer as inactive, restore nothing,
and report `COMPLETE` with all ingestion halted — a success report for a silent
outage. The first capture is persisted next to the wrapper backup
(`<backup>.timers.json`) and preferred on later attempts. A run that finds every
consumer timer already inactive and has no persisted baseline refuses with
`CUTOVER_NO_ACTIVE_CONSUMER_TIMERS`; pass `--assume-timers-stopped` with the set
to restore when that state is genuinely intended.

> **Before you pass it, read R5 in *Release-tooling backlog* above.** That flag
> now carries a SECOND meaning: `verify_candidates` treats it as the signal that
> this is a resumption of an interrupted cutover, and lets an unproven
> bootability verdict through for the target **and** the predecessor. On a first
> run with production healthy — which is the situation this remedy addresses —
> passing it silently waives that gate, including the predecessor check that
> exists to stop the tool installing a fallback that cannot start. Known defect,
> deliberately not fixed; the two meanings need separating.

**Partial pointer sequences are reported, never inferred.** The pointer sequence
runs inside one release-management critical section and advances a sub-state
before each activation (`UNTOUCHED` → `ATTEMPTED` → `PREDECESSOR_ACTIVATED` →
`SEQUENCED`). If the M0 activation succeeds and the target activation fails, the
error and the unwind read the pointers back and report the real pair rather than
claiming nothing moved. The pointers are inert until a release wrapper is
installed; re-running the transaction re-sequences them idempotently, or restore
them explicitly with `ops/manage_release.py activate`.

**Waits are bounded.** The exclusive barrier waits up to 900 s for jobs already
running, and the wrapper-install lock up to 300 s. Do not schedule a cutover
close to a Workflow B window (06:00/20:00, `TimeoutStartSec=6h`): a run in
flight will exhaust the barrier wait and the transaction will unwind.

**If a step fails part-way.** The transaction unwinds for you, and reports what
it did. Any failure before the replacement restores exactly the timers it stopped and
leaves the wrapper untouched — note that if the pointer sequence had already
run, the unwind reports the real pointer pair; the pointers are inert until a
release wrapper is installed. A failure in the replacement phase deliberately
leaves the timers stopped, because the wrapper was replaced but could not be proven correct; the
error carries the rollback command and the timer set to restore. An unexpected
error (`sudo` refused for want of a tty, a full disk) is wrapped as
`CUTOVER_UNEXPECTED_FAILURE` and unwinds the same way rather than escaping as a
traceback. If the unwind itself cannot restart a timer it says so explicitly —
`timer_state_restored: false` plus the units to start by hand. Never walk away
from a run whose output you have not read: an aborted cutover with timers left
stopped silently halts all ingestion.

Rollback has two independent levers, and the right one depends on what broke:

```bash
# Bad release, boundary itself fine — move the pointer back.
ops/manage_release.py rollback --execute

# Boundary itself suspect — restore the wrapper and run from the dev tree again.
#
# Record which timers are running BEFORE stopping anything, and restart only
# those: this lever must restore scheduler state, not invent it.
systemctl is-active log-job@dispatcher.timer log-workflow-b.timer log-job@retention-purge.timer
sudo systemctl stop log-job@dispatcher.timer log-workflow-b.timer log-job@retention-purge.timer

# The write takes the same host lock the cutover and identity provisioning use.
# Without it, a restore racing a cutover can interleave with that cutover's own
# install and leave the development wrapper live while the cutover reports a
# verified promotion — the very race the lock exists to close.
# Both locks, in the documented order: execution barrier, then wrapper install.
# The barrier matters here for the same reason it matters during a cutover — a
# manually started job may be resolving a tree right now.
sudo flock /run/lock/log-platform-execution.lock -c '
 flock /run/lock/log-platform-wrapper-install.lock -c "
  install -o root -g root -m 0755 \
    /etc/log-platform/log-job-runner.sh.pre-release-boundary.bak \
    /usr/local/bin/.log-job-runner.sh.new &&
  mv -f /usr/local/bin/.log-job-runner.sh.new /usr/local/bin/log-job-runner.sh"'
sha256sum /usr/local/bin/log-job-runner.sh   # expect the pre-boundary hash

# Start only the timers that the `is-active` check above showed running.
sudo systemctl start <exactly those timers>
```

**The pointer lever needs a predecessor, and it has one.** `previous` is only
written by a *second* activation, so `rollback --execute` fails closed with
`RELEASE_POINTER_INVALID: no_previous_release_recorded` until at least one
promotion has happened. Two releases are therefore prepared before cutover: the
boundary release itself, and the M0 commit as its recorded predecessor. That
predecessor is a genuine rollback target — it carries the live `/trips`
wire-time fix, so falling back to it removes only the release tooling and no
ingestion behaviour. Never manufacture a predecessor that lacks a live
correctness fix just to populate the pointer; a rollback that silently reverts
production behaviour is worse than no pointer at all.

Note the ordering constraint this creates: the release being promoted must
itself contain `ops/systemd/proposed/log-job-runner.release.sh`, because step 3
installs the wrapper *from the release* rather than from the mutable working
tree. A release cut before the boundary existed cannot be promoted onto it.

Rollback depth is one — `previous` is overwritten on each activation, so you can
oscillate between two releases but cannot reach an older one by pointer alone.
Older releases stay on disk and can be re-activated explicitly by id.

**The development tree remains load-bearing after cutover.** `.env` and `.venv`
are symlinked into it, and `log-platform-unit-failure@.service` — the operator
alert path — runs `/opt/log-platform/.venv/bin/python`.
If that tree is moved or deleted, every tick exits `3` and the failure handler
cannot start either: no ingestion **and** no alert, only journald. Do not
relocate or delete the development tree on the assumption that production now
runs from the release root. Removing this coupling — a per-release venv, `.env`
under `/etc/log-platform/`, and an interpreter for the failure adapter that does
not depend on the development tree — is follow-up work.

Implementation: `ops/release_boundary.py` (library), `ops/manage_release.py`
(CLI), `ops/systemd/proposed/log-job-runner.release.sh` (cutover wrapper, **not
installed**), `ops/tests_manual/test_release_boundary.py` (deterministic tests
for pinning, dirty-tree isolation, immutability, tamper detection, activation,
rollback and the wrapper guards).

### Wersjonowane w repo

- `api/`
- `jobs/`
- `db/migrations/`
- `ops/runner.py`
- `ops/db_migrate.sh`
- `ops/backup.sh`
- `ops/diag.sh`
- `ops/smoke.sh`
- `ops/snapshot.sh`
- `ops/systemd/log-backup.service`
- `ops/systemd/log-backup.timer`
- `ops/platform_prune.sh`
- `ops/systemd/log-platform-prune.service`
- `ops/systemd/log-platform-prune.timer`
- `ops/systemd/proposed/log-workflow-b.{service,timer}` — **installed and enabled in production** since 2026-07-13; fires the orchestrator at 06:00 and 20:00 Europe/Warsaw. (Historical note: this line previously described the pair as a proposal that must never be installed. That was already false when the audit of 2026-08-09 checked the host.) Still never install it as part of prune repair.
- `ops/systemd/proposed/log-platform-api.service` — **authoritative, release-bound API/portal unit; prepared, NOT installed** (see *Long-running services and the release boundary*)
- `ops/systemd/log-platform-api.service.example` (pre-release-boundary bootstrap example for a host with no release root; adjust before installing)
- `ops/systemd/install_log_platform_api_service.sh` (pre-release-boundary bootstrap helper; refuses on a host that has an active release pointer)
- `ops/nginx/log-platform.conf.example` (example reverse proxy; configure host TLS separately)
- `ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer` (przykład dla **Workflow B**)
- `ops/systemd/proposed/log-job@dispatcher.{service,timer}` (propozycja dla Workflow A dispatcher)
- `ops/systemd/proposed/log-job@retention-purge.{service,timer}` (propozycja dla Workflow A retention)
- `ops/release_boundary.py`, `ops/manage_release.py`,
  `ops/systemd/proposed/log-job-runner.release.sh`,
  `ops/systemd/proposed/log-ops-runner.sh` — release boundary. Both wrappers are
  installed and active, and since 2026-08-20 the long-running services
  (`log-platform-api.service`, `database-export-worker.service`) also execute
  the active release through `log-ops-runner.sh`, see *Release boundary* above

**P0 operational safety foundation — implemented in repo, `NOT_DEPLOYED`.**
Authoritative description, failure semantics and deployment order:
`docs/17_production_hardening_roadmap.md` and the deployment sequence in
`docs/11_operational_readiness.md`. Do not install these piecemeal — the order
matters.

- `ops/operational_alert.py` — shared boundary turning a terminal failure into a
  durable, deduplicated incident; `python -m ops.operational_alert` answers
  "can this platform actually email me?" with an exit code.
- `ops/systemd_failure_adapter.py` + `ops/systemd/proposed/log-platform-unit-failure@.service`
  and the `95-onfailure.conf` drop-ins — covers failures that die before a
  `public.runs` row exists.
- `ops/execution_watchdog.py` + `ops/watchdog_expectations.json` +
  `ops/systemd/proposed/execution-watchdog.{service,timer}` — missing-run,
  stuck-run and scheduler-heartbeat detection. Needs migration `059`.
- `ops/backup_retention.py` + `ops/systemd/proposed/backup-retention.{service,timer}` —
  enforces `BACKUP_RETENTION_DAYS`, which `ops/backup.sh` never applied. Dry-run by
  default; `--execute` deletes.
- `ops/disk_space_monitor.py` + `ops/systemd/proposed/disk-space-monitor.{service,timer}`.
- `db/migrations/059_operational_watchdog_state.sql` — **not applied in production**
  as of 2026-08-09.

### Wymagane na hoście (poza repo)

- finalny template `log-job@.service`
- pliki env dla systemd (`/etc/log-platform/*.env`)
- runtime env dla dispatcher/retention i jobów **Workflow A** per klient

## Planned / not implemented w repo

Migrations 047–050 and the two active selector combinations are ready, and the proposed complete Workflow B units validate the command `/usr/local/bin/log-job-runner.sh jobs.reports.workflow_b.orchestrator {}` at 06:00 and 20:00 Europe/Warsaw with a persistent timer. The complete timer remains uninstalled. Before its separate installation task, the repository prune wrapper and units must be installed byte-identically, the direct production dry-run and Compose API contract must pass, and the systemd API must be restarted onto the same contract. A process still running pre-repair API code remains a blocker. Backup continues to use `ops/backup.sh create`, coordinated locking, partial files, full validation and manifest-last publication.

- Retencja plików Workflow B pod `REPORTS_DATA_DIR` i lokalnych cleaned outputs Stage 2 pod `/tmp/log-platform-stage2` nie ma jeszcze osobnego joba cleanup w repo. Traktuj je jako zakres follow-upu, nie jako część `/maintenance/prune`.

## 7. Monitoring

API logs:

```bash
docker compose logs --tail=200 api
```

Workflow B — fetch reports (host), jeśli używasz proponowanego unitu:

```bash
journalctl -u log-job@jobs.mail.fetch_reports.service -n 200 --no-pager
```

Backup logs (host):

```bash
journalctl -u log-backup.service -n 200 --no-pager
./ops/backup.sh verify YYYYmmdd_HHMMSS
```

Repozytorium i zainstalowany unit muszą wskazywać dokładnie `ops/backup.sh create`. Timer pozostaje daily 03:00 z `Persistent=true`; lock zapobiega overlapowi manual/scheduled. Finalny backup jest recoverable dopiero po pojawieniu się manifestu. Pliki `.partial`, `.failed` oraz znana para `20260712_232326` bez poprawnego manifestu są wykluczone z restore selection.


Workflow A dispatcher:

```bash
journalctl -u log-job@dispatcher.service -n 200 --no-pager
```

Workflow A retention:

```bash
journalctl -u log-job@retention-purge.service -n 200 --no-pager
```

## 7.1 suspected_bug incidents and alert email delivery

`suspected_bug` is an **error classification**, not a run status: a run that reported one keeps whatever status its own contract gives it. The durable ERROR log is written even when email is disabled, unconfigured or failing.

### Inspection (read-only)

`ops/inspect_suspected_bugs.py` never sends, retries or mutates anything; it runs in a read-only transaction and redacts recipient addresses by default.

```bash
python3 ops/inspect_suspected_bugs.py --open
python3 ops/inspect_suspected_bugs.py --incident-id <uuid> --occurrences
python3 ops/inspect_suspected_bugs.py --fingerprint <sha256>
python3 ops/inspect_suspected_bugs.py --outbox pending --outbox retry --outbox dead_letter
python3 ops/inspect_suspected_bugs.py --delivery-history --client-code ALPHA00001 --since 2026-07-01
python3 ops/inspect_suspected_bugs.py --incident-code ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT --limit 20
```

Filters: `--client-code`, `--incident-code`, `--incident-id`, `--fingerprint`, `--since`, `--until`, `--limit` (1–1000), `--show-recipients` (unredacted addresses, use deliberately). Views: `--open` incidents, `--occurrences` history, `--outbox <status>` (`pending`, `sending`, `sent`, `retry`, `dead_letter`, `suppressed`), `--delivery-history` (= `sent` + `dead_letter`). Queue preview without claiming: `python3 -m ops.suspected_bug_email_worker --show-due`.

Correlated log lookup:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT id, ts, source, message FROM logs
   WHERE context->>'classification' = 'suspected_bug' ORDER BY ts DESC LIMIT 20;"
```

### Delivery states

| Status | Meaning | Operator action |
|---|---|---|
| `pending` | enqueued, waiting for the worker | none |
| `sending` | claimed with a lease; SMTP in flight | none; an expired lease is auto-recovered to `retry` |
| `sent` | delivered; `provider_message_id` holds the `Message-ID` | none |
| `retry` | transient failure; `available_at` holds the backoff deadline, `last_error` the reason | none unless it keeps failing |
| `dead_letter` | permanent SMTP rejection or attempts exhausted | fix the cause, then re-enqueue deliberately |
| `suppressed` | reserved for administratively stopped rows | none |

An occurrence that never produced an outbox row records why in `suspected_bug_occurrences.email_decision_reason`: `cooldown`, `recipients_not_configured`, `alerts_disabled` or `duplicate_notification_key`.

Delivery is retried with bounded exponential backoff (`SUSPECTED_BUG_EMAIL_INITIAL_RETRY_SECONDS` doubling up to `SUSPECTED_BUG_EMAIL_MAX_RETRY_SECONDS`) until `SUSPECTED_BUG_EMAIL_MAX_ATTEMPTS`. Delivery failures are ordinary operational errors on stderr/journal; they never create another suspected_bug incident.

There is intentionally **no retry command**: a dead-lettered alert is a signal to fix SMTP or the recipient configuration. If a specific alert must be re-sent after the cause is fixed, do it as a reviewed, explicitly approved DB change on the single outbox row (`status='pending'`, `attempts=0`, `available_at=now()`, `claim_token=NULL`) — never as a bulk update.

### Worker

```bash
PYTHONPATH=/opt/log-platform \
  .venv/bin/python -m ops.suspected_bug_email_worker --once
PYTHONPATH=... .venv/bin/python -m ops.suspected_bug_email_worker --loop --poll-seconds 60
journalctl -u suspected-bug-email-worker.service -n 200 --no-pager
```

Proposed host units live in `ops/systemd/proposed/suspected-bug-email-worker.{service,timer}` (5-minute oneshot). The repo does not install or enable them. Concurrent workers are safe: rows are claimed with `FOR UPDATE SKIP LOCKED` plus a claim token, so the same alert cannot be sent twice.

### Configuration

All variables are documented in `docs/02_infrastructure.md` (`SUSPECTED_BUG_*`). SMTP transport reuses `AUTOMATION_SMTP_*`. **No recipient fallback exists**: with `SUSPECTED_BUG_ALERT_TO` unset, incidents are still recorded and email is suppressed as `recipients_not_configured`; the mechanism never borrows report, customer or Eco Driving recipients. Migration `052` and recipient configuration are separate deployment steps from this code.

**"Unset" is per process, not per host.** `/etc/log-platform/runtime.env` is `root:root 0600`, so a job process cannot read it — the value arrives only because systemd reads the file and injects it before dropping privileges. A unit without `EnvironmentFile=` therefore resolves zero recipients however the file is configured, which is how every incident raised from inside a job was recorded and never sent while the `OnFailure=` unit-level alerts delivered normally. Every incident-raising unit must source it: `log-workflow-b.service`, `log-job@dispatcher.service`, `log-job@retention-purge.service`. Check with `systemctl show -p EnvironmentFiles <unit>`; the repository contract is asserted by `ops/tests_manual/test_workflow_b_alert_hardening.py`. See `docs/17_production_hardening_roadmap.md` §1 *Propagation* and §5.7.

### Alert-delivery health

The worker cannot report its own failure by email, so two independent signals exist instead. It stamps `ops_control.scheduler_heartbeat` under `alerting.email_worker` on every batch — including empty ones, because "nothing to send" and "not running" are otherwise identical — and exits `3` when a message dead-letters, which makes the unit fail visibly without involving the mail path. `ops/execution_watchdog.py` reads both: a lost heartbeat past 30 minutes raises `SCHEDULER_HEARTBEAT_LOST`, and outstanding `dead_letter` rows or a queued backlog older than 180 minutes raise `ALERT_DELIVERY_FAILED` as a durable non-OK observation that persists until an operator clears the rows.

```bash
# is the alert channel actually delivering?
psql -c "SELECT status, count(*) FROM suspected_bug_email_outbox GROUP BY 1;"
psql -c "SELECT component, last_beat_at FROM ops_control.scheduler_heartbeat WHERE component = 'alerting.email_worker';"
systemctl --failed | grep -i suspected-bug || echo "worker unit not failed"
```

**Rollout order.** A component that has never beaten is classified `HEARTBEAT_LOST`, not "not yet started" — the same semantics as the dispatcher heartbeat and migration `059`. Activate in this order: release the worker implementation, obtain one worker execution so the first `alerting.email_worker` row exists, and only then rely on the heartbeat expectation. Loading the expectation first reports a false `SCHEDULER_HEARTBEAT_LOST` for a healthy worker. This is deliberately handled by ordering rather than by a bootstrap grace window, because such a window would blunt the same subject's ability to detect a genuinely dead worker.

## 8. Walidacja ingestu (Workflow B) w DB

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT filename, applied_at FROM public.schema_migrations ORDER BY applied_at DESC;"

docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT id, account, sha256, status, dedup_basis, http_range_fp, duplicate_of_id FROM ingest.raw_file ORDER BY id DESC LIMIT 50;"
```

## 9. Narzędzia operacyjne

Smoke:

```bash
./ops/smoke.sh
```

Diag:

```bash
./ops/diag.sh
```

Snapshot:

```bash
./ops/snapshot.sh
```

## Fail-closed environment identity guard — local/dev bootstrap

Guarded D105.2 migration commands require migrations `042_platform_environment_identity.sql` in `logdb` and `038_environment_identity.sql` in the selected client DB. Migrations create structure only. Local marker values are provisioned separately and explicitly.

Set the non-secret local declarations in `.env` (use unique canonical UUIDs; do not copy production values):

```text
LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev
LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID=<local-platform-uuid>
LOG_PLATFORM_EXPECTED_POSTGRES_HOST=127.0.0.1
LOG_PLATFORM_EXPECTED_POSTGRES_PORT=5432
LOG_PLATFORM_EXPECTED_POSTGRES_DB=logdb
LOG_PLATFORM_EXPECTED_POSTGRES_USER=loguser
```

After confirming `docker compose ps` shows the loopback-bound local stack, apply the structure migrations through the established migration commands. Then inspect before applying local markers:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/provision_local_environment_identity.py \
  --client-code ALPHA00001 \
  --platform-identity-id "<same-local-platform-uuid>" \
  --client-identity-id "<new-local-client-uuid>"
```

The default is inspect-only. Apply requires the explicit confirmation flag:

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/provision_local_environment_identity.py \
  --client-code ALPHA00001 \
  --platform-identity-id "<same-local-platform-uuid>" \
  --client-identity-id "<same-local-client-uuid>" \
  --apply
```

Exit code `0` means inspect/apply success, `2` means fail-closed identity/config refusal, and `3` means a database/runtime error. The utility accepts only `local_dev`, requires loopback platform and client hosts, verifies platform/client connection identity, refuses conflicting markers, grants the client runtime user marker `SELECT`, and omits UUID values from output. It cannot provision production or staging.

For `replay_workflow_b_file_to_stage3.py`, only `--load-stage3` is in this first guard scope; replay `--dry-run` retains its existing read-only planning boundary. `--load-stage3` additionally requires `WORKFLOW_B_LOCAL_REPLAY_ALLOW=1` and verified local platform/client markers.

D105.2 migration dry-run and write-mode both require verified runtime, platform, and client identity. Production dry-run requires a clean approved checkout and `auto_grant_permissions=false`. Production write-mode is outside local provisioning and additionally requires explicit `client_code`, external manual approval, and exact `production_write_confirmation`; there is no production default or automatic fallback.

### 5.3.2 Workflow A - Eco Driving Person for BRAVO00016

Eco Driving Person is an isolated Workflow A implementation for real-person aggregation from `public.client_trips.driver_name`. It is separate from ALPHA00001 Eco Driving. ALPHA00001 continues to use `Driver_Restrictions` before `Dysponent_ID`, its `eco_drivers_id_chart`, private-tag exclusion, existing stats tables, templates, schedules, and send logs.

Required migrations for existing clients:

```bash
# Platform registry only; do not enable schedules automatically.
bash ops/db_migrate.sh

# Client-business DDL rollout; default dry-run first.
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py
PYTHONPATH="$PWD" python3 scripts/apply_client_business_migrations.py --apply
```

Do not apply migrations directly to production databases as part of code review. For `BRAVO00016`, verify the intended client account and database first. New onboarding includes `039_eco_person_driving_schema.sql` followed by `040_eco_person_runtime_privileges.sql`, then creates disabled schedule/retention rows. Existing clients receive platform schedule rows from `046_workflow_a_eco_person_registry.sql`, all with `enabled=false`. If an aggregation or email dry-run fails with `permission denied for view eco_person_people_email_view`, verify that the client-business migration history contains `040_eco_person_runtime_privileges.sql`; do not patch production with an ad hoc `GRANT`.

After applying the client-business migrations, verify the runtime grants with the client runtime role:

```sql
SELECT has_table_privilege(current_user, 'public.eco_person_people_email_view', 'SELECT') AS can_read_people_email_view;
SELECT has_table_privilege(current_user, 'public.eco_person_weekly_trends_view', 'INSERT') AS cannot_write_weekly_trends_view;
```

The expected result is `can_read_people_email_view=true` and `cannot_write_weekly_trends_view=false`.

Mapping import CSV columns:

```text
person_id,person_name,email,driver_name,ranking_included,is_active
```

`person_id` is optional. When it is omitted, the importer resolves within the same client by existing active alias, then by exact normalized `(person_name, email)`, and only creates a new UUID if neither exists. Repeating the same valid CSV is stable and does not create another person. Do not put real employee emails in committed fixtures or docs. Dry-run first:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_mapping_import \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","csv_path":"/path/to/eco_person_mapping.csv","dry_run":true}'
```

Apply only after reviewing `rows_rejected`, `conflicts`, insert/update counts and unchanged rows:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_mapping_import \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","csv_path":"/path/to/eco_person_mapping.csv","dry_run":false,"apply":true}'
```

Driver-name normalization: trim leading/trailing whitespace, collapse repeated internal whitespace including tabs/newlines, lowercase, preserve Polish characters, and compare case-insensitively. No accent stripping, fuzzy matching, substring matching, token similarity or guessing. One active normalized `driver_name` may map to only one real person per client. Several aliases may map to the same `person_id`; inactive historical aliases may coexist when they do not create an active conflict. Empty person names and driver names are rejected; blank email is allowed by the same convention as existing Eco Driving email jobs, but sends will skip missing recipients. If any CSV group has inconsistent email, person name, ranking inclusion, or alias ownership, the import is rejected before writes.

Trip inclusion: after a trip maps to a real person, all trip modes contribute, including business trips, private trips, `NULL` mode, unknown mode and rows where `driver_tag_description` contains `pryw`. Missing driver names and unmapped names are written to `eco_person_trip_assignments` diagnostics but do not contribute to stats.

Manual aggregation examples:

```bash
# Full selected month rebuild, weekly + monthly, dry-run.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_aggregate \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","month":"2026-06","dry_run":true,"recalculate":true}'

# Dispatcher-compatible weekly cumulative snapshot.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_aggregate \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","mode":"weekly_cumulative_snapshot","include_weekly":true,"include_monthly":false}'

# Dispatcher-compatible final month-end weekly snapshot.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_aggregate \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","mode":"final_month_weekly_snapshot","include_weekly":true,"include_monthly":false}'

# Dispatcher-compatible independent monthly aggregation.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_aggregate \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","mode":"monthly_full_aggregation","include_weekly":false,"include_monthly":true}'
```

Email notifications use the shared fail-closed execution and period contract documented in §5.3.1. BRAVO weekly **and monthly** use the same dedicated `BRAVO_ECO_WEEKLY_EMAIL_*` SMTP/IMAP namespace — one sender mailbox per client, not per period. Render-only mode requires neither SMTP nor IMAP access.

Production idempotency remains reservation-based. Migration `044_eco_email_fail_closed_idempotency.sql` changes the normal key to `(client_id, person_name_group_key, report_type, period_start_date, period_end_date)`, excluding template and rating. A committed normal `pending` or `sent` row blocks another normal worker. Stale `pending` rows are not marked failed, deleted, reused, or overwritten; `STALE_PENDING_REQUIRES_RECONCILIATION` requires an explicit investigation and separately reviewed reconciliation action.

Test and forced rows do not consume the normal key. `test_send` requires one explicit test address. `force_resend` requires a reason, uses forced scope, preserves the original normal row, and links it through `parent_send_log_id` where practical.

#### Ambiguous SMTP submissions (all four Eco mailers)

`SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO`. example.invalid SMTP carries no idempotency key, so a second submission is a second message. When a submission is attempted and this host cannot prove the server did not accept it — the connection dropped during DATA, the final response timed out, the socket died — the reservation stays `pending` and is marked `metadata_json.smtp_submission_result='AMBIGUOUS'` with `requires_operator_reconciliation=true`. It is never marked `failed`, because `failed` means "safe to send again".

Consequences an operator must know:

- no automatic run will submit that message again — normal *or* forced. `force_resend` overrides an established `sent`, not an unknown outcome;
- the run reports it as `smtp_ambiguous_count` (the submission that became ambiguous) and `ambiguous_reconciliation_blocked_count` (candidates a later run refused to touch at all). Both are per driver/person; other subjects continue normally;
- no Driver Eco Dashboard capability is published or rotated for a blocked message: the eligibility check runs before the dashboard step;
- a definite pre-submission failure (connection, STARTTLS, authentication, `SMTPSenderRefused`, `SMTPRecipientsRefused`, or an `SMTPDataError` carrying a real 4xx/5xx reply code) is unchanged: `failed`, and automatically retried. An `SMTPDataError` with any other code — including `-1`, which is what `smtplib` reports for an unparsable reply — is AMBIGUOUS, because it can be raised after the message was already transmitted.

Resolution is manual, explicit and attested. Establish the truth outside this host first — the BRAVO weekly Sent-folder archive preserves the exact MIME, the provider log, or the recipient:

```bash
# Read-only: everything a human still has to decide about.
PYTHONPATH="$PWD" python3 ops/reconcile_eco_email_ambiguous_send.py --list

# The message DID arrive -> the row becomes 'sent'; no rerun mails it again.
PYTHONPATH="$PWD" python3 ops/reconcile_eco_email_ambiguous_send.py \
  --client-code BRAVO00016 --send-log-table eco_person_weekly_email_send_log \
  --resolve <SEND_LOG_ID> --as delivered \
  --operator "<who>" --reason "<evidence>"

# It did NOT -> the row becomes 'failed'; the next normal run mails it once.
PYTHONPATH="$PWD" python3 ops/reconcile_eco_email_ambiguous_send.py \
  --client-code BRAVO00016 --send-log-table eco_person_weekly_email_send_log \
  --resolve <SEND_LOG_ID> --as not-delivered \
  --operator "<who>" --reason "<evidence>"
```

The tool opens no SMTP connection and sends nothing. It refuses any row that is not an unresolved ambiguous submission, and it stores the operator identity, the reason and the timestamp in `metadata_json`.

```bash
# Weekly render-only (default; no SMTP or IMAP).
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","limit":5}'

# Weekly test send to one explicit mailbox.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","execution_mode":"test_send","test_recipient_email":"test@example.test","limit":5}'

# Monthly render-only for an explicit closed half-open month.
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","month_start_date":"2026-06-01","month_end_date":"2026-07-01"}'
```

Dispatcher dataset names and safe default state:

- `eco_person_driving_weekly_snapshot` - disabled by default; recommended after successful `trips_sync`;
- `eco_person_driving_month_end_weekly_snapshot` - disabled by default; recommended after previous month trips are complete;
- `eco_person_driving_monthly_aggregation` - disabled by default; run after month-end weekly or after trips sync for the complete month;
- `eco_person_driving_weekly_email_notifications` - disabled by default; do not enable before separate sender-readiness approval;
- `eco_person_driving_monthly_email_notifications` - disabled by default; do not enable before separate sender-readiness approval.

Recommended ordering: `trips_sync` completes, then the appropriate Eco Driving Person aggregation completes, then the matching email job runs. The dispatcher has no explicit dependency graph, so keep schedule spacing conservative and rely on the email preflight check.

Verification SQL in the client DB:

```sql
-- Mapping aliases and conflicts.
SELECT person_id, person_name, email, ranking_included, driver_name, normalized_driver_name, is_active
FROM public.eco_person_driver_mappings_view
ORDER BY person_name, driver_name;

-- Assignment diagnostics.
SELECT assignment_source, aggregation_included, count(*)
FROM public.eco_person_trip_assignments
WHERE client_id = '<BRAVO00016_CLIENT_UUID>'
GROUP BY assignment_source, aggregation_included
ORDER BY assignment_source, aggregation_included;

-- Weekly/monthly stats.
SELECT period_start_date, period_end_date, assigned_id AS person_id, trips_count, total_kilometers, qualification_status, ecodriving_rating_type, ranking_position
FROM public.eco_person_weekly_stats
WHERE client_id = '<BRAVO00016_CLIENT_UUID>'
ORDER BY period_end_date DESC, ranking_position NULLS LAST
LIMIT 20;

SELECT month_start_date, assigned_id AS person_id, trips_count, total_kilometers, qualification_status, ecodriving_rating_type, ranking_position
FROM public.eco_person_monthly_stats
WHERE client_id = '<BRAVO00016_CLIENT_UUID>'
ORDER BY month_start_date DESC, ranking_position NULLS LAST
LIMIT 20;

-- Send logs.
SELECT report_type, send_scope, idempotency_key, parent_send_log_id, period_start_date, period_end_date, status, template_type, recipient_email, original_recipient_email, attempted_at, sent_at
FROM public.eco_person_weekly_email_send_log
WHERE client_id = '<BRAVO00016_CLIENT_UUID>'
ORDER BY attempted_at DESC
LIMIT 20;
```

Rollback/disable procedure: set the five `eco_person_driving_*` rows in `workflow_a_control.client_dataset_schedule` to `enabled=false`; stop running email jobs; leave tables in place for audit unless a separate reviewed data-retention task removes them. Do not disable or alter ALPHA00001 `eco_driving_*` schedules when operating on Eco Driving Person.

## Canonical runtime identity provisioning and promotion

Provisioning and promotion are deliberately separate reviewed operations. Provisioning establishes one authoritative source while all database markers remain `local_dev`; promotion later changes the existing installation's classification without changing any UUID. Do not edit runtime identity declarations, database markers, or control-plane expectations manually.

### Provision the runtime contract first

The canonical path is `/etc/log-platform/environment-identity.env`, containing only:

```text
LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev
```

It is non-secret but operationally sensitive (`root:logplatform`, `0640`). Existing secret-bearing `.env`, `/etc/log-platform-host.env`, `/etc/log-platform/runtime.env`, and `/etc/log-platform/backup.env` remain in use for unrelated settings but must have no active `LOG_PLATFORM_TARGET_ENVIRONMENT` after convergence.


Before the provisioning dry-run can inspect root-only legacy sources, install only the reviewed read-only inspector boundary. Its installer is itself dry-run-first:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/install_runtime_identity_inspector.py \
  --expected-host '<EXACT_HOSTNAME>' \
  --expected-repository-head '<EXACT_COMMIT>' \
  --backup-reference '<VERIFIED_CHECKPOINT_JSON>'
```

Review the two fixed destinations, source hashes, plan SHA-256, and exact attestation. **NOT EXECUTED — REQUIRES EXPLICIT USER APPROVAL:** rerun the identical command as root with `--execute --attestation '<EXACT GENERATED ATTESTATION>'`. Execute may atomically install only `/usr/local/sbin/log-platform-runtime-identity-inspector` (`root:root`, `0755`) and `/etc/sudoers.d/log-platform-runtime-identity-inspector` (`root:root`, `0440`); it validates hashes, metadata, `visudo`, the non-root fixed sudo command, rejection of extra arguments, and idempotent convergence. Installing it does not create the canonical identity file, edit any legacy file, provision identity, reload/restart services, recreate containers, update markers, or promote the environment.

The provisioning dry-run reports `ROOT_IDENTITY_INSPECTOR_NOT_INSTALLED` when the executable is absent, `ROOT_IDENTITY_INSPECTOR_NOT_AUTHORIZED` when `sudo -n` refuses the exact command, and `ROOT_IDENTITY_INSPECTION_FAILED` with a bounded reason for unsafe files, malformed/duplicate/unsupported declarations, or invalid helper JSON/schema/hash/scope. It never prompts and never falls back to `sudo cat`, `grep`, a shell, or a repository Python program. Operators can run the combined redacted read-only inventory with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/inspect_runtime_identity_sources.py
```

That command reports the directly read repository source, fixed-helper legacy-source results, and already-supported systemd/Docker effective identity probes. It performs no writes.

Create and verify a backup/checkpoint, then run the provisioning dry-run with exact current host and release identity:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/provision_runtime_environment_identity.py \
  --expected-host '<EXACT_HOSTNAME>' \
  --expected-repository-head '<EXACT_COMMIT>' \
  --expected-current-environment local_dev \
  --backup-reference '<VERIFIED_CHECKPOINT_JSON>'
```

Both provisioning dry-run and execute require a clean Git worktree at the actual repository root. Staged, unstaged, deleted, untracked, conflicted, dirty-submodule, or active merge/rebase/cherry-pick/revert state returns `REPOSITORY_WORKTREE_NOT_CLEAN` before root inspection, checkpoint-dependent planning, attestation generation, or any write. Ignored runtime files remain intentionally excluded. Commit intentional customer-facing changes through the normal review process before generating a plan; never stash/reset merely to bypass this gate. A commit changes HEAD and invalidates every earlier plan and attestation, so rerun dry-run against the new exact HEAD.

Review the plan, target files, hashes, conflicts, systemd units/drop-ins, wrapper, helper, sudoers fragment, Docker contract, `required_attestation`, and the exact generated `execution_command`. **NOT EXECUTED — REQUIRES EXPLICIT USER APPROVAL:**

```bash
# Re-run the identical command with:
#   --execute --attestation '<EXACT GENERATED PROVISIONING ATTESTATION>'
```

Provisioning writes verified recovery copies outside the checkout under the restricted repository-adjacent recovery root, creates the canonical file, removes only old active identity assignments, installs byte-identical reviewed assets, and runs `systemctl daemon-reload`. Every recovery-specific directory is exactly `0700`; backup/evidence files are exactly `0600`, owned by the fixed `logplatform` service account, and bind the operation, plan, HEAD, checkpoint, logical source, source/backup hashes, and execution ID. Dry-run and root execute resolve the same numeric service UID/GID instead of using the current EUID; a sudo-origin tuple, when present, must match the account exactly. It fails before modifying a source when recovery storage is inside the worktree, symlinked, permissive, incorrectly owned, unusable, or unverifiable. It reports success only after canonical/legacy/asset/recovery/effective-systemd and clean-worktree postconditions pass; a post-write failure is `RUNTIME_IDENTITY_PROVISIONING_FAILED_PARTIAL_STATE`. It does not restart services or recreate containers.

Manual host job commands use the installed wrapper. There is no supported
production alternative:

```bash
/usr/local/bin/log-job-runner.sh '<JOB_MODULE>' '<PARAMS_JSON>'
```

The wrapper applies the same canonical identity contract as
`ops/run_with_environment_identity.py` and additionally decides which source
tree runs. Invoking `ops/run_with_environment_identity.py -- … ops/runner.py`
by hand keeps the identity guarantee but drops the boundary, so after cutover it
executes the mutable development tree — unpromoted code against production data.
That form is development / local / debug only; see *Release boundary* above.

### Readiness classifications

`ops/promote_environment_identity.py --check-production-readiness` reports canonical value/checksum/metadata, every consumer and configured source, installed asset hashes, cached versus per-invocation behavior, running systemd/Docker API values, required reloads, client marker/control-plane UUIDs/environments, exact client primitive capability, and direct marker UPDATE privileges. Classifications are:

- `RUNTIME_IDENTITY_NOT_PROVISIONED`: canonical metadata/helper/installed assets are absent or do not match;
- `RUNTIME_IDENTITY_MIXED`: conflicting declarations or an unconverted consumer exists;
- `RUNTIME_RELOAD_REQUIRED`: files are correct but at least one cached process/container predates canonical configuration, lacks health/provenance evidence, or otherwise cannot be proven converged;
- `CLIENT_PROMOTION_PRIMITIVE_MISSING`: selected client migration 045/function is absent or not executable;
- `CLIENT_PROMOTION_PRIVILEGE_UNSAFE`: runtime role still has direct marker UPDATE;
- `PRODUCTION_PROMOTION_READY`: every configured and running surface converges and every selected client has only the least-privilege primitive.

No promotion execute may begin unless the result is `PRODUCTION_PROMOTION_READY` (except an idempotent resume paused specifically at the journalled reload stage).

### Promotion dry-run and execute

Apply platform migration 053 and client-business migration 045 through their normal reviewed migration runners before final readiness. Migration 053 is unchanged; its free-form `current_step` and ordered `completed_steps` already represent the extra reload stages. Migration 045 installs `ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)` and removes direct marker UPDATE from the runtime role.

Create verified platform and separate selected-client recovery media and a protected checkpoint binding source environment and every existing UUID. Run readiness and then the same command without readiness mode for the immutable dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/promote_environment_identity.py \
  --from-environment local_dev \
  --to-environment production \
  --platform-uuid '<EXISTING_PLATFORM_UUID>' \
  --client-code '<CLIENT_CODE>' \
  --expected-client-db-uuid '<CLIENT_CODE>=<EXISTING_CLIENT_DATABASE_UUID>' \
  --runtime-environment-file /etc/log-platform/environment-identity.env \
  --backup-reference '<VERIFIED_PROMOTION_CHECKPOINT_JSON>' \
  --recovery-root '<APPROVED_RECOVERY_ROOT>' \
  --preserved-recovery-backup '<VERIFIED_PRESERVED_BACKUP>' \
  --recovery-evidence '<VERIFIED_RECOVERY_EVIDENCE_JSON>'
```

The read-only result binds before/intended-after checksums, helper/version/source+installed+root-owned-parser hashes, installed consumer hashes, selected clients, client primitive contract, reload/recreate actions, verification commands, plan SHA-256, and exact attestation. It creates no journal or file. Promotion and rollback execute the fixed helper only through `sudo -n`; absent NOPASSWD authorization fails immediately with `BLOCKED_BY_CANONICAL_IDENTITY_PRIVILEGE_PATH`, with no interactive retry, askpass/password mechanism, shell, or broad-root fallback.

Contract v5 requires the three explicit recovery arguments (`--recovery-root`, `--preserved-recovery-backup`, `--recovery-evidence`) on readiness, dry-run, execute, resume and rollback planning. The hashed plan—not an adjacent report—contains the checkpoint hash/metadata, recovery owner/modes/hashes and provisioning plan, exact host/HEAD/branch, systemd PID and InvocationID, Docker container/image/creation identity, ordered drop-in hashes, Compose `config --hash api` fingerprint, database marker/control-plane/capability baselines, normalized effective helper sudo policy and fingerprint, deterministic security-critical implementation asset hashes, sorted rolled-back v4 journal/evidence history, the complete explicit exclusions, and post-promotion gates. Execute reconstructs the same plan again immediately before journal creation. A new service invocation, container, Compose hash, checkpoint, recovery artifact, source asset, sudo-policy fingerprint/structure, historical evidence, exclusion set, marker, journal baseline, host or HEAD requires a new dry-run and attestation. New forward plans and execute use only `promotion_plan_contract_version=5` and `contract=v5`; v3/v4 forward attestations return `PROMOTION_PLAN_CONTRACT_SUPERSEDED`. Historical v4 rows remain readable and recovery-v2 continues to bind their original v4 bytes without reinterpretation.

Promotion dry-run, execute, readiness planning, resume planning, recovery-v2 planning, and execution use the same clean-worktree guard. New v5 promotion plans bind the exact original execution HEAD. Recovery-v2 keeps its separately designed original/current implementation binding. Forward finalization now uses resume-v2's dual-HEAD boundary: `original_execution_head` comes only from the frozen v5 plan, while `resume_implementation_head` comes from the current clean checkout and must be identical to or a descendant of the original. Unrelated, sibling, or reversed ancestry is `RESUME_IMPLEMENTATION_HEAD_UNRELATED`. Frozen `original_implementation_assets` remain historical evidence and are not compared with current disk bytes; the separately sorted `resume_implementation_assets` must match the live security-critical resume implementation, including `ops/resume_plan_v2.py`. A dirty checkout still blocks before journal activity, and neither head relaxation nor the new asset set weakens original v5 execution or recovery-v2.

**NOT EXECUTED — REQUIRES EXPLICIT USER APPROVAL:**

```bash
# Re-run the identical reviewed dry-run command with:
#   --execute --attestation '<EXACT required_attestation FROM DRY-RUN>'
```

The v5 canonical `excluded_actions` list is sorted and contains: `worker_activation`, `email_activation`, `email_send`, `smtp_access`, `imap_access`, `alpha_backfill`, `snapshot_recalculation`, `schedule_change`, `business_data_mutation`, `unrelated_schema_change`, `systemd_api_restart_during_initial_promotion`, `docker_api_recreation_during_initial_promotion`, `timer_restart`, `prune_invocation`, `backup_invocation`, `systemctl_daemon_reload`, `docker_daemon_restart`, `recovery_evidence_cleanup`, `recovery_artifact_removal`, `uuid_generation`, `uuid_change`, `manual_direct_client_marker_update`, `automatic_retry`, `automatic_resume`, and `automatic_rollback`. Removing or renaming any item changes the plan hash and execute rejects it before journal creation.

Execute order:

1. lock and freeze the immutable migration-053 journal;
2. revalidate backup/readiness/helper/client primitive evidence;
3. for each selected client, open an IDLE mutation connection, commit one top-level SECURITY DEFINER transaction, close it, and verify the marker/UUID/capability through a fresh read-only connection before journalling completion;
4. update only selected control-plane expected environments;
5. update only the platform marker environment;
6. invoke the narrow root helper for the canonical file and journal checksums/backup;
7. remain `in_progress` at `runtime_reload_required` and print the exact read-only `--resume-plan` phase plus a separately approved execution phase;
8. after separately approved systemd restart/Docker recreation, generate a fresh read-only resume-v2 plan; do not reuse the retired original resume command;
9. approve only the generated `future_command` containing the exact `--resume-plan-sha256` and resume-v2 attestation; verify every surface, atomically finalize the journal, and re-read it before reporting completion.

A client function call locks the primary marker, checks database name, `client_business` role, UUID, source and target, updates only `environment`, and returns verified values. That return is in-session evidence only. Durable completion requires successful top-level commit and a new read-only connection observing the expected marker and UUID. A repeated already-target call returns `changed_row_count=0` but receives the same fresh verification before journal completion. Unknown values, missing capability, direct UPDATE, wrong UUID/role/name/source, commit failure, post-commit mismatch, changed helper/config hash, stale process, or unplanned client fails closed.

### Failure, inspection, resume and rollback

Read-only journal inspection remains available during an active promotion:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/promote_environment_identity.py \
  --runtime-environment-file /etc/log-platform/environment-identity.env \
  --inspect-promotions all
```

A historical `completed_steps` entry is never accepted without fresh reconciliation. `--resume-plan` generates only `environment_identity_resume_plan_v2` (serialized plan version `3`, attestation `contract=resume-v2`). Versions 1 and 2 are non-executable and receive no compatibility defaults. Resume-v1 is permanently non-executable; its retired hash is `2b4fafc8d4b43446fe2d21a7da5d1ed57ff13f476fd70e8c9a8dbfc3174e67f5`. `--resume-plan-v1-diagnostic` reports `executable=false`, `contract_retired=true`, and that hash, but its output cannot enter execution.

#### Resume-v2 approval and execution flow

Apply additive platform migration `054_environment_identity_resume_contract.sql` through the normal reviewed migration runner before generating or executing a deployed resume-v2 plan. It adds nullable, write-once `resume_contract` and `resume_plan_sha256` audit fields, validates a non-null plan hash as lowercase 64-hex, makes existing runtime-file backup/hash evidence write-once after its first non-null value, and completes immutable plan-column coverage by protecting `started_at`. Never apply this migration as part of plan generation or resume execution.

##### Remote Git and journal schema capability gates

Every resume-v2 dry-run queries the actual remote with `git ls-remote origin refs/heads/main`. The canonical approval binds remote name, normalized repository identity, exact full ref, actual remote SHA, local HEAD and equality. It never fetches and never trusts a stale local `origin/main`. Query failure, a missing/duplicate record, malformed SHA, repository-identity drift, unsupported branch relationship, or local/remote mismatch blocks generation. A remote change after approval is `RESUME_REMOTE_HEAD_DRIFT`.

The promotion tooling reads the live catalog instead of assuming a schema version. The lightweight schema-053/054 capability probe remains available to forward-v5, rollback and inspection. Resume-v2 additionally collects and validates the complete `migration_054_complete_catalog_v1` contract before plan construction: immutable database identity and expected database name; exact migration filename, one history row and migration ceiling; both columns and their type/null/default/generated/identity/physical-position evidence; exact named and validated constraint predicates; trigger-function body hash, owner, language, SECURITY mode, volatility and configuration; enabled trigger definition and function binding; both exact column comments; and `target_promotion_resume_audit_state` for the exact operator-selected promotion UUID.

The production-lineage `attnum` values 22/23 are hard invariants because migration 054 is additive to the exact migration-053 table layout; a restore with different physical positions needs independent review and a regenerated contract. OIDs are not approval identity. The collector validates the known expected contract before serializing it. Correct names with changed predicates, `NOT VALID` constraints, altered function body/return type/owner/security/volatility/search path, a disabled/rebound/changed trigger, missing/duplicate migration history, changed comments, unexpected defaults/nullability/types, and nonzero audit state on the target promotion all block with precise schema-contract evidence. Comment drift is deliberately blocking.

The audit-value query binds the target UUID through the PostgreSQL driver and never derives it from active-row cardinality. Its target promotion ID must equal the IDs in `approval_identity`, `journal_state`, and `remaining_execution_contract.writable_promotion_id`; a mismatch rejects the version-3 plan. Historical completed or failed promotions may retain non-null write-once resume evidence and do not block a later independent paused promotion. The target row's two audit fields must remain null during plan generation, initial execution collection, under-lock pre-write collection, every post-progress collection, and pre-finalization collection. Target drift reports `RESUME_SCHEMA_CONTRACT_DRIFT` with `target promotion resume audit columns already contain values` and the sanitized target ID. Before the first write this is a zero-write failure with lock cleanup; after a progress write it is a truthful partial result requiring reconciliation. Changing audit values on an unrelated historical row does not change the target contract or plan hash.

Disposable PostgreSQL 16 regression coverage includes a completed Promotion A with preserved write-once resume-v2 evidence and an independent paused Promotion B with null audit fields. Promotion B's version-3 plan is collected, validated, serialized and hashed without deleting or clearing Promotion A. Negative cases cover each partial/full target audit-value shape, a missing target row, cross-section promotion-ID mismatch, pre-write and post-progress target drift, unrelated-history changes, and refusal to clear Promotion A's evidence.

After that raw expected-contract validation succeeds, legitimate catalog absence is normalized to non-empty canonical plan sentinels: `not_identity`, `not_generated`, `no_default`, `no_arguments`, and the existing `not_configured` function-configuration sentinel. The exact field-to-sentinel mapping is part of plan-version-3 canonical bytes. It does not weaken missing-evidence checks: an unexpected catalog value is rejected before normalization, and a missing key, null, or empty string is still non-approvable. Plan version remains 3, and no rejected or older plan gains compatibility from this representation.

Journal inspection itself is compatible with both schemas: the optional audit fields are read through `to_jsonb(journal) ->> '<column>'`, which yields the stored text under migration 054 and SQL null under migration 053. Missing-column errors are never caught and suppressed, no identifier is interpolated from operator input, and no audit value is ever fabricated.

The gate is asymmetric on purpose:

- **Forward-v5 execution, rollback and promotion inspection do not require migration 054.** Forward and rollback execution require a *consistent* schema. Each execution route checks it through a short-lived read-only capability-probe connection, closes that probe, and only then may open write-capable execution connections or attempt the advisory lock.
- **Resume-v2 requires the complete migration-054 contract.** Missing migration 054 returns `RESUME_V2_AUDIT_SCHEMA_REQUIRED`; the one-column transition retains `ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE`; all other partial or semantically incorrect 054 shapes return `RESUME_SCHEMA_CONTRACT_DRIFT`. Every case stops with `writes_performed=false` before the first durable write.
- **Reachable partial shapes are blocked.** This includes two columns with zero constraints, two columns/two constraints with the migration-053 function, full DDL without its history row, wrong predicates, unvalidated constraints, an altered function, and a disabled or rebound trigger. Forward-v5 and inspection remain supported on complete schema 053; the existing early consistency gate still protects forward and rollback from the one-column transition state.

Migration 054 is forward-only. Its replacement immutability trigger function reads the audit columns, so dropping those columns without restoring the migration-053 function body would leave a trigger referencing fields the row type no longer has and every journal `UPDATE` would fail. Never "un-apply" it by dropping columns; the disposable-database regression `ops/tests_manual/test_promotion_journal_schema_compatibility_postgres.py` re-applies migration 053 when it needs to model the older schema.

##### Production deployment order

Preferred sequence, each step a separate approval:

1. publish and deploy the reviewed code to the production checkout;
2. apply migration `054_environment_identity_resume_contract.sql` through its separate migration gate (`ops/db_migrate.sh`);
3. verify the complete migration-054 contract, not merely the two columns;
4. generate the read-only production resume-v2 plan with `--resume-plan`;
5. separately approve the exact `resume_plan_sha256` and the exact resume-v2 attestation from that output;
6. execute the journal-only finalization using only the generated `future_command`.

Step 2 may safely occur before step 1: migration 054 is additive and the previously deployed code never references the audit columns, never rewrites `started_at`, and never changes a non-null runtime-file evidence value. Deploying code before the migration is also safe now — forward-v5 execution and promotion inspection stay fully functional on migration 053. What the code-before-migration interval is **not** safe for is resume-v2: plan generation and execution both refuse with `RESUME_V2_AUDIT_SCHEMA_REQUIRED` until step 2 completes, and that refusal happens before any write. Do not treat that interval as a window in which resume-v2 may be attempted.

The canonical plan-version-3 document adds required `remote_repository_binding` and `migration_054_schema_contract` sections to the earlier resume-v2 approval identity. It binds host/repository/branch, both local HEADs, actual `origin` repository/ref/HEAD equality, promotion/original-plan identity, the complete validated migration-054 contract including the exact target promotion audit state, source/target/platform/client UUIDs, canonical file metadata and hash, marker/control-plane/capability conclusions, ordered journal reconciliation and history, readiness, systemd drop-ins/PID/InvocationID/ControlGroup/cgroup membership/listener ownership, Docker container/image digest/Compose fingerprint/identity, helper/parser/provisioning/recovery evidence, old and live implementation assets, structural sudo policy, and retired contracts. Canonical bytes reuse the platform's sorted compact ASCII JSON serializer and SHA-256 helper. Changing the target promotion ID or target audit state changes canonical bytes or fails validation; unrelated historical audit values are not serialized into this field. Missing new sections, version 2, or the rejected production hash `4a409a35ae96cb64d4c6027af0c04db5f2dfe58a3e9dcb6436f58f073dfbb5f5` cannot enter execution.

Sudo collection is pinned to `LC_ALL=C`, `LANG=C`, and `LC_MESSAGES=C` with `COLUMNS` removed. The parser normalizes the complete ordered command-policy section, including broad and helper-specific matches, run-as identities, tag state and later-rule effects. The primary `sudo_n_l_normalized_v2` fingerprint hashes the explicit locale and the complete normalized structural records, while the frozen v5 fingerprint remains historical compatibility evidence. An exact root `NOPASSWD` helper-specific rule is mandatory; multiple exact records are represented, and any later matching rule that changes the effective requirement to `PASSWD` fails closed.

The parser understands a deliberately small grammar and refuses anything else instead of guessing. Command lists are split by an explicit tokenizer, so a bare `ALL` counts as matching the helper wherever it appears in the list (`/bin/true, ALL`, `ALL, /bin/true`, `/bin/true,ALL`). Supported: `ALL`, absolute command paths, negated absolute paths, comma-separated lists of those, escaped commas inside a command specification, the existing run-as forms, and the `NOPASSWD`/`PASSWD` tags. Refused with `RESUME_SECURITY_BINDING_DRIFT`, before any plan is built: unresolved or negated command aliases (for example `LOGPLATFORM_HELPER`), other bare uppercase identifiers, malformed lists, ambiguous or trailing escapes, unsupported wildcard/argument forms, and any specification that cannot be proven not to match the helper. On a rule that can match the helper, only `NOPASSWD` and `PASSWD` are accepted, so `SETENV`, `NOEXEC`, `EXEC`, `LOG_INPUT`, `LOG_OUTPUT` and unknown tags fail closed rather than being recorded and approved. If you legitimately need one of these forms on this host, the parser must be extended and rereviewed; do not work around it by loosening the policy.

Runtime approval uses independent probes: the systemd API at `http://127.0.0.1:8001/docs` and Docker API at `http://127.0.0.1:8000/docs`. A failed/unavailable probe blocks generation. Systemd binds `NeedDaemonReload=false`, ControlGroup and current membership; `MainPID` must be the sole cgroup member and must own the listening socket for `127.0.0.1:8001`. Health from an unrelated process is insufficient. Docker requires exactly one active Compose `api` container and binds its full ID, immutable image digest, working directory, ordered Compose files, and `config --hash api` fingerprint; volatile full inspect/network/MAC/PID/duration/timestamp data is excluded. Every Docker convergence boolean is computed from those observations — state, active count, observed identity versus target, and the actual canonical-source and health results — so the serialized plan changes when the runtime changes and refuses when any required component is false.

This systemd proof supports exactly one runtime shape: a single persistent process in the service cgroup (`MainPID`), the cgroup v2 unified hierarchy, the currently supported loopback listener representation for `127.0.0.1:8001` owned by that PID, and one uvicorn worker. Configure the host accordingly before generating a plan. Multiple uvicorn workers, a native `::1`-only listener, cgroup v1 or a hybrid hierarchy, and any leftover helper process in the service cgroup all fail closed; that is intended, and the fix is to converge the runtime to the supported shape (or extend and rereview the collector), not to relax the guard. See `docs/06_security.md` for the full envelope.

The only permitted actions, in canonical order, are:

1. `reverify_completed_steps_read_only`
2. `verify_runtime_processes_against_bound_identity`
3. `reconcile_journal_against_persistent_and_runtime_reality`
4. `record_completed_step:runtime_reload_required`
5. `reverify_runtime_processes_against_bound_identity`
6. `record_completed_step:runtime_processes_verified`
7. `final_read_only_cross_surface_verification`
8. `atomic_record_final_verification_and_transition_completed`
9. `fresh_connection_verify_journal_completed`

Only the exact promotion row may change, only from `in_progress` to `completed`. Resume-v2 attempts no persistent identity-surface write and performs no restart, recreation, or daemon reload.

Step 8 is a single atomic transaction, and the original forward-v5 executor now finalizes the same way through `journal_finalize_forward_v5` (that path stays schema-053 compatible and writes no resume audit columns). Neither executor can therefore leave a journal with all steps complete, `current_step=NULL` and `state='in_progress'` — the shape no contract could resume.

If a resume or forward-v5 run is interrupted after a durable write, trust the reported result: every subsequent failure — including a raw database, connection-close or lock-release error — is structured, reports `writes_performed=true`, `reconciliation_required=true` and the partial exit code, and is never reduced to a plain precondition failure. A primary execution error always wins over cleanup errors. Forward cleanup independently handles write-connection close, read-connection close, advisory-lock release and lock-connection close; failures are sanitized and appended in order under `cleanup_failures` without replacing `FORWARD_V5_EXECUTION_INTERRUPTED` or another primary classification.

When execution itself reached its normal pause or completed result but cleanup failed, the forward route reports `FORWARD_V5_CLEANUP_FAILED`. This is a partial/ambiguous result after writes, not proof that execution failed: `runtime_reload_required` may remain durably paused, or atomic completion may already be durable. Re-read the journal with the valid read-only form `--inspect-promotions all --promotion-id '<PROMOTION_UUID>'` to establish the actual state, and do not retry automatically. Cleanup is not infallible. The guarantee is only that cleanup failures cannot hide the durable write state or replace the primary execution classification. A failure reported with `writes_performed=false` means no journal transaction committed.

The canonical sorted prohibited set covers automatic retry/rollback; backup, backfill, business-data, provider, job, mail, Selenium, snapshot, schedule, worker and prune activity; client/control-plane/platform/file/helper mutations; UUID changes; all systemd/Docker restart/recreate/daemon actions; and direct marker primitives/updates. Resume-v2 does not call or reference the original idempotent mutation path even when an old reconciliation would have called it.

Phase 1 is read-only plan generation with every original scope/recovery argument and no `--execute`:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/promote_environment_identity.py \
  --from-environment '<ORIGINAL_SOURCE>' \
  --to-environment '<ORIGINAL_TARGET>' \
  --platform-uuid '<ORIGINAL_PLATFORM_UUID>' \
  --client-code '<ORIGINAL_CLIENT_CODE>' \
  --expected-client-db-uuid '<CLIENT_CODE>=<ORIGINAL_DATABASE_UUID>' \
  --runtime-environment-file '<ORIGINAL_CANONICAL_PATH>' \
  --backup-reference '<ORIGINAL_CHECKPOINT>' \
  --recovery-root '<ORIGINAL_RECOVERY_ROOT>' \
  --preserved-recovery-backup '<ORIGINAL_PRESERVED_BACKUP>' \
  --recovery-evidence '<ORIGINAL_RECOVERY_EVIDENCE>' \
  --promotion-id '<PROMOTION_UUID>' \
  --resume-plan
```

Phase 2 is separately approved execution. Copy only the exact generated `future_command`; it includes both `--resume-plan-sha256 '<EXACT_SHA256>'` and `--attestation '<EXACT_RESUME_V2_ATTESTATION>'`. Never fabricate an unknown hash and never execute a paused command that omits either argument. Retired resume-v1 command material is non-executable diagnostic evidence.

Execution of a journal already in the supported post-convergence suffix requires both `--resume-plan-sha256 '<EXACT_SHA256>'` and the exact single-line `RESUME_ENVIRONMENT_IDENTITY_V2 ...` attestation; no v1 fallback exists. A promotion interrupted earlier is classified `PRE_RUNTIME_FORWARD_RESUME_CONTRACT_REQUIRED` before it can be mistaken for a resume-v2 approval error, and no early-stage forward-recovery contract is implemented here. Before lock acquisition, execution reconstructs the whole plan from fresh observations, including a new actual-remote query and complete schema-contract query, and verifies the requested hash/canonical bytes/attestation. It then acquires the advisory lock through an autocommit session whose `default_transaction_read_only=on` setting is verified and reconstructs and byte-compares everything again before the first write. Every later non-journal revalidation repeats those remote/schema collectors before the next journal write or finalization. Ordinary writes through the lock session are rejected. Remote and schema movement are `RESUME_REMOTE_HEAD_DRIFT` and `RESUME_SCHEMA_CONTRACT_DRIFT`, never journal drift.

If the remote or schema collector fails after a journal write has committed, execution preserves the primary drift/failure classification, reports the exact phase, `writes_performed=true`, partial exit status and `reconciliation_required=true`, and performs no later write. Reconcile the durable completed-action prefix and regenerate a plan; never reuse the approved value as a current observation.

The plan generated on 2026-07-30 with SHA-256 `4a409a35ae96cb64d4c6027af0c04db5f2dfe58a3e9dcb6436f58f073dfbb5f5` is **REJECTED — INCOMPLETE APPROVAL BINDING / DO NOT APPROVE / DO NOT EXECUTE**. Its evidence is retained for audit only. No plan generated before this fix is approvable. After this fix is independently reviewed, deployed, and production state is rechecked, generate a completely new production plan; any Git remote or schema change requires regeneration.

`runtime_reload_required` and `runtime_processes_verified` remain separate exact-row transactions. Final verification performs another complete read-only reconciliation, then one atomic transaction idempotently appends `final_verification`, records the null-or-same resume contract/hash, sets `state='completed'` and `completed_at`, and clears `current_step`, `failed_at`, and `error`. Its predicate includes the exact promotion ID, `state='in_progress'`, current final step, exact pre-final step prefix and null-or-approved audit fields; exactly one row must change. A fresh read-only connection must observe `completed`, null current step, the exact full step list and matching audit fields. There is no durable all-steps-complete/`in_progress` state. Interruptions before the atomic transaction leave a generatable final suffix; interruptions after commit or during the fresh read leave a completed row for independent reconciliation. After any earlier committed progress write, a later failure forces `writes_performed=true` and partial exit code `6`, overriding stale inner details. Regenerate `--resume-plan` after interruptible progress because the suffix/hash changes.

Failed-promotion rollback is dry-run by default and generates `failed_environment_identity_recovery_plan_v2`. The plan binds the current implementation HEAD, original v4 hash, failed journal, actual markers/control plane, canonical metadata and explicit null promotion backup/evidence, UUIDs/capabilities, runtime IDs/health/Compose fingerprint, sudo/helper/parser assets, checkpoint/provisioning recovery artifacts, ordered transactions and exclusions. The audit-only recovery-v1 hash `9d417d3bb88451d1edb1bba4c67564ae0b3947a59cec93060bd25f7ad6278cf5` is never executable.

Recovery-v2 uses a dedicated read-only autocommit lock connection; `default_transaction_read_only=on` is verified, advisory locking remains available, ordinary DML is refused, and rollback writes use a separate connection. Each client reverse primitive commits from IDLE and is freshly verified. Platform marker plus selected control-plane reversions commit in one explicit transaction. Fresh connections must then prove all surfaces, unchanged runtime IDs/health and `PRODUCTION_PROMOTION_READY`. Only afterward does a separate journal transaction commit `rolled_back`; a new connection verifies it, checksum-bound evidence is atomically written under the recovery root, and its path/hash is committed into the journal. No success is printed before these boundaries. Cleanup independently closes the write connection, releases an acquired lock, and closes the lock connection; all failures use ordered `operation`/`exception_class`/`detail` records and cannot replace the primary result. No automatic retry occurs.

Rollback interruption classifications and required action:

- `FAILED_PROMOTION_RECOVERY_PARTIAL_STATE`: a rollback mutation started or may have committed. Treat completed/in-flight action and journal/evidence commit flags as bounded evidence, not a retry instruction. Stop writes and reconcile every named surface, the journal and rollback evidence through fresh read-only connections.
- `RECOVERY_V2_EXECUTION_INTERRUPTED`: an unexpected raw exception occurred before any rollback mutation. It reports `writes_performed=false`; correct the precondition only after confirming the journal remains unchanged. Do not infer that a separate operator/process made no concurrent change.
- `RECOVERY_V2_CLEANUP_FAILED`: the rollback body reached its normal result but releasing resources failed. This is partial/ambiguous after any mutation, even when a fresh read had already observed `rolled_back` and the evidence reference. Never attempt to change a terminal row back to `failed`.

After rollback mutations begin, an existing path-specific `PromotionError` retains its original classification, for example `RECOVERY_READINESS_FAILED`, while the rollback boundary upgrades its exit to `EXIT_PARTIAL` and reports `writes_performed=true`, `reconciliation_required=true`, completed and in-flight actions, and any cleanup failures. This has the same operator consequence as `FAILED_PROMOTION_RECOVERY_PARTIAL_STATE`, `RECOVERY_V2_EXECUTION_INTERRUPTED`, or `RECOVERY_V2_CLEANUP_FAILED`: (1) do not rerun automatically; (2) inspect the journal read-only; (3) inspect recovery evidence; (4) compare the actual platform, client, and canonical surfaces; and (5) obtain a new reviewed recovery plan before any further mutation. This preserves the primary classification while applying the partial-state handling required by `docs/06_security.md`.

Use this read-only reconciliation first:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/promote_environment_identity.py \
  --runtime-environment-file /etc/log-platform/environment-identity.env \
  --promotion-id '<PROMOTION_UUID>' \
  --inspect-promotions all
```

Confirm the exact journal state/current step, surface environments, unchanged UUIDs, and any `ROLLBACK_EVIDENCE path=... sha256=...` reference. Inspect the referenced evidence as a regular non-symlink mode-`0600` file under the approved recovery root and verify its recorded SHA-256 without editing it. Do not automatically rerun recovery, resume, or a second rollback. A newly reviewed plan/approval is required only after the read-only reconciliation establishes the actual durable state.

**NOT EXECUTED — REQUIRES EXPLICIT USER APPROVAL:**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
  ops/promote_environment_identity.py \
  --runtime-environment-file /etc/log-platform/environment-identity.env \
  --backup-reference '<ORIGINAL_PROMOTION_CHECKPOINT_JSON>' \
  --recovery-root '<ORIGINAL_APPROVED_RECOVERY_ROOT>' \
  --preserved-recovery-backup '<ORIGINAL_VERIFIED_PRESERVED_BACKUP>' \
  --recovery-evidence '<ORIGINAL_VERIFIED_RECOVERY_EVIDENCE_JSON>' \
  --promotion-id '<PROMOTION_UUID>' \
  --rollback-plan
# After review, use the exact future_command from the plan. It includes:
#   --rollback --execute --recovery-plan-sha256 '<EXACT SHA-256>'
#   --attestation '<EXACT recovery-v2 ATTESTATION>'
# Never substitute the recovery-v1 audit hash/attestation.
```

Exit codes remain: `0` success/read-only result, `2` invalid intent/attestation, `3` precondition/runtime/database refusal, `4` readiness incompatibility, `5` concurrent promotion, and `6` durable partial/reload-required state.

### API drop-in precedence remediation

Systemd merges drop-ins lexically. An empty `EnvironmentFile=` in a later drop-in clears every earlier entry, so `90-environment-identity.conf` is ineffective when the host `override.conf` sorts after it. The repository API asset is therefore `zz-environment-identity.conf`, containing only the later canonical `EnvironmentFile=/etc/log-platform/environment-identity.env` directive; it does not reset or remove the host file used for unrelated settings.

Use `ops/remediate_runtime_identity_systemd.py` without `--execute` to bind the host, clean committed HEAD, canonical/override/obsolete/source hashes, fixed service-user recovery ownership, and preserved recovery evidence into an immutable plan. Dry-run performs the same recovery-root/artifact UID/GID/mode, symlink, hash, and evidence-plan checks as root execute. Its separately approved execute path installs and verifies `zz-...`, runs `systemctl daemon-reload`, proves the canonical source survives the merged reset order, preserves then retires obsolete `90-...`, reloads metadata again, and verifies the final merge. It never restarts a service or timer and never recreates Docker.

If this dry-run reports a recovery-root permission mismatch, do not change permissions ad hoc and do not retry systemd remediation. Generate the separate immutable dry-run with `ops/remediate_runtime_identity_recovery_permissions.py`; its only allowed mutation is the exact approved recovery root mode transition from `0755` to `0700`, followed by owner/mode/symlink/hash/evidence verification. It performs no systemd change or daemon reload. Commit changes invalidate earlier plans; permission repair and systemd remediation always require separate attestations and approvals.

Readiness reports prune and backup as `PER_INVOCATION_READY` when their inactive oneshot units have recognized canonical drop-ins. Their next invocation loads the canonical source; restarting their timers gives no identity benefit. Only the long-running systemd API requires a separately approved restart, and only the Compose API container requires a separately approved recreation. A matching old `local_dev` value is still stale until creation/start time and health prove canonical provenance.
