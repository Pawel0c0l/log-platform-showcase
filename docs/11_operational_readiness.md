# Operational Readiness — Single Laptop

Scope: one Linux laptop, local Docker Compose stack, host `systemd` timers, no
new scheduler or external services.

## Verdict

The repository is **usable for continuous single-laptop operation after manual
bootstrap**, but it is **not turnkey**. The implemented runtime paths are enough
to run the API, dispatch Workflow A jobs, run Workflow A retention, run backup,
and inspect logs. Remaining blockers are operational, not architectural:

- operator must create host env files under `/etc/log-platform/`,
- operator must copy/enable the proposed systemd units,
- there is no restore script; restore is documented command-by-command only,
- Workflow A client business databases and Workflow B report files need explicit
  backup coverage outside `ops/backup.sh`,
- no generic `log-job@.service` is versioned for the Workflow B sample timer.

## Bootstrap Checklist

1. Install host packages:

   ```bash
   sudo apt-get update
   sudo apt-get install -y docker.io python3.12 python3.12-venv git curl
   sudo systemctl enable --now docker
   sudo usermod -aG docker logplatform
   ```

   Log out and back in after changing Docker group membership.

2. Prepare repo and Python environment:

   ```bash
   cd /opt/log-platform
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-host.txt
   cp .env.example .env
   chmod 600 .env
   ```

   `requirements-host.txt` is the **complete** native-host contract and is the
   only file this step needs: it `-r`-includes `api/requirements.txt`, so the
   same virtualenv satisfies the systemd unit's `.venv/bin/uvicorn api.main:app`
   as well as the host jobs and `ops/` tools. The API's packages are pinned once,
   in `api/requirements.txt`; do not restate them here and do not install
   anything into `.venv` by hand — a package that reaches the runtime only by
   manual installation is not a deployable contract, and
   `ops/tests_manual/test_host_dependency_contract.py` fails when the API
   acquires an unconditional import this file does not cover.

3. Fill `.env` with platform values:

   - `POSTGRES_*`
   - `MINIO_*`
   - `API_READ_TOKEN`
   - `API_WRITE_TOKEN`
   - `LOG_API_URL=http://127.0.0.1:8000`
   - Workflow A client secret env vars or `file:` secret paths

4. Start platform containers:

   ```bash
   docker compose -f docker-compose.yml up -d
   ./ops/smoke.sh
   bash ops/db_migrate.sh
   ```

   The base file is the production definition: the API runs code baked into
   `log-platform-api:latest` as `1000:1000`, with no capabilities and no source
   bind. Append `-f docker-compose.dev.yml` only for a development stack that
   should follow the working tree.

5. Create host env files for systemd:

   ```bash
   sudo install -d -m 0700 /etc/log-platform
   sudo install -m 0600 .env /etc/log-platform/runtime.env
   sudo install -m 0600 .env /etc/log-platform/backup.env
   sudo tee /etc/log-platform/retention-purge.params.json >/dev/null <<'JSON'
   {"dry_run": true, "batch_size": 5000}
   JSON
   sudo chmod 0600 /etc/log-platform/retention-purge.params.json
   ```

6. Onboard Workflow A clients as needed:

   ```bash
   PYTHONPATH="$PWD" .venv/bin/python scripts/onboard_workflow_a_client.py \
     --config scripts/<client>.yaml --apply
   ```

7. Copy and enable host units:

   ```bash
   sudo cp ops/systemd/log-backup.service /etc/systemd/system/log-backup.service
   sudo cp ops/systemd/log-backup.timer /etc/systemd/system/log-backup.timer
   sudo cp ops/systemd/proposed/log-job@dispatcher.service /etc/systemd/system/log-job@dispatcher.service
   sudo cp ops/systemd/proposed/log-job@dispatcher.timer /etc/systemd/system/log-job@dispatcher.timer
   sudo cp ops/systemd/proposed/log-job@retention-purge.service /etc/systemd/system/log-job@retention-purge.service
   sudo cp ops/systemd/proposed/log-job@retention-purge.timer /etc/systemd/system/log-job@retention-purge.timer
   sudo systemctl daemon-reload
   sudo systemctl enable --now log-backup.timer
   sudo systemctl enable --now log-job@dispatcher.timer
   sudo systemctl enable --now log-job@retention-purge.timer
   ```

8. Keep Docker enabled. The Compose services use `restart: unless-stopped`, so
   already-created containers restart after reboot when the Docker daemon starts.

## Runtime Model

Continuously running processes:

- Docker daemon.
- Compose containers:
  - `postgres`
  - `minio`
  - `api`

Since **2026-08-09** the `api` container runs its application code from the image
only — no source bind and no host mount — as `1000:1000`, with all capabilities
dropped and `no-new-privileges`, still published on `127.0.0.1:8000`. Its identity
is the immutable image ID
`sha256:c718529f89ad2a822f5f014de6f8cdafa6db2f5227637c8fa4176757f7a6df46`, not the
mutable `log-platform-api:latest` tag. Editing `api/` therefore changes nothing
until the image is rebuilt and the service recreated; the live source bind is
development-only and opt-in through `docker-compose.dev.yml`. Rollback material —
the previous image **and** the previous bind-based definition, both required — is
deliberately retained pending operator acceptance. See
`docs/17_production_hardening_roadmap.md` § 5.6.

Timer-triggered one-shot jobs:

Verified against the production host on 2026-08-09:

- `log-job@dispatcher.timer` -> `jobs.api.telematics.dispatcher`, every 5 minutes.
- `log-workflow-b.timer` -> `jobs.reports.workflow_b.orchestrator`, 06:00 and
  20:00 Europe/Warsaw. **Installed and enabled**, not a proposal.
- `log-job@retention-purge.timer` -> `jobs.api.telematics.retention_purge`, Sunday
  03:30 UTC. Note the installed wrapper hard-codes `{"dry_run": true}`, so this
  timer currently deletes nothing.
- `log-platform-prune.timer` -> `ops/platform_prune.sh --execute --days 60`,
  daily 03:30 host local time.
- `log-backup.timer` -> `ops/backup.sh`, daily 03:00 in host local time.
- `database-export-cleanup.timer` -> Database Explorer export expiry, hourly.

Enabled but **not running** as of 2026-08-09 (both need a decision — see
`docs/17_production_hardening_roadmap.md` §5.3): `log-platform-api.service`
(host portal on port 8001) and `database-export-worker.service`.

Versioned but not production-ready by itself:

- `ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer` needs a host
  `log-job@.service` template that is not included in this repo. It is
  deliberately not enabled: Workflow B mail ingest runs as Stage 1 inside the
  orchestrator.
- `ops/systemd/log-platform-prune.{service,timer}` are versioned in the repo; the
  installed units differ only by supplying the environment-identity file through
  a drop-in.

P0 operational safety foundation — see `docs/17_production_hardening_roadmap.md`.
**Installed and enabled on 2026-08-09:**

- `suspected-bug-email-worker.{service,timer}` — alert email delivery, every 5
  minutes.
- `execution-watchdog.{service,timer}` — missing-run and stuck-run watchdog,
  every 15 minutes.
- `disk-space-monitor.{service,timer}` — filesystem headroom, hourly.
- `log-platform-unit-failure@.service` — `OnFailure=` target, plus
  `95-onfailure.conf` drop-ins for the dispatcher, Workflow B, backup and prune
  units.

- `backup-retention.{service,timer}` — backup retention, daily 04:15,
  `Persistent=false`. Installed after the identity-bootstrap fix (roadmap §5.1a);
  first destructive run performed manually under separate authorization (§5.4b).

### Deployment sequence for the P0 operational safety units

**Executed in full on 2026-08-09** under explicit authorization. Migration `059`
is applied; the mail worker, failure routing, disk monitor, execution watchdog and
backup retention are installed, enabled and verified in production — see
`docs/17_production_hardening_roadmap.md` §5.4a and §5.4b for the measured state.

Step 7's dry-run exposed the identity-bootstrap defect recorded in §5.1a; it was
fixed and independently re-reviewed before the corrected unit was installed. The
first destructive retention run removed 17 expired sets (96.6 GB), every protected
survivor verified, and `backup-retention.timer` is enabled with its next fire at
2026-08-10 04:15 CEST. The step 7(b) independent recovery copy was **explicitly
waived by the operator** for that one run — see §5.4b; it is not a precedent.

**Activation semantics — read before running any `systemctl` command.**

* Enable **timers**, never the services: `systemctl enable <name>.timer`. The
  timer-driven services carry no `[Install]` section on purpose, so
  `systemctl enable backup-retention.service` fails loudly rather than silently
  arranging for destructive retention to run at every boot.
* Use `enable`, **not `enable --now`**, for `backup-retention.timer`. The timers
  no longer carry `Requires=<their own service>`, so starting a timer no longer
  starts its job — but `--now` remains a needless risk on a destructive unit.
  `enable --now` is fine for the watchdog, disk monitor and mail worker.
* `Persistent=` — `true` on the watchdog, disk monitor and mail worker, so a
  fire missed while the laptop was asleep is caught up. **`false` on
  `backup-retention.timer`**: replaying a missed calendar event would run
  destructive retention immediately and unattended after a boot. A skipped
  retention window costs disk space; an unexpected one costs backups.
* The first destructive retention run is therefore always a deliberate manual
  act (step 5), never a side effect of installation.
* **Identity bootstrap — retention only.** `ops.backup_retention` and
  `ops.verify_backup_set` are the only two P0 modules that attest the platform
  identity, and `load_runtime_identity()` requires
  `LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID` plus the four expected PostgreSQL
  values. Those live in the repository `.env`;
  `/etc/log-platform/environment-identity.env` carries only
  `LOG_PLATFORM_TARGET_ENVIRONMENT`, and `/etc/log-platform/runtime.env` carries
  no `LOG_PLATFORM_*` key at all. Both modules must therefore run behind
  `ops/run_with_environment_identity.py`, exactly as every host job already does
  via `/usr/local/bin/log-job-runner.sh`. Invoked directly they exit 1 with
  `EXPECTED_PLATFORM_IDENTITY_MISSING` — fail-closed, so nothing is deleted, but
  retention never runs either. The watchdog, disk monitor, mail worker and
  failure adapter do not attest identity and need no wrapper.
* **Which source tree each unit executes.** Two launchers pin code to the active
  release; everything else runs the Workflow A development checkout by absolute
  path.

  | Unit | Executes | Launcher |
  |---|---|---|
  | `log-job@dispatcher.service`, `log-workflow-b.service`, `log-job@retention-purge.service` | active release | `/usr/local/bin/log-job-runner.sh` |
  | `suspected-bug-email-worker.service`, `execution-watchdog.service` | active release | `/usr/local/bin/log-ops-runner.sh` |
  | `log-platform-unit-failure@.service` | development checkout, **by decision** | none |
  | `disk-space-monitor`, `backup-retention`, `log-backup`, `log-platform-prune` | development checkout | none |

  The watchdog matters most here: it reads `ops/watchdog_expectations.json` from
  its own `REPO_ROOT`, so whichever tree supplies the module also supplies the
  subjects it asserts. Since `log-ops-runner.sh` that is the active release, and
  activating a release is sufficient to change the expectation set — no
  development worktree is touched. The failure handler is host-bound on purpose:
  a broken `current` fails every release-bound unit at once, and a release-bound
  last-resort reporter could not start to report it. See
  `docs/17_production_hardening_roadmap.md` §5.8.

  `log-ops-runner.sh` must be installed **before** the two sidecar units are
  repointed at it, and both must be in place before a release is expected to
  deliver worker or watchdog behaviour.

  **Install from a prepared release directory, never from a worktree and never
  through `current`.** A prepared release is verified byte-for-byte against its
  commit; a worktree is mutable and routinely dirty. `current` is wrong for a
  different reason: the launcher and the rewritten units are new files, so the
  release `current` names before the activation does not contain them. Address
  the prepared tree by its own release id, which is what keeps the installation
  source and the pointer independent:

  ```bash
  PREPARED_RELEASE_DIR=/opt/log-platform-release/releases/<release_id>

  sudo install -m 0755 -o root -g root -D \
    "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/log-ops-runner.sh" \
    /usr/local/bin/log-ops-runner.sh
  ```

  Installing while `current` still names the previous release is intentional and
  safe: the rewritten sidecars resolve `current` at each invocation, so until the
  pointer moves they keep running the previous release's code — now from an
  immutable tree rather than a checkout. Each fire logs the release it resolved
  (`log-ops-runner release=…`), which is the check that the launcher landed
  correctly before anything depends on it.

* Validate the units on the host before installing — this could not be run in
  the development sandbox, which denies `systemd-analyze` a working directory:

  ```bash
  systemd-analyze verify ops/systemd/proposed/*.timer \
                         ops/systemd/proposed/*.service
  ```

  `systemd-analyze` reports `Command /usr/local/bin/log-ops-runner.sh is not
  executable` until the launcher is installed. That diagnostic is the install
  order asserting itself, not a defect in the unit.

Order matters — the alert path must be able to deliver before anything is wired
to depend on it.

1. **Apply the migration, then let the dispatcher prove it works.**
   `db/migrations/059_operational_watchdog_state.sql` creates
   `ops_control.scheduler_heartbeat` and `ops_control.watchdog_observation`. Both
   are additive and re-runnable; the `ops_control` schema already exists. Until it
   is applied the dispatcher heartbeat silently no-ops by design. Applying the
   migration is necessary but **not sufficient** — the deployed dispatcher must
   also be the heartbeat-capable code and must have actually ticked. Step 6 will
   not proceed without a fresh heartbeat row.
2. **Configure the recipient and confirm the transport.** Set
   `SUSPECTED_BUG_ALERT_TO` in `/etc/log-platform/runtime.env` and make sure
   `AUTOMATION_SMTP_HOST` is present, then confirm:

   ```bash
   .venv/bin/python -m ops.operational_alert    # must exit 0
   ```

   This is the single highest-value step. Without a recipient every incident is
   suppressed as `recipients_not_configured`; without `AUTOMATION_SMTP_HOST`
   every alert dead-letters. In both cases the alert path looks healthy while
   delivering nothing, so the exit code — not the absence of errors — is the
   check. `SUSPECTED_BUG_ALERT_FROM` is an optional override and its absence is
   not a problem.

   **Setting the variable in that file is necessary but not sufficient.**
   `/etc/log-platform/runtime.env` is `root:root 0600`, so a job process cannot
   read it: the value reaches a process only because systemd reads the file and
   injects it *before dropping privileges*. A unit with no `EnvironmentFile=`
   therefore resolves zero recipients however the file is configured, and the
   readiness command above will not reveal it, because a shell that sources the
   repository `.env` is not the environment the unit produces.

   Every unit that can raise an incident must source it. That is
   `log-workflow-b.service`, `log-job@dispatcher.service` and
   `log-job@retention-purge.service`, alongside the delivery units. The contract
   is asserted by `ops/tests_manual/test_workflow_b_alert_hardening.py`
   (`test_every_incident_raising_job_unit_sources_the_same_configuration`), which
   builds the effective environment from the shipped unit text rather than from
   `os.environ`. On a live host:

   ```bash
   systemctl show -p EnvironmentFiles log-workflow-b.service
   ```

   When this is wrong the symptom is precise: incidents are persisted with
   `email_decision='suppressed'` and
   `email_decision_reason='recipients_not_configured'` in
   `suspected_bug_occurrences`, with no matching `suspected_bug_email_outbox`
   row. That condition is now also logged as an `operational_error` named
   `operational_alert_delivery_not_configured` at the moment it occurs, so it no
   longer requires a database query to notice.
3. **Install the mail worker first** — `suspected-bug-email-worker.{service,timer}` —
   and confirm a real incident produces a real email before trusting anything else.
4. **Install the failure handler and its drop-ins** —
   `log-platform-unit-failure@.service`, then the `95-onfailure.conf` drop-ins for
   `log-job@dispatcher`, `log-workflow-b`, `log-backup` and `log-platform-prune`.
   `OnFailure=` is a `[Unit]` directive; under `[Service]` systemd ignores it with
   only an "Unknown key name" warning and the routing is silently inert.
5. **Disk monitor** — `disk-space-monitor.{service,timer}`. Run
   `.venv/bin/python -m ops.disk_space_monitor --dry-run` once and inspect the
   JSON, then `systemctl enable --now disk-space-monitor.timer`.

6. **Execution watchdog — only after a FRESH heartbeat exists.**
   The dispatcher stamps `ops_control.scheduler_heartbeat` on every tick, but only
   once migration `059` is applied *and* the heartbeat-capable dispatcher code is
   the code actually running. Enabling the watchdog before that guarantees a
   `SCHEDULER_HEARTBEAT_LOST` incident for a perfectly healthy dispatcher — a
   false alarm on the very first scan, which is the worst possible introduction to
   a new alerting system.

   ```bash
   # a. Confirm the deployed dispatcher writes heartbeats (step 1 applied 059).
   #    Wait for at least one 5-minute dispatcher tick, then:
   psql -Atc "SELECT component, last_beat_at, beat_count
                FROM ops_control.scheduler_heartbeat
               WHERE component = 'workflow_a.dispatcher'"

   # b. REQUIRED: last_beat_at must be within the last 30 minutes (the heartbeat
   #    grace). An empty result or a stale row means step (a) is not done.
   ```

   Only once that row is fresh:

   ```bash
   .venv/bin/python -m ops.execution_watchdog --dry-run    # inspect verdicts
   systemctl enable --now execution-watchdog.timer
   ```

   A dry run persists nothing, so it does not establish the eligibility epochs
   either. The first *real* scan records them; schedules whose most recent fire
   predates that scan report `NOT_YET_EXPECTED` once and are monitored normally
   from their next fire.

7. **Backup retention — the first destructive run is a separate, authorized act.**
   Install the units but **do not enable the timer yet**.

   ```bash
   # a. Dry run under the authoritative contract (dry run is the default).
   #    Retention and the verifier attest the platform identity, so both must run
   #    behind ops/run_with_environment_identity.py — see "Identity bootstrap"
   #    above. Invoked directly they exit 1 with EXPECTED_PLATFORM_IDENTITY_MISSING.
   .venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.backup_retention | tee /tmp/retention-plan.json
   ```

   Read, in this order:
   * `authoritative_identity` — must be this platform's own environment/database/
     platform UUID, attested from `ops_control.environment_identity`;
   * `verified_anchor_count` — below `keep_minimum` means the run fails closed and
     deletes nothing. Recent backups not verifying is the problem to solve first;
   * `plan.protected_floor` — the anchors that will survive;
   * `plan.rejected_candidates` — why anything was refused;
   * `plan.delete` — the exact destructive plan.

   ```bash
   # b. REQUIRED: preserve an independent recovery backup OUTSIDE the deletion
   #    candidate scope, on a different filesystem, and verify it there.
   #    Retention never touches paths outside its backup root.
   cp backups/{postgres,minio}_<newest>.* backups/backup_<newest>.manifest.json \
      /path/on/another/filesystem/
   ```

   Verify that copy independently before continuing. It is the thing that makes
   the first destructive run reversible.

   **c. Obtain explicit authorization for the destructive run.** It deletes real
   backups and is not covered by the authorization to deploy.

   ```bash
   # d. One destructive execution, by hand.
   .venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.backup_retention --execute | tee /tmp/retention-run.json
   ```

   ```bash
   # e. REQUIRED post-delete verification of EVERY protected survivor, under the
   #    full contract — exact members, attested identity pins, checksums, archive
   #    traversal. The run already does this internally and reports it; verify
   #    externally too, because this is the moment the safety floor is either real
   #    or not.
   #
   #    One command, not a loop: ops.verify_backup_set attests this platform's
   #    identity itself and exits non-zero if ANY survivor fails, naming the ones
   #    that did.
   #
   #    The whole stage runs in a SUBSHELL with `set -euo pipefail`, and the
   #    subshell is the last thing in this block, so the block's exit status IS
   #    the verifier's. That matters: an earlier version ended with `echo`, `jq`
   #    and `df`, each of which succeeded and overwrote the verifier's failure —
   #    the operator saw exit 0 after a genuinely failed verification. A subshell
   #    (not a bare `set -e`) also means a pasted failure cannot close the
   #    operator's interactive session.
   (
     set -euo pipefail
     trap 'echo "SURVIVOR VERIFICATION FAILED — STOP. Do not enable backup-retention.timer. The recovery backup preserved in step (b) is now the operative copy." >&2' ERR

     mapfile -t SURVIVORS < <(jq -r '.protected_survivors[]' /tmp/retention-run.json)
     # An empty survivor list is a failure, not a vacuous success.
     [ "${#SURVIVORS[@]}" -gt 0 ]

     .venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.verify_backup_set "${SURVIVORS[@]}"

     # Everything below is reachable only when EVERY survivor verified.
     jq '{ok, operator_action_required, protected_survivors,
          post_delete_verification, freed_bytes: .mutations.freed_bytes}' \
        /tmp/retention-run.json
     df -h /
   )
   ```

   **Any non-zero exit from that block stops the deployment here and requires
   operator intervention. Do not continue to step (f).** Exit 1 means a protected
   survivor did not verify — the safety floor is not what the run claimed, and the
   independently preserved recovery backup from step (b) is now the operative
   copy. Exit 2 means the target identity could not be attested at all, which is a
   refusal rather than a warning. The block prints the `jq` summary and `df` only
   on success, so seeing them is itself the signal that verification passed;
   their absence means it did not.

   `ok` must be true, `operator_action_required` false, every
   `post_delete_verification` entry `verified: true`, and free space must have
   increased by the reclaimed amount.

   ```bash
   # f. Only now enable automation. `enable`, not `enable --now`.
   systemctl enable backup-retention.timer
   systemctl list-timers backup-retention.timer   # NEXT must be a future 04:15
   ```

   Before starting any timer, confirm: `NEXT` is a future intended calendar event;
   `Persistent=false` for retention; the timer has no activating dependency on its
   service; and starting the timer does not activate the service.

As of 2026-08-09 a dry run over the live 242 GB `backups/` directory planned
removal of 17 expired sets (~90 GB), keeping 29, with the newest three verifying
authentically in 48 seconds against manifest checksums
(`environment=production`, `database=logdb`). Re-inspect on the host; the numbers
will have moved.

## Health and Monitoring

> **Deployment state (2026-08-09).** The automated monitoring described in
> `docs/17_production_hardening_roadmap.md` is now **installed and running in
> production**: alert worker, systemd failure routing, disk-space monitor and
> execution watchdog. A controlled test alert was delivered to the operator
> mailbox, so `suspected_bug_email_outbox` is no longer empty. **Backup retention
> is the exception** — implemented, not deployed (§5.1a). The manual commands
> below remain valid and are still the way to inspect state by hand.

Alerting readiness (exit 0 = alerts can actually be delivered):

```bash
.venv/bin/python -m ops.operational_alert
.venv/bin/python ops/inspect_suspected_bugs.py --open
.venv/bin/python ops/inspect_suspected_bugs.py --outbox dead_letter --outbox retry
```

Watchdog, disk and retention, all read-only in these forms:

```bash
.venv/bin/python -m ops.execution_watchdog --dry-run
.venv/bin/python -m ops.disk_space_monitor --dry-run
# Retention attests the platform identity, so it needs the identity bootstrap:
.venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.backup_retention
```

API:

```bash
curl -fsS http://127.0.0.1:8000/health
docker compose ps
docker compose logs --tail=200 api
```

Timers and recent host logs:

```bash
systemctl list-timers --all | grep -E 'log-job|log-backup|log-platform'
journalctl -u log-job@dispatcher.service -n 200 --no-pager
journalctl -u log-job@retention-purge.service -n 200 --no-pager
journalctl -u log-backup.service -n 200 --no-pager
```

Platform run visibility:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT run_id, source, status, started_at, ended_at FROM runs ORDER BY started_at DESC LIMIT 20;"

docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT level, source, message, ts FROM logs ORDER BY ts DESC LIMIT 50;"
```

Dispatcher schedule visibility:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT dataset_name, scheduled_fire_ts, status, platform_run_id, error_summary FROM workflow_a_control.client_schedule_run_history ORDER BY scheduled_fire_ts DESC LIMIT 20;"
```

Ops helpers:

- `./ops/smoke.sh` checks `/health`.
- `./ops/diag.sh` prints Compose state, API logs, timer status, and recent
  prune/backup/dispatcher/retention journal entries.
- `./ops/snapshot.sh` writes a redacted local diagnostic snapshot under
  `snapshots/`.

Logs live in:

- Docker logs for API/container processes.
- systemd journal for host timers and one-shot jobs.
- platform DB `logs` table for structured job logs.

There is no repo-managed logrotate policy. Docker and journald retention are
host configuration.

## Backup and Recovery

`ops/backup.sh` currently backs up:

- platform Postgres database to `backups/postgres_YYYYmmdd_HHMMSS.sql.gz`,
- MinIO data to `backups/minio_YYYYmmdd_HHMMSS.tar.gz`.

For named Docker-volume MinIO storage, the script copies `/data` from the
running MinIO container and archives it locally. The stack must be running.

Optional backup retention:

```bash
BACKUP_RETENTION_DAYS=14 ./ops/backup.sh
```

Not covered automatically:

- separate Workflow A client business databases if they are not the same
  database dumped by `ops/backup.sh`,
- `/etc/log-platform/*.env` secret files,
- Workflow B raw/normalized report files under `REPORTS_DATA_DIR`,
- Stage 2 cleaned files under `/tmp/log-platform-stage2/cleaned`,
- repository working tree state.

Restore support is manual only. See `docs/09_disaster_recovery.md` for the
current command sequence.

## Secrets and Permissions

- `.env` and `/etc/log-platform/*.env` must be `0600`.
- systemd units use `EnvironmentFile`, not secret command-line arguments.
- Workflow A client secrets should be env-var references or `file:` references
  in `workflow_a_control.client_account`.
- Do not put secret values in runner params; `runs.params` is stored in DB.
- MinIO ports bind to all interfaces in `docker-compose.yml`; restrict laptop
  firewall/VPN exposure if the machine is on untrusted networks.

## Laptop Caveats

Reboot:

- Docker containers restart if Docker is enabled and containers were created.
- systemd timers with `Persistent=true` run missed one-shot jobs after boot.

Sleep/hibernate:

- Timers do not fire while suspended.
- `Persistent=true` catches timer events after wake, but dispatcher semantics
  still run only the latest due fire per schedule, not every missed fire.
- A process suspended mid-HTTP request may resume late or fail by timeout.

Network loss:

- API and local DB continue if Docker is running.
- External provider jobs may fail; failures are visible in platform runs/logs.

GUI browser / Selenium:

- Current production jobs do not use Selenium, Chrome, Chromedriver, or a GUI
  browser. No repo systemd unit is prepared for GUI browser automation.
- If a future job needs Selenium from systemd, it must define display/headless
  behavior, browser dependencies, process timeouts, and cleanup separately.

Logged-out laptop user:

- System-level units run as `User=logplatform`; they do not depend
  on an interactive login session, as long as the machine is powered on and not
  suspended.

## Blockers Before Calling This Fully Ready

1. Create and protect `/etc/log-platform/runtime.env`,
   `/etc/log-platform/backup.env`, and retention params on the host.
2. Copy and enable the dispatcher, retention, and backup timers.
3. Decide whether platform prune should be enabled and provide a host
   `log-platform-prune.*` unit, or explicitly leave platform run/log/artifact
   pruning manual.
4. Add backup coverage for Workflow A client business DBs if they are separate
   from the platform DB.
5. Add backup coverage for `/etc/log-platform` and `REPORTS_DATA_DIR`.
6. Configure host journald/Docker log retention.
7. Keep V2 out of schedules/retention until actual V2 job modules exist.
