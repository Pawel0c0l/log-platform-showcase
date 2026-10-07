# Proposed systemd timer: jobs.api.telematics.retention_purge

Weekly retention purge for Workflow A — deletes rows from each client business
table whose registered retention key column is older than `now() - retention_days`.

Reads policies from `workflow_a_control.client_table_retention` joined with
`workflow_a_control.table_registry`. The cutoff is computed in Python (UTC).
Identifiers are validated against the `jobs.api.telematics.registry` allowlist
and interpolated via `psycopg.sql.Identifier`.

The unit is **not enabled by default**. Treat the proposed `.service` /
`.timer` files as a template the operator copies to `/etc/systemd/system/`.

## Files

* `log-job@retention-purge.service` — non-templated oneshot service that
  invokes `python3 ops/runner.py jobs.api.telematics.retention_purge <params_file>`.
* `log-job@retention-purge.timer` — fires weekly (Sun 03:30 UTC).
* `/etc/log-platform/retention-purge.params.json` — JSON params consumed by
  the runner. Operator-managed; not in the repo.

## Deployment

```bash
# 1. Create a parameters file (start with a dry-run!)
sudo tee /etc/log-platform/retention-purge.params.json >/dev/null <<'JSON'
{"dry_run": true, "batch_size": 5000}
JSON

# 2. Stage the unit files
sudo cp ops/systemd/proposed/log-job@retention-purge.service \
        /etc/systemd/system/log-job@retention-purge.service
sudo cp ops/systemd/proposed/log-job@retention-purge.timer \
        /etc/systemd/system/log-job@retention-purge.timer
sudo systemctl daemon-reload

# 3. Rehearse in dry-run BEFORE enabling the timer
sudo systemctl start log-job@retention-purge.service
journalctl -u log-job@retention-purge.service -n 200 --no-pager

# 4. Inspect the platform DB to confirm policies and impact
psql -d logdb -c "SELECT client_id, table_name, enabled, retention_days,
                          last_purge_run_at, last_purge_deleted_count
                  FROM workflow_a_control.client_table_retention
                  ORDER BY client_id, table_name;"

# 5. Once satisfied, flip dry_run=false and enable the timer
sudo sed -i 's/"dry_run": true/"dry_run": false/' \
         /etc/log-platform/retention-purge.params.json
sudo systemctl enable --now log-job@retention-purge.timer
systemctl list-timers --all | grep retention-purge
```

## Operational notes

* **Default safe**: the worker defaults to `dry_run=true` even if the params
  file is missing; explicit `false` is required to delete.
* **Batched COMMITs**: each `batch_size` rows are committed before the next
  batch starts. Interrupting the job (e.g. by `systemctl stop`) leaves
  already-committed batches in place — re-running resumes naturally.
* **Allowlist**: `jobs/api/telematics/registry.py` is the source of truth for
  `(schema, table, retention_key_column)`. Any divergence between the Python
  registry and `workflow_a_control.table_registry` aborts that (client, table)
  with an ERROR log, the rest of the run continues.
* **Audit**: each successful (non-dry-run) purge updates
  `workflow_a_control.client_table_retention.last_purge_run_at`,
  `last_purge_cutoff_ts`, `last_purge_deleted_count`.

## Related

* Plan: `/home/logplatform/.cursor/plans/workflow_a_central_config_plan_2dbb39f5.plan.md`
* Worker: `jobs/api/telematics/retention_purge.py`
* Policy data model: `db/migrations/013_workflow_a_client_table_retention.sql`
