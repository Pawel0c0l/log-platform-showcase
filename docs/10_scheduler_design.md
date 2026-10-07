# Scheduler / central per-client config design (Workflow A)

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the
> form `PYTHONPATH="$PWD" python3 ops/runner.py …` are development / local /
> debug only — they execute the mutable working tree. The supported production
> entrypoint is the installed wrapper
> `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See
> `docs/07_operations.md` -> *Release boundary*.


> **Platform-wide schedule visibility lives elsewhere.** This document is the
> Workflow A scheduler's *design*. The single operational view of every
> recurring execution on the platform — systemd timers, the dispatcher's
> database-driven fires, the self-pacing export worker and the Worker
> maintenance the Eco mailing run drives — is `ops/schedule_catalog.py`:
>
> ```bash
> PYTHONPATH="$PWD" .venv/bin/python -m ops.schedule_catalog --runtime --database
> ```
>
> That catalogue derives cadence, timezone and command from the unit files and
> from `workflow_a_control.client_dataset_schedule` rather than restating them,
> and fails when a recurring job exists outside it. It also derives each
> schedule's **guaranteed maintenance interval** and cross-checks it against
> `ops.retention_registry.MAINTENANCE_CYCLES` — retention's deadline look-ahead
> is computed from that number, so a timer slowed down without updating its
> declared cycle is a validation failure rather than a silent retention breach.
> See `docs/42_platform_retention_and_schedule_governance.md` §5 and §3.1.

This document is the canonical reference for the **central, per-client,
per-table configuration system** for Workflow A. It covers:

- the data model (registry + per-client schedule + per-client retention),
- the runtime contract on jobs (`record_id`, `synced_at`, `sync_run_id`,
  `overwrite_existing`),
- the retention worker,
- the **dispatcher** (lightweight DB-driven scheduler).

> **Current state.** The dispatcher landed in v1.1
> (`jobs.api.telematics.dispatcher` + migration `014_*`). Both the
> dispatcher and the retention worker are implemented and reachable via
> proposed systemd timers under `ops/systemd/proposed/`.

---

## 1. Hybrid granularity

A "logical dataset" in Workflow A is a job that writes one or more tables.
Two granularities matter:

- **Scheduling and sync behavior** are properties of the **producer** (the
  job that fetches and writes data): cadence, lookback window, "overwrite
  vs keep on conflict". These are per **dataset**.
- **Retention** is a property of the **data itself**: the speeding
  notifications stream and the daily fuel rollups can — and should —
  have very different lifetimes even when the same job populates both.
  Retention is per **table**.

This is the **hybrid model**. It is reflected in two distinct platform
tables, one for each axis.

| Concern             | Granularity | Platform table                                |
|---------------------|-------------|-----------------------------------------------|
| Scheduling          | dataset     | `workflow_a_control.client_dataset_schedule`  |
| Overwrite behavior  | dataset     | `workflow_a_control.client_dataset_schedule`  |
| Retention policy    | table       | `workflow_a_control.client_table_retention`   |

---

## 2. Source of truth: the registry

The Python module `jobs/api/telematics/registry.py` is the **runtime allowlist**
for datasets and tables the current dispatcher/retention worker may act on.
The platform DB tables `dataset_registry` and `table_registry` are seeded by
`011_workflow_a_dataset_registry.sql` for implemented V1 rows, then corrected by
`016_workflow_a_disable_declared_v2_registry.sql` so the active SQL registry is
also implemented-only.

V2 status: `015_workflow_a_v2_datasets.sql` previously declared V2
dataset/table rows, but the corresponding job modules are not implemented.
`016_*` removes those rows from the active platform registry. The V2 staging DDL
under `db/client_business/017_v2_staging_tables.sql` remains declared-only and
is not part of the dispatcher/retention runtime allowlist.

The Python registry also acts as an **identifier allowlist** for the
retention worker: every `(schema, table, retention_key_column)` triple that
ends up inside dynamic SQL must appear in `registry.TABLES`. Combined with
`psycopg.sql.Identifier`, this gives defense-in-depth against accidental
identifier drift.

```text
DATASETS                                     TABLES
─────────                                    ──────
trips_sync                                   client_trips                       (retention_key_column=start_timestamp)
  job: jobs.api.telematics.sync_trips_         client_speeding_notifications      (retention_key_column=event_ts)
       and_speeding
  tables: client_trips,
          client_speeding_notifications

fuel_daily_aggregation                       client_vehicle_daily_fuel          (retention_key_column=day)
  job: jobs.api.telematics.aggregate_          client_vehicle_driver_daily_fuel   (retention_key_column=day)
       trip_fuel_daily
  tables: client_vehicle_daily_fuel,
          client_vehicle_driver_daily_fuel
```

Adding a production-ready dataset/table is a four-step change:

1. Add a `DatasetSpec` / `TableSpec` to `registry.py`.
2. Add a platform migration that re-seeds `dataset_registry` /
   `table_registry` (the existing 011/016 pattern uses `ON CONFLICT DO UPDATE`).
3. Add a client-business DDL file under `db/client_business/` if a new
   physical table is required, and apply it via
   `scripts/apply_client_business_migrations.py`.
4. Update this document and the registry-sync test under
   `ops/tests_manual/`.

---

## 3. record_id — deterministic per-row UUID

Every Workflow A row carries a deterministic `record_id UUID` (UUID v5,
namespace `331a59e5-a43c-4447-895a-ebc72cdd4eac`). Helpers live in
`jobs/api/telematics/record_id.py`.

The formula is per table; the canonical function is
`record_id.compute(table_name, **business_key_kwargs)`. Components are
joined with the ASCII Unit Separator (`U+001F`) to avoid accidental
field-boundary collisions.

| Table                                | Composite business key                            |
|--------------------------------------|---------------------------------------------------|
| `client_trips`                       | `(client_id, provider_trip_id)`                   |
| `client_speeding_notifications`      | `(client_id, provider_notification_id)`           |
| `client_vehicle_daily_fuel`          | `(client_id, vehicle_id, day)`                    |
| `client_vehicle_driver_daily_fuel`   | `(client_id, vehicle_id, driver_id, day)`         |

`record_id` is:

- **deterministic** — recomputable offline, allowing dedup and back-references,
- **unique per dataset** — backed by a `UNIQUE INDEX CONCURRENTLY` after
  the rollout completes,
- **stable across schema changes** — independent of physical column names
  beyond the business key.

### 3.1 Safe rollout (multi-step)

`record_id` cannot just appear NOT NULL on day 1 — existing rows have no
value. The rollout therefore follows a strict sequence:

1. **Add nullable column** — `db/client_business/014_add_record_id_and_synced_at.sql`
   adds `record_id UUID NULL` to all four tables.
2. **Backfill** — `scripts/backfill_record_id.py backfill --apply` reads
   the business key from each row, computes `record_id` in Python, and
   batches `UPDATE … WHERE record_id IS NULL AND <pk>=%s …`.
3. **Validate** — `scripts/backfill_record_id.py validate` asserts no
   NULLs remain and no duplicates exist.
4. **Lock down** — `scripts/backfill_record_id.py set-not-null --apply`
   issues `ALTER COLUMN record_id SET NOT NULL`. The script refuses
   unless validate is clean.
5. **Add unique index** — `scripts/backfill_record_id.py
   create-unique-index --apply` runs `CREATE UNIQUE INDEX CONCURRENTLY
   uq_<table>_record_id` outside a transaction.

`scripts/backfill_record_id.py all` chains these in order, stopping on
the first failure.

### 3.2 ON CONFLICT target

Until step 5 above completes, the jobs continue to use the existing
**primary-key columns** as the `ON CONFLICT` target — those columns are
exactly the business key and so are uniquely as good as `record_id`. The
jobs already write `record_id` on every INSERT and (when overwriting) every
UPDATE, so the column is fully populated for new rows. Switching the
conflict target to `(record_id)` is a one-line follow-up after the unique
index exists.

---

## 4. synced_at + sync_run_id

Each job run computes a single
`synced_at = datetime.now(timezone.utc)` at the start and passes it to
**every** INSERT/UPDATE in that run. SQL never calls `NOW()` for this
purpose. Effects:

- All rows touched by one run share the same `synced_at`. This makes
  "rows touched by run X" trivially expressible as `WHERE sync_run_id = …`
  or as `WHERE synced_at = …`.
- The bucket-counter UPDATE in `sync_trips_and_speeding` adds
  `AND sync_run_id = %s` so it only touches rows just upserted by the
  current run — which gives the right behavior under both
  `overwrite_existing=true` and `=false`.

`sync_run_id` is the runner's `run_id` (UUID); it is the same for every
row in the run.

### 4.1 updated_at on daily fuel tables

The daily fuel aggregation tables (`client_vehicle_daily_fuel`,
`client_vehicle_driver_daily_fuel`) keep their pre-existing `updated_at`
column as `NOT NULL DEFAULT NOW()`, refreshed on each upsert. `synced_at`
and `sync_run_id` are added **alongside** — neither replaces nor changes
`updated_at`. The two timestamps mean different things:

- `updated_at` — when this physical row was last written (the underlying
  Postgres mutation timestamp).
- `synced_at` — when the run that produced this version of the row started
  (uniform across the run, matches `sync_run_id`).

---

## 5. overwrite_existing

The `client_dataset_schedule.overwrite_existing` boolean flips the
on-conflict semantics for the dataset's job:

| Value   | Behavior                                                    |
|---------|-------------------------------------------------------------|
| `TRUE`  | `INSERT … ON CONFLICT (…) DO UPDATE SET …`                  |
| `FALSE` | `INSERT … ON CONFLICT (…) DO NOTHING`                       |

Both jobs (`sync_trips_and_speeding` and `aggregate_trip_fuel_daily`)
honor this flag. When the per-client row is missing the loader returns a
permissive default (`exists=False`, `enabled=true`,
`overwrite_existing=true`) and emits a one-shot WARNING — this is
backward-compatible with pre-config-era clients but should not be the
steady state.

---

## 6. Retention worker

`jobs/api/telematics/retention_purge.py` is a standard runner job
(`run(client, run_id, params)`). Behavior:

1. Reads enabled rows from `workflow_a_control.client_table_retention`
   joined with `workflow_a_control.table_registry` and
   `workflow_a_control.client_account`. The policy row carries
   denormalized `client_code` beside `client_id` for audit/filtering;
   migration `017_*` backfills it where possible and keeps it consistent
   with `client_account` when a code exists.
2. Cross-checks every `(table_name, schema, retention_key_column)` triple
   against the Python registry (`registry.TABLES`). On mismatch the
   `(client, table)` is logged with ERROR and skipped — never executed.
3. Computes `cutoff_ts` **in Python**, per `(client, table)`, as
   `max(now_utc - retention_days, hard_retention_cutoff(now_utc))` — the
   configured horizon, closed off by the global 13-calendar-month ceiling from
   `ops/retention_registry.py`. A later cutoff deletes MORE, so
   a shorter per-client policy is untouched and a longer one cannot retain past
   the ceiling. Passed to SQL as a single `%s` parameter; SQL never builds
   `NOW() - INTERVAL …`.
4. Connects to the client business DB (admin credentials from the
   platform env), then issues batched DELETEs:

```sql
WITH victims AS (
  SELECT ctid FROM <schema>.<table>
  WHERE <retention_key_column> < %s   -- cutoff_ts
  ORDER BY <retention_key_column>
  LIMIT %s                            -- batch_size
)
DELETE FROM <schema>.<table>
WHERE ctid IN (SELECT ctid FROM victims);
```

   Schema/table/column are interpolated via `psycopg.sql.Identifier`,
   never via Python f-strings. Each batch COMMITs before the next one
   starts.

5. After a successful (non-dry-run) purge, updates the audit columns on
   `client_table_retention`:
   `last_purge_run_at`, `last_purge_cutoff_ts`,
   `last_purge_deleted_count`.

Per-client failures are logged and skipped — they do not abort the rest
of the run.

### 6.1 Default safe

The worker defaults to `dry_run=true` even if `params` is empty. Dry-run
performs a `SELECT COUNT(*)` against the same predicate to estimate
impact, but writes nothing.

### 6.2 Proposed systemd timer

`ops/systemd/proposed/log-job@retention-purge.{service,timer}` schedules
the worker for **Sundays 03:30 UTC**. Params come from a JSON file at
`/etc/log-platform/retention-purge.params.json` (operator-managed,
outside the repo) so toggling `dry_run` does not require editing the unit.
See the README in that folder for the rehearsal-first deployment flow.

---

## 7. Per-client business migration runner

Adding new client-business DDL files used to require manually re-applying
them to every existing client DB. `scripts/apply_client_business_migrations.py`
formalizes this:

- iterates over `workflow_a_control.client_account` (filterable),
- connects to each client DB as the platform admin user,
- ensures `public.schema_migrations` exists in the client DB,
- applies any `db/client_business/*.sql` not yet recorded there,
- records the applied filename + checksum.

Onboarding a brand-new client still uses
`scripts/onboard_workflow_a_client.py`, which applies a curated base DDL set
(`020`, `021`, `012`, `013`, `014`) and records superseded final-schema files
as applied. It does **not** apply `017_v2_staging_tables.sql` directly; that
file is picked up later by `scripts/apply_client_business_migrations.py`
because it is not marked applied by onboarding. The migration runner is the
canonical way to roll out pending `db/client_business/*.sql` files to enabled
client DBs.

---

## 8. Dispatcher (implemented)

The dispatcher lives at `jobs/api/telematics/dispatcher.py` and runs as a
standard runner job. The intended host integration is the
`*:0/5` systemd timer in
`ops/systemd/proposed/log-job@dispatcher.{service,timer}` (every 5
minutes; safe to tighten to 1 minute later without code changes).

### 8.1 Tick algorithm

On every tick the dispatcher:

```text
1. SELECT pg_try_advisory_lock(<dispatcher-lock-key>)
   if false:
       log "another dispatcher process already holds the advisory lock" and exit
2. Mark stale RUNNING rows older than stale_running_timeout_minutes as FAILED.
3. SELECT COUNT(*) FROM client_schedule_run_history WHERE status='RUNNING'
   if > 0:
       log "dispatcher skipped: job already running" and exit
4. Read enabled rows from client_dataset_schedule joined with client_account
   and dataset_registry (FRESH on every tick — no caching). The schedule row
   carries denormalized client_code beside client_id for audit/filtering;
   migration 017 backfills it from client_account where possible and enforces
   consistency when a code exists.
5. For each row, validate (dataset_name, job_module) against the Python
   registry (jobs/api/telematics/registry.py). Mismatches are logged ERROR
   and skipped — the dispatcher never executes a DB-supplied job_module
   that does not match the allowlist.
6. For each registry-valid row, compute the latest scheduled fire time
   <= now in the schedule's local timezone (zoneinfo). Schedules whose
   latest fire is in the future are skipped.
7. Sort due rows by (scheduled_fire_ts ASC, client_code ASC,
   dataset_name ASC).
8. Walk the queue, claiming the first row by INSERTing a RUNNING row in
   client_schedule_run_history with client_id + client_code. The UNIQUE (schedule_id,
   scheduled_fire_ts) constraint rejects re-fires of the same logical
   timestamp.
9. Run exactly one Workflow A job via subprocess to ops/runner.py,
   passing client_id, client_code when available, and window_start_ts /
   window_end_ts as ISO-8601 UTC. The dispatcher sets
   LOG_PLATFORM_RUN_ID_FILE so ops/runner.py writes the platform run_id.
10. UPDATE client_schedule_run_history.platform_run_id when the run id file
   appears.
11. UPDATE the run-history row to SUCCESS or FAILED with finished_at
   and (on FAILED) a truncated stderr+stdout tail in error_summary.
   End the tick.
```

A multi-job environment "queues" naturally across consecutive ticks:
each tick picks at most one due fire, and any other due fires wait their
turn on the next tick.

### 8.2 Schedule evaluation rules

| Frequency | Latest-fire-`<=`-now logic |
|---|---|
| `daily`   | Today's `run_time` if already passed in local; otherwise yesterday's. |
| `weekly`  | Walk back up to 7 days, return the most recent matching `day_of_week` (0=Mon..6=Sun, matches Python `weekday()`) at or before `now`. |
| `monthly` | Walk back up to 12 months. With `day_of_month=N` (1..28) return year/month/N at `run_time`; with `day_of_month_last=true` return the last day of each month at `run_time`. The very-first month is skipped if the day's `<=`-now condition fails. |

`run_time` is a local time in `timezone` (a `zoneinfo` name like
`UTC` or `Europe/Warsaw`). The dispatcher computes the fire time as
`datetime.combine(date, run_time, tzinfo=ZoneInfo(timezone))` then
converts to UTC for storage. DST transitions are handled by `zoneinfo`.

### 8.3 Window calculation

For every claimed fire:

* `scheduled_fire_ts = fire_time` (UTC),
* `window_end_ts = scheduled_fire_ts`,
* `window_start_ts = window_end_ts - timedelta(days=lookback_days)`.

Example: `02:00 Europe/Warsaw` in winter with `lookback_days=1` →
`window_end_ts = 01:00:00Z`, `window_start_ts = previous-day 01:00:00Z`.
The job receives both as ISO-8601 UTC (`...Z`).

### 8.4 Single-job guarantee

The dispatcher uses a session-level Postgres advisory lock
(`pg_try_advisory_lock`) before entering the scheduling loop. If another
dispatcher process holds it, the tick exits cleanly. Two more protections
remain:

1. **Count-RUNNING gate.** Step 3 above bails out if any row is in
   `status='RUNNING'`. With the recommended 5-minute timer and a
   well-behaved job, this prevents new dispatches from starting during
   another tick's subprocess.
2. **Unique fire claim.** `UNIQUE (schedule_id, scheduled_fire_ts)` on
   `client_schedule_run_history` rejects duplicate INSERTs for the same
   logical fire.

### 8.5 Stale RUNNING rows

Before applying the count-RUNNING gate, the dispatcher marks `RUNNING` rows
older than `stale_running_timeout_minutes` as `FAILED`. The default is
`720` minutes and can be overridden by job params or
`WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES`. This timeout is
intentionally much longer than the proposed systemd `TimeoutStartSec=2h` so
normal slow jobs are not failed prematurely. Manual unblock SQL remains in
`docs/07_operations.md` for cases where an operator intentionally wants to
clear a row before the timeout.

### 8.6 Limitations (intentional)

| Behavior | Status |
|---|---|
| Catch-up of multiple historical missed fires | **not supported** — only the most recent fire per schedule is considered. |
| Multiple parallel Workflow A jobs | **not supported** — explicit single-job invariant. |
| Cron expressions (`* * * * *` etc.) | **not supported** — only `daily`, `weekly`, `monthly` (numeric or "last day"). |
| APScheduler / Celery / Redis | **not used** — Python stdlib only (`datetime` + `zoneinfo` + `subprocess`). |
| UI / dashboard for schedule edits | **not in repo** — operator edits via SQL or the onboarding script. A read-only inventory of every fire is `python -m ops.schedule_catalog --database`. |
| Email triggers | **not in repo** — host-side concern. |
| Returning `platform_run_id` to the run-history row | supported through runner `LOG_PLATFORM_RUN_ID_FILE`; remains `NULL` only if the subprocess fails before creating a platform run or the handoff file cannot be read. |

### 8.7 Manual invocation

```bash
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \
  jobs.api.telematics.dispatcher \
  '{}'
```

The dispatcher takes no required params today. Optional param:
`stale_running_timeout_minutes`. Manual invocation is useful for rehearsing
the queue logic without enabling the systemd timer.

---

## 9. Rollout sequence (cheat sheet)

For an operator deploying v1 to an environment that already has clients:

1. **Platform migrations**

   ```bash
   bash ops/db_migrate.sh
   # applies 011, 012, 013, 014, 015, 016, 017 when pending.
   # 012 renames legacy client_schedule -> client_schedule_legacy.
   # 014 reclaims client_schedule_run_history for dispatcher.
   # 015 declared V2 registry rows; 016 removes them until V2 jobs exist.
   # 017 denormalizes client_code into schedule/history/retention rows.
   ```

2. **Client business DDL on existing clients (additive 014)**

   ```bash
   python scripts/apply_client_business_migrations.py            # dry-run
   python scripts/apply_client_business_migrations.py --apply
   ```

3. **Backfill record_id, validate, lock, index**

   ```bash
   # rehearse against one client first
   python scripts/backfill_record_id.py all --client-name DELTA --apply

   # then everyone
   python scripts/backfill_record_id.py all --apply
   ```

4. **Seed schedule + retention rows (existing clients)**

   The new tables are empty for existing clients — until rows exist, the
   jobs use a permissive default and log a WARNING. Either:

   - re-run `scripts/onboard_workflow_a_client.py` against an existing
     client with `--skip-db-create --skip-ddl --skip-grants
     --skip-control-plane --skip-provider-auth-check` so only the
     seeding step runs (the script is idempotent via
     `ON CONFLICT DO NOTHING`), or
   - hand-`INSERT` the rows yourself — they are dataset/table-name
     keyed and constrained by FKs to the registry.

5. **Decide dataset enable + retention enable per client**

   ```sql
   UPDATE workflow_a_control.client_dataset_schedule
      SET enabled=true, run_time='03:30', lookback_days=2
    WHERE client_id='<uuid>' AND dataset_name='trips_sync';

   UPDATE workflow_a_control.client_table_retention
      SET enabled=true, retention_days=180
    WHERE client_id='<uuid>' AND table_name='client_speeding_notifications';
   ```

6. **Stage the retention timer**

   See `ops/systemd/proposed/log-job@retention-purge.README.md`. Always
   rehearse with `dry_run=true` before flipping the timer on.

---

## 10. References

- Plan: `/home/logplatform/.cursor/plans/workflow_a_central_config_plan_2dbb39f5.plan.md`
- Registry: `jobs/api/telematics/registry.py`
- record_id helpers: `jobs/api/telematics/record_id.py`
- Backfill: `scripts/backfill_record_id.py`
- Per-client DDL runner: `scripts/apply_client_business_migrations.py`
- Retention worker: `jobs/api/telematics/retention_purge.py`
- Dispatcher: `jobs/api/telematics/dispatcher.py`
- Proposed systemd: `ops/systemd/proposed/log-job@retention-purge.*`,
  `ops/systemd/proposed/log-job@dispatcher.*`
- Platform migrations: `db/migrations/011_*.sql` … `017_*.sql`
- Client-business DDL: `db/client_business/014_add_record_id_and_synced_at.sql`, `db/client_business/017_v2_staging_tables.sql`
