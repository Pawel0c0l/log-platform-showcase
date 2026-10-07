# Proposed systemd timer: jobs.api.telematics.dispatcher

Lightweight DB-driven dispatcher for Workflow A. On every tick it:

1. Tries a global Postgres advisory lock. If another dispatcher process is
   active, the tick exits cleanly.
2. Marks stale `RUNNING` history rows older than
   `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES` (default 720 minutes)
   as `FAILED`, then counts remaining `RUNNING` rows. If any remain, the tick
   is a no-op (strict queue: max one Workflow A job at any time).
3. Reads enabled rows from `workflow_a_control.client_dataset_schedule`,
   joined with `client_account` and `dataset_registry`, fresh each tick
   (no caching — config changes apply on the very next tick).
4. Validates each row's `dataset_name` + `job_module` against the Python
   allowlist `jobs/api/telematics/registry.py`. Rows the registry does not
   know are skipped with an ERROR log.
5. Computes the latest scheduled fire time `<= now` per row in the
   schedule's local timezone (Python stdlib `zoneinfo` only).
6. Sorts due schedules by `(scheduled_fire_ts ASC, client_code ASC,
   dataset_name ASC)` and claims **one** by inserting a `RUNNING` row.
7. Runs the matching Workflow A job via `python ops/runner.py
   <job_module> '{...}'` (subprocess, synchronous), then updates the
   row to `SUCCESS` or `FAILED`. The dispatcher also sets
   `LOG_PLATFORM_RUN_ID_FILE` for the subprocess and writes the resulting
   platform run id to `client_schedule_run_history.platform_run_id`.

The unit is **not enabled by default**. Treat the proposed `.service` /
`.timer` files as a template the operator copies to `/etc/systemd/system/`.

## Files

* `log-job@dispatcher.service` — non-templated oneshot service that
  invokes `python3 ops/runner.py jobs.api.telematics.dispatcher '{}'`.
* `log-job@dispatcher.timer` — fires every 5 minutes (`*:0/5`).

## Deployment

```bash
# 1. Confirm migrations are applied (014 is the dispatcher one).
cd /opt/log-platform
bash ops/db_migrate.sh

# 2. Stage the unit files
sudo cp ops/systemd/proposed/log-job@dispatcher.service \
        /etc/systemd/system/log-job@dispatcher.service
sudo cp ops/systemd/proposed/log-job@dispatcher.timer \
        /etc/systemd/system/log-job@dispatcher.timer
sudo systemctl daemon-reload

# 3. Rehearse with a single manual tick BEFORE enabling the timer
sudo systemctl start log-job@dispatcher.service
journalctl -u log-job@dispatcher.service -n 200 --no-pager

# 4. Inspect the platform DB to confirm the run-history row landed
psql -d logdb -c "
  SELECT scheduled_fire_ts, dataset_name, status, started_at, finished_at
    FROM workflow_a_control.client_schedule_run_history
   ORDER BY scheduled_fire_ts DESC
   LIMIT 20;"

# 5. Enable the timer
sudo systemctl enable --now log-job@dispatcher.timer
systemctl list-timers --all | grep dispatcher
```

## Tightening the tick interval

The default `*:0/5` is conservative for a laptop. To switch to once-per-minute:

```bash
sudo sed -i 's|OnCalendar=\*:0/5|OnCalendar=\*:0/1|' \
         /etc/systemd/system/log-job@dispatcher.timer
sudo systemctl daemon-reload
sudo systemctl restart log-job@dispatcher.timer
```

No code change is required — the dispatcher is stateless across ticks.

## Operational notes

* **Global dispatcher lock**: every tick first tries a Postgres advisory lock.
  If another dispatcher process is active, this tick exits cleanly before
  reading schedules.
* **Single-job execution**: after auto-failing stale rows, the dispatcher has a
  count-RUNNING guard and the `UNIQUE (schedule_id, scheduled_fire_ts)`
  constraint rejects duplicate claims of the same fire.
* **Live config**: editing `client_dataset_schedule` (or any joined table)
  takes effect on the next tick. There is no in-process cache.
* **No catch-up**: only the latest fire `<= now` per schedule is ever
  considered. If the host was off for two days under a daily schedule,
  exactly one fire (yesterday's) is run, not both missed days.
* **Allowlist**: `jobs/api/telematics/registry.py` is the source of truth
  for valid `(dataset_name, job_module)` pairs. Any divergence between
  the Python registry and `dataset_registry` aborts that schedule with
  an ERROR log; no arbitrary `job_module` from the DB is ever executed.
* **Stuck RUNNING rows**: if the dispatcher process is killed mid-job
  (OOM, SIGKILL), a `RUNNING` row remains temporarily. A later tick marks it
  `FAILED` once it is older than `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES`
  (default 720). Operator unblock before the timeout:

  ```sql
  UPDATE workflow_a_control.client_schedule_run_history
     SET status='FAILED',
         finished_at=now(),
         error_summary='manual unblock: dispatcher killed'
   WHERE status='RUNNING';
  ```

## Related

* Worker: `jobs/api/telematics/dispatcher.py`
* Schedule data model: `db/migrations/012_workflow_a_client_dataset_schedule.sql`
* Dispatcher data-model bring-up: `db/migrations/014_workflow_a_dispatcher_v1.sql`
* Design doc (with limitations): `docs/10_scheduler_design.md`
* Operator runbook: `docs/07_operations.md` § 5.5
