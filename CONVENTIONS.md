# CONVENTIONS

Patterns and rules observed in code. Use these when adding or refactoring.

---

## 1. Languages, runtime, dependencies

- **Python 3.12** (API container `python:3.12-slim`; host venv expected at `.venv/`).
- **No type-checking tool, no linter, no formatter** is configured. Existing code uses informal type hints (`from __future__ import annotations`, `Optional[X]`, dataclasses).
- **HTTP**: `requests`. **DB**: `psycopg` (v3, with `dict_row`). **S3/MinIO**: `boto3`. **API server**: `fastapi`.
- **Doc language is mixed Polish/English**; identifiers, log messages, commit messages, code comments are predominantly English. Polish is mostly in narrative docs and README.

---

## 2. Job module conventions

- Every runnable job module exports exactly one entrypoint:

```python
def run(client, run_id: str, params: dict) -> None:
    ...
```

- **Do not** call `client.start_run(...)` or `client.finish_run(...)` in a job — the runner's `run_context` owns lifecycle.
- Use `client.log("INFO" | "WARNING" | "ERROR" | "DEBUG", "SCRIPT", JOB_SOURCE, message, run_id=run_id, context={...})` for all observability.
- Use `client.upload_artifact(path, kind="...", run_id=run_id, raw_file_id=optional_uuid_str)` for binary outputs. `kind` is a free-form short code (existing values: `"REPORT"`, `"REPORT_RAW"`, `"REPORT_CLEANED"`, etc.).
- Define `JOB_SOURCE = "jobs.subpkg.module"` once at the top of the file; use it as the `source` argument in every log call. (`source` doubles as the `runs.source` column.)
- Validate `params` early; raise `ValueError("Missing required param: …")` for missing/invalid input — this surfaces as `FAILED` run status with the traceback in `logs.error`.
- **Idempotence is the default.** Use `INSERT … ON CONFLICT (…) DO UPDATE SET …` rather than `DELETE+INSERT` when writing into client business tables.

---

## 3. Naming

| Element | Convention | Example |
|---|---|---|
| Job modules | `jobs.<area>.<concrete_name>` | `jobs.api.telematics.sync_trips_and_speeding` |
| Job source string | full dotted module path | `JOB_SOURCE = "jobs.api.telematics.sync_trips_and_speeding"` |
| ENV vars (platform) | `UPPER_SNAKE_CASE` | `POSTGRES_HOST`, `LOG_API_URL`, `API_READ_TOKEN` |
| ENV vars (per client) | `<CLIENT_NAME_UPPER>_<PURPOSE>` | `DELTA_API_KEY`, `ECHO_DB_USERNAME` |
| Secret references in DB | env-var name OR `file:/abs/path` | `provider_basic_auth_password_secret_ref = 'DELTA_API_KEY'` |
| Telematics safety ENV | `TELEMATICS_PROVIDER_*` | `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN` |
| SQL identifiers (schema/tables in jobs) | passed through `_safe_ident()` (regex `^[a-zA-Z_][a-zA-Z0-9_]*$`) before string-formatting into queries | `_safe_ident(cfg.client_db_schema)` |
| Postgres tables | `snake_case` plural; primary keys `(client_id, …)` for client-business | `client_trips`, `client_speeding_notifications` |
| Migration files (platform) | `NNN_<purpose>.sql`, applied in lexical order | `008_workflow_a_control_plane.sql` |
| Migration files (client business) | `NNN_<purpose>.sql` in `db/client_business/`, applied by onboarding script in declared order | `011_extend_client_trips.sql` |

**`client_id` vs `client_code`:** `client_id` is the canonical UUID join key; `client_code` is an operator-friendly TEXT (e.g. `DELTA00001`). Always carry both in client-business rows; never substitute one for the other.

---

## 4. Lifecycle and error handling

- **Errors propagate.** Inside `run_context`, an exception triggers `client.log("ERROR", …, error=traceback)` then `PATCH /runs` to `FAILED`, then re-raise. Don't swallow exceptions in jobs unless the work is genuinely optional (e.g. per-trip fuel fetch in `_fetch_fuel_for_trips` returns `None` on failure and logs a `WARNING`).
- **Provider safety errors** (`TelematicsProviderSafetyError`) are intentionally **fatal at the run level** — never catch and continue requests after a safety stop.
- **Secret resolution failures** raise `SecretResolutionError`; do not log secret values, refs, or truncated derivatives.
- **Unknown payload shape** (e.g. notifications without a stable id) is handled defensively: derive a deterministic UUID, log a `WARNING`, continue.

---

## 5. Database access patterns

- All Python code uses `psycopg` v3. Two DSN families:
  - **Platform DB** (where `runs`, `logs`, `artifacts`, `ingest.*`, `workflow_a_control.*` live) — built from `POSTGRES_HOST/PORT/DB/USER/PASSWORD`.
  - **Client business DB** — built from `ClientAccountConfig.client_db_*` and `resolve_secret(client_db_password_secret_ref)`.
- Always close connections in `finally:` blocks; use `with conn.cursor() as cur:` for cursors.
- For multi-row writes, prefer `cur.executemany(...)` with a single `ON CONFLICT … DO UPDATE`.
- Schema names from config must pass `_safe_ident(...)` before f-string interpolation. Never f-string user-controlled values into SQL otherwise — use parameterized `%s`.

---

## 6. HTTP client patterns (provider integrations)

Pattern from `jobs/api/telematics/provider_client.py`:

- One `requests.Session` per client instance with Basic Auth.
- All calls go through `_request_json(...)` → bounded retries (timeout/connection only), no `5xx` retry, raise `TelematicsProviderSafetyError(code, ...)` on any anomaly.
- All paginated calls go through `_fetch_paginated(...)`:
  - cap on pages per sub-window,
  - strict `meta.current_page == requested_page`,
  - identical-fingerprint detection → `PAGINATION_LOOP`,
  - empty-page streak detection → `PAGINATION_NON_PROGRESS`.
- All time windows are split via `iter_31d_windows(...)` (provider 31-day rule) with a 1-second overlap.
- Every request goes through a `ProviderRunBudget` first → cumulative caps are checked before issuing.

When adding a new provider:
- Subclass / mirror this pattern. Define a `*ProviderSafetyError` and `SafetyLimits` for it. Don't share counters across providers.
- Surface tuning ENV with the same `<PROVIDER>_PROVIDER_*` prefix.

---

## 7. Secrets

- **Never** commit a real `.env`. The repo `.env` is gitignored.
- Control-plane DB **only** stores **references** to secrets (env-var name or `file:/path`), not values.
- `resolve_secret(ref)` is the single resolver. Two forms:
  - Plain string → env-var name (e.g. `"DELTA_API_KEY"`).
  - `file:/path/to/secret.txt` → reads file contents, `.strip()`-ed.
- Empty/missing → `SecretResolutionError`.
- Anything that ever needs to read a secret must accept a **ref**, not a value, and call `resolve_secret(...)` at the latest possible moment.

---

## 8. Run / log / artifact data model

- `runs.status ∈ {RUNNING, SUCCESS, FAILED, CANCELED}` — enforced by the API.
- `runs.params` is JSONB. The runner serializes the entire `params` dict; **don't** put secrets in `params`.
- `logs.context` is JSONB; prefer flat scalar keys for filterability (`source`, `run_id`, `endpoint`, `sub_window`, `abort_code`, `phase`).
- `logs.error` is reserved for traceback / multi-line error detail. Use it instead of stuffing tracebacks into `message` or `context`.
- `artifacts.raw_file_id` (UUID, optional) links artifact rows to `ingest.raw_file(id)` for **Workflow B** lineage. Don't reuse this for Workflow A; A jobs leave it `NULL`.

---

## 9. Time

- Wall-clock everywhere = UTC. Helpers:
  - `api/main.py:utcnow()` (server side).
  - `jobs/api/telematics/sync_trips_and_speeding.py:_parse_runner_iso_ts(s)` for runner-supplied `window_*_ts` (accepts ISO with `Z` or `+00:00`).
  - Provider time format is space-separated `%Y-%m-%d %H:%M:%S` UTC — produced by `provider_client._provider_dt_str(...)`.
- Never store naive datetimes. `_parse_provider_dt` and `_parse_runner_iso_ts` always assume UTC for missing tzinfo.

---

## 10. State management

- Platform DB owns **operational** state (runs, logs, artifacts, control plane, ingest).
- Client business DB owns **business** state (trips, notifications, daily aggregates) — separate per client.
- No additional "in-memory caches across runs" — each job run is a self-contained process; control-plane is loaded fresh at the start of `run`.
- Optional progress tracking lives in `workflow_a_control.client_sync_state(client_id, dataset, last_success_window_end_ts)` — currently **not written** by any job in repo (table exists but unused).

---

## 11. Testing

- No CI; no `pytest` config in repo.
- `ops/tests_manual/` holds ad-hoc scripts for local sanity checks (CSV normalization, `fetch_reports` dedup, Stage 2 detection). Run them by hand.
- New code is expected to be exercised manually via `ops/runner.py` against a local stack.

---

## 12. Migrations

- **Never edit** an applied migration. Always add a new one.
- Platform DB migrations live in `db/migrations/` and are applied by `ops/db_migrate.sh` (tracked in `public.schema_migrations`).
- Client-business migrations live in `db/client_business/` and are applied by `scripts/onboard_workflow_a_client.py`. Add new files in numeric order; update the `CLIENT_BUSINESS_DDL_FILES` list at the top of the onboarding script.
- Document migrations that change behavior in `docs/05_jobs.md` (data shape) or `docs/02_infrastructure.md` (ENV/connection impact).

---

## 13. Documentation discipline

- **Code is the source of truth** for implemented behavior.
- Docs explicitly distinguish **implemented**, **planned / target-state**, and **paused / backup**. Preserve those labels.
- When adding a new job: update `docs/05_jobs.md` (catalog) and add ENV to `docs/02_infrastructure.md` if a new var is required.
- When changing the API: update `docs/03_api_spec.md` and `docs/04_runner.md`; if behavior changes, also `docs/01_architecture.md`.
- Avoid silent renames of ENV vars or job modules — they are also part of host systemd units.
