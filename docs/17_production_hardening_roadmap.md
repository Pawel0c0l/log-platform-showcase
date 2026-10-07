# 17. Production Hardening — Operational Safety Model and Roadmap

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the
> form `PYTHONPATH="$PWD" python3 ops/runner.py …` are development / local /
> debug only — they execute the mutable working tree. The supported production
> entrypoint is the installed wrapper
> `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See
> `docs/07_operations.md` -> *Release boundary*.


Authoritative, durable record of how this platform detects and reports operational
failure, and of what remains to be done before it can be trusted to run unattended.

A read-only production-readiness audit on **2026-08-09** classified the platform
`NOT_READY_FOR_UNATTENDED_SCHEDULING`. This document carries that assessment
forward: it is the recovery point for the remaining work. Read it, and
`docs/11_operational_readiness.md`, before changing anything in the scheduling,
alerting or backup layers.

**Status legend**

| Status | Meaning |
|---|---|
| `DEPLOYED_VERIFIED` | Implemented, installed on the production host and confirmed working there. |
| `IMPLEMENTED_NOT_DEPLOYED` | Implemented and deterministically tested in the repository; **not installed in production**. |
| `REMAINING` | Not implemented. |
| `VERIFIED_CURRENT` | A statement of fact re-confirmed against production on the date given. |
| `NO_LONGER_APPLICABLE` | Superseded; kept only so the history reads correctly. |

---

## 1. Operator alert architecture

One mechanism, not several. Everything that needs a human routes through the
`suspected_bug` incident and outbox model introduced by migration
`052_suspected_bug_incidents_and_email_outbox.sql`.

```
failure
  └─ SuspectedBugEvent  (api/suspected_bug.py)
     └─ report_suspected_bug()          ── one transaction ──┐
        ├─ logs                (durable ERROR row)           │
        ├─ suspected_bug_incidents      (identity + state)   │ atomic
        ├─ suspected_bug_occurrences    (every repeat)       │
        └─ suspected_bug_email_outbox   (at most one email) ─┘
              └─ ops/suspected_bug_email_worker.py  (own systemd timer, every 5 min)
                 claim (FOR UPDATE SKIP LOCKED + claim token + lease)
                 └─ SMTP  →  sent | retry (bounded backoff) | dead_letter
```

### Incident identity and the anti-storm property

An incident is identified by its **cause**, never by its attempt. The fingerprint
(`SuspectedBugEvent.fingerprint_identity`) excludes run ids, timestamps, counts and
artifact ids; `ops/operational_alert.error_signature()` additionally strips
timestamps, UUIDs, hashes and bare numbers from the error message.

This is load-bearing. The Workflow A dispatcher fires every five minutes; the
2026-08-01 → 2026-08-04 provider-safety outage produced 135 failed ticks. Under
this model those collapse to **one** incident, and the existing 120-minute cooldown
plus 24-hour reminder interval turn it into roughly one initial email plus a daily
reminder — not 135 emails.

Deterministically verified: `ops/tests_manual/test_operational_alert_contract.py`
(24 replayed failures → 1 incident, 1 outbox row, 24 durable ERROR logs).

### Configuration

| Variable | Meaning | Consequence if unset |
|---|---|---|
| `SUSPECTED_BUG_ALERT_TO` | Recipients, comma or semicolon separated | **Every alert is suppressed** as `recipients_not_configured`. Incidents still persist. Unset *for the reporting process* has the same effect as unset globally — see the propagation note below. |
| `SUSPECTED_BUG_ALERT_FROM` | Sender override | Falls back to `AUTOMATION_SMTP_FROM`, then to the emailer's own default. Harmless when unset. |
| `AUTOMATION_SMTP_HOST` | **The transport that actually sends.** | `load_smtp_config_from_env()` raises, so every alert dead-letters. Readiness reports `smtp_host_not_configured`. |
| `SUSPECTED_BUG_ALERTS_ENABLED` | Master switch, default `true` | `false` suppresses all delivery. |
| `SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES` | Re-alert floor, default `120` | — |
| `SUSPECTED_BUG_ALERT_REMINDER_HOURS` | Reminder interval, default `24` | `0` disables reminders. |
| `SUSPECTED_BUG_EMAIL_MAX_ATTEMPTS` | Delivery attempts, default `6` | — |
| `AUTOMATION_SMTP_*` | Transport used by `jobs/common/emailer.py` | Delivery fails; rows dead-letter. |

An unconfigured alert path is indistinguishable from a healthy one unless you look
for it, so it is a first-class, machine-detectable state:

```bash
.venv/bin/python -m ops.operational_alert          # exit 0 = ready, 1 = not ready
```

The same readiness verdict is embedded in every `ops/execution_watchdog.py` scan
result as `alerting_ready` / `alerting_problems`.

#### Propagation: the variable must reach the *reporting* process

Configuration is per-process, not per-host. `/etc/log-platform/runtime.env` is
`root:root 0600`, so the job user cannot read it; systemd reads it and injects it
before dropping privileges, which makes `EnvironmentFile=` the only supply route.

A unit without that directive resolves zero recipients however the file is
configured. Production ran in exactly that state: `log-workflow-b.service` and
the installed `log-job@.service` template carried no `EnvironmentFile=`, so
**every** incident raised from inside a job — including both real Workflow B
orchestrator failures — was persisted with
`email_decision_reason='recipients_not_configured'` and never sent, while the
`OnFailure=` unit-level alert (whose handler *does* load the file) delivered
normally. Transport was healthy the whole time; the rich alerts were not.

Two consequences worth keeping in mind:

* the readiness CLI run from an interactive shell answers a different question
  than the unit does, because the shell sources the repository `.env`;
* `ops/systemd/proposed/log-job@dispatcher.service` and
  `log-job@retention-purge.service` already carried the correct pair, so a
  repository-only review would have shown the contract as satisfied.

`ops/tests_manual/test_workflow_b_alert_hardening.py` closes both by building the
effective environment from the shipped unit text and asserting that every
incident-raising unit and every delivery unit read the same file.

### Known limitation — single channel

**The platform cannot email you that it cannot email you.** There is exactly one
alert channel (SMTP), and no out-of-band fallback. What exists instead:

* the incident and its occurrences persist regardless of delivery;
* delivery state is explicit — `pending`, `sending`, `retry`, `dead_letter`, `sent`;
* `dead_letter` is a terminal, machine-detectable state meaning *this alert will
  never be delivered*;
* the failure is also visible as a failed systemd unit.

Therefore **`suspected_bug_email_outbox` must be checked by something outside this
platform** — an uptime service, a calendar reminder, anything:

```bash
.venv/bin/python ops/inspect_suspected_bugs.py --outbox dead_letter --outbox retry
```

The architecture is deliberately compatible with adding a second channel later:
`deliver_row()` takes an injectable `sender`, and delivery state lives in the
outbox rather than in the transport. Adding one is out of scope for this milestone.

### Anti-recursion

The alert path must never alert about itself.

* `ops/operational_alert.SELF_ALERTING_COMPONENTS` refuses any report whose
  component is the email worker or the failure adapter.
* `suspected-bug-email-worker.service` carries **no** `OnFailure=`.
* `log-platform-unit-failure@.service` carries **no** `OnFailure=`, and always
  exits 0 — a failure handler that can fail is a loop waiting to happen.
* The worker never reports a `suspected_bug` for its own delivery failures.

Deterministically verified: `test_operational_alert_contract.py`.

---

## 2. Terminal failure integration

`ops/runner.py` is the single boundary every scheduled job crosses. Terminal
failure reporting lives there, not scattered across business logic:

```
log-job-runner.sh → ops/runner.py main()
    └─ _execute()  raises
       └─ _report_terminal_failure() → JOB_TERMINAL_FAILURE incident
          (never masks the original exception; re-raised unchanged)
```

This one hook covers the Workflow A dispatcher and sync, the Workflow B
orchestrator and all its stages, retention purge, and the Eco aggregate and email
jobs. **No job's own success semantics changed** — the platform run is already
`FAILED` before reporting happens.

Failures that never reach application code are covered from outside by systemd:

```
any monitored unit fails
  └─ OnFailure=log-platform-unit-failure@%n.service
     └─ ops/systemd_failure_adapter.py → SYSTEMD_UNIT_FAILURE incident
        (talks straight to PostgreSQL — the platform HTTP API may be what failed)
```

This is what covers the 2026-07-31 20:00 and 2026-08-01 06:00 class, where
`log-workflow-b.service` exited non-zero *before* creating a `public.runs` row and
the only evidence was journald, which has since rotated.

Units routed into the alert path: `log-job@dispatcher`, `log-workflow-b`,
`log-backup`, `log-platform-prune`, `execution-watchdog`, `disk-space-monitor`,
`backup-retention`.

---

## 3. Missing-run watchdog (dead-man semantics)

`ops/execution_watchdog.py`, its own systemd unit, its own 15-minute timer.
It asserts the *opposite* of in-process error handling:

> This execution was expected, and it must have reached a terminal state by this deadline.

Evidence, in order of authority — **journald is never consulted**, because it
rotates and correctness must not depend on it:

1. `workflow_a_control.client_schedule_run_history` — the dispatcher's own claim record;
2. `public.runs` — for systemd-driven workflows with no DB schedule metadata;
3. `ops_control.scheduler_heartbeat` — liveness for schedulers whose idle ticks
   deliberately persist no run row.

### Verdicts

| Verdict | Meaning | Alerts? |
|---|---|---|
| `OK` | Expected fire reached SUCCESS | no |
| `EXPECTED_FAILED` | Expected fire reached FAILED | no — already alerted by `JOB_TERMINAL_FAILURE`; a second incident would double-notify one cause |
| `IN_WINDOW` | Due, but still inside its grace window | no |
| `DISABLED` | Schedule disabled; nothing is expected | no |
| `MISSING` | Grace expired with no execution record | **yes** — `SCHEDULED_RUN_MISSING` |
| `STALE` | Non-terminal beyond its stale grace | **yes** — `SCHEDULED_RUN_STALE` |
| `HEARTBEAT_LOST` | Scheduler has not reached the database | **yes** — `SCHEDULER_HEARTBEAT_LOST` |
| `NOT_YET_EXPECTED` | Eligible now, but this fire predates eligibility | no |

### Eligibility is the dispatcher's definition, not a lookalike

The watchdog must never expect an execution the dispatcher would never perform.
It originally selected on `schedule.enabled` alone while the dispatcher selects:

```sql
FROM   client_dataset_schedule cds
JOIN   client_account   ca ON ca.client_id    = cds.client_id
JOIN   dataset_registry dr ON dr.dataset_name = cds.dataset_name
WHERE  cds.enabled = true AND ca.enabled = true
```

Both joins are inner, so an enabled schedule belonging to a **disabled client**,
or naming an **unregistered dataset**, is ineligible — and was being reported
`MISSING` forever, an alert no operator action could ever clear.
`ops/execution_watchdog.schedule_eligibility()` now mirrors that predicate
exactly, and the watchdog selects *all* schedules so ineligible ones are reported
`DISABLED` with a reason instead of vanishing (the documented `DISABLED` verdict
was previously unreachable, because the query filtered those rows out).

Drift is guarded from both directions: a live fixture drives the dispatcher's own
SQL and the watchdog's mirror and asserts the selected sets are identical, and a
static check fails if the dispatcher's predicate changes without the mirror.

#### Eligibility epoch — when the subject *became* eligible

"First observed" is not "became eligible", and conflating them was exploitable in
the ordinary course of operations: a schedule observed while its client was
disabled kept an old `first_observed_at`, and re-enabling the client — which
writes no timestamp anywhere, since `client_account` has none — made fires from
the disabled interval retroactively expected and reported `MISSING`.

`ops_control.watchdog_observation` therefore persists `eligible` and
`eligible_since`, and `eligibility_epoch()` maintains them:

| Previous state | Now | Epoch |
|---|---|---|
| never observed | eligible | schedule `updated_at`/`created_at` — real evidence, so a long-established schedule is monitored from the first scan |
| never observed | ineligible | none; `DISABLED` |
| `eligible=false` | eligible | **now** — this scan is the transition |
| `eligible=true` | eligible | the stored epoch, unchanged; repeated scans never push it forward |
| any | ineligible | cleared; the next re-enable starts a fresh epoch |

Fires older than the epoch are `NOT_YET_EXPECTED`. The state lives in the
database, not in process memory, because every scan is a separate short-lived
process. Each disable→enable cycle gets its own epoch, so repeated cycles behave
identically to the first.

### Occurrence identity vs root incident identity

A missed fire is an **occurrence**. "This scheduled workflow is not producing its
expected runs" is the **incident**. Conflating them made the subject key
`systemd:<subject>:<fire timestamp>`, which put the fire time inside the
fingerprint, so a continuing Workflow B outage opened a fresh incident at 06:00
and another at 20:00 every day — and none of them could ever resolve, because a
later successful fire carried a different key and closed nothing. They simply
stayed open once they fell out of the scan horizon.

`fold_systemd_occurrences()` evaluates every fire in the horizon and reduces them
to one root observation keyed `systemd:<subject>`, with no timestamp in the
identity:

* root health = the verdict of the **newest decided fire**; a fire still inside
  its grace window decides nothing, and cannot mask an older decided failure;
* individual missed fires survive as evidence — `missed_fires`,
  `missed_fire_count`, `evaluated_fires`, `latest_decided_fire`;
* a later successful fire makes the root healthy, which resolves the incident;
* a subsequent outage opens a new lifecycle under the same key, and the
  resolved→open transition is material, so it alerts again.

Only a genuine healthy execution resolves. `DISABLED`, `NOT_YET_EXPECTED` and
`IN_WINDOW` mean "nothing was expected" — disabling a broken schedule must not
quietly close the incident it left open.

### Grace windows, and why they are what they are

| Window | Default | Justification |
|---|---|---|
| completion grace | 180 min (`trips_sync`: 240) | Workflow B runs have taken ~109 min in production; the dispatcher serialises all datasets globally, so a downstream fire can legitimately queue behind an upstream one. |
| stale grace | 240 min | Deliberately **shorter** than the dispatcher's own 720-minute stale reaper, so the operator hears about a wedged job before it is silently auto-failed. |
| heartbeat grace | 30 min | Six consecutive missed 5-minute dispatcher ticks. |

Workflow A expectations are derived from `client_dataset_schedule`, so enabling or
disabling a dataset automatically changes what is expected — nothing is hard-coded.
Expected fire times are computed with the dispatcher's **own**
`latest_scheduled_fire_local`, so watchdog and dispatcher cannot disagree.

Schedules with no DB metadata are declared in `ops/watchdog_expectations.json`.
Backup and prune are deliberately **not** listed: they persist no `public.runs`
row, so there would be nothing to assert against. Their failures are covered by
`OnFailure=` routing instead.

### Idempotency and recovery

`ops_control.watchdog_observation` holds one row per subject. Repeated scans
re-observe the same row and never accumulate subjects; alert cooldown remains owned
by `suspected_bug_incidents`. When a subject returns to a healthy verdict, the
watchdog resolves the incidents recorded in that subject's
`open_incident_fingerprints` — scoped both to the exact subject and to the three
incident codes it owns, so neither another subject's incident nor a business
incident can be closed by watchdog recovery.

### Watchdog self-monitoring

**The watchdog cannot fully monitor itself, and this document does not claim it
can.** A dead watchdog unit is visible only through
`OnFailure=log-platform-unit-failure@%n.service` (which catches a *failing* run,
not a *disabled timer*) and through the external outbox check in §1.

---

## 4. Backup retention and disk-space monitoring

### Retention — `ops/backup_retention.py`, `backup-retention.timer` (daily 04:15)

`ops/backup.sh` is unchanged; creation and deletion stay separate so a retention
bug can never damage the set that was just published.

**Validity is decided by one authoritative contract.** The first implementation
treated "three correctly named, unsuffixed files" as proof a backup was good, on
the assumption that `backup.sh` publishes final names only after verification.
That assumption is false: `backup.sh` renames to the final names and *then* runs
`verify_pair`, so a process killed in between leaves a structurally perfect,
never-verified triple. Silent corruption after publication was invisible to a
filename check at all. Codex found the realistic consequence — an old genuinely
restorable backup expiring and being deleted while three corrupt newer triples
held the safety floor, leaving zero restorable backups.

Validity now comes from `ops/backup_manifest.verify_manifest()`, the same
function `ops/backup.sh verify` calls:

| Clause | Rejects |
|---|---|
| schema + contract version | foreign or unparseable manifests |
| timestamp agreement | a manifest filed under the wrong set |
| identity pins | a backup of another environment/database/platform |
| filename safety | traversal and absolute paths |
| **member identity** | a manifest borrowing another set's archives |
| archive present, mode 0600 | missing or world-readable archives |
| recorded size | truncation |
| **sha256** | silent bit-rot, and any set never actually verified |
| **archive traversal** (`check_archives`) | gzip/tar damage that still hashes correctly |

#### A backup set must be self-contained

Checking only that a referenced basename was *safe* was exploitable. A manifest
filed as `backup_T2.manifest.json` could name `postgres_T1.sql.gz` and
`minio_T1.tar.gz` — with T1's genuine sizes and checksums — and verify. Three such
sets became the entire survivor floor, retention then deleted T1 as expired, and
nothing restorable remained. The failure surfaced only *after* the deletion.

Member filenames are therefore derived from the discovered set timestamp
(`backup_manifest.member_filename()`, the one place the naming contract lives) and
must match exactly. A set is a survivor anchor only if it is restorable on its own.

#### Identity comes from the platform, not from the directory

Anchors were previously required to agree with *the newest accepted backup*, which
made the newest file in the directory the source of truth. Dropping coherent
staging backups in was enough to redefine identity, after which the real
production backups read as foreign and expired.

Identity is now attested from the running platform via
`jobs.common.environment_identity.attest_platform_identity()` reading
`ops_control.environment_identity` — the same source `ops/backup.sh verify` pins
against. `AuthoritativeIdentity` pins `environment`, `database` and
`platform_uuid`. `repository_commit` is deliberately **not** pinned: it records
the commit a backup was taken at, so pinning it to current HEAD would reject every
backup older than the last commit. `backup.sh verify` can pin it only because it
runs immediately after creation. It is provenance, not identity.

If the identity cannot be attested, retention does not run.

* `BACKUP_RETENTION_KEEP_MINIMUM` (default 3) is a floor of **verified** sets.
  Anchors are verified newest-first — the cheap order, since verification hashes
  and traverses whole multi-GB archives, and the safe one, since those are the
  sets an operator would restore from. Measured against the live 242 GB
  directory on 2026-08-09: **5 min 6 s** for three anchors (~21 GB) under the
  full contract including archive traversal, against a `TimeoutStartSec=1800`
  budget. Checksums alone were 48 s; the traversal is what brings retention up
  to `verify_pair`'s bar and is worth the extra four minutes on a nightly job.
  Re-measure if the archive set grows several-fold.
* If fewer than `keep_minimum` verified anchors can be proven, retention **fails
  closed and deletes nothing**. A full disk is recoverable; a missing last good
  backup is not.
* The floor is **re-verified from disk immediately before any `unlink()`**, under
  the same held lock and against the **full** contract — identity pins, member
  identity, sizes, checksums and archive traversal — so the last gate before an
  irreversible deletion is evidence rather than an earlier belief.
* After deletion, still under the lock, every protected survivor is verified
  again. The result carries `protected_survivors`, `post_delete_verification`,
  `authoritative_identity`, `plan.rejected_candidates` and
  `operator_action_required`, so what survived and why is inspectable rather than
  inferred.
* A configured backup root that is absent or not a directory is a backup outage,
  not an empty success: it fails closed too.
* `.partial`/`.failed` remnants are never anchors and are removed only with the
  explicit `--purge-invalid` flag. Files outside the naming contract are never
  touched.
* Dry-run is the default; `--execute` is required to delete. A dry run emits
  exactly the candidate list an execute run would delete. Repeated runs are
  idempotent.

**Locking.** One `flock` on `backups/.backup.lock`, three participants:

| Participant | Critical section |
|---|---|
| `ops/backup.sh` | the whole run |
| `ops/backup_retention.py` | discover → verify → plan → **re-verify** → delete → post-delete verify |
| restore (manual) | **select → verify → consume postgres → consume minio** |

Retention is non-blocking: if a backup is running it skips the window rather than
plan deletions against a directory being written.

### Validation parity — one contract, five call sites

Retention was pinned to the attested platform identity while the two *documented*
procedures were not. They called `ops/backup_manifest.py verify`, whose
`--expected-*` flags are optional, and omitted them — so a valid, self-contained,
correctly hashed **staging** backup returned `status: VALID` inside the production
restore procedure and inside post-retention survivor verification.

`ops/verify_backup_set.py` closes that gap by construction: it attests the target
itself via `load_authoritative_identity()` and **offers no flag to skip or
override it**, so a procedure that calls it cannot forget the pins.

| Call site | env | database | platform UUID | exact members | hashes | archive read | fail closed |
|---|---|---|---|---|---|---|---|
| Retention anchor | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ refuses to plan |
| Pre-delete revalidation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ deletes nothing |
| Internal post-delete | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ `RetentionUnsafe` |
| **Documented restore** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ aborts before consuming |
| **External survivor check** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ aggregate non-zero |

All five now go through `backup_manifest.verify_manifest()` with the same pins.

**Deliberate differences, unchanged:**

* `repository_commit` is pinned by **none** of them. It records the commit a
  backup was taken at, so requiring it to equal current HEAD would make every
  backup older than the last commit unrestorable. It is provenance, not identity.
* `ops/backup.sh verify` still pins `repository_commit` to current HEAD. That is
  correct for its own use — it runs immediately after creation — but means the
  manual `ops/backup.sh verify <old_stamp>` command cannot validate a historical
  backup. Pre-existing, non-blocking, and deliberately not changed here; for
  historical sets use, behind the identity bootstrap (§5.1a):
  `.venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.verify_backup_set <stamp>`
* `ops/backup.sh` performs its gzip/tar traversal in shell, so the CLI keeps
  `--check-archives` opt-in and does not double the nightly cost. Every Python
  call site requests the traversal.

**Bare-metal caveat.** Attestation reads the marker from the target platform
database. Restoring into an existing identified platform works; restoring into a
freshly created empty database exits 2 until identity is provisioned with the
existing promotion tooling. There is no bypass flag, because a bypass is
indistinguishable from the mistake it would enable.

Restore must acquire the lock **before** selection and verification, and hold the
same file descriptor until the last byte is consumed. Verifying first and locking
afterwards leaves a window in which retention deletes the very set that was just
declared valid, and the operator finds out half-way through replaying it. The
documented sequence is a single `flock ... bash -s <<'RESTORE'` heredoc so it
cannot be split by accident, and
`test_documented_restore_locks_before_it_verifies` asserts that ordering against
the document itself. Full procedure: `docs/09_disaster_recovery.md`.

Deterministically verified: `ops/tests_manual/test_backup_retention.py` (15
tests) using fixtures built the way `backup.sh` builds them — real gzip and
tar.gz payloads, real sha256 digests, 0600 modes — with one corruption per test
breaking exactly one clause of the contract. Includes the Codex scenario end to
end and a real cross-process `flock` race.

### Disk space — `ops/disk_space_monitor.py`, `disk-space-monitor.timer` (hourly)

Two thresholds, each expressed as a free-percentage floor **and** a free-bytes
floor, because a percentage alone is useless on a large disk and bytes alone are
useless on a small one. Either floor can trigger independently; critical wins.

| Variable | Default |
|---|---|
| `DISK_MONITOR_WARNING_PERCENT` | 20 |
| `DISK_MONITOR_CRITICAL_PERCENT` | 10 |
| `DISK_MONITOR_WARNING_FREE_GB` | 60 |
| `DISK_MONITOR_CRITICAL_FREE_GB` | 25 |

Malformed or negative values fall back to defaults rather than silently disabling
the guard. Incidents carry mountpoint, total/used/free bytes, free percentage and
the thresholds that were breached. Repeated scans below a threshold do not
re-email; recovery resolves the incident so a later relapse reads as new.

#### Incident lifecycle is per filesystem, not per component

Every mountpoint shares `component = ops.disk_space_monitor`, and
`suspected_bug_incidents` has no subject column — so resolving by
`(component, incident_code)` meant `/var/lib/docker` recovering closed the still
critical incident for `/`. Identity is the fingerprint, so each subject records
the fingerprints it currently has open in
`ops_control.watchdog_observation.open_incident_fingerprints`, and may close
those and nothing else.

Per mountpoint, independently of every other mountpoint:

```
HEALTHY ──► WARNING ──► CRITICAL ──► WARNING ──► HEALTHY
             │            │            │            │
             │            │            │            └─ resolves this subject's
             │            │            │               open incidents only
             │            │            └─ critical superseded: resolved,
             │            │               warning re-opened
             │            └─ warning superseded: resolved, critical opened.
             │               One filesystem never holds an open warning and an
             │               open critical simultaneously.
             └─ opens the warning incident
```

A relapse after recovery reads as new news: the incident model already treats a
resolved→open transition as material, so it alerts again rather than staying
silent inside the original cooldown.

Deterministically verified: `ops/tests_manual/test_disk_space_monitor.py` — seven
pure classification tests plus five against a disposable database proving that
four consecutive critical scans yield one incident and one email, that a second
filesystem is a separate subject, that recovery closes the incident, and that a
relapse after recovery alerts again instead of staying silent.

---

## 4a. Reproducing the verification

Everything is deterministic and offline. No test touches production, real SMTP or
real backups; all of them refuse a DSN containing `logdb`.

```bash
# Pure tests — no database required.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
    ops/tests_manual/test_backup_retention.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" .venv/bin/python \
    ops/tests_manual/test_disk_space_monitor.py
```

Persistence, dedup, recovery and delivery need a throwaway PostgreSQL. Each suite
bootstraps its own schema, so an empty database is enough:

```bash
docker run -d --name lp-p0-test-pg -p 127.0.0.1:55432:5432 \
    -e POSTGRES_USER=testuser -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=p0_test \
    postgres:16-alpine
DSN='postgresql://testuser:testpw@127.0.0.1:55432/p0_test'

SUSPECTED_BUG_TEST_DSN="$DSN"    ... ops/tests_manual/test_suspected_bug_outbox_postgres.py
OPERATIONAL_ALERT_TEST_DSN="$DSN" ... ops/tests_manual/test_operational_alert_contract.py
WATCHDOG_TEST_DSN="$DSN"          ... ops/tests_manual/test_execution_watchdog.py
DISK_MONITOR_TEST_DSN="$DSN"      ... ops/tests_manual/test_disk_space_monitor.py
```

Each DSN variable is independently optional: with it unset the suite prints `SKIP`
and still passes. **A green run that says `SKIP` has not verified persistence** —
check for the SKIP lines before trusting a result.

---

## 5. Roadmap

### 5.1 Completed in this milestone

Deployment ran on **2026-08-09** as a separate, explicitly authorized task
(Gate A, with a second authorization at Gate B for the first destructive
retention). All four items are installed and verified on the production host.
Retention was delayed mid-deployment by the defect in §5.1a, fixed, independently
re-reviewed, then deployed.

| Item | Status | Evidence |
|---|---|---|
| **P0-1** Functional operator alert delivery | `DEPLOYED_VERIFIED` (2026-08-09) | `test_suspected_bug_outbox_postgres.py` (15 tests) on a disposable DB with a fake SMTP transport — covers `sent`, bounded retry, `dead_letter`, exhausted attempts, stale-claim recovery and `SKIP LOCKED` double-delivery prevention; readiness CLI; production-ready units in `ops/systemd/proposed/` |
| **P0-2** Terminal failure integration | `DEPLOYED_VERIFIED` (2026-08-09) | `ops/runner.py` hook + `ops/systemd_failure_adapter.py`; `test_operational_alert_contract.py` (11 tests) incl. the 24-repeat anti-storm proof and a persisted 6-repeat unit-failure proof |
| **P0-3** Missing-run watchdog | `DEPLOYED_VERIFIED` (2026-08-09) | `ops/execution_watchdog.py`; `test_execution_watchdog.py` (12 pure + 3 persistence tests); migration `059` |
| **P0-4** Backup retention + disk monitoring | `DEPLOYED_VERIFIED` (2026-08-09) — disk monitor, then retention after the §5.1a fix | `ops/backup_retention.py` (11 tests incl. the alert path), `ops/disk_space_monitor.py` (7 pure + 5 persisted dedup/recovery tests); dry-run plan validated against the live 242 GB `backups/` directory |

The alert worker code (`ops/suspected_bug_email_worker.py`) and its units already
existed and were already correct; this milestone did not rewrite them.

**Independent Codex review, 2026-08-09: `CHANGES_REQUIRED_BEFORE_DEPLOYMENT`.**
Two BLOCKER and four HIGH findings were confirmed against the code and fixed. All
six are remediated and verified; the milestone is awaiting **re-review**, and is
still `IMPLEMENTED_NOT_DEPLOYED`.

| # | Finding | Fix | Regression test |
|---|---|---|---|
| B1 | Retention treated correctly named triples as good; no manifest/checksum validation, no backup lock | Validity delegated to `backup_manifest.verify_manifest()`; verified newest-first floor; re-verify before `unlink()`; fail closed; shared `flock` | `test_backup_retention.py` (15) |
| B2 | `Requires=` made `systemctl start <timer>` run destructive retention immediately; `[Install]` on the services would also run them at boot; `Persistent=true` could replay a missed destructive fire | `Requires=` removed; `[Install]` removed from all timer-driven oneshots; `Persistent=false` on the destructive timer | `test_systemd_unit_contract.py` (6) |
| H1 | `%I` unescapes `-`→`/`, so the adapter received `log/workflow/b.service` | Handler reads `%i` (verbatim instance) | `test_systemd_unit_contract.py`, checked against `systemd-escape` |
| H2 | Watchdog checked `schedule.enabled` only; dispatcher also requires `client_account.enabled` and a registered dataset. `DISABLED` was unreachable | `schedule_eligibility()` mirrors the dispatcher; all schedules selected; `NOT_YET_EXPECTED` for pre-eligibility fires | `test_execution_watchdog.py` — live both-paths comparison + static drift guard |
| H3 | Disk recovery resolved by component, closing other filesystems' incidents; warning stayed open under critical | Fingerprint-scoped resolution per subject; explicit supersession | `test_disk_space_monitor.py` (14) |
| H4 | Fire timestamp in the subject key → one unresolvable incident per fire | `fold_systemd_occurrences()`: one root subject per schedule, fires kept as evidence | `test_execution_watchdog.py` (24) |

**Second independent Codex review: `CHANGES_REQUIRED_BEFORE_DEPLOYMENT`.** It
confirmed four of the six above as resolved (timer activation, `%i`, disk
cross-filesystem recovery, per-fire incident growth) and reproduced three further
defects, all now fixed:

| # | Finding | Fix | Regression test |
|---|---|---|---|
| B1a | A manifest for T could reference T′'s archives with T′'s real hashes; three such sets formed the floor and retention deleted the one real backup they pointed at | Member filenames derived from the set timestamp and matched exactly | `test_cross_referenced_manifests_are_never_anchors`, `test_exact_member_identity_is_required_per_section` |
| B1b | Identity was taken from the newest accepted backup, so newer foreign backups redefined it and production backups expired | `AuthoritativeIdentity` attested from `ops_control.environment_identity`; the directory has no vote | `test_foreign_identity_backups_never_anchor_the_current_platform` |
| H | Restore verified *then* locked, leaving a window for retention to delete the verified set | One `flock`, one fd, across select → verify → consume | `test_documented_restore_locks_before_it_verifies` |
| H | `first_observed_at` made a disabled-period fire expected after a client re-enable | Persisted `eligible`/`eligible_since` epoch, advancing only on a false→true edge | `test_codex_disabled_client_re_enable_does_not_backdate_expectation` + 4 more |

Archive traversal (`gzip -t` / `tar -tzf` equivalents) was also added to retention
so its bar matches `verify_pair`'s.

**Third independent Codex review: one remaining HIGH, now fixed.** It confirmed
the retention work above as resolved and reproduced a *procedure* gap: retention
was identity-pinned, but the documented restore and post-retention survivor
verification were not, so a valid self-contained staging backup passed the
production restore check. The external survivor loop also used
`... || echo "FAILED: $STAMP"`, which printed a warning and left the stage's exit
status at 0 — a verification step that could not fail.

| Finding | Fix | Regression test |
|---|---|---|
| Restore verifier omitted `--expected-environment/database/platform-uuid` | New `ops/verify_backup_set.py` attests the target itself; no override flag exists | `test_staging_backup_is_rejected_by_the_production_restore_verifier` |
| Survivor verification omitted the same pins | Same helper, invoked once for all survivors | `test_survivor_verification_fails_closed_on_any_single_failure` |
| `\|\| echo FAILED` masked failures | Aggregate exit 1 if any set fails; exit 2 if identity is unattestable | same, plus `test_verification_fails_closed_when_identity_cannot_be_attested` |
| Docs could regress to the unpinned CLI | Both documents asserted structurally | `test_documented_procedures_use_the_pinned_verifier` |

Measured before/after on the reproduction: the old command returned
`{"status": "VALID"}` exit 0 for a staging backup; the pinned verifier returns
`INVALID (manifest environment mismatch)` exit 1.

**Fourth independent Codex review: one remaining HIGH, now fixed.** The verifier
was correct, but the *documented block* around it was not: it ended with `echo`,
`jq` and `df`, three commands that succeed and therefore reset the stage's exit
status. An operator could paste the procedure, have survivor verification fail,
and still see an overall exit 0.

The stage is now a subshell with `set -euo pipefail` and an `ERR` trap, and the
subshell is the **last** thing in the block, so the block's exit status *is* the
verifier's:

| Scenario | verifier | block exit | later commands |
|---|---:|---:|---|
| all valid | 0 | 0 | run |
| corrupt / foreign / missing survivor | 1 | 1 | skipped |
| identity unattestable | 2 | 2 | skipped |
| unexpected verifier error | 3 | 3 | skipped |

An empty `protected_survivors` list also fails rather than passing vacuously. A
subshell rather than a bare `set -e` so a pasted failure cannot close the
operator's interactive session, and the exact status is preserved rather than
flattened to 1.

The regression **executes the fenced block extracted from
`docs/11_operational_readiness.md`** with a stubbed verifier, rather than
pattern-matching its text — the previous guard only checked that `|| echo` was
absent, which the defective block satisfied. Verified: the old block form exits 0
with the later commands running; the current one exits 1 with them skipped.

Two MEDIUM findings were fixed because they sat directly inside those changes:
readiness now checks `AUTOMATION_SMTP_HOST` (the transport that actually
delivers) instead of the optional `SUSPECTED_BUG_ALERT_FROM`, and a missing
backup root fails closed. The remaining MEDIUM findings are in §5.5.

Two further defects were found and repaired after the implementation session was
interrupted, both of the same shape — code that looks wired up but is inert:

* **`OnFailure=` was written under `[Service]`** in all four `95-onfailure.conf`
  drop-ins. It is a `[Unit]` directive; systemd ignores it elsewhere with only an
  "Unknown key name" warning, so the entire infrastructure-failure routing would
  have deployed silently dead. The contract test asserted only that the string was
  present, so it passed; it now asserts the enclosing section.
* **`record_observation()` stamped its own module's `watchdog_name`**, so
  disk-space rows were filed under `execution_watchdog` and
  `idx_watchdog_observation_open` misattributed them. `Observation` now carries
  `watchdog_name` and the producer owns it.

### 5.1a Deployment discovery — retention identity bootstrap (2026-08-09)

Status before the fix: **`P0-4_RETENTION_DEPLOYMENT_BLOCKED_IDENTITY_BOOTSTRAP`**.
Status after the repository fix, before installation: **`IMPLEMENTED_NOT_DEPLOYED`**.

Found during the controlled deployment, **before `backup-retention.timer` was
enabled**, when the authoritative dry-run was attempted in the unit's own
environment:

```
BACKUP_RETENTION_FAILED
EXPECTED_PLATFORM_IDENTITY_MISSING: required non-secret variable
  LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID is missing
exit 1
```

`ops.backup_retention` and `ops.verify_backup_set` are the **only two** P0
modules that attest the platform identity. `load_runtime_identity()` requires
five variables, and they do not all live in the same place:

| Variable | Source |
|---|---|
| `LOG_PLATFORM_TARGET_ENVIRONMENT` | `/etc/log-platform/environment-identity.env` |
| `LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID` | repository `.env` |
| `LOG_PLATFORM_EXPECTED_POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` | repository `.env` |

`/etc/log-platform/runtime.env` carries no `LOG_PLATFORM_*` key at all. Every
other host job reaches the complete contract through
`/usr/local/bin/log-job-runner.sh` → `ops/run_with_environment_identity.py`, but
`backup-retention.service` invoked the module directly. The consequence was the
same shape as the `OnFailure=`-under-`[Service]` defect above — an installed unit
that looks wired up and is inert:

* every 04:15 fire would exit 1 before planning anything;
* retention would never run, while the disk kept filling;
* `OnFailure=` would raise a `SYSTEMD_UNIT_FAILURE` incident **nightly**.

It fails closed, so no backup was ever at risk. The same gap existed in the
*documented* procedures — `docs/11_operational_readiness.md` (dry run, execute,
survivor verification, monitoring) and `docs/09_disaster_recovery.md` (restore
verification) all invoked the modules bare, so an operator pasting the approved
procedure hit the identical failure.

**Fix.** `ExecStart` and every executable documented invocation now run behind
`ops/run_with_environment_identity.py`, which loads the repository environment
without overriding what systemd already supplied, applies the canonical identity
file with `reject_conflict=True`, and sets
`LOG_PLATFORM_REQUIRE_CANONICAL_IDENTITY=1`. The expected identity values were
deliberately **not** duplicated into `runtime.env`: two copies of an identity
contract is how the identity drifts. Nothing about deletion policy, verification
policy, locking, timeouts or timer semantics changed — `Persistent=false` and the
absent `[Install]` are intact.

| Regression | Guards |
|---|---|
| `test_systemd_unit_contract.test_retention_bootstraps_the_platform_identity` | parses `ExecStart` semantically: wrapper before `--`, `-m ops.backup_retention --execute` behind it, no hard-coded identity, `--execute` on no other unit |
| `test_backup_retention.test_documented_procedures_bootstrap_the_platform_identity` | every **operator-facing** invocation of either identity-attesting module carries the bootstrap, with the module behind the separator — across all four surfaces: `docs/09_disaster_recovery.md`, `docs/11_operational_readiness.md`, this roadmap, and the verifier's own usage docstring |
| existing documented-block execution tests | still execute the **extracted** block; the verifier substitution now matches the wrapped form, so a regression to the bare form fails the test rather than silently passing |

Both new guards were proven to **reject** the defective forms by reconstructing
them in a throwaway copy — a guard that passes on the broken form proves nothing.

**Independent Codex re-review: one HIGH, now fixed.** The systemd artifact passed
(`IDENTITY_BOOTSTRAP_CONTRACT`, `SYSTEMD_RETENTION_CONTRACT`, `systemd-analyze
verify` exit 0, survivor fail-closed and restore safety all `PASS`, no BLOCKER),
but two **operator-facing examples still bypassed the bootstrap** and the first
version of the documentation guard did not look at them: the historical-set
recommendation in §4 of this document, and the usage line at the top of
`ops/verify_backup_set.py`. Both would have reproduced
`EXPECTED_PLATFORM_IDENTITY_MISSING` for an operator who copied them. Both are
now wrapped, and the guard was generalized from "the two procedure documents" to
"every operator-facing surface", keyed on lines that name a Python interpreter
*and* run one of the modules with `-m` — so prose, imports, implementation
details and test fixtures that deliberately reconstruct invalid forms are not
flagged. Each reversion is proven rejected. Retention remains
`IMPLEMENTED_NOT_DEPLOYED`.

### 5.2 Remaining P0 — all `REMAINING`

These block unattended scheduling and were deliberately **out of scope** here.

* **P0-5 — Generalized upstream dependency/readiness gate.**
  `jobs/api/telematics/dispatcher.py:_is_compatibility_trips_fire()` restricts the
  coverage gate to `trips_sync`. Every other dataset — fuel aggregation, all Eco
  snapshots, all Eco email jobs — fires on wall-clock ordering alone with no
  readiness check. If `trips_sync` fails at 02:00, an Eco snapshot at 03:00 will
  compute complete-looking driver scores from incomplete trips.
  *Invariant needed:* a fire is refused, with a distinct
  `BLOCKED_DEPENDENCY_NOT_READY` terminal state, unless its declared upstream
  succeeded and covers the period. Note `ck_run_history_status` currently permits
  only `RUNNING`/`SUCCESS`/`FAILED`.

* **P0-6 — Customer-email batch success semantics.**
  Run `1fa5f596-7df5-4770-80ff-cb136cbff633` (2026-08-07, ALPHA00001 monthly) was
  recorded **SUCCESS** while `eco_driving_monthly_email_send_log` held
  `sent=1137`, `failed=105` (104 × `INVALID_RANKING_SNAPSHOT`, 1 × SMTP timeout)
  and `skipped_missing_email=10`.
  *Invariant needed:* a send batch with terminal per-recipient failures cannot
  report SUCCESS.

* **P0-7 — BRAVO00016 Report 207 load policy.**
  `workflow_b_control.report_type_client_load_policy` contains only two rows, both
  `ALPHA00001`. For BRAVO00016 this raises `missing_report_policy` →
  `BLOCKED_UNSUPPORTED_CONFIGURATION` → whole run `FAILED_NON_RETRYABLE`, and
  leaves `client_trips` speeding counters unpopulated
  (`telematics_reports.report_207`: 1 036 633 rows, 24 867 migrated ≈ 2.4 %).
  *Invariant needed:* a missing per-client policy is either configured or
  classified SKIPPED — never a silent data hole that also reds the run.
  **Partly addressed by `P0-G` (§5.9), not closed.** The write-ordering half is
  fixed: the policy is now proven *before* the first client-data commit, so a
  missing row can no longer change customer data and then fail the run. The data
  hole is not fixed — `BRAVO00016` still has no policy row, and §5.9 blocks the
  file rather than classifying it SKIPPED, because a silent SKIP would leave the
  same 2.4 % migration gap with no signal at all. Configuring the row (or
  deciding SKIPPED is correct for this client) remains an operator action and is
  what actually closes `P0-7`.

### 5.3 Remaining P1 — all `REMAINING`

* **Bounce/DSN ingestion.** No code anywhere reads a mailbox for delivery-status
  notifications. `BRAVO_ECO_WEEKLY_EMAIL_IMAP_*` is used **only** to APPEND sent MIME
  to the Sent folder (`jobs/ecodriving_person/email_delivery.py`); it never opens
  INBOX. `sent` therefore means "accepted for relay", the lifecycle ends there, and
  a hard-bouncing address keeps receiving every future period. See §6.
* **Register the ALPHA Eco email jobs as dispatcher datasets.**
  `workflow_a_control.dataset_registry` has no row for
  `eco_driving_weekly/monthly_email_notifications`, so ALPHA customer sends are
  only possible as a manual `ops/runner.py` invocation — which is how 1 137 real
  customer emails were sent on 2026-08-07.
* **`finish_run` retry + stale `public.runs` recovery.** **Partially closed** by
  the P0-E/P1-I slice; the remaining scope is narrower than the original entry
  and is stated precisely so it is not read as done.

  Closed: a successful run whose finalization fails is no longer recorded as
  `FAILED` (`finish_run(SUCCESS)` moved outside `run_context`'s `try`, and the
  failure raises `RunFinalizationError` carrying `application_succeeded=True`);
  a swallowed `finish_run(FAILED)` is no longer silent (`RUN_FINALIZATION_FAILED`
  is logged with the intended status and the primary exception); and
  `PATCH /runs/{id}` is now a compare-and-set, so a settled outcome cannot be
  silently rewritten by a late or duplicated finalization.

  Still open: `finish_run` has **no retry** — still a single `requests.patch` —
  and nothing *repairs* an already-orphaned row. Detection is covered
  (`execution_watchdog` `stale_grace_minutes`, verdict `STALE`, incident
  `SCHEDULED_RUN_STALE`); repair was deliberately kept out of `run_context`,
  because a process that has just proven it cannot reach the API is the worst
  candidate to perform recovery. The 720-minute reaper still covers only
  `client_schedule_run_history`, not `public.runs`. Six orphaned `RUNNING` rows
  exist as of 2026-08-16 — the oldest from 2026-05-11, one of them
  `jobs.reports.stage3.job_stage3` — and are historical debt owned by a separate
  reconciliation operation.
* ~~**Stage 3 files stranded by a crashed or failed attempt (P0-E).**~~ **Closed**
  by the same slice. `stage3_status` in `RUNNING` or `ERROR` was terminal by
  omission: no discovery, reconciliation or watchdog ever selected such a row
  again, so a process killed just after `_mark_stage3_started` committed removed
  the file from the autonomous system permanently. Both states are now
  discoverable, and what happens next is decided from durable **attempt-scoped**
  destination evidence rather than from the status label.

  Three properties are load-bearing and each was established against the real
  loaders after independent review rejected a weaker first attempt:

    * *attempt identity is content identity* — the discriminator is the cleaned
      artifact each destination row records, not `raw_file_id`. Counting by raw
      file cannot distinguish a committed load from a superseded one, so a
      re-cleaned file whose new attempt crashed pre-commit would have been
      finalized `OK` against its previous generation's rows;
    * *provenance is per strategy* — `telematics_reports.*` carries
      `_raw_file_id`/`_source_artifact_id`, while
      `telematics_reports."Alpha_GPS_Baza_LOG"` carries `raw_file_id`/
      `cleaned_artifact_id` and no underscore-prefixed column at all. 44
      production files load through the latter;
    * *liveness is a lock, not a clock* — Stage 3 takes a per-raw-file session
      advisory lock, because `TimeoutStartSec=6h` and unbounded standalone
      invocation mean no wall-clock threshold can prove no owner is alive.

  Postprocessor re-offer is gated on a declared `recovery_safety` per registry
  entry: `report_207_speeding_migration` is **not** convergent — it increments
  counters and is safe only because its `migrated_to_client_db` marker and the
  increment are one statement — so an undeclared postprocessor is never
  re-offered. Durable `ERROR` retryability is carried in a parseable
  `stage3_error` marker, so a deterministic failure is not retried at 06:00 and
  20:00 forever. No schema change was required. Committed on
  `feat/workflow-b-autonomous-readiness`; **not released and not activated** —
  production continues to run the previous release until a separate authorized
  release operation says otherwise.
* ~~**Structured evidence on failed Workflow B runs.**~~ **Closed** by the
  Workflow B alert-hardening slice (§5.7). `run()` persists the terminal payload
  on both paths, `_invoke_stage` retains exception type, message and traceback,
  and `WorkflowBOrchestrationError` carries sanitized incident-shaped evidence
  into the alert. Implemented on `feat/workflow-b-autonomous-readiness`; not yet
  released.
* **GPS-baza completeness protection.** `_replace_alpha_gps_rows` deletes all rows
  then inserts the parsed workbook, guarded only by `empty_result`.
* ~~**Source/report freshness monitoring.**~~ **WITHDRAWN — not a requirement**
  (owner decision, 2026-08-21; §5.10). Workflow B is an autonomous *mailbox
  ingestion* pipeline and is not responsible for predicting whether a given
  client or report should have sent an email. **Absence of a new report email is
  not, by itself, a Workflow B failure**, so `SUCCEEDED_NO_WORK` being
  indistinguishable from a quiet sender is the intended contract, not a gap. No
  per-report expected-arrival schedule, weekly deadline, `max_age_days`,
  `SOURCE_REPORT_MISSING` code or holiday calendar is to be introduced. What
  replaced it is a signal over the inputs that *did* arrive — §5.10.
* **BRAVO person-path parity with ALPHA.** Missing mapping-coverage gate (57.6 % of
  trips currently dropped unmapped), private trips counted in scoring, and no
  ranked-snapshot validation.
* **Bounded IMAP/SMTP timeout and retry hardening.** `imaplib.IMAP4_SSL` is opened
  without `timeout=`; eco email sends have exactly one attempt and no terminal
  retry state.
* **BRAVO00016 `trips_sync` has an unrecovered gap.** The weekly 2026-08-03 fire
  failed with `TelematicsProviderSafetyError` (pagination loop guard) and was never
  re-run, so that client's last successful sync is 2026-07-27. Surfaced by the
  watchdog as `EXPECTED_FAILED`, which deliberately does not alert because
  `JOB_TERMINAL_FAILURE` owns that cause. Needs a recovery run for the missing
  window, then a decision on whether a *persistently* failed weekly schedule
  deserves escalation beyond the original terminal-failure alert.
* **No general-purpose incident-resolution mechanism.** The only writer of
  `state='resolved'` is `ops/execution_watchdog.py`, scoped to its own three
  incident codes and to the subject's own `open_incident_fingerprints`. Nothing
  can close an incident raised by any other component, so the
  `DEPLOYMENT_ALERT_CHANNEL_TEST` incident from the 2026-08-09 alert proof, and
  the two `ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT` incidents, remain `open`
  indefinitely. Harmless today — a reminder email requires a *re-report* of the
  same fingerprint, which never happens for a one-shot incident — but
  `inspect_suspected_bugs.py --open` accumulates rows no operator action can
  clear. Needs a documented, fingerprint-scoped resolve path; deliberately not
  improvised during deployment.
* **Portal / export worker.** `log-platform-api.service` (port 8001) and
  `database-export-worker.service` are both enabled and, re-verified read-only on
  **2026-08-15**, both `active (running)` — correcting an earlier entry here that
  recorded them as dead. Both are long-lived processes executing the Workflow A
  development checkout directly, so they hold whatever module images were on disk
  when they started and are not covered by the release boundary. Decide whether
  they are supported services or should be retired; until then, treat "which
  commit is this process running?" as unanswerable for them.

### 5.5 Open Codex MEDIUM findings — `REMAINING`

Deliberately not addressed in the remediation pass; none of them blocks
deployment, and each is small and independently testable.

* **Partial SMTP recipient refusal can be recorded as full success.**
  `send_html_email` treats a non-raising `smtplib` call as delivered even when the
  server refused some envelope recipients.
* **Numeric fingerprint normalisation may merge distinct errors.**
  `error_signature()` replaces every bare number with `<n>`, so two genuinely
  different failures whose messages differ only numerically (e.g. distinct HTTP
  status codes) collapse into one incident. The anti-storm value currently
  outweighs this; revisit if a merged incident is ever observed.
* **Workflow B watchdog attribution can accept a manual run.**
  `load_systemd_run()` matches on `source` and start time because timer-driven
  runs record `trigger='MANUAL'`; a manual run inside the window can therefore
  satisfy a scheduled fire.
* **Runner coverage is narrower than the prose implies.** The `ops/runner.py`
  hook catches exceptions that escape `run()`. A job that catches its own
  exception and returns normally, or one invoked outside the runner, is not
  covered.
* **Backup timestamp parsing assumes UTC.** `ops/backup.sh` stamps with local
  `date +%Y%m%d_%H%M%S` while `backup_retention.parse_timestamp()` reads it as
  UTC. In Europe/Warsaw that shifts age by 1–2 hours — immaterial against a
  14-day window and a verified floor, but wrong, and it should be made explicit
  before the retention window is ever shortened to hours.

### 5.4 Verified-current facts (2026-08-09)

Re-confirmed read-only against the production host and database at the end of the
implementation milestone. These are `VERIFIED_CURRENT`, not carried-over audit
numbers.

* Repository and deployed runtime are **aligned**: host jobs execute the working
  tree directly via `.venv/bin/python`, and the API container bind-mounts `api/`.
* Workflow A's coverage state machine, `uq_run_history_schedule_fire`, provider
  pagination safety and dispatcher advisory locking are sound and were **not**
  modified by this milestone.
* Backups are created and verified nightly (`status: VALID`); only *retention* was
  missing. `backups/` holds 242 GB across 44 PostgreSQL dumps, of which **35 are
  known-good sets** (nine older dumps predate the manifest contract and are
  therefore never retention anchors and never deleted).
* A dry run of `ops/backup_retention.py` over the live directory plans deletion of
  **17 expired sets (~90 GB)** and keeps 29, including the three-set safety floor.
  Nothing was deleted.
* Free space on `/` is **111 GB of 466 GB (23.8 %)**, down from 118 GB at the audit
  — consistent with continued unchecked backup growth. This is still above both
  configured warning floors (20 % and 60 GB), so the guard would currently report
  healthy; the 60 GB floor is roughly a week away at the observed growth rate.
* Superseded by the 2026-08-09 deployment (§5.4a): at the end of the
  implementation milestone `suspected_bug_email_outbox` contained **zero rows**,
  `suspected_bug_incidents` contained 2, and `ops_control` held only
  `environment_identity` / `environment_identity_promotion` — i.e. migration
  `059` was not applied and nothing was deployed.

### 5.4a Deployed production state (2026-08-09, post-deployment)

Verified directly against the production host and database during the authorized
Gate A deployment. `VERIFIED_CURRENT`.

| Component | State |
|---|---|
| Migration `059` | **applied** 12:10:42+02, one registration row, 57 → 58; `ops_control` now also holds `scheduler_heartbeat` and `watchdog_observation`; schema otherwise unchanged (47 → 49 tables) |
| Dispatcher heartbeat | `workflow_a.dispatcher` stamping every 5-minute tick; genuine, not fabricated |
| `SUSPECTED_BUG_ALERT_TO` | configured in `/etc/log-platform/runtime.env` (root:root 0600), together with the `AUTOMATION_SMTP_*` transport block, which had been absent there |
| Alert readiness | `ready: true`, `problems: []`, exit 0, measured in the worker's own unit environment |
| `suspected-bug-email-worker.timer` | installed, `enabled`, 5-minute cadence |
| Controlled alert proof | one `DEPLOYMENT_ALERT_CHANNEL_TEST` incident → outbox `sent`, `attempts=1/6`, claim released, **physically delivered** to the operator mailbox at 12:26. The first operator alert this platform has ever sent |
| Failure routing | `log-platform-unit-failure@.service` installed (`static`, no `[Install]`); `95-onfailure.conf` on `log-job@dispatcher`, `log-workflow-b`, `log-backup`, `log-platform-prune`. Live merged config expands `%i` to the real unit name (`log-workflow-b.service`, not the `%I`-corrupted `log/workflow/b.service`); handler's own `OnFailure=` is empty |
| `disk-space-monitor.timer` | installed, `enabled`, hourly; observation persisted under `watchdog_name=disk_space_monitor`, verdict `OK`, no incident |
| `execution-watchdog.timer` | installed, `enabled`, 15-minute cadence. Validated by a manual real scan **before** the timer was armed |
| Watchdog verdicts | 51 subjects: 44 `DISABLED`, 6 `OK`, 1 `EXPECTED_FAILED`; zero `MISSING`/`STALE`/`HEARTBEAT_LOST`/`NOT_YET_EXPECTED`; zero alerts; all five eligibility epochs equal their schedule's `updated_at` |
| **Backup retention** | **`DEPLOYED_VERIFIED`** after the §5.1a fix — see §5.4b |
| Free space on `/` | 119.0 GB of 466 GB (23.82 %) before retention; **215.5 GB (55 % used)** after |

### 5.4b First destructive retention (2026-08-09, Gate B)

One manual `--execute` run, authorized separately from the deployment itself and
never repeated. `backup-retention.timer` was disabled throughout.

**The independent off-filesystem recovery copy required by
`docs/11_operational_readiness.md` step 7(b) was explicitly waived by the
operator**, who accepted the increased recovery risk in writing. The host presents
only one filesystem (`/dev/mapper/ubuntu--vg-ubuntu--lv`), so any copy would have
shared the disk, the LVM volume and every failure mode that takes them out. What
stood in its place was the retention contract itself: the survivor floor
re-verified from disk under the held lock immediately before any `unlink()`, and
verified again afterwards. **Do not treat this waiver as precedent** — it applied
to one reviewed plan on one day.

Preconditions checked immediately before execution: timer disabled/inactive, no
backup or restore running, lock unheld, no retention process alive, next nightly
backup 12 h away. A **fresh** plan was generated through the identity bootstrap
and compared field-by-field against the reviewed one: protected survivors,
deletion stamps, keep set, reclaimable bytes, identity, anchor count and rejected
list all **identical** — only the cutoff timestamp advanced, and no candidate
crossed the 14-day boundary.

| | |
|---|---|
| Attested identity | `production` / `logdb` / `52517750-7438-4558-8490-2736ae4cc629` |
| Contract | `retention_days=14`, `keep_minimum=3`, `verified_anchor_count=3` |
| Deleted | **17 sets, 51 files, 96 592 608 559 B** — exactly the authorized list, no extra file |
| Survivors | `20260807_030000`, `20260808_030000`, `20260809_030000` — `post_delete_verification` all `verified: true` under the identity-pinned verifier |
| Untouched | all **11** manifest-less sets (never anchors, never candidates) |
| Result | `ok: true`, `operator_action_required: false`, `errors: []` |
| After | 29 sets, 93 files, 163 012 946 939 B; free on `/` 215 549 734 912 B (55 % used) |
| Durations | dry-run 4m49s, survivor verification 5m01s — against `TimeoutStartSec=1800` |

`backup-retention.timer` was then enabled **and** started — `enable`, never
`enable --now`. Starting it cannot fire the job: the timer carries only
`OnCalendar=*-*-* 04:15:00` with no `OnBootSec=`/`OnUnitActiveSec=`, and
`Persistent=false`. Confirmed after activation: timer enabled+active, next fire
**2026-08-10 04:15:00 CEST**, `backup-retention.service` still `inactive` with an
**empty `ExecMainStartTimestamp`** — the unit has never executed as a unit — and
no further backup was deleted.

One `BACKUP_RETENTION_FAILED` incident (`0d761c54…`, `open`, occurrence_count 1,
2026-08-09 12:52:44+02) is **deliberately preserved**. It was raised by the
§5.1a identity-bootstrap failure ~99 minutes *before* the destructive run, and
delivered to the operator mailbox at 12:56. It is the platform's first genuine,
non-synthetic operator alert: it detected its own broken retention and said so.
It stays open because no general-purpose incident-resolution mechanism exists
(§5.3); it will not re-alert unless the same fingerprint recurs.

Two production conditions the new monitoring surfaced, neither caused by the
deployment and neither addressed by it:

* `workflow_a:BRAVO00016:trips_sync` is `EXPECTED_FAILED`. The weekly 2026-08-03
  fire failed with `TelematicsProviderSafetyError` (pagination loop guard) and does
  not appear to have been re-run, so that client's last successful `trips_sync`
  is 2026-07-27. `EXPECTED_FAILED` deliberately does not alert —
  `JOB_TERMINAL_FAILURE` owns that cause.
* The two pre-existing `ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT` incidents are `open`
  with **no outbox rows**: they were raised while `recipients_not_configured`
  suppressed delivery, and will not retroactively alert.

---

### 5.6 Docker API execution trust boundary — `DEPLOYED_VERIFIED` (2026-08-09)

A read-only production investigation on **2026-08-09** established that the `api`
container executed user-writable checkout code as container root: read-write bind
of `api/` onto `/app`, UID 0 on a rootful daemon, no userns remapping, full
default capabilities, `NoNewPrivs=0`. The image's baked `/app` was a stale
partial snapshot shadowed by the bind, so the image digest attested nothing about
executed code. This blocked authorization of the pending credential-rotation
batch, whose helper model depends on being able to trust what a privileged
recreation starts.

**Repository implementation.**

| Artifact | Change |
|---|---|
| `docker-compose.yml` | Base = the only production definition. `api`: no source bind, explicit `image: log-platform-api:latest`, `user: "1000:1000"`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`. Postgres/MinIO untouched; publication still `127.0.0.1:8000`. |
| `docker-compose.dev.yml` | New. The live `./api:/app` bind, opt-in only. Never auto-loaded, because Compose auto-loads only `docker-compose.override.yml` / `compose.override.yaml` and this repository does not use those names. |
| `api/Dockerfile` | Bakes the context into `/app`; creates and switches to a non-root `1000:1000`; leaves source root-owned and non-writable; normalizes modes, without which the four `0600` `eco_driving_explorer/admin_*.py` modules would fail their unguarded imports. |
| `api/.dockerignore` | New. Excludes bytecode caches (including the stale root-owned `.pyc` in the checkout), test/type caches, virtualenvs, `.env*`, VCS/editor noise. |
| `ops/runtime_identity_readiness.py`, `ops/provision_runtime_environment_identity.py` | Recorded refresh action and the Compose provenance probe pin `-f docker-compose.yml`. |
| `ops/tests_manual/test_docker_api_execution_boundary.py` | New. Hermetic static guard against the bind returning, root returning, capabilities returning, missing `no-new-privileges`, an auto-loadable override file appearing, and refresh strings drifting off the production Compose file. |

**Configuration verification.** Verified statically and against the real Compose
resolver: the production resolution has no volumes, `user 1000:1000`,
`cap_drop [ALL]`, `no-new-privileges:true`, `127.0.0.1:8000`; a bare
`docker compose config` is byte-identical to the explicit base; the development
overlay restores the bind.

**Candidate verification.** Completed successfully before production deployment.
The image was built from this checkpoint to a distinct tag,
`log-platform-api:candidate-151bd32`, and verified without touching the production
port. Re-checked immediately before the deployment gate opened: that tag still
resolved to the exact immutable ID below, with `User 1000:1000`, `WorkingDir
/app` and the expected `uvicorn` command; the image was never rebuilt and never
re-pulled between verification and deployment.

**Production deployment — 2026-08-09.** Deployed image, and the only durable
runtime attestation:

```
sha256:c718529f89ad2a822f5f014de6f8cdafa6db2f5227637c8fa4176757f7a6df46
```

`log-platform-api:latest` is a mutable pointer that merely happens to resolve to
that ID today; it is not evidence. Verify the image **ID** on the running
container, never the tag.

The deployment was exactly one operation —
`docker compose -f docker-compose.yml up -d --no-deps --force-recreate --no-build --pull never api`
(return status 0) — and recreated **only** the Docker API service. Verified on the
new container: image ID equal to the candidate above; `Mounts: []`, so no
application-source bind and no host mount at all; UID/GID `1000:1000`;
`CapInh/CapPrm/CapEff/CapBnd/CapAmb` all zero; `NoNewPrivs=1`; `Privileged=false`;
published on `127.0.0.1:8000`; `docker diff` zero entries. `/health` 200,
authenticated read 200, unauthenticated 401 and invalid token 403, clean startup
with no traceback. The `/openapi.json` route set is byte-identical to the
pre-deployment baseline (107 routes, 7 eco-driving routes) — and because the
baseline executed the checkout through the old bind, that identity is what proves
the image carries exactly this checkpoint's source.

Component isolation held: PostgreSQL and MinIO kept their container IDs, start
timestamps and restart counts, and the native `log-platform-api.service` kept its
MainPID, InvocationID and NRestarts, still answering 200 on `127.0.0.1:8001`.

Operational note for the next recreation: the project still contains a stale
one-off container `log-platform-api-recovery` (exited, `oneoff=True`, service
`api`). Compose emits an orphan warning for it, and `docker compose --dry-run`
**falsely** reports it would be started — reproduced in an isolated project, the
real run leaves it untouched. Do not treat that dry-run line as a hazard, and do
not "fix" it with `--remove-orphans`; it is retained deliberately.

**Rollback — `ROLLBACK_RETAINED`, retirement is a separate operator decision.**
A valid rollback is **both** halves, never the image alone, because the old
image's baked `/app` is a six-month-old snapshot that only worked because the bind
shadowed it:

| Half | Retained as |
|---|---|
| Previous immutable image | `sha256:57e120445980…`, tagged `log-platform-api:rollback-pre-immutable-57e120445980` |
| Previous bind-based deployment definition | `tmp/api_execution_boundary_rollback/docker-compose.rollback-api.yml` (gitignored, never staged, no secret values — credentials stay `${VAR}` interpolation) |

The definition was resolved and its API-only, no-build, no-pull scope proven
before the deployment, and re-verified after it. Neither half may be deleted,
pruned, retagged or edited until the operator explicitly accepts the deployment.

**Accepted trust-model decision.** `logplatform` remains a trusted
privileged operator and keeps `docker` group membership, which is itself
host-root-equivalent. Reducing that privilege is a separate future hardening item
and is explicitly **not** a prerequisite for the credential-rotation batch. See
`docs/06_security.md` → *Docker API execution trust boundary*.

**Credential-rotation prerequisite — `CLEARED`.** The execution-boundary defect
that blocked authorization of the credential-rotation batch is closed in
production. That batch has **not** resumed: no credential was rotated and neither
`API_READ_TOKEN` nor `API_WRITE_TOKEN` was changed by this work. Clearing the
prerequisite is not progress on rotation itself; the remaining batch findings are
still open and need their own authorized task.

**Deferred, evidence-backed:** `read_only: true` with an explicit `/tmp` tmpfs.
`docker diff` returned zero entries on the old container over ten days, and again
on the new hardened container immediately after deployment; the only runtime write
path in `api/` is one `tempfile.NamedTemporaryFile`. Neither window demonstrably
exercised the artifact-upload path, so this stays gated on observing the write set
across a real upload rather than assuming it.

### 5.7 Workflow B alert hardening — `DEPLOYED_VERIFIED` (2026-08-16)

First slice of the Workflow B autonomous-readiness programme: `P0-A`, `P0-F` and
`P1-H` from the 2026-08-15 readiness audit. `P0-C`, `P0-D` and `P0-G` landed
afterwards and are recorded in §5.9. `P0-B` source freshness is **withdrawn**
(§5.10) and `P0-E` Stage 3 recovery is **released and active** in production
release `d69fc5e94e78`; neither is open.

**Production state.** `B1` + `B1-R` are active on release `0e2ad513cff9`
(commit `0e2ad513cff916e105272ec0df5218f1729fdfc9`), activated
`2026-08-15T22:33:42Z`; previous release `b682df90c958`. Both sidecars resolve
the release through `/usr/local/bin/log-ops-runner.sh`, the
`alerting.email_worker` heartbeat is live and advancing one beat per fire, and
the watchdog carries 53 subjects with `heartbeat:alerting_email_worker` and
`alert_delivery:alert_delivery` both `OK`. The `P0-A` drop-ins are effective on
`log-workflow-b.service` and `log-job@dispatcher.service`.

One piece of `P0-A` evidence is still outstanding and is observational only:
`RICH_JOB_ALERT_NATURAL_PROOF_PENDING`. The acceptance condition is a genuine
job-originated incident persisting `email_decision='enqueued'` instead of
`'suppressed' / 'recipients_not_configured'`. The pre-`P0-A` baseline in
`public.suspected_bug_occurrences` is 55 suppressed for
`recipients_not_configured` against 3 enqueued — and those 3 came from the
alert-delivery units, which already carried the EnvironmentFiles. No incident
may be manufactured to close it.

**Programme workspace.** This work lives on the dedicated branch
`feat/workflow-b-autonomous-readiness` in the persistent worktree
`ops/log-platform-worktrees/workflow-b-autonomous-readiness`, kept separate from
the primary worktree so it cannot disturb concurrent Workflow A work. Subsequent
Workflow B hardening tasks continue there rather than creating a new workspace.

| Defect | Change |
|---|---|
| `P0-A` job incidents email-suppressed | `EnvironmentFile=/etc/log-platform/runtime.env` + `environment-identity.env` on `log-workflow-b.service`, plus retrofit drop-ins `90-runtime-environment.conf` for `log-workflow-b` and `log-job@dispatcher`. Configuration suppression is now classified separately from throttling (`CONFIGURATION_SUPPRESSION_REASONS`) and logged as `operational_alert_delivery_not_configured` when it occurs. |
| `P0-F` silent delivery failure | The worker stamps `ops_control.scheduler_heartbeat` under `alerting.email_worker` on every batch, and exits `3` on a dead-letter transition so the unit fails visibly. The watchdog gains a heartbeat subject for worker liveness and an `alert_delivery` subject raising `ALERT_DELIVERY_FAILED` for outstanding dead letters or an unrecovered backlog. |
| `P1-H` lost failure evidence | `_invoke_stage` binds the exception; `run()` persists the terminal payload on the failure path; `WorkflowBOrchestrationError` carries `operational_incident_details`, which `report_job_terminal_failure` lifts into the incident — including `durable_writes_committed`, the fact that decides whether a blind re-run is safe. |

**Anti-recursion is preserved.** The worker still neither imports the alert
reporter nor carries `OnFailure=`; its signals are an exit code and a heartbeat
it does not read. The watchdog — a separate unit and process — is what reports
on the mail path, which is why `alerting.delivery` and `alerting.email_worker`
are deliberately *not* in `SELF_ALERTING_COMPONENTS`.

**Delivery semantics unchanged.** Outbox uniqueness, notification keys,
deduplication, cooldown, reminders, leases, claim tokens, `FOR UPDATE SKIP
LOCKED` and bounded retry are untouched; `test_suspected_bug_outbox_postgres.py`
(15 tests) passes unmodified.

**Verification.** `ops/tests_manual/test_workflow_b_alert_hardening.py` (23
assertions, 4 of them against a disposable PostgreSQL 16 with an in-memory SMTP
transport) plus the unmodified operational-alert, suspected-bug, outbox,
watchdog, disk-monitor, systemd-contract, orchestrator and stage-contract suites.
No real email was sent at any point.

**Independent review.** Codex returned
`B1_REVIEW_APPROVED_WITH_NONBLOCKING_FINDINGS`: no blocking and no important
findings, and all three mandatory design decisions — dead-letter exit semantics,
the self-alerting reporter/topic boundary, and incident fingerprint identity —
approved as `CURRENT_DESIGN_CORRECT`. Two non-blocking findings were closed
afterwards: `unit_environment()` had `Environment=` beating `EnvironmentFile=`,
which is the inverse of systemd's documented and empirically confirmed
precedence; and the originating stage traceback was captured only in memory, so
it is now projected into bounded `path:line:function` frames and chained as
`__cause__`. Neither touched the reviewed dead-letter, self-alerting or
fingerprint semantics.

#### Pending controlled activation — order matters

The code half is live-safe on its own. Two steps remain, and they are **not
interchangeable**:

**Step 0 — prepare the release, then install the operational launcher (`B1-R`)
from the prepared tree.** Until `/usr/local/bin/log-ops-runner.sh` is installed
and the two sidecar units point at it, releasing B1 delivers only half of it.
`suspected-bug-email-worker.service` and `execution-watchdog.service` execute the
*Workflow A development checkout*, so the worker heartbeat, the dead-letter exit
and the new watchdog subjects would not arrive with a release activation at all.
See §5.8.

**`PREPARED_RELEASE_DIR` and `current` intentionally differ here, and the whole
step depends on it.** `current` still names the baseline release — that is
deliberate, so the sidecars keep running baseline code from an immutable tree
while the unit files change, leaving the whole B1 delivery to one later pointer
move. But the launcher and the rewritten units are **B1-R artifacts: they do not
exist in the baseline release**, so `current/ops/systemd/proposed/…` cannot be
the installation source. Install from the prepared release directory, addressed
by its own release id:

```bash
SOURCE_REPO=/opt/log-platform          # tooling only
RELEASE_ROOT=/opt/log-platform-release

# 1. Prepare. `prepare` reads the commit object, never the working tree, so the
#    source repository being dirty does not contaminate the release. Take the
#    release id from prepare's own output — never assume it.
"${SOURCE_REPO}/.venv/bin/python" "${SOURCE_REPO}/ops/manage_release.py" prepare \
  --commit <B1_COMMIT_SHA> \
  --env-file "${SOURCE_REPO}/.env" \
  --venv "${SOURCE_REPO}/.venv"
B1_RELEASE_ID=<release_id printed by prepare>
PREPARED_RELEASE_DIR="${RELEASE_ROOT}/releases/${B1_RELEASE_ID}"

# 2. Confirm the prepared tree is the one being installed from, and that it is
#    NOT what current points at yet.
test -d "${PREPARED_RELEASE_DIR}"
readlink "${RELEASE_ROOT}/current"          # must still name the BASELINE release

# 3. Install the B1-R artifacts from the prepared, immutable tree.
sudo install -m 0755 -o root -g root -D \
  "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/log-ops-runner.sh" \
  /usr/local/bin/log-ops-runner.sh
sudo install -m 0644 -o root -g root -D \
  "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/suspected-bug-email-worker.service" \
  /etc/systemd/system/suspected-bug-email-worker.service
sudo install -m 0644 -o root -g root -D \
  "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/execution-watchdog.service" \
  /etc/systemd/system/execution-watchdog.service
sudo systemctl daemon-reload
```

The development worktree is **not** the installation source. A prepared release
is verified byte-for-byte against its commit; a worktree is mutable and routinely
dirty, and using it here would reintroduce exactly the provenance gap `B1-R`
exists to close.

Acceptance for Step 0, before any pointer moves: the rewritten sidecars must
resolve **baseline** through the unchanged `current`. The next natural worker and
watchdog fires are the check — each logs its resolved release, and it must be the
baseline id:

```bash
journalctl -u suspected-bug-email-worker.service -n 20 --no-pager | grep log-ops-runner
journalctl -u execution-watchdog.service         -n 20 --no-pager | grep log-ops-runner
```

Both must print `release=<baseline id>`, and both units must remain `success`. If
either instead fails with `RELEASE_POINTER_INVALID` (exit `90`) the launcher is
refusing the pointer and Step 1 must not proceed.

**Step 1 — activate the release, then obtain the first worker heartbeat, then
rely on the heartbeat expectation.** `alerting_email_worker` is evaluated like
any heartbeat subject: a component that has never beaten is classified
`HEARTBEAT_LOST`, not "not yet started" — with `last_beat_at IS NULL` the
30-minute grace does not apply at all. The worker fires every ~5 minutes and the
watchdog scans every ~15, so the new expectation becoming visible *before* the
new worker code has run once produces a temporary false
`SCHEDULER_HEARTBEAT_LOST` for a perfectly healthy worker.

After `B1-R` both halves arrive from the same pointer move, so they are visible
in the same instant and cannot be ordered relative to each other. The race is
closed by making the worker run first rather than by ordering the deployment,
and it is deliberately **not** solved with bootstrap-window code: a "never seen
yet, wait a while" grace would weaken the same subject's ability to detect a
genuinely dead worker, which is the condition it exists for.

The failure mode is benign and self-healing — one false incident and one email,
resolved automatically by the next scan once the heartbeat exists — but it is
avoidable:

```
1. move the pointer to the already-prepared release:
     ops/manage_release.py activate --release "${B1_RELEASE_ID}"             # dry run
     ops/manage_release.py activate --release "${B1_RELEASE_ID}" --execute
   PREPARED_RELEASE_DIR and current name the same tree from here on.
2. trigger one worker execution immediately (under authorization):
     sudo systemctl start suspected-bug-email-worker.service
   Its blast radius is identical to an ordinary 5-minute fire.
3. confirm the first row BEFORE the next watchdog scan:
     SELECT component, last_beat_at, beat_count
       FROM ops_control.scheduler_heartbeat
      WHERE component = 'alerting.email_worker';
4. only then is the alerting_email_worker expectation trustworthy
```

Without step 2 the same result can be had by timing: activate immediately after
a watchdog fire, so at least two worker fires precede the next scan.
`systemctl list-timers` gives both next-fire times exactly.

**Step 2 — install the environment drop-ins.** `P0-A` is not effective until
this lands. These are `B1` artifacts, absent from the baseline release, so they
too are installed from the prepared tree — which by now is also what `current`
names:

```bash
sudo install -m 0644 -o root -g root -D \
  "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/log-workflow-b.service.d/90-runtime-environment.conf" \
  /etc/systemd/system/log-workflow-b.service.d/90-runtime-environment.conf
sudo install -m 0644 -o root -g root -D \
  "${PREPARED_RELEASE_DIR}/ops/systemd/proposed/log-job@dispatcher.service.d/90-runtime-environment.conf" \
  /etc/systemd/system/log-job@dispatcher.service.d/90-runtime-environment.conf
sudo systemctl daemon-reload
systemctl show -p EnvironmentFiles log-workflow-b.service   # must list runtime.env
```

**Rollback.** `ops/manage_release.py rollback --execute` returns `current` to the
baseline release. The sidecars are `Type=oneshot`, so the next timer fire
resolves the pointer again and runs baseline code with no unit edit and no
`daemon-reload`; the baseline expectation file has no `alerting_email_worker`
subject, so the heartbeat row left behind is simply unread. Reverting `B1-R`
itself means reinstalling the two previous unit files and removing the launcher.

Acceptance after activation: the next Workflow B terminal failure produces a
`JOB_TERMINAL_FAILURE` occurrence with `email_decision='enqueued'` and a
`suspected_bug_email_outbox` row naming the workflow, stage, client and run, and
— for an unexpected stage error — the originating frame. Until then the
`OnFailure=` unit-level alert remains the delivered signal, as it was before.

### 5.8 Alerting runtime execution-surface isolation (`B1-R`) — `DEPLOYED_VERIFIED` (2026-08-16)

A read-only activation preflight for §5.7 established that production had **two**
code-delivery paths, not one. `log-workflow-b.service` and
`log-job@dispatcher.service` resolve `release/current` through
`/usr/local/bin/log-job-runner.sh`. `suspected-bug-email-worker.service`,
`execution-watchdog.service` and `log-platform-unit-failure@.service` did not:
they named `/opt/log-platform/.venv/bin/python` with
`PYTHONPATH` rooted in the **Workflow A development worktree**, and
`ops/execution_watchdog.py` derives `DEFAULT_EXPECTATIONS_PATH` from its own
`__file__`, so the expectation set came from that checkout too.

Consequence: `P0-F` — the worker heartbeat, the dead-letter exit and the
`alerting_email_worker` / `alert_delivery` subjects — could not be delivered by
any release activation. The only route was to fast-forward the Workflow A
worktree, i.e. to use a development checkout as a deployment mechanism.

| Component | Binding | Why |
|---|---|---|
| `suspected-bug-email-worker.service` | **release** via `log-ops-runner.sh` | stamps the heartbeat the watchdog asserts; must be the same version as the subject that reads it |
| `execution-watchdog.service` | **release** via `log-ops-runner.sh` | supplies its own `ops/watchdog_expectations.json`, and shares `latest_scheduled_fire_local` with the release-bound dispatcher |
| `log-platform-unit-failure@.service` | **host**, deliberately | last-resort reporter; a broken `current` fails every release-bound unit at once and a release-bound handler could not start to say so |
| `disk-space-monitor`, `backup-retention`, `log-backup`, `log-platform-prune`, `database-export-worker`, `log-platform-api` | host, unchanged | outside B1; none shares a version-sensitive contract with the alerting subjects, and repointing them is a separate decision |

The host-bound handler's version boundary is bounded rather than accidental:
`ops/systemd_failure_adapter.py` imports exactly four names from repository code
(`INCIDENT_UNIT_FAILURE`, `is_self_alerting`, `report_operational_failure`,
`utcnow`, all from `ops.operational_alert`) and touches no outbox, watchdog or
fingerprint internals. `ops/tests_manual/test_release_runtime_isolation.py`
parses both release trees with `ast` and asserts every one of those names, and
every keyword the adapter passes, exists and is accepted in each.

**Why a second launcher.** `log-job-runner.sh` is a job entry point: it execs
`ops/runner.py`, which opens a `public.runs` lifecycle. Routing the sidecars
through it would buy release resolution at the price of writing run rows into the
dataset the watchdog reads. `log-ops-runner.sh` reuses the pointer resolution,
the release-shape assertion and the `PYTHONPYCACHEPREFIX` redirect, then execs
`python -m <module>`. It takes neither the execution barrier nor the cutover
fence — both gate *job* execution, and the observability plane must survive a
maintenance window that stops jobs. It requires `.venv` but deliberately **not**
`.env`: these units take configuration from `EnvironmentFile=` only.

**P2-L is not a blocker.** Code provenance, dependency environment and secret
source are separable. `.venv` is a plain virtualenv with no `.pth` and no
editable install, so it contributes site-packages and never puts the repository
on `sys.path`; no module in the sidecars' import graph loads a dotenv file. The
launcher sets `PYTHONPATH` (never prepends) because `ops` is a namespace package,
and it must set it in-process because `/etc/log-platform/runtime.env` exports a
development-tree `PYTHONPATH` and `EnvironmentFile=` overrides `Environment=`.

**Verification.** `ops/tests_manual/test_release_runtime_isolation.py` builds
disposable release roots for `b682df90c958` and `1bec254d67ac` with
`git archive`, seals them read-only, and drives the real launcher against a
temporary pointer: baseline resolves baseline code, the candidate resolves B1's
worker/watchdog/alert/api modules and B1's expectation file, and
baseline → candidate → baseline restores baseline with no unit edit. A missing,
dangling, development-tree or malformed target is refused with exit `90`/`2`. The
production pointer is never read or moved. `systemd-analyze verify` returns clean
for both rewritten units.

### 5.9 Workflow B autonomous routing (`P0-C`, `P0-D`, `P0-G`) — `DEPLOYED_VERIFIED` (verified on the first natural scheduled run, 2026-08-16 20:00 Europe/Warsaw)

Three defects with one shape: Workflow B could report operational success while
the work it exists to do had not happened. All three are production-active; the
first natural scheduled run (2026-08-16 20:00, run
`6017ea3c-793c-40ac-8f7c-6ec289fac382`) confirmed them.

#### `P0-D` — lock contention could satisfy a scheduled cycle

`run_workflow_b_batch()` returned `SKIPPED_LOCKED` with a plain `return`, so
`run_context` recorded **SUCCESS**. `evaluate_systemd_subject()` classifies a
SUCCESS run for a scheduled fire as `OK`, so a cycle that entered no stage at all
satisfied the schedule. Workflow B fires only at 06:00 and 20:00, so nothing
retried it for another 10-14 hours.

The corrected contract splits the outcome by who lost the lock, because that is
what decides whether anything is wrong:

| mode | outcome | run status | operator |
|---|---|---|---|
| `manual_diagnostic` | `SKIPPED_LOCKED` | SUCCESS, exit 0 | silent — an ad-hoc run standing aside for the scheduled owner is the intended ownership model |
| `scheduled` | `BLOCKED_CONCURRENT_EXECUTION` | **FAILED** | existing terminal-failure incident |

The scheduled case raises `WorkflowBOrchestrationError`, so the durable fact the
watchdog reads is a FAILED run — classified `EXPECTED_FAILED`, never counted as a
satisfied fire. No new schema, no new alert channel and no watchdog change: the
`B1` terminal-failure path already carries it, and the incident states
`cycle_executed=false`, `durable_writes_committed=false` and the contended lock
namespace, which together say plainly that a manual re-run is safe.

`WorkflowBBatchResult.cycle_executed` is the new distinction and is derived from
`lock_acquired` rather than stored, so it cannot drift from it. `SUCCEEDED_NO_WORK`
now means a real cycle ran and found nothing — which still satisfies the schedule.

#### `P0-C` — Stage 2 outcomes owned by nobody

Stage 2 discovery re-picks a NORMALIZED file only when its state is unset,
`stage2_retryable = true`, or the historical `PENDING_REVIEW` / `stage2_exception`
shape. Stage 3 discovery consumes a file only when `stage2_status = 'OK'` **and**
it is routable — a client code, a report type and a cleaned artifact. Everything
else belongs to neither, and is reported exactly once, in the cycle that created
it, then never again.

Read-only production evidence, 2026-08-16: **55 files** are in that state, the
oldest since 2026-07-21 and the newest 2026-08-10, so the set is still growing.

| class | count | shape |
|---|---|---|
| `STRANDED_UNROUTABLE` | 52 | `stage2_status='OK'`, `report_112`, `client_code IS NULL` — Stage 2 reports success, Stage 3's `client_code IS NOT NULL` filter silently drops them |
| `STRANDED_AWAITING_REVIEW` | 3 | `PENDING_REVIEW` / `cleaning_not_implemented` (2, `eco_driving_driver`) and `low_detection_confidence` (1, `d105_2`) |

The 52 matter most because they defeat the audit's own framing: their Stage 2
result *is* the expected success state, and they are stranded anyway.

`stage2_unrouted_files()` is a read-only sweep run at the end of every autonomous
Stage 2 batch. It reprocesses nothing — silently retrying a file parked for human
review is exactly how a duplicate Stage 3 load would be created — and appends a
`review_required` item per unowned file, so `operator_action_required` keeps
asserting through `SUCCEEDED_WITH_REVIEW_ITEMS` on every cycle until a human
resolves it. Same durability idea as the `alert_delivery` subject in §5.7.

A file is left alone only when the batch item already present for it *owns* it
(`item_carries_operator_ownership`): a review item, a reconciliation outcome, or
a technical failure, which already raises `Stage2BatchError`. Presence of the
`raw_file_id` alone is not ownership. Stage 2 can process a file to
`stage2_status='OK'` with no `client_code` in the current batch — the 52-row
`report_112` shape above — and append it as an ordinary `SUCCEEDED_CREATED` with
`review_required=false`. Skipping it on id alone would let the cycle that
stranded the file report plain `SUCCEEDED` and defer operator visibility to the
next 06:00/20:00 fire, 10-14 hours later. The durable classification therefore
supersedes that item in place: same position, no duplicate id, and the file
stops counting as a Stage 2 success.

Its predicate is written as the literal complement of the two discovery
predicates (`STAGE2_REDISCOVERY_SQL`, `STAGE3_ROUTABLE_SQL`), and a PostgreSQL
test asserts the partition rather than a hand-written expectation of it —
comparing it against the live `_candidate_rows` and `_select_stage3_candidates`
queries, not only against the restated predicates. A
targeted operator run (`raw_file_ids` / `input_files` / `input_dir`) does not
reconcile: answering a scoped question with every unrelated stranded file in the
platform would bury its actual result.

**Deliberately out of scope:** a file with any `stage3_status` set — including
`'ERROR'`, which is *also* not an eligible state in
`_stage3_batch_status_eligible_sql` — is left to Stage 3's own recovery story
(`P0-E`). And no historical stranded file was repaired; that needs its own
authorization.

#### `P0-G` — policy discovered after the irreversible write

`_process_candidate()` marked Stage 3 started, loaded the artifact and
**committed rows into the client business database**, and only afterwards did
`_discover_postprocessor_plans()` resolve the per-client policy, raising
`missing_report_policy` → `BLOCKED_UNSUPPORTED_CONFIGURATION` → whole run
`FAILED_NON_RETRYABLE`. A missing configuration row therefore produced changed
customer data *and* a red run.

`_require_downstream_policy_configured()` now runs the identical resolution
before anything is marked, downloaded or written — imported from
`jobs.reports.workflow_b.trip_metrics_selector`, the orchestrator's own module,
so the pre-write gate and the post-load requirement cannot drift apart. Its
inputs are entirely static (a client code and a report type), which is why it can
be asked first. It blocks on both refusal shapes: the resolver raising, and a
resolution whose `error_category` is set.

Two placements are load-bearing:

* **before `_mark_stage3_started`** — that call stamps `'RUNNING'` and clears
  `stage3_finished_at`; a file blocked on configuration never entered Stage 3.
* **outside the `try`** — the handler calls `_mark_stage3_error`, which stamps
  `'ERROR'`, and `'ERROR'` is not an eligible state. A configuration block that
  stamped it would remove the file from batch discovery permanently, so
  configuring the missing policy would fix nothing without a manual
  force-reprocess. Writing no `stage3_status` at all is what makes the retry
  automatic.

The postprocessor itself deliberately does **not** move before the load commit:
it has its own transaction and external effects, and after the commit is where it
belongs. Only the static configuration question moved.

**Deliberate difference from §5.2 `P0-7`.** That entry proposed classifying a
missing policy as `SKIPPED`. This implements the stricter reading: the file is
`BLOCKED_OPERATOR_ACTION`, counted as non-retryable, so the cycle still cannot
report success — the same terminal severity as today, but with **no** business
data written and the file still eligible for a free retry. `SKIPPED` would let an
unconfigured client silently produce no data forever, which is the data hole
`P0-7` also objects to. Reconciling the two readings is an operator decision, not
a code one.

**What this changes for real clients.** The pre-write gate blocks exactly the
`(client, report_type)` combinations that already fail after the commit today:
`BRAVO00016`/`report_207`, `BRAVO00016`/`report_d105_2_ecodriving` and
`ALPHA00001`/`report_d105_2_ecodriving` — none of which has a
`workflow_b_control.report_type_client_load_policy` row. `ALPHA00001`/`report_207`
and `ALPHA00001`/`Alpha_GPS_Baza_LOG` are configured and are untouched. Files
already at `stage3_status='OK'` are not re-selected, so history is unaffected.

### 5.10 Workflow B autonomous mailbox contract — `DEPLOYED_VERIFIED` (released in `6279e168c628` on 2026-08-24; confirmed on natural production cycles, see §5.11)

Owner decision, and the correction that follows from it. Two things happened here:
a requirement was **withdrawn**, and the gap it had been standing in front of was
found and closed.

> **Review history.** Independent review of the first candidate found five
> blocking correctness defects; all five are now fixed and deterministically
> verified against a disposable PostgreSQL 16. The table is kept because each row
> names a failure mode that must not come back, and because the guarantees below
> read the same before and after the fix — only the implementation differed.
>
> | # | blocker | how it is closed |
> |---|---|---|
> | 1 | `stage3_status='OK'` was terminal proof for **every** incident, so a `postprocess` incident closed while postprocessing was still owed | resolution is stage-specific: `_LOAD_PROVABLE_STAGES` limits load evidence to `stage1`/`stage2`/`stage3`; a `postprocess` incident closes only on an observed postprocessor completion, or on the raw-file row being gone |
> | 2 | supersession closed the predecessor on the mere presence of a different current fingerprint | the incident table is re-read after creation and only replacements found **open** in that re-read may supersede; a deferred or failed replacement leaves the predecessor open |
> | 3 | the Stage 2 unrouted sweep read one fixed oldest-200 page, so a persistent backlog hid every newer input permanently | the sweep is complete keyset pagination over the whole qualifying set, bounded per page and by a 50-page resource backstop that logs when it fires |
> | 4 | the per-cycle incident bound was spent by *attempts*, so a leading fingerprint that never persists starved the ones behind it | the bound counts incidents **opened**; a failed attempt releases the slot and the next candidate is tried in the same cycle |
> | 5 | signalling wrote through the session holding the advisory lock, and cleanup could replace the real outcome | `_finish_cycle` opens its own connection; `_relinquish_lock_session` contains both the unlock and the close so neither can replace a determined result, and the session close relinquishes the lock even when the explicit unlock fails |
>
> The lock is **session-level** (`pg_try_advisory_lock`), so commit/rollback on a
> healthy connection never released it. Blocker 5 was failure containment and
> cleanup precedence, not lock lifetime.

#### The withdrawn requirement

`P0-B` was carried in §5.2 as *"source/report freshness monitoring — it needs a
per-report-type expected-arrival contract"*. That framing is now explicitly
superseded:

> Workflow B is an autonomous **mailbox ingestion and processing** pipeline. It
> is not responsible for predicting whether a specific client or report should
> have sent an email on Monday, every 7 days, or within 10 days. **Absence of a
> new report email is not, by itself, a Workflow B failure.**

Nothing of the proposed mechanism is to be built: no expected weekday per report,
no grace window per `(client, report_type)`, no `max_age_days`, no source-arrival
deadline, no stale-source subject, no `SOURCE_REPORT_MISSING` raised from the
absence of mail, no holiday calendar. `SUCCEEDED_NO_WORK` over a quiet mailbox is
the intended healthy outcome.

The effective requirement is now:

> Workflow B monitors **its own execution** and the **complete lifecycle of the
> reports that actually enter its mailbox-processing pipeline**. "Something is
> missing" means something needed to process an input that *has arrived* —
> client, report type, classification, load policy, columns, format,
> normalization, destination, downstream load, operator review.

The durable statement of the whole contract lives in `docs/05_jobs.md`
§ „Kontrakt operacyjny Workflow B (autonomiczny mailbox ingestion)". This entry
records only what changed and why.

#### What the audit found instead — `WORKFLOW_B_REVIEW_BACKLOG_SATURATED`

Re-proved read-only against production on **2026-08-21**, not carried over from
the 2026-08-15/16 audit:

| fact | evidence |
|---|---|
| every scheduled cycle since 2026-08-17 ends `SUCCEEDED_WITH_REVIEW_ITEMS` | `public.logs`, `jobs.reports.workflow_b.orchestrator`, 2026-08-18 → 2026-08-21 |
| durable unresolved inputs | **59** — 56 `report_112` `missing_client_code`, 2 `cleaning_not_implemented` (`eco_driving_driver`), 1 `low_detection_confidence` (`d105_2`) |
| the set is still growing | `report_112` arrives **4 files per week**, oldest 2026-05-18, newest **2026-08-17** |
| what the operator receives | **nothing.** `runs.status = SUCCESS`; the watchdog subject `systemd:workflow_b_orchestrator` is `OK` with `missed_fire_count = 0`; `suspected_bug_incidents` contains **no** incident code for the condition |
| every discovered input is accounted for | 164 `NORMALIZED` + 1 `DUPLICATE_CONTENT`; 105 `stage3_status='OK'`, 60 unset of which 59 are the sweep's set |

So the state model (`P0-C`) was correct and the *signal* was not. The run-level
`SUCCEEDED_WITH_REVIEW_ITEMS` is a permanently saturated aggregate: with 59 items
already outstanding, the 60th newly stranded input changes nothing an operator
can see. That is exactly the invariant this work exists to kill:

    ARRIVED INPUT -> NOT FULLY PROCESSED -> NO ADEQUATE ACTIONABLE SIGNAL

#### The correction — a durable incident per unresolved input

`jobs/reports/workflow_b/unresolved_inputs.py`. Granularity, not severity: the
cycle still finishes as `SUCCEEDED_WITH_REVIEW_ITEMS`, because turning every
review condition into a global hard failure would stop unrelated valid inputs
from being processed. Each unresolved input additionally gets its own
`WORKFLOW_B_INPUT_UNRESOLVED` incident, raised through the existing platform
`suspected_bug` boundary — no new schema, no new alert channel, no migration.

Three properties, and each is the answer to a specific way this could go wrong:

* **fingerprint = `(raw_file_id, stage, reason_code)`.** Two stranded files are
  two incidents, so a new one is `REASON_NEW` and is delivered while 59 others
  are open. An aggregate keyed on a count could not do this: `material_signature`
  buckets `affected_record_count` logarithmically, so 59 → 60 is not a material
  change at all.
* **report once, not once per scan.** An input whose incident is already open is
  not re-reported. Without this, re-reporting twice a day would cross the
  24-hour reminder interval once per incident per day and turn 59 open incidents
  into ~59 emails a day — a differently-shaped saturation.
* **positive, stage-appropriate resolution only.** An incident closes when the
  platform can prove *the condition that incident names* is no longer
  unresolved. `stage3_status='OK'`, `DUPLICATE_CONTENT` or a missing row prove
  the ingest/clean/load chain finished, and they close a `stage1`/`stage2`/
  `stage3` incident. They prove **nothing** about work owed after the load, so
  they may not close a `postprocess` one: that resolves only on an observed
  postprocessor completion (`SUCCEEDED` or `SKIPPED_ALREADY_COMPLETED`, and not
  while the item still asks for an operator) or on the row being gone. There is
  no durable postprocessor-completion record in the platform — plans are derived
  from the Stage 3 identities loaded *in the same cycle*, so once Stage 3 is `OK`
  the postprocessor is never re-offered — and no schema was added to invent one;
  missing or ambiguous evidence leaves the incident open, which is the correct
  failure direction. Supersession is ordered: when an input's blocking reason
  changes, the incident for the old reason is closed only after the new one is
  found **open** in a re-read of the incident table, so a replacement the bound
  deferred or one whose persistence failed leaves its predecessor in place. An
  input this cycle simply did not examine keeps its incident open.

The per-cycle intake is bounded by
`WORKFLOW_B_MAX_NEW_UNRESOLVED_INCIDENTS_PER_CYCLE` (default 20) so that first
activation against the existing 59-item backlog cannot enqueue an unbounded burst
into the outbox. What the bound defers is logged as an explicit WARNING — a
silent cap would read as full coverage when it is not. The bound counts incidents
**opened**, not attempts: it exists to keep the alert outbox from taking an
unbounded burst, which is a property of what persisted, and spending it on
attempts let a leading fingerprint whose persistence keeps failing hold the only
slot forever. A failed attempt is retried, logged (individually for the first
`MAX_LOGGED_REPORT_FAILURES`, then in aggregate) and releases the slot to the next
candidate in the same cycle.

**That bound governs *replayable* inputs only — after the Stage 2 refutation, the
Stage 3 class alone — and replayability is proved per event rather than assumed
from a stage name. See §5.11, which corrects the claim that everything this bound
defers is picked up by the next cycle, and §5.11.1 for why no Stage 2 event
qualifies.**

Signalling runs on every exit from the stage sequence, including the ones that
end in `WorkflowBOrchestrationError`: one input's failure is not a reason to stay
silent about another's. Its own failures are caught and logged, never allowed to
change the cycle's verdict.

#### Failure containment around the orchestration lock

`_finish_cycle` opens **its own** platform connection for signalling and closes
it in `finally`. Signalling writes — incidents, occurrences, outbox rows — and a
write that fails at connection level poisons the session it ran on; sharing the
lock session meant a signalling fault could break the one connection whose only
job is to hold `workflow_b.orchestrator.v1` until the cycle ends, and the failure
then surfaced from cleanup rather than from the place that contains it.

`_relinquish_lock_session` replaces the bare `unlock(); close()` in both
`run_workflow_b_batch` and `run_alpha_source_refresh_batch`. An exception raised
in `finally` *replaces* whatever the block was returning or raising, so both
steps are contained: a failing `pg_advisory_unlock` is logged and the connection
is closed anyway, and a failing close is swallowed. Because the lock is
session-level, closing the session is what actually relinquishes it — the
explicit unlock is the cheap legible release on the healthy path, not the
mechanism the guarantee rests on. A healthy outcome stays healthy, an original
`WorkflowBOrchestrationError` stays the surfaced exception, and the next cycle
can take the lock.

#### `_assert_mailbox_was_inspected` — the no-work invariant

`SUCCEEDED_NO_WORK` is the single most expensive claim in the workflow: it says
"we looked and there was nothing". `_finish_or_raise` now refuses to reach it
when Stage 1 completed without `mailbox_check_completed`, classifying the cycle
`FAILED_RETRYABLE` / `mailbox_check_not_completed` instead. Unreachable today —
every mailbox-access failure Stage 1 knows about (login, `SELECT`, `SEARCH`,
`FETCH`) raises `Stage1BatchError`, which the orchestrator already turns into a
failure rather than no-work — and asserted anyway, because a future Stage 1 that
returned a clean empty result without reaching `IMAP SEARCH` would be
indistinguishable from an empty mailbox forever.

#### Mailbox polling and recovery — `BOUNDED_AND_SAFE`

Audited, unchanged, now documented. The IMAP horizon is `SINCE now() - 30 days`
(`DEFAULT_SINCE_DAYS`); there is deliberately no unbounded history crawler.
Deduplication is `(account, mailbox, uidvalidity, uid)` in `ingest.imap_message`,
and that row is written inside a **per-message transaction** — a message whose
processing fails is rolled back, so its UID is not marked seen and the next cycle
re-picks it. A `UIDVALIDITY` change invalidates the dedup key and re-fetches the
horizon, which `sha256` content dedup absorbs. Sender filtering is off
(`DEFAULT_SENDER_FILTERS = ()`, and `IMAP_SENDER_FILTERS` is unset in the
production `.env`), so owner-sent reports are first-class input. The one
loss condition is an outage longer than 30 days, which the execution watchdog
detects within hours; it is recorded rather than engineered away.

#### The Stage 2 sweep — complete pagination, bounded pages

The reviewed candidate read one page of `UNROUTED_SWEEP_LIMIT = 200` rows ordered
`stage2_updated_at ASC` and stopped. Because the page is oldest-first and the
backlog is persistent, rows 201+ were **never inspected at all** — not on that
cycle and not on any cycle after it, so a newly arrived unresolved report got
neither a review item nor a per-input incident. Logging a WARNING reported the
cliff without removing it, and raising the page size only moves it:

    A PERSISTENT HISTORICAL BACKLOG MUST NEVER MAKE A NEWLY UNRESOLVED INPUT INVISIBLE

`reconcile_unrouted_stage2_files` now pages through the **whole** qualifying set
with keyset pagination on `(COALESCE(stage2_updated_at,'epoch'), id)` — the same
expression the `ORDER BY` uses, so pages neither overlap nor skip. The null-safe
`COALESCE` matters: `stage2_updated_at` is nullable, and a cursor cannot be
compared against a sort key that is sometimes NULL. `UNROUTED_SWEEP_LIMIT` is now
one page — one query, one page of memory — and `UNROUTED_SWEEP_MAX_PAGES = 50` is
a pure resource backstop two orders of magnitude above the real set. Reaching it
means unowned files went uninspected, so it is a WARNING naming the page size,
the page cap and the rows inspected; ordinary completion, including an empty set,
is silent. `reconcile_unrouted_stage2_files` returns `Stage2UnroutedSweep`
(`rows_inspected`, `pages_read`, `truncated`) rather than a bare row count,
because "how many rows came back" can no longer distinguish coverage from a cut.

#### Verification

Both suites provision a **disposable PostgreSQL 16 they start and remove
themselves**; neither accepts a DSN from the environment, so neither can be
pointed at production, and neither can print `OK` with its database half skipped
— an unavailable instance now exits `VERIFICATION BLOCKED` instead. Executed
2026-08-21 against PostgreSQL 16.11:

| suite | checks | database half |
|---|---|---|
| `ops/tests_manual/test_workflow_b_unresolved_input_signal.py` | **103 pass, 0 fail** | ran (disposable PostgreSQL 16.11) |
| `ops/tests_manual/test_workflow_b_p0cdg_autonomous_routing.py` | **108 pass, 0 fail** | ran (disposable PostgreSQL 16.11) |

The pure half covers collection (including the two classes that must *not* raise
a per-input incident: a retryable failure, and a cycle-level failure with no raw
file), fingerprint identity across cycles, the no-work invariant, and the sweep's
pagination loop. The PostgreSQL half runs the incident lifecycle end to end:
backlog intake, no duplicate alert on rescan, a new input alerting while a
backlog is open, resolution on repair, a blind cycle resolving nothing, ordered
supersession, the bounded intake with its deferral log, and one real
`run_workflow_b_batch` proving the wiring rather than the module in isolation. No
SMTP is configured, so the outbox is inspected and never delivered.

One regression per blocker, each written so it fails against the behaviour it
replaces. Re-running the suite with all five defects deliberately reintroduced
produces **27 failing checks** across them, which is what makes the evidence
falsifying rather than confirmatory:

* **1** — a `postprocess` incident on a Stage 3 `OK` raw file stays open across
  repeated cycles, stays open on a retryable postprocessor failure and on another
  input's success, and resolves only on an observed completion for that input;
  a `stage2` incident on the same evidence still resolves, so the fix is
  stage-appropriate proof rather than a blanket refusal.
* **2** — with the bound at 1 and two inputs changing reason, the input whose
  replacement persisted is superseded and the one whose replacement was deferred
  keeps its predecessor; an injected persistence failure keeps the predecessor
  too; a later successful replacement finally supersedes it; an unchanged reason
  opens and resolves nothing. No input is ever left with zero open incidents.
* **3** — 250 durably unowned rows in a real `ingest.raw_file`. One oldest-first
  page provably stops at 200 and sees none of the newest 50; the paginated sweep
  inspects all 250 in two pages with no repeats, does so again on the next cycle
  over the same stuck backlog, and signals a brand-new arrival immediately. The
  page-count backstop truncates and reports itself.
* **4** — bound at 1, the leading fingerprint's persistence failing every cycle:
  each cycle attempts two candidates, opens one, and after three cycles every
  input behind the failing head has its own incident while the head is still
  reported as a failure. A healthy backlog then drains and goes quiet.
* **5** — fault injection on a real lock: signalling gets a connection distinct
  from the lock session; a poisoned signalling path leaves `SUCCEEDED_NO_WORK`
  and `SUCCEEDED_WITH_REVIEW_ITEMS` intact and is still logged; an orchestration
  failure remains the surfaced `WorkflowBOrchestrationError` with its own
  outcome; an injected `pg_advisory_unlock` failure cannot replace a healthy
  result, is logged, and the connection is closed anyway; an independent session
  can take the lock after every one of those cycles; and a subsequent natural
  cycle completes normally.

`test_workflow_b_p0cdg_autonomous_routing.py` previously gated its PostgreSQL
half on `WORKFLOW_B_ROUTING_TEST_DSN`, which both could be pointed anywhere and
let a completely unverified run print `OK`. It now uses the same disposable
instance as the rest.

Existing suites re-run with no regression: `test_workflow_b_orchestrator`,
`test_workflow_b_alert_hardening`, `test_workflow_b_stage_contracts`,
`test_workflow_b_p0e_p1i_recovery`, `test_workflow_b_report_selector_override`,
`test_execution_watchdog`, `test_suspected_bug_contract`. Four
`Stage1BatchResult()` fixtures in the P0-C/D/G suite were corrected to set
`mailbox_check_completed=True`: they stood for a healthy Stage 1 while carrying
the field's `False` default, which the no-work invariant correctly rejects. No
assertion was weakened.

Still DSN-gated and therefore not exercised here: the persistence halves of
`test_workflow_b_alert_hardening`, `test_execution_watchdog` and
`test_workflow_b_p0e_p1i_recovery`, and the whole of
`test_suspected_bug_outbox_postgres` / `test_alpha_dysponent_suspected_bug`. None
of them carries evidence for these five blockers — the incident, occurrence and
outbox writes this work depends on are exercised against real PostgreSQL by the
suite above — and converting them belongs to their own tasks.

Deployed: the contract above was released in `6279e168c628` (active
2026-08-24 12:18:08 Europe/Warsaw), is contained in every later release, and its
semantics were confirmed on the natural production cycles of 2026-08-24/25 (see
§5.11 for the one defect those cycles exposed and its fix).

### 5.11 One-shot unresolved inputs, replayability proof and the fail-closed boundary — `DEPLOYED_VERIFIED` (verified on the first natural post-hardening run, 2026-08-25 20:00 Europe/Warsaw)

§5.10 is live and its semantics were confirmed on natural production cycles.
One statement in it was wrong, and production found it on the first cycle after
activation: **not everything the per-cycle bound defers is picked up by the next
cycle.** Independent review of the first correction then disproved two of *its*
claims, and both are fixed here.

**Observed.** `ec6c3c8` became active in release `6279e168c628` at
`2026-08-24 12:18:08 Europe/Warsaw`. On the first natural cycle after that —
`2026-08-24 20:00`, run `ab5b4b0d-a93c-4a96-80b3-13c8178c6363` — raw file
`937731f7-8c67-4a8b-8e2c-436713cd9c53` (`Alpha_GPS_Baza_LOG`, ALPHA00001) arrived,
cleaned, loaded 8 999 rows, and its `alpha00001_dysponent_id_enrichment`
postprocessor returned `FAILED_NON_RETRYABLE` / `AMBIGUOUS_ENRICHMENT_MATCH` with
`operator_action_required=true`. The signal collected 64 unresolved inputs, opened
20 and deferred 44. At `2026-08-25 06:00` (`ada6124c-…`) the count was 63,
`incidents_resolved: 0`, `postprocessor_plans_discovered: 0` — the input had left
the population without ever receiving a `WORKFLOW_B_INPUT_UNRESOLVED` incident,
and could not receive one later.

**Root cause, four steps, all confirmed in the deployed code.**

1. `collect_unresolved_inputs` appends postprocess items last, after every
   stage-1/2/3 item, and the bound was spent in that order.
2. With 63 Stage 2 stranded inputs against 20 slots, a postprocess item was
   deferred by construction.
3. A postprocess item exists only in the cycle whose postprocessor ran, whereas
   Stage 2 items are re-derived from durable state by the sweep every cycle.
4. `_discover_postprocessor_plans` is fed `result.stage3.…successful_load_identities`
   — this cycle's loads only — so with `stage3_status='OK'` the file is never
   offered again. "Deferred to the next cycle" was therefore false for that class.

Only a domain-specific `ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT` incident kept the
input visible. **A generic safety contract may not depend on every postprocessor
alerting for itself**, so this is a defect in the mechanism even though the
observed instance stayed actionable.

The durable invariant, stronger than §5.10's wording:

```text
ARRIVED INPUT -> MATERIAL WORK REMAINS UNRESOLVED -> DURABLE ACTIONABLE SIGNAL EXISTS
```

#### 5.11.1 Replayability is proved per event, never assumed from a stage name

The first candidate derived replayability from a `REPLAYABLE_STAGES` table and
review disproved the Stage 2 half of it. The second candidate kept Stage 2
replayable for *sweep-observed* items only, arguing from ordinal position inside
the `UNROUTED_SWEEP_MAX_PAGES × UNROUTED_SWEEP_LIMIT` = 10 000-row oldest-first
prefix: a row a sweep page returned is inside the prefix, and its position was
claimed to be non-increasing.

**Review disproved that too, and this section records the correction.** The
premise fails against the repository's own mutation paths:

* `_candidate_rows(params={"raw_file_ids": […]})` drops the eligibility
  predicate entirely and admits any `NORMALIZED` row by id — the targeted /
  diagnostic Stage 2 path;
* `_persist_stage2` then writes `stage2_updated_at = NOW()` unconditionally;
* if the resulting durable state is still materially unresolved and still
  non-retryable, the row stays in the sweep's qualifying set carrying the
  **newest** key in it, so it sorts **last** — behind the persistent prefix, on
  that cycle and on every later one — while `_candidate_rows` normal discovery
  still refuses it as non-retryable;
* `ops/renormalize_raw_file.py --apply --reset-stage2` is a second path out of
  the swept prefix: it clears Stage 2 state and ordering fields with no
  guarantee of exhaustive later discovery of that row.

A position argument is only as strong as the set of writers that can change the
position, and Stage 2 ordering is writable by supported operator paths that carry
no obligation to preserve it. The proof is therefore not repairable by refining
the provenance test — refining it is what produced two invalid candidates — so
**all** material Stage 2 unresolved conditions are one-shot, with no sweep-origin
versus current-cycle distinction. A *loud truncation WARNING was never proof of
eventual rediscovery either*, and neither is having been swept.

The rule is per event:

> An unresolved event may take the bounded replayable deferral path only when the
> mechanism that would re-offer it is proved, from this cycle's own evidence, to
> reach that exact event again on a later natural cycle.

`UnresolvedInput.replayable` is a stored field defaulting to `False`, set only in
`collect_unresolved_inputs`, so an undeclared or future source is fail-safe by
construction rather than by remembering to update a table.

| event source | nominal stage | decision | proof of rediscovery / fail-safe behaviour |
|---|---|---|---|
| Stage 2, **whatever produced the item** — the unrouted reconciliation sweep (`STRANDED_UNROUTABLE`, `STRANDED_AWAITING_REVIEW`) and this cycle's own processing (`PENDING_HUMAN_REVIEW`, `UNSUPPORTED_REPORT`, `AMBIGUOUS_DETECTION`, `REJECTED_VALIDATION`, `FAILED_NON_RETRYABLE`, `FAILED_IDEMPOTENCY_CONFLICT`) alike | `stage2` | **one-shot** | no proof that survives a supported mutation path. Targeted `_candidate_rows(raw_file_ids=…)` + `_persist_stage2` re-stamp `stage2_updated_at` on a row that is still unresolved and still non-retryable, moving it out of the 10 000-row sweep prefix permanently while normal discovery continues to refuse it; `--reset-stage2` clears the ordering fields outright. See the paragraphs above — being swept is not a rediscovery guarantee, and there is no refinement of the provenance test that makes it one |
| Stage 3 leaving durable `stage3_status` NULL / `''` / `ERROR` / `RUNNING` | `stage3` | **replayable** | `_stage3_batch_status_eligible_sql` (P0-E) re-admits exactly those, **and the orchestrator passes no `limit`** to `process_stage3_batch`, so `_select_stage3_candidates` emits no `LIMIT` clause and returns the entire eligible set. Membership is a function of the row's own durable status; `ORDER BY stage2_updated_at ASC, id ASC` only sequences a set every member of which is already selected. A mutation that re-stamps the sort key therefore moves the row *inside an unbounded set* rather than *out of a bounded prefix* — the exact property Stage 2 lacks, which is why the Stage 2 refutation does not propagate here |
| Stage 3 leaving any other durable status, including one added in future | `stage3` | **one-shot** | nothing re-selects it; the decision reads the item's `persisted_status`, not its stage name |
| postprocessor | `postprocess` | **one-shot** | plans derive solely from this cycle's Stage 3 load identities; `stage3_status='OK'` retires the file and nothing durable records that the work is owed |
| Stage 1 | `stage1` | **one-shot** | a collected item names a raw file, so its `ingest.imap_message` row is committed and later cycles skip the message as `REUSED_MESSAGE`; the only re-deriving path is the `limit`-bounded artifact reconciliation batch |
| anything not listed | any | **one-shot** | the field default |

Known bounded limitation, recorded rather than hidden: a file this cycle
processed into the `stage2_status='OK'` + missing-`client_code` shape is
reclassified only by the sweep, so while a ≥ 10 000-row backlog exists it is not
*collected* at all that cycle. It remains a durable `ingest.raw_file` row and
returns to visibility once the backlog falls below the backstop — and that
backlog is itself reported input by input. This is a collection-window property,
not a deferral that discards evidence, and it is not addressed here.

#### 5.11.2 Budgets, and the aggregate that stands in for the rest

The two classes no longer contend for one budget.
`WORKFLOW_B_MAX_NEW_UNRESOLVED_INCIDENTS_PER_CYCLE` (default 20, unchanged) now
governs replayable candidates only — what it defers really is rediscovered.
One-shot candidates get `WORKFLOW_B_MAX_NEW_ONE_SHOT_UNRESOLVED_INCIDENTS_PER_CYCLE`
(default 100), a resource backstop rather than pacing.

**Operational consequence of Stage 2 becoming one-shot.** "One-shot" means *must
become durably actionable in the observing cycle*, not *will occur only once*: a
persistent Stage 2 condition will in fact usually be observed again, correctness
simply does not rely on it. Since Stage 2 is one-shot, this budget now meets a
standing population rather than only one cycle's fresh work, and the consequence
is bounded on both sides:

* **first activation.** The production unrouted Stage 2 set is on the order of 60
  files growing by ~4 a week — below the 100 bound, so the first cycle opens ~60
  incidents at once instead of draining 20 a cycle for three days. Each is one
  `REASON_NEW` outbox row; the delivery worker runs every 5 minutes at
  `SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE` (10), i.e. 120/hour, so the outbox
  itself spreads a burst of that size over well under an hour and no synchronous
  send storm is possible;
* **every later cycle.** The same files match an already-open incident by
  fingerprint, leave `pending` *before* any budget is consulted, and enqueue
  nothing. The standing backlog costs its notifications once, not once per cycle
  — the dedup does that, not the bound;
* **a backlog above the bound.** The remainder is still durably represented, with
  an exact count and a complete-set digest, by the truncation incident, and
  fail-closes if even that cannot be persisted. It then gains individual
  incidents over following cycles as the represented ones stop consuming
  capacity. No path loses an input silently.

Whatever is **not durably open when the pass ends** — refused by that budget *or* attempted and
failed to persist, which for a one-shot input have the same consequence — is
named in exactly one bounded `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED` incident.
Membership is decided by the same post-write re-read of the incident table that
supersession uses: proof, not intent. That incident is outside the
`WORKFLOW_B_INPUT_UNRESOLVED` lifecycle and is not auto-resolved.

**Its durable identity is the complete membership set.** The first candidate
fingerprinted the *displayed* list, capped at `MAX_LISTED_UNREPORTED_ONE_SHOT`
(50), so two populations sharing a count and their first 50 sorted identities
collided onto one incident and the second was folded in as another occurrence of
a problem it does not name. `fingerprint_fields` now carries
`one_shot_membership_digest` — a streaming SHA-256 over the canonically sorted,
deduplicated `(raw_file_id, stage, reason_code)` set, with record separators that
cannot occur inside a UUID, a stage or a reason code — plus the exact count. Two
distinct memberships are two incidents; the same membership in any order, with
any repeats, is one. The human-readable half stays capped and says so, and
`affected_record_count` is always the true total. The stored payload is
therefore constant-size: measured at **5 711 / 5 721 / 5 721 / 5 726 bytes** for
populations of 51 / 1 000 / 5 000 / 50 000.

#### 5.11.3 Fail-closed, and how narrow it is

The aggregate write can fail too, and the candidate proved its success from
`report.error is None` — a statement about a call, not about durable state. Worse,
its post-write re-read is scoped to `incident_code = WORKFLOW_B_INPUT_UNRESOLVED`,
so it could never have seen the truncation incident at all. With both writes
failing, a one-shot condition ended the cycle with no individual incident, no
aggregate incident and a healthy `SUCCEEDED_WITH_REVIEW_ITEMS`.

By the end of a cycle every observed one-shot unresolved input now satisfies one
of:

**A.** an individual durable `WORKFLOW_B_INPUT_UNRESOLVED` incident exists —
proved by re-reading the incident table;
**B.** a durable `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED` incident represents it
— proved by `truncation_incident_is_durable(conn, fingerprint)`, which queries
`suspected_bug_incidents` by that incident's own fingerprint and answers `False`
on any failure to establish the fact;
**C.** the cycle itself failed durably, because neither could be established.

Only C's precondition — `signal.safety_contract_violated`, i.e.
`one_shot_without_durable_evidence > 0` — reaches
`WorkflowBBatchResult.unresolved_signal_safety_failure`, and `_finish_or_raise`
turns it into `FAILED_NON_RETRYABLE` with the cause
`one_shot_unresolved_input_without_durable_signal` in the exception text and
`unresolved_signal_safety_failure: true` in `incident_details()`. The run is then
FAILED and `ops/runner.py` raises the existing `JOB_TERMINAL_FAILURE` incident,
with systemd `OnFailure` behind it — **the minimum existing durable mechanism; no
second alerting framework was introduced.** The branch sits *below* the stage
failures so a cycle that already failed for a stage reason keeps its own cause.
If the signalling pass could not run at all, whether that matters is answered by
the pure, database-free `one_shot_unresolved_input_count`; with no one-shot work
the poisoned-signalling containment of §5.10 is unchanged.

Everything else stays contained: a failed replayable incident, a deferred
replayable candidate, a domain review item, or an unreachable alert path on a
cycle with no one-shot inputs never fails the run. Signalling still runs on its
own connection, so no signalling fault can poison the session holding
`workflow_b.orchestrator.v1`.

No schema change, no migration, no new alert channel, no per-report freshness or
expected-arrival logic. `P0-B` stays withdrawn.

**Verification.** Executed against disposable PostgreSQL 16.11:

| suite | checks | database half |
|---|---|---|
| `ops/tests_manual/test_workflow_b_unresolved_input_signal.py` | **240 pass, 0 fail** (was 103 before §5.11, 136 in the blocked candidate) | ran |
| `ops/tests_manual/test_workflow_b_p0cdg_autonomous_routing.py` | **108 pass, 0 fail** | ran |
| `ops/tests_manual/test_workflow_b_orchestrator.py` | pass | n/a (pure) |
| `ops/tests_manual/test_workflow_b_alert_hardening.py` | pass | DSN-gated half skipped, as before |
| `test_workflow_b_stage_contracts`, `test_workflow_b_stage2_batch_contract`, `test_workflow_b_p0e_p1i_recovery`, `test_suspected_bug_contract`, `test_replay_workflow_b_file_to_stage3`, `test_workflow_b_report_status` | pass | unaffected |

The regressions are falsifying, not confirmatory. Reverting each fix in turn to
the reviewed candidate's behaviour fails the suite:

| fix reverted | failing checks |
|---|---|
| truncation fingerprint back to the capped display list | **9**, including `[pg] two different memberships are two durable incidents`, which reproduces the collision as a single incident with `occurrence_count = 2` in real PostgreSQL |
| fail-closed path removed | **8**, including a one-shot input settling as `SUCCEEDED_NO_WORK` and as `SUCCEEDED_WITH_REVIEW_ITEMS` |
| replayability back to the stage table | **15**, including the Stage 2 item behind the sweep backstop being deferred and left with no incident |
| Stage 2 sweep-origin items back to `replayable` | **6**, including `test_pg_stage2_targeted_reprocessing_breaks_sweep_replayability`, which executes the real `_candidate_rows(raw_file_ids=…)` + `_persist_stage2` path against 10 000 rows of real sweep-backstop pressure and shows the deferred row becoming permanently unreachable |
| all three | **32** |

Two suites needed a fixture correction rather than a weakened assertion, and the
reason is the same in both: a bare `PENDING_HUMAN_REVIEW` Stage 2 item is now
one-shot, so with a stubbed incident store that writes nothing it is exactly the
fail-closed condition. `test_workflow_b_orchestrator.py` now asserts the review
outcome with a swept (replayable) review item **and additionally** asserts that
the one-shot variant fails closed; `test_workflow_b_alert_hardening.py`'s
`_review_item()` is the swept shape, which is the production `report_112` case
that assertion was always about. No existing check was removed or relaxed.

Deployed: committed as `023e6452fd5babf5617abc31622ec34cf5ead02e`
(`fix(workflow_b): make one-shot unresolved inputs fail closed under the signal
budget`), released as `023e6452fd5b` and activated in production on
2026-08-25 at 11:40:18 UTC (13:40 Europe/Warsaw), contained byte-identically in
`92c53ece27da` and in every release since.

**Verified on the first natural post-hardening run — 2026-08-25 20:00
Europe/Warsaw, read-only observation.** `log-workflow-b.timer` fired naturally
(`LastTrigger = 20:00:00`, no manual invocation in the journal); the service ran
20:00:00→20:00:03 on release `92c53ece27da` (wrapper-logged), systemd
`Result=success`. `public.runs` run `7a0019d3-ccde-4453-8784-88f1c260f535`,
`SUCCESS`; application outcome `SUCCEEDED_WITH_REVIEW_ITEMS`. The signal
summary: `unresolved_input_count = 63`, all one-shot (`one_shot_input_count =
63`), `already_open_count = 40`, `incidents_attempted = 23`, `incidents_opened
= 23`, `incidents_deferred = 0`, `one_shot_unreported = 0`,
`one_shot_without_durable_evidence = 0`, `safety_contract_violated = false`,
`unresolved_signal_safety_failure = false` — the 23 previously deferred Stage 2
one-shot items each received an individual durable
`WORKFLOW_B_INPUT_UNRESOLVED` incident (23 new rows confirmed in
`suspected_bug_incidents`, total open unresolved incidents now 63 = the whole
population; no silent evidence loss). No truncation occurred (63 ≤ the 100
one-shot budget; the 20 replayable budget was unused — 0 Stage 3 candidates)
and no ALPHA `AMBIGUOUS_ENRICHMENT_MATCH` condition arose this cycle
(`ambiguous_count = 0`, `postprocessor_plans_discovered = 0`; its absence is
acceptable). All 23 generated outbox rows drained to `sent` by 20:13 through
the worker's normal 5-minute cadence (0 pending, 0 failed, heartbeat healthy).
The observational gate this section carried is closed by that evidence.

## 6. Email delivery-state semantics

Unchanged by this milestone; recorded here so the limitation is not rediscovered.

`sent` means **`smtplib` returned without raising for one envelope recipient** —
i.e. accepted for relay by `mail.example.invalid`. It does not mean delivered. The
lifecycle terminates at `sent`: no code path mutates a send-log row afterwards.
Recipient-delivery confirmation is not obtainable over SMTP and its absence is not
counted as a defect; **bounce/DSN ingestion is obtainable and is entirely absent**,
which is (P1, §5.3).

This applies to customer email only. Operator alert email uses the separate outbox
model in §1, whose `dead_letter` state is honest about non-delivery.

---

## 7. Deployment

**Executed in full on 2026-08-09** under explicit authorization: steps 1–6 first,
then step 7 after the §5.1a identity-bootstrap defect was found at the retention
dry-run, fixed, independently re-reviewed and the corrected unit installed. The
first destructive retention run and its fail-closed survivor verification are
recorded in §5.4b. `backup-retention.timer` is enabled and active, next fire
2026-08-10 04:15 CEST.

**This does not make the platform ready for unattended operation.** P0-5, P0-6 and
P0-7 remain outstanding, and the P1 backlog in §5.3 is untouched. What P0-1–P0-4
establish is that a failure now reaches a human, and that backups no longer grow
without bound — not that every failure mode is covered.

The ordered deployment sequence
lives in `docs/11_operational_readiness.md` → *Deployment sequence for the P0
operational safety units*. It must be executed as a separate, explicitly
authorized task. In short: apply migration `059`, configure
`SUSPECTED_BUG_ALERT_TO`, install the mail worker **first** and prove a real alert
reaches a real mailbox, then the failure handler and its drop-ins, then the
watchdog and monitors — each run once by hand in its read-only form before its
timer is enabled.

Order is not cosmetic. The dispatcher heartbeat no-ops until `059` exists, so
enabling `execution-watchdog.timer` first would report `HEARTBEAT_LOST` for a
perfectly healthy dispatcher; and `backup-retention.timer` deletes real backups on
its first `--execute` fire.
