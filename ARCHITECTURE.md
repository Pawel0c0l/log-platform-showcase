# ARCHITECTURE

High-level overview of the **Log Platform** repository. Detailed counterpart: `docs/01_architecture.md`. **Code is the source of truth** for implemented behavior; this file is a navigational map for AI agents and engineers.

---

## 1. System position

Single-host automation, observability and data-integration platform composed of three layers:

| Layer | Status | Purpose |
|---|---|---|
| **Platform core** | implemented | Run lifecycle, structured logs, artifacts, retention. Reusable for any job. |
| **Workflow A — Telematics sync (primary)** | partially implemented (Phase 2 jobs + dispatcher + retention worker) | External provider API → client business Postgres DB; incremental upsert per client. |
| **Workflow B — IMAP report pipeline (paused backup)** | partially implemented | IMAP → `ingest.*` Stage 1 → Stage 2 detection/validation. Stage 3 not implemented. |

**Default for new development:** Workflow A. Do not extend Workflow B unless explicitly scoped.

---

## 2. Top-level components

```
+-------------------+        +----------------------+         +--------------------+
|  ops/runner.py    | -----> |  api/client.py       | ----->  |  FastAPI           |
|  (host process)   |        |  LogPlatformClient   |  HTTP   |  api/main.py       |
|                   |        |  + run_context       |  + JSON |  (Docker)          |
+-------------------+        +----------------------+         +---------+----------+
        |                                                               |
        | importlib                                                      |
        v                                                               v
+-------------------+                                          +--------+--------+
|  jobs/<job>.run() |                                          | Postgres + MinIO|
|  (Workflow A / B) |                                          | runs/logs/artif |
+--------+----------+                                          +--------+--------+
         |                                                              |
         | (Workflow A only)                                            |
         v                                                              |
+-----------------------------+    +-----------------------+            |
| workflow_a_control.*        |<---+ control plane lookup  |            |
| (platform DB, schema)       |    +-----------------------+            |
+-----------------------------+                                         |
         |                                                              |
         | per-client DB (separate Postgres DB per client)              |
         v                                                              |
+-----------------------------+                                         |
| client business DB          |                                         |
|  client_trips               |                                         |
|  client_speeding_notifs     |                                         |
|  client_vehicle_daily_fuel  |                                         |
|  client_vehicle_driver_..._ |                                         |
+-----------------------------+                                         |
                                                                        |
                                                       legacy Workflow B
                                                                        v
                                                              ingest.imap_message
                                                              ingest.raw_file
                                                              (+ stage2 cols)
```

### 2.1 Runner (`ops/runner.py`)

- Loads `.env`, builds `LogPlatformClient.from_env()`, dynamic `importlib.import_module(job_module)`, calls `run(client, run_id, params)` inside `run_context`.
- Invocation: `python3 ops/runner.py <job_module> [params_json_or_file]`.
- `trigger`/`actor` derived from `params`; `source` = job module name.

### 2.2 Internal API (`api/main.py`)

- FastAPI on `127.0.0.1:8000`. Bearer auth: `API_READ_TOKEN` / `API_WRITE_TOKEN`.
- DB schema for `runs`, `logs`, `artifacts` is bootstrapped on startup. SQL migrations in `db/migrations/` add Workflow B (`ingest.*`) and Workflow A control-plane (`workflow_a_control.*`).
- Endpoints (canonical: `docs/03_api_spec.md`): `/health`, `/secure/ping`, `/runs` (POST/PATCH/GET), `/logs`, `/artifacts/upload|list|download`, `/maintenance/prune`.
- **Known issue:** duplicate `@app.patch("/runs/{run_id}")` and `@app.get("/runs")` route registrations — only the first is active (FastAPI registration order). Second variants are dead code.

### 2.3 Client (`api/client.py`)

- `LogPlatformClient`: `start_run`, `finish_run`, `log`, `upload_artifact`.
- `run_context` context manager: posts `RUNNING`, yields `run_id`, on exit `PATCH` to `SUCCESS`/`FAILED` (with traceback log) and re-raises.

### 2.4 Job contract

```python
def run(client, run_id: str, params: dict) -> None:
    ...
```

Job MUST NOT create or finish runs itself — `run_context` owns lifecycle.

---

## 3. Workflow A — Telematics provider sync (primary)

Implemented as Phase 2 data jobs plus host-driven scheduling support. Operator can invoke runner manually with `client_id` and time window, or run the DB-driven dispatcher from a host timer.

### 3.1 Components

| File | Responsibility |
|---|---|
| `jobs/api/telematics/sync_trips_and_speeding.py` | Main job: fetch `/trips` and fleet-wide raw `/vehicles/events`; upsert trips; compute per-trip speeding counts and provider-labeled HIGH_RPM / OVERREV counts from locally filtered raw events. Trip-level fuel columns are deprecated and no longer written. |
| `jobs/api/telematics/aggregate_trip_fuel_daily.py` | Daily aggregation job: uses trips for distance/driver attribution, fetches fuel level via `GET /fuel/level/{registration}`, and writes `client_vehicle_daily_fuel` + `client_vehicle_driver_daily_fuel`. |
| `jobs/api/telematics/dispatcher.py` | DB-driven scheduler tick: validates enabled schedules against `registry.DATASETS`, claims one due fire, launches one dataset job through `ops/runner.py`, updates run history. |
| `jobs/api/telematics/retention_purge.py` | Client-business retention worker: validates tables/columns against `registry.TABLES`, deletes eligible rows in batches, defaults to dry-run. |
| `jobs/api/telematics/control_plane.py` | Loads `workflow_a_control.client_account` row for `client_id` into a frozen `ClientAccountConfig`. |
| `jobs/api/telematics/provider_client.py` | HTTP client for Telematics Fleet API: ≤31-day sub-window splitting, paginated GETs with strict pagination meta validation. |
| `jobs/api/telematics/provider_safety.py` | `SafetyLimits`, `ProviderRunBudget`, `TelematicsProviderSafetyError`. Hard caps on requests/run, /endpoint, /sub-window, /pages, retries, timeout. Fail-fast on suspected loops or pagination anomalies. |
| `jobs/api/telematics/secret_resolver.py` | Resolves `*_secret_ref` strings: env-var name → `os.getenv(name)`, or `file:/path` → file contents. |
| `jobs/api/telematics/registry.py` | Python allowlist/source for implemented V1 datasets and tables. Note: platform migration `015_*` declares V2 rows whose job modules are not implemented and are not present in the Python registry yet. |
| `scripts/onboard_workflow_a_client.py` | One-shot onboarding: provider auth preflight, client DB + user create, DDL, GRANTs, control-plane row, validation. Best-effort rollback on failure. |
| `scripts/templates/workflow_a_client.template.yaml` | Non-secret YAML template for onboarding. |

### 3.2 Data flow per run

1. `load_client_account_config(client_id)` from platform DB (`workflow_a_control.client_account`).
2. `resolve_secret(provider_basic_auth_password_secret_ref)` and `…client_db_password_secret_ref`.
3. `TelematicsFleetProviderClient.fetch_trips(window)` → split into ≤31-day sub-windows; paginated.
4. Get distinct registrations from fetched trips, fetch fleet-wide vehicle inventory once per run, then fetch `/vehicles/events` in adaptive chunks using default `limit=1000` (capped at 1000) and default max pages 500 per chunk.
5. Filter fleet events locally to trip registrations and `speed >= 140`; compute per-trip speeding counts from raw telemetry. Counts are per matching `/vehicles/events` row with no timestamp grouping or deduplication; rows with the same registration, timestamp, and speed still count separately. Buckets are `speeding_140_160_count`, `speeding_160_170_count`, and `speeding_170_plus_count`. `/trips.max_speed` is not used for exact counting. A per-day `max_pages=500` cap plus provider request budget prevents unbounded pagination.
6. Use the same fleet `/vehicles/events` fetch, without the speed filter, for provider-labeled HIGH_RPM / OVERREV. `*_START` and unsuffixed labels count as one event; `*_END` labels are recognized and ignored to avoid double-counting one provider incident. No numeric `rpm` thresholds are used.
7. Trip-level fuel enrichment is deprecated and skipped; `params["skip_fuel"]` remains accepted temporarily as a no-op.
8. Upsert into client business DB (`{schema}.client_trips`) keyed by `(client_id, provider_trip_id)`. `{schema}.client_speeding_notifications` remains in the schema for historical notification rows, but this job no longer writes it for HIGH_RPM / OVERREV counts.
9. Daily aggregation (separate job) reads upserted trips for distance/driver attribution, fetches daily fuel levels via `GET /fuel/level/{registration}`, computes fuel as start tank level minus end tank level, and writes to `client_vehicle_daily_fuel` + `client_vehicle_driver_daily_fuel`.

### 3.3 Conflict / freshness model

- Upserts are idempotent via `ON CONFLICT`; current Telematics jobs use `DO UPDATE` only when the dataset schedule has `overwrite_existing=true`.
- Speed unit semantics from raw fleet vehicle events are **not yet validated against payloads** — see `WARNING` log in `sync_trips_and_speeding.run`.
- `client_speeding_notifications.provider_notification_id` remains part of the legacy notification table shape, but the current trip sync writes speeding/RPM counts to `client_trips` and no longer inserts notification rows.

### 3.4 Per-client identity

- `client_id` (UUID, canonical) AND `client_code` (TEXT, human-readable, e.g. `DELTA00001`, optional but unique).
- Both are propagated to all client-business rows.

---

## 4. Workflow B — IMAP report pipeline (paused backup)

| Stage | Module | Status |
|---|---|---|
| 1 | `jobs.mail.fetch_reports` | implemented (~1.7k LOC) |
| 2 | `jobs.reports.stage2.job_stage2` | implemented |
| 3 | (DB write-back from accepted reports) | not implemented in repo |

Persistence: `ingest.imap_message`, `ingest.raw_file` (+ `stage2_*` columns). Artifacts may set `raw_file_id` linking to `ingest.raw_file(id)`.

---

## 5. Storage topology

| Store | Where | Contents |
|---|---|---|
| Platform Postgres | container `postgres` (mapped `127.0.0.1:5432`), DB `logdb` | `runs`, `logs`, `artifacts`, `ingest.*` (B), `workflow_a_control.*` (A control plane), `public.schema_migrations` |
| Client business Postgres | host or remote, **separate DB per client** | `public.client_trips`, `public.client_speeding_notifications`, `public.client_vehicle_daily_fuel`, `public.client_vehicle_driver_daily_fuel` |
| MinIO | container `minio` (`9000:9000`, `9001:9001`, all interfaces) | bucket `artifacts` (object key: `YYYY/MM/DD/{artifact_id}/{filename}`) |

---

## 6. Execution modes

| Mode | Status |
|---|---|
| Manual terminal: `python3 ops/runner.py <module> '{...}'` | implemented |
| systemd timer (host): `ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer` | template only |
| Workflow A dispatcher timer: `ops/systemd/proposed/log-job@dispatcher.{service,timer}` | proposed host unit; dispatcher job implemented |
| Workflow A retention timer: `ops/systemd/proposed/log-job@retention-purge.{service,timer}` | proposed host unit; retention job implemented |
| systemd timer for backups (`log-backup.timer`/`.service`) | service references missing `ops/automation_cli.py` |
| Email-triggered run | planned, host-side, not in repo |
| Legacy `client_schedule` cron model | superseded; `012_*` renames it to `client_schedule_legacy` |

---

## 7. Key design decisions (inferred from code)

- **First-class observability via the internal API**, not files/stdout: every job logs through `LogPlatformClient.log(...)` so a single Postgres query reconstructs job history per `run_id`.
- **Run is platform-owned, not job-owned**: `run_context` owns `start_run`/`finish_run`. Jobs may not change run status. Keeps lifecycle uniform.
- **Per-client isolation via separate DBs**: `workflow_a_control.client_account.client_db_*` points at an independent Postgres DB; one onboarded client = one new DB + user + grants.
- **Secret indirection by reference**: control-plane stores ref names (env-var name or `file:` path), not secret values. Resolution is host-managed via `secret_resolver.resolve_secret`.
- **Hard local safety budget for external API**: `provider_safety.SafetyLimits` caps requests / pages / retries; pagination loop / mismatch / non-progress raises `TelematicsProviderSafetyError` and fails the run rather than burning provider quota.
- **Dual identity (`client_id` UUID + `client_code` string)**: code carried alongside UUID so operator-friendly listings stay stable; UUID remains the join key.
- **Trip↔speeding-event matching with deterministic tie-break**: shortest containing trip, then earliest start. Avoids ambiguous attribution and keeps re-runs idempotent.

---

## 8. Integration points

- **External (Workflow A)**: Telematics Fleet API (HTTPS Basic Auth) — endpoints `/trips`, `/vehicles/events` for fleet-wide raw speeding telemetry and provider-labeled HIGH_RPM / OVERREV events, `/fuel/level/{registration}` for daily aggregation, plus legacy `/alerts/notifications`, `/fuel/consumed/{registration}`, and batch `/fuel/consumed` provider methods retained but not used by the current client flow. See `provider_client.py`.
- **External (Workflow B)**: IMAP server (TLS, port 993). Optional HTTP fetch of report links with allowlisted domains (`REPORT_LINK_DOMAINS_ALLOWLIST`).
- **Internal**: Postgres (psycopg3), MinIO (boto3 S3), runner ↔ API (HTTP + Bearer).

---

## 9. Threat model & limits

- Single shared read/write token per role → no multi-tenant auth at the platform API.
- API and Postgres bound to `127.0.0.1`; MinIO listens on all interfaces (intentional: console + S3).
- Repository must not contain real secrets (`.env` is gitignored, `.env.example` is the template).
- Provider HTTP client is the only caller bounded by `SafetyLimits`; other future provider clients should follow the same pattern.

---

## 10. Where to read next

- Lifecycle / API contract: `docs/01_architecture.md`, `docs/03_api_spec.md`, `docs/04_runner.md`.
- Job catalog: `docs/05_jobs.md`.
- ENV matrix: `docs/02_infrastructure.md`.
- Onboarding & ops: `docs/07_operations.md`.
- Cross-cutting: `CONVENTIONS.md`, `REPO_MAP.md`, `CURRENT_TASK_CONTEXT.md`.
