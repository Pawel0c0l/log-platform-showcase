# Platform retention and recurring-schedule governance

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Any
> `PYTHONPATH="$PWD" .venv/bin/python -m …` command below is development /
> local / rehearsal use: it executes the mutable working tree. Production
> execution goes through the installed unit
> (`platform-hard-retention.service`) or the installed wrapper. See
> `docs/07_operations.md` → *Release boundary*.

This document is the router for platform-wide retention and schedule
governance. It states the policy and names the authoritative artefacts; it
deliberately does **not** restate the registry, because a prose copy of a
machine-readable table is a second source of truth that drifts.

Store-by-store retention mechanics stay in `docs/08_retention.md`. Workflow A
scheduling mechanics stay in `docs/10_scheduler_design.md`.

---

## 1. The owner policy

> **Persisted platform data must not remain stored beyond 13 calendar months**
> unless a shorter lifecycle already removes it earlier, or unless an explicit
> owner-approved override is recorded against that data category.

Three things follow, and all three are enforced by code rather than by this
paragraph:

- **13 CALENDAR months, not a day count.** The cutoff is calendar arithmetic
  with month-end clamping: 31 March minus 13 months is 28 February, and 29
  February exists in the answer only when the target year is a leap year.
  `395 days` is not the policy and is rejected by
  `ops/tests_manual/test_retention_registry.py`.
- **It is a maximum across ALL surviving copies.** No original row, derived
  copy, backup copy, log entry, snapshot, object, export or tombstone may
  outlive its 13-month deadline — not because cleanup runs periodically, and not
  because another copy exists somewhere else. "13 months plus the next
  maintenance run" is not 13 months, and "13 months live, longer in backup" is
  not either. §3.1 and §6 are how each of those is prevented.
- **It is a maximum, never a target.** Nothing was lengthened to reach it. The
  Eco weekly capability still lives 10 days and the monthly one 60; a Database
  Explorer export still expires after 3 days; a backup set after 14; a browser
  session in hours; platform runs, logs and artifacts after 60 days; provider
  request evidence after 180.
- **There are no silent exceptions.** A category that cannot follow the ceiling
  is recorded in the registry with `Status.BLOCKED_OWNER_DECISION` and a written
  blocker, and every validation run reports it. It is never dropped from the
  registry, and it is never executed.
- **There is exactly one approved exception.** GPS Baza Log
  (`telematics_reports."Alpha_GPS_Baza_LOG"`) is owner-approved as having **no
  age-based retention** at all, recorded as `Mode.OWNER_EXEMPT` with an
  attributed `OwnerExemption` (§8). It is centrally visible in the registry and
  is neither unmanaged nor blocked. Every other governed store still answers to
  the 13-calendar-month ceiling or to a shorter lifetime, and a new exception
  requires the same explicit registration — validation refuses an anonymous one.

---

## 2. The authoritative retention registry

`ops/retention_registry.py` is the single machine-readable source. It carries,
per governed data family: the store, backend, owning domain, retention duration,
the timestamp the age is measured from, the deletion mechanism, the cleanup job
responsible, the lifecycle mode (hard delete, compaction, secret minimisation,
tombstone retention, logical expiry then hard delete, lifecycle-bound, not
applicable, owner-exempt), any shorter TTL that applies first, whether the entry
is active, deprecated, unsupported or blocked, and any owner override or
owner exemption.

The ceiling itself exists **once**, as `HARD_RETENTION_MONTHS`. Cleanup
implementations import it; none restates it.

### Inspecting it

```bash
PYTHONPATH="$PWD" .venv/bin/python -m ops.retention_registry              # table
PYTHONPATH="$PWD" .venv/bin/python -m ops.retention_registry --format json
PYTHONPATH="$PWD" .venv/bin/python -m ops.retention_registry --validate-only
PYTHONPATH="$PWD" .venv/bin/python -m ops.retention_registry --coverage [--clients]
```

`--validate-only` exits non-zero when anything violates the policy: a horizon
longer than the ceiling without an attributed override, an override with no
approver or reason, an exemption with no written rationale, an age-based policy
with no cleanup job, or a blocked entry with no stated blocker.

One surface answers all of the operator's questions: what policies exist, what
is exempt, what is shorter, what is lifecycle-bound, whether the known
persistent relations of a live database are covered, and where the host's real
behaviour differs from what an entry declares.

`--coverage` is the last of those and is **strictly read-only**: one `pg_class`
census per database, rolled back — no ledger row, no incident, no advisory lock,
no DDL. `--clients` extends it to every enabled client business database.
An unreachable database is reported as unproven and exits non-zero; answering
"clean" to a question that was never asked is the failure this registry exists
to prevent.

The offline half of the same report names every `EffectiveMechanism`: a store
something outside this repository empties sooner (host tmpfiles on the stage-2
scratch root) and a scheduled mechanism configured not to delete at all (the
dry-run per-client purge). A shorter policy that is CONFIGURED is never
presented as one that is ENFORCED.

### Unknown stores are detectable, not invisible

`GOVERNED_RELATIONS` and `GOVERNED_CLIENT_RELATIONS` map every relation the
repository can create to the policy that governs it.
`test_retention_registry.py` parses every `CREATE TABLE` in `db/migrations/`,
`db/client_business/` and `api/main.py` and fails when one is unmapped — so a
migration that introduces a table cannot merge without a retention decision.
Relations the loader creates at runtime (the Workflow B Stage 3 destinations)
are mapped explicitly, with a comment saying where they come from.

### Cross-runtime propagation

The Cloudflare Worker cannot import Python, so
`delivery/driver_eco_dashboard/worker/lib/retention_policy.js` mirrors the
constant and the calendar arithmetic. The two are pinned to each other by
`test_retention_registry.py` (constant and policy id) and by
`test_driver_eco_dashboard_hard_retention.py`, which compares the cutoff
produced by each runtime on the same boundary vectors.

**A mirror in the repository is not a mirror at the edge.** The Worker half only
enforces once a Worker VERSION carrying it is deployed, and that is a separate
authorized step from any host release — see § 7.9.

---

## 3. Cleanup execution

Configuration without an execution path is not governance. Every governed family
has one:

| horizon | executor | scheduled by |
| --- | --- | --- |
| 60 days — runs, logs, artifacts, MinIO objects | `api/platform_prune.py --execute --days 60` | `log-platform-prune.timer` |
| 180 days — provider request evidence | same module, fixed constant | same timer |
| 3 days — Database Explorer exports | `ops.database_export_worker --cleanup-only` | `database-export-cleanup.timer` |
| 14 days — backup sets | `ops.backup_retention --execute` | `backup-retention.timer` |
| 10 / 60 days — Eco capability bearers | `DeliveryLedger.retire_expired_capabilities` | the Eco mailing maintenance boundary |
| **13 calendar months — everything else** | `ops.hard_retention --execute` | `platform-hard-retention.timer` (new, proposed) |
| none — GPS Baza Log, owner-exempt (§8) | nothing deletes it by age; reported as `RETENTION_EXEMPT_NO_AGE_RETENTION` | — |
| 13 calendar months — D1 grants, publication ledger, R2 snapshots | `POST /api/publish/maintenance`, called by `ops.hard_retention` | `platform-hard-retention.timer` (an Eco mailing run also calls it) |
| 13 calendar months — journald | `MaxRetentionSec` + `journalctl --vacuum-time` | `journald-retention-vacuum.timer` |

### 3.1 Why a weekly sweep still meets a strict maximum age

A sweep that deletes "older than 13 months" and runs weekly leaves a record
alive for up to another week past its deadline. That is not compliance, so no
cleanup here uses the bare deadline as its cutoff.

Every sweep deletes **early**, by a lead composed from declared descriptors:

```text
cutoff = (now + lead) − 13 calendar months
lead   = maintenance cycle of the responsible schedule
       + backup shadow, when the store is inside a backup set
```

A record whose deadline falls anywhere before the next guaranteed cleanup
opportunity therefore goes on *this* pass. Deleting a few days early is
permitted by the policy — shorter retention always is. Deleting late is not.

Both inputs are declared, not typed at a call site:

- `MAINTENANCE_CYCLES` in `ops/retention_registry.py` gives the worst-case
  interval between two guaranteed cleanup opportunities per schedule;
- `ops/schedule_catalog.py` **derives** that interval from the unit file
  (`OnCalendar`, `OnUnitActiveSec`, or a `--cleanup-interval-seconds` flag) and
  fails validation if the declared and real cadences disagree. A timer slowed
  from daily to weekly without updating its cycle would otherwise silently
  shrink every dependent policy's lead;
- `PLATFORM_BACKUP_SET` gives the backup shadow (§6).

Current leads: **7 days** for a client-business, Cloudflare or filesystem store
swept weekly; **22 days** for a platform-database or MinIO store, which is the
same 7 days plus the 15-day backup shadow. `python -m ops.retention_registry`
prints the CYCLE, BACKUP and LEAD columns per policy.

The Worker mirrors the same rule with its own constant
(`HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS`), pinned to the registry by test.

An explicitly **shorter** policy — 60-day logs, 3-day exports, 14-day backup
sets — keeps its own horizon unchanged. Validation proves each one still clears
the ceiling once its lead is added; shortening them further would lose data the
owner never asked to lose.

### `ops/hard_retention.py`

Sweeps the platform database, every enabled client business database and the
repository-controlled filesystem roots. It is **dry-run by default**;
`--execute` is required to delete anything.

```bash
# rehearsal — reads only, writes no ledger row
PYTHONPATH="$PWD" .venv/bin/python -m ops.hard_retention --no-record
# one store, one client
PYTHONPATH="$PWD" .venv/bin/python -m ops.hard_retention \
    --policy client_db.workflow_b_stage3_report_tables --client ALPHA00001
```

Properties, each of which has a deterministic test:

- **bounded** — every delete is `ctid IN (SELECT … LIMIT n FOR UPDATE SKIP
  LOCKED)` with a COMMIT per batch, so a seven-million-row table never becomes
  one transaction and an interruption leaves committed progress;
- **idempotent and restart-safe** — every decision is a pure function of the
  cutoff and the row's own timestamp;
- **concurrency-safe** — a session advisory lock makes two sweeps mutually
  exclusive. Each batch is one statement under one snapshot, so a `ctid` cannot
  be chosen and then reused by a different row before the delete reaches it.
  There is deliberately **no** row-locking clause: PostgreSQL requires the
  `UPDATE` privilege for `FOR UPDATE SKIP LOCKED`, and a retention role that can
  rewrite customer rows is a worse trade than one that occasionally retries. A
  contended batch is bounded by `lock_timeout` and resumes from committed
  progress on the next run;
- **fail-closed per store, not per run** — a missing relation, a missing anchor
  column, a missing privilege or a failed connection is recorded and skipped; it
  never causes a different store's eligible data to be retained;
- **it refuses to guess an anchor** — rows whose anchor timestamp is NULL are
  counted as `unanchored`, reported, and the run is a partial failure. They are
  never deleted;
- **it refuses a blocked policy** — whatever the flags say.

The artifact/MinIO ceiling pass runs through `api/platform_prune.py
--hard-ceiling`, invoked *inside* the sweep rather than as a second `ExecStart`,
so an object-store outage cannot abort the row sweep. That pass is the same
planner, the same identity attestation, the same `pg_constraint` reference
contract, the same SERIALIZABLE transaction and the same object-before-row
ordering as the 60-day pass; only the cutoff and two exclusions differ.
`artifact_workflow_b` and `artifact_retained_reference` stop protecting at the
ceiling — those two classes were previously unbounded. `artifact_active_run`,
the two storage-identity refusals, `artifact_separate_retention` and
`artifact_generated_report_available` still protect, because they are
correctness guards rather than horizons.

### Observability

Each sweep reports the cutoff used, the policy id, the store and scope, and the
examined / deleted / skipped / failed / unanchored counts plus the oldest
still-eligible record. `ops_control.retention_execution` (migration `070`) keeps
the last outcome per `(policy_id, scope)` — current state, one row per pair,
rewritten in place, so the governance ledger never needs governing itself.
`oldest_remaining_ts` is the compliance number: `NULL` is the compliant steady
state, and a value older than `cutoff_ts` means the ceiling is not being met.

Structured output carries no row payloads, object keys, filenames or capability
values.

---

## 4. Access TTL, secret minimisation and hard retention are three things

The Eco Dashboard is where all three meet, and conflating them is how the
previous "retain indefinitely" reading arose:

1. **Access TTL** — the capability stops authorising at its period's expiry: 10
   days weekly, 60 monthly. Nothing here changes that.
2. **Secret minimisation** — the host destroys the raw bearer at that same
   expiry (`CAPABILITY_RETIRED`, migration `051`), long before the ceiling. A
   secret is never retained merely because its audit record has not aged out.
3. **Hard retention** — what is left is non-secret audit identity: which grant,
   which generation, when it stopped working. That tombstone is what lets an old
   link answer `410 LINK_EXPIRED` rather than looking like a link that never
   existed, and it now ends at the ceiling.

The historical snapshot decision stands and is unchanged: capability expiry does
not delete the R2 object, and consecutive periods deliberately overlap. What
changed is only the end of the sentence — the snapshot outlives its link by
months, not indefinitely. It is deleted at the ceiling, measured from the
object's own R2 `uploaded` stamp.

**Cloudflare retention does not depend on Eco business traffic.** The Worker's
D1/R2 sweep used to run only from the Eco mailing run's maintenance boundary —
which made it conditional on somebody sending e-mail, while four of five
production clients have every Eco schedule disabled. `ops/hard_retention.py`
now calls the same publisher-authenticated `POST /api/publish/maintenance`
itself, on the weekly platform cadence. No new route, no Cloudflare cron
trigger, no unauthenticated surface, and the publisher credential is still
required; a mailing run calling it as well only shortens the interval.

That describes the CALLER. The endpoint only performs the D1/R2 ceiling sweep in
a Worker version that carries `worker/lib/retention_policy.js`, which was
deployed on 2026-09-03 — five days after the host began calling it. See § 7.9.

The grant row is anchored on `issued_at`, not `expires_at`. Anchoring on expiry
would keep a monthly grant for thirteen months *plus* its own sixty-day TTL,
which is a breach dressed as caution.

---

## 5. The central recurring-schedule catalogue

`ops/schedule_catalog.py` is the single operational view of every recurring
execution: installed systemd timers, timers still under `ops/systemd/proposed/`,
the database-driven Workflow A dispatcher, the self-pacing export worker, and
the Worker maintenance the Eco mailing run drives.

```bash
PYTHONPATH="$PWD" .venv/bin/python -m ops.schedule_catalog                       # table
PYTHONPATH="$PWD" .venv/bin/python -m ops.schedule_catalog --runtime --database  # + host + per-client
PYTHONPATH="$PWD" .venv/bin/python -m ops.schedule_catalog --format json
PYTHONPATH="$PWD" .venv/bin/python -m ops.schedule_catalog --validate-only
```

**It is not a second list.** The registry holds only what cannot be derived —
the logical identity of a schedule, its owner, whether it is retention work.
Everything else is read at call time from whatever actually decides it:

- cadence, timezone, persistence and command are **parsed from the unit files**
  in `ops/systemd/`, including the two-`OnCalendar` case an ini parser would
  silently halve;
- installed / enabled state and next fire come from `systemctl` (read-only,
  optional — absent runtime reports `None`, never a guess);
- per-client Workflow A fires come from
  `workflow_a_control.client_dataset_schedule`.

### Drift validation

`validate()` fails when:

- a `.timer` exists in `ops/systemd/` with no catalogue entry;
- an entry names a unit file that does not exist (a templated instance whose
  `.service` is a host-installed template must declare that explicitly);
- an entry does not say where its schedule is actually decided;
- a retention policy names a schedule the catalogue does not define, or a
  schedule claims to run a policy the registry does not declare;
- a timer's `[Unit]` names the very service its `[Timer]` triggers, so starting
  the timer — and every boot — runs the job outside its `OnCalendar`
  (`TIMER_ACTIVATES_ITS_SERVICE`);
- a retention policy's `EffectiveMechanism` names a schedule the catalogue does
  not define, so the mechanism behind a governed store's real lifecycle would
  not be centrally readable;
- with `--runtime`: a platform-looking timer is installed on the host and is not
  in the catalogue.

**Runtime discovery is derived, not pattern-matched.** It used to ask "which
installed timers look like ours?" from a prefix list, and
`journald-retention-vacuum.timer` — shipped here, installed, enabled, executing
a governed policy — matched none of them. The candidate set is now the union of
every `.timer` in `ops/systemd/`, every unit a catalogue entry names, and the
prefix heuristic kept for the one thing a name pattern can honestly do: catch a
platform-looking timer that no repository file and no entry explains.

**Non-calendar semantics are visible.** `unit_semantics()` reports a timer's
`[Unit]` activating dependencies and whether any of them pulls in the paired
service, and `enforcement` is derived from the unit's own `ExecStart`, so a
schedule that is installed, enabled and firing while deleting nothing reads as
`DRY RUN` rather than as retention work being done. It is deliberately not a
systemd reimplementation.

---

## 6. Backup and DR retention

A hard delete at the source does not reach copies already inside retained
archives, and backup **file** age proves nothing: a 14-day-old full dump can
contain a record whose own age is thirteen months.

### The audited topology

`ops/backup.sh` runs exactly one `pg_dump -d $POSTGRES_DB` — the **platform**
database — and tars the MinIO data directory. It does **not** touch the client
business databases, Cloudflare D1 or R2, `REPORTS_DATA_DIR` or the stage-2
scratch directory. So only two backends carry a backup shadow at all, and the
rest correctly carry none:

| backend | in a repository-controlled backup set? |
| --- | --- |
| platform PostgreSQL | yes |
| MinIO objects | yes |
| client business PostgreSQL | **no** |
| Cloudflare D1 / R2 | no |
| filesystem roots | no |

(That client business databases have no repository-controlled backup at all is a
disaster-recovery observation, not a retention one. It is recorded here because
the audit found it.)

### The guarantee, and how it is obtained

`PLATFORM_BACKUP_SET.shadow` = set retention (14 days) + one expiry cycle
(1 day) = **15 days**. Stores inside the set therefore delete at the **source**
15 days earlier than they otherwise would — on top of the 7-day maintenance
lead, giving the 22-day lead in §3.1.

The consequence is the guarantee the owner asked for:

> the last surviving platform-controlled backup copy of a datum disappears no
> later than that datum's 13-calendar-month deadline.

Because the source copy is gone by `deadline − 15 days` at the latest, the newest
archive that can still contain it was taken before that instant, and every such
archive has expired by the deadline. Shorter live retention is what buys this,
and shorter live retention is explicitly permitted.

`ops.retention_registry.final_surviving_copy()` models the whole chain —
created → backed up → source-deleted → last containing archive expires — and
`test_retention_registry.py` walks 130+ creation instants asserting
`final_surviving_copy <= deadline`. It also runs the **counterfactual**: leading
by the maintenance cycle alone, without the backup shadow, produces copies that
outlive the deadline. Without that, the passing case would prove nothing.

No test asserts "backup file age ≤ 13 months" and calls compliance achieved.

### One window, and configuration cannot establish another

The guarantee above is computed from `PLATFORM_BACKUP_SET.retention_days`, and
until the governance audit `ops/backup_retention.py` owned a second copy of that
number — `DEFAULT_RETENTION_DAYS = 14`, overridable by `BACKUP_RETENTION_DAYS`,
by `--retention-days` and by a unit — with nothing comparing the two. A longer
configured window would have left deleted records inside retained archives past
the shadow the whole proof rests on, and nothing would have reported it.

The executor now READS the registry. An operational override is still accepted
and is still useful — a smaller disk is a real reason to expire sooner — but it
passes `ops.retention_registry.validate_backup_retention_days()` first:

| direction | result |
| --- | --- |
| shorter than the declared window | accepted; the shadow stays an upper bound |
| longer | **refused, deletes nothing**, from `--retention-days`, from `BACKUP_RETENTION_DAYS` and from `plan_retention()` alike |

Changing the policy itself means changing `BackupTopology.retention_days`, which
moves the operator-facing entry, the executor's window and every dependent
enforcement lead together.

### What a backup set IS, and why nothing may live forever for want of a manifest

`discover_sets` recognises three shapes, and all three expire on the window
above:

| kind | shape | may anchor the floor? |
| --- | --- | --- |
| `manifest_set` | postgres + MinIO + manifest | yes, if it verifies |
| `legacy_pair` | postgres + MinIO, no manifest — what `ops/backup.sh` produced before manifests | no: nothing to verify against |
| `incomplete_remnant` | a lone half, or a manifest whose archive is gone | no |

Only a `.partial`/`.failed` member still needs `--purge-invalid`: that suffix
marks a run's own in-flight or explicitly-failed state, which an operator may be
triaging.

This closes a real defect. A set without a manifest used to be retained as
`invalid_remnant_retained` **regardless of age**, and the production unit does
not pass `--purge-invalid`, so eleven legacy sets — eight `legacy_pair`s from
July 2026 and three February 2026 orphan halves, 38.73 GB — were unbounded while
this document and the registry both said 14 days. The end-to-end guarantee above
was therefore not true for anything inside those archives.

Fail-closed remains a property of **discovery**, not of age: only the exact
`postgres_<ts>.sql.gz` / `minio_<ts>.tar.gz` / `backup_<ts>.manifest.json`
grammar with a parseable timestamp is recognised, so the client database dumps,
schema snapshots and forensic archives that also live in `backups/` are
invisible to retention at any age.

`ops/backup_retention.py --classify` answers "what is in `backups/`, and what
would expire?" with no lock, no identity attestation, no incident and no
deletion — the read-only surface an audit should use.

### What this does not change

Backups are still full dumps with no per-record expiry and no cryptographic
erasure. Nothing here deletes a record from inside an archive; the archive
expires as a whole. If a future requirement needs a shorter backup shadow, the
lever is `BackupTopology.retention_days` in `ops/retention_registry.py` (or the
expiry cycle), and it flows into the lead automatically.
`BACKUP_RETENTION_DAYS` is an operational override that can only shorten the
declared window; it is no longer a way to state a policy.

## 7. Rollout state — 2026-08-29, recurring enforcement ACTIVE

Recurring enforcement was activated here on 2026-08-29 from commit
`c6944d76b15d2c3b0acf056656ab92999e4ae016` / release `c6944d76b15d`. **Retention
now runs on its own.** Both timers are installed, enabled and waiting, the
journald ceiling is part of the running journald configuration, and the first
authorized execution plus one activation-time execution have both completed
against production.

**Current identities are later than this section.** The governance-audit
corrections (§7.7) are deployed: implementation commit
`a5da0a3d1ceac7d9ff505eeecdc994a19b74f709`, live release `a5da0a3d1cea`,
rollback identity `c6944d76b15d`, plus migration `071`. Sections 7.1–7.6 are the
record of the ORIGINAL activation and are deliberately not rewritten; read
current state from §7.7, and read the running identities from
`git rev-parse HEAD` and `ops/manage_release.py status` rather than from any
document.

**The Cloudflare half of this rollout lagged the host half by five days.** The
host was enforcing from 2026-08-29; the Worker code that enforces the ceiling in
D1 and R2 did not reach the edge until 2026-09-03. § 7.9 records the gap, what
it did and did not affect, and how to avoid repeating it.

| step | state | evidence |
| --- | --- | --- |
| Platform migration `070` | **APPLIED** | ledger table present with the committed constraints and index |
| Client migration `052` | **APPLIED to all five** | `SELECT` (30) and `DELETE` (87) added fleet-wide; nothing on the exempt GPS log |
| Host release | **LIVE at the time** — `current` = running = `c6944d76b15d`, previous `d49c7c718c59`. Superseded by `a5da0a3d1cea` (§7.7) | `pointer_matches_running_release: true`; both service PIDs have the release as their kernel-pinned cwd |
| First execute | **DONE, 2026-08-29T09:35:17Z** | `RETENTION_EXECUTION_SUCCEEDED`, `ok: true`, `operator_action_required: false`, 0 defects, **70 rows deleted, all of them expired Eco browser sessions in D1** (§7.1) |
| Execution ledger | **62 rows** at the time, 0 dry-run, 0 failed, 0 non-compliant | one row per (policy, scope), rewritten in place; the 10 exempt outcomes are deliberately not recorded. That key COLLAPSED multi-relation policies — migration `071` widens it to (policy, scope, target); see §7.7 |
| Post-execute rehearsal | **CLEAN and idempotent** | second dry run: 162 stores, 0 candidates, 0 defects |
| `platform-hard-retention.{service,timer}` | **INSTALLED, timer ENABLED, active/waiting** — next fire Sun 2026-08-30 05:00 CEST. The service has since lost its `[Install]` and is now `static` (§7.7) | byte-identical to source; §7.4, and to `a5da0a3d1cea` after §7.7 |
| `journald-retention-vacuum.{service,timer}` | **INSTALLED, ENABLED, active/waiting** — next fire Sun 2026-08-30 05:45 CEST | byte-identical to source; §7.4 |
| `journald-retention.conf` | **INSTALLED and ACTIVE** — `MaxRetentionSec=385d`, `MaxFileSec=7d` in the running configuration | `systemd-analyze cat-config systemd/journald.conf`; §7.5 |
| Schedule catalogue | **0 drift** with `--runtime --database` | both new timers reported `installed: true`, `enabled`, with the expected next fire |

### 7.1 The first production execution

Run as the exact `ExecStart` the unit carries, in the same working directory,
through the same identity wrapper:

```bash
cd /opt/log-platform
.venv/bin/python ops/run_with_environment_identity.py -- \
  .venv/bin/python -m ops.hard_retention --execute
```

```json
{"ok": true, "classification": "RETENTION_EXECUTION_SUCCEEDED",
 "operator_action_required": false, "ledger_rows_written": 152,
 "totals": {"stores": 162, "examined": 70, "deleted": 70, "failed": 0,
            "unanchored": 0, "blocked": 0, "exempt": 10},
 "defects": {}}
```

| backend | outcomes | examined | deleted |
| --- | --- | --- | --- |
| client_business_postgres | 140 | 0 | 0 |
| platform_postgres | 15 | 0 | 0 |
| cloudflare_d1 | 3 | 70 | **70** |
| cloudflare_r2 | 1 | 0 | 0 |
| filesystem | 3 | 0 | 0 |

**The one number that differs from the dry run, and why it is not drift.** The
dry run predicted zero candidates; the execution deleted 70. Every one of them is
`cloudflare_d1.eco_session` — expired browser sessions under a 12-hour
`Mode.COMPACTION` policy anchored on `expires_at`, i.e. state that already
authorises nothing. A dry run reports zero for the Cloudflare stores **by
design**: `POST /api/publish/maintenance` has no plan-only mode, so
`sweep_eco_dashboard_maintenance` deliberately contacts nothing and says so in
its note rather than performing a mutation nobody asked for. No PostgreSQL row,
filesystem file, MinIO object or R2 object was deleted, and the three remaining
Cloudflare policies (capability grants, publication ledger, R2 snapshots)
reported zero.

**D1/R2 first live verification.** All four Cloudflare policies returned
`RETENTION_EXECUTION_SUCCEEDED`, which is the publisher credential
authenticating against the real route for the first time from the platform
schedule. The route stays publisher-authenticated and answers 404 to everyone
else; only maintenance ran — no publication, no capability minting. No credential
value appears in the run output.

**GPS Baza Log was not touched.** Ten `RETENTION_EXEMPT_NO_AGE_RETENTION`
outcomes, zero deleted, zero ledger rows. `alpha_main` holds 9 002 rows with
oldest `assignment_date` 2017-06-20 before and after the execution.

**Nothing survives its deadline.** `oldest_remaining_ts` is NULL for every one of
the 62 ledger rows — the compliance number, and the steady state the policy asks
for.

### 7.2 The canonical dry run, and what it proved

Run in the execution context the scheduled unit will use — the same identity
wrapper, the same working directory, the same interpreter — differing only by
omitting `--execute` and adding `--no-record`:

```bash
cd /opt/log-platform
./.venv/bin/python ops/run_with_environment_identity.py -- \
  ./.venv/bin/python -m ops.hard_retention --no-record
```

```json
{"ok": true, "classification": "RETENTION_DRY_RUN_SUCCEEDED",
 "operator_action_required": false, "ledger_rows_written": 0,
 "totals": {"stores": 162, "examined": 0, "deleted": 0, "failed": 0,
            "unanchored": 0, "blocked": 0, "exempt": 10},
 "defects": {}}
```

Three things this settles, none of which an ad-hoc invocation could:

- **The 27 `INSUFFICIENT_PRIVILEGE` stores are gone.** Migration `052` closed
  them; every governed client relation is now inspectable by the runtime role.
- **`ARTIFACT_CEILING_FAILED: PRUNE_ENVIRONMENT_IDENTITY_MISSING` was an
  execution-context defect, not a code one — proven rather than assumed.** The
  artifact/MinIO pass demands a canonical environment identity; invoked without
  `ops/run_with_environment_identity.py` it fails closed, and through the wrapper
  it succeeds. Every variable the executor reads (`POSTGRES_*`,
  `REPORTS_DATA_DIR`, `MINIO_BUCKET`, `ECO_DASHBOARD_PUBLISHER_*`) resolves in
  that context.
- **The publisher credential is present**, so D1/R2 are governed by this sweep
  and report `RETENTION_DRY_RUN_SUCCEEDED` rather than
  `ECO_PUBLISHER_UNAVAILABLE`. The maintenance route has no plan-only mode, so
  actual connectivity was first exercised by the first authorized execution
  (§7.1), where it succeeded.

**Which tree executes.** `platform-hard-retention.service` runs from
`/opt/log-platform`, not from the release root, for
the same reason `log-platform-prune.service` does: it takes the shared,
non-blocking backup lock under `backups/`, and a release tree is immutable —
running the sweep from one fails at the lock file. Retention execution is
therefore bounded by the development tree being clean at the released commit,
which it is, rather than by the release pointer.

### 7.3 The rehearsal after the execution

The same dry run, repeated immediately afterwards: 162 stores, **0 candidates, 0
defects, 0 blocked, 0 unanchored**, GPS still exempt, `oldest_remaining_ts` NULL
everywhere. The 70 sessions do not reappear, nothing new became eligible, and the
executor is idempotent over its own output — established without a second
`--execute`.

No store on this platform currently holds anything past its enforcement cutoff.
That is an observation about today's data, not a standing promise: what a future
run deletes is whatever it finds, and the recurring timer is authorized to act on
it.

### 7.4 Activation of recurring enforcement — 2026-08-29, 13:01:57–13:02:00 CEST

The root-required installation block ran under explicit owner authorization:
five files copied into place, `daemon-reload`, `systemd-journald` restarted, one
manual first `journald-retention-vacuum.service` start, then `enable --now` on
both timers.

**Installed bytes are the committed bytes.** All five files compare equal
(`cmp`) to their tracked sources at `HEAD`, and their SHA-256 prefixes match the
digests recorded before installation:

| installed path | source | sha256 (first 16) |
| --- | --- | --- |
| `/etc/systemd/system/platform-hard-retention.service` | `ops/systemd/proposed/platform-hard-retention.service` | `b48ead56f926f8bf` |
| `/etc/systemd/system/platform-hard-retention.timer` | `ops/systemd/proposed/platform-hard-retention.timer` | `46872340f50e8432` (superseded — §7.6) |
| `/etc/systemd/system/journald-retention-vacuum.service` | `ops/systemd/proposed/journald-retention-vacuum.service` | `4822addc7dfd082d` |
| `/etc/systemd/system/journald-retention-vacuum.timer` | `ops/systemd/proposed/journald-retention-vacuum.timer` | `4b2ff396b3e019b8` (superseded — §7.6) |
| `/etc/systemd/journald.conf.d/10-log-platform-retention.conf` | `ops/systemd/proposed/journald-retention.conf` | `736bb00c5306c1b5` |

Both timers: `LoadState=loaded`, `UnitFileState=enabled`, `ActiveState=active`,
`SubState=waiting`, `Persistent=yes`, `Result=success`, `FragmentPath` under
`/etc/systemd/system`, no failed state. `LastTriggerUSec` is empty on both — no
scheduled fire has happened yet.

**A sweep ran at activation, and `Persistent=true` is not why.** Both timers
carry `Requires=<their service>` in `[Unit]`. `Requires=` is a *start-time*
dependency, so `systemctl enable --now <timer>` pulled the service in and ran it
immediately, and the journal shows exactly that ordering: `Started
platform-hard-retention.timer` at 13:01:58 followed in the same second by
`Starting platform-hard-retention.service`. The persistence stamps under
`/var/lib/systemd/timers/` were merely initialized at 13:01:58; systemd recorded
no trigger. The same mechanism produced a second `journald-retention-vacuum`
run at 13:01:58, one second after the manual one at 13:01:57.

**Consequence, recorded rather than assumed:** the sweep would also run whenever
its timer unit is started — at every boot, and after any `daemon-reload` that
restarts the timer — not only at Sunday 05:00. That is harmless for data (the
sweep is idempotent, deletes only past-deadline records, and takes the shared
non-blocking backup lock so it skips rather than colliding with a running
backup), but it defeats the ordering the unit's own comment argues for.
**Corrected in §7.6.**

**What that activation-time sweep did:** `--execute`, 13:01:58 → 13:02:00,
`ExecMainStatus=0`, `Result=success`.

```json
{"ok": true, "classification": "RETENTION_EXECUTION_SUCCEEDED",
 "operator_action_required": false, "ledger_rows_written": 152,
 "deadline_cutoff": "2025-07-29T11:01:58.500657+00:00",
 "totals": {"stores": 162, "examined": 0, "deleted": 0, "failed": 0,
            "unanchored": 0, "blocked": 0, "skipped": 0, "exempt": 10},
 "defects": {}}
```

| backend | outcomes | examined | deleted | failed |
| --- | --- | --- | --- | --- |
| client_business_postgres | 140 | 0 | 0 | 0 |
| platform_postgres | 15 | 0 | 0 | 0 |
| cloudflare_d1 | 3 | 0 | 0 | 0 |
| cloudflare_r2 | 1 | 0 | 0 | 0 |
| filesystem | 3 | 0 | 0 | 0 |

**Zero deletions is the expected reconciliation with §7.1 and §7.3.** The 70 D1
sessions were already gone, no new session had expired in the intervening 3½
hours, and the post-execute rehearsal had already shown zero candidates
everywhere else. All four Cloudflare policies again returned
`RETENTION_EXECUTION_SUCCEEDED` — the publisher-authenticated maintenance route
answering a second time from the platform schedule, with no publication and no
capability action. Thirty-four outcomes carry `defect_code: RELATION_ABSENT`
(relations a given client simply does not have); they are excluded from
`defects` by design, which is why `defects` is `{}` and
`operator_action_required` is `false`.

**`ledger_rows_written: 152` and a 62-row ledger are the same fact.** The
counter counts upserts — one per non-exempt outcome, 162 − 10 exempt = 152 —
while the table holds one row per `(policy_id, scope)`, so multi-store policies
collapse. After the run the ledger holds **62 rows, every one stamped
2026-08-29 13:02:00+02**, `deleted_count` 0, `failed_count` 0,
`oldest_remaining_ts` NULL everywhere, and **zero rows for the exempt GPS log**.
The 70-deletion detail from §7.1 was overwritten in place; that is what a
current-state ledger is for, and the durable record of it is §7.1.

**GPS Baza Log untouched, again.** Ten `RETENTION_EXEMPT_NO_AGE_RETENTION`
outcomes from the single `OWNER_EXEMPT` policy, zero ledger rows, and
`alpha_main` still holds **9 002 rows** with oldest `assignment_date`
2017-06-20 — identical to the figure recorded before the first execution.

**Final canonical dry run — clean.** Re-run read-only through the same identity
wrapper at 2026-08-29T11:12:34Z: `RETENTION_DRY_RUN_SUCCEEDED`, 162 stores, 0
candidates, 0 defects, 0 failed, 0 blocked, 0 unanchored, 0 privilege failures,
10 exempt, `ledger_rows_written: 0`.

**Host unaffected.** `log-platform-api.service` and
`database-export-worker.service` both `active (running)` from
`releases/c6944d76b15d`, `NRestarts=0`, unchanged across the installation;
`/health` returns 200 `{"ok": true}`. The only unit in `failed` state is
`thinkfan.service`, pre-existing OS/hardware state unrelated to this work.

### 7.5 journald after activation

The drop-in is the only source of either setting on this host, so the running
configuration is unambiguous:

```
# /etc/systemd/journald.conf.d/10-log-platform-retention.conf
MaxRetentionSec=385d
MaxFileSec=7d
```

`systemd-journald` restarted cleanly at 13:01:57 (`active (running)`,
`Result=success`) and reported `System Journal … is 3.9G, max 4.0G, 72.3M free`.

**No journal was deleted by retention.** Both vacuum runs printed `Vacuuming
done, freed 0B` for `/var/log/journal`, its machine directory and
`/run/log/journal`. Nothing on this host is anywhere near the 385-day horizon —
the whole journal is nine days deep.

| | before activation | after |
| --- | --- | --- |
| oldest journal entry | `2026-08-20T23:29:18+02:00` | `2026-08-21T03:42:18+02:00` |
| disk use | ~4.0G | 3.9G |

**The four-hour advance is size pressure, not retention.** This host sets no
`SystemMaxUse`, so journald's default ceiling is 4.0G and it was already sitting
at it; the restart's new active file cost one 83.8M archived file, which is
roughly four hours of system journal. `--vacuum-time=385d` freed 0B in the same
window, and a 385-day horizon cannot reach a nine-day-old entry, so no entry
younger than the configured horizon was removed by this change. Bounding the
journal by *time* is what this drop-in adds; bounding it by *size* is
pre-existing behaviour that this work did not alter.

### 7.6 The `Requires=` correction — timer start no longer executes

`Requires=<paired service>` in a timer's `[Unit]` is a **start-time**
dependency: systemd pulls the service in when the *timer* is started, which has
nothing to do with the calendar. `Unit=` in `[Timer]` is the trigger binding and
the only one a timer needs. Both retention timers carried the redundant
`Requires=` and both therefore executed during `enable --now` (§7.4).

Both `[Unit]` sections now carry a comment where the directive was, and no
activating dependency at all. **Nothing else moved** — cadence, `Persistent=true`,
`AccuracySec`, `RandomizedDelaySec`, `Unit=`, `[Install]`, both `.service` files
and the journald drop-in are byte-identical to what was already installed.

**Proved in an isolated user-systemd instance, not argued from the manual.** Two
probe pairs, identical except for the one directive, each with
`OnCalendar=Sun *-*-* 05:00:00` and `Persistent=true`:

| probe | `[Unit] Requires=` | after `systemctl --user start <timer>` | next elapse |
| --- | --- | --- | --- |
| A — corrected shape | absent | service **never ran**: `inactive`, no `ExecMainStartTimestamp`, marker file absent | Sun 05:00 |
| B — shape before the fix | present | service **ran immediately**, marker written at timer-start second | Sun 05:00 |

The identical next elapse is the second half of the result: removing the
dependency changes what a *start* does and leaves the *schedule* — including the
`Persistent=true` catch-up — untouched. The probes were removed afterwards.

**Deterministic guard, and why the old one missed this.**
`test_starting_a_timer_does_not_start_its_service` already encoded the invariant,
but iterated a hand-maintained tuple of four timers written in an earlier slice;
the retention timers were added later and nobody extended it. The test now
derives its subject from `ops/systemd/proposed/*.timer` **minus** a named
exception list, so a new timer is guarded by construction and escaping the guard
takes a deliberate, reviewable edit. Reintroducing `Requires=` fails the suite —
verified by putting the line back and watching it fail.

Three pre-existing timers also activated their own service on start —
`database-export-cleanup.timer`, `log-job@dispatcher.timer` and
`log-job@retention-purge.timer`. They have since been corrected the same way
(§7.7), so `TIMERS_WITH_KNOWN_ACTIVATING_DEPENDENCY` is now empty and the guard
covers every proposed timer.

`test_retention_timers_keep_their_cadence_and_catch_up` pins the other half:
cadence, `Persistent=true`, `Unit=`, `AccuracySec`, `RandomizedDelaySec=0` and
`WantedBy=timers.target`, so the next edit to these files cannot quietly change
the schedule.

**Corrected digests** (the other three installed files are unchanged and are not
reinstalled):

| file | sha256[0:16] |
| --- | --- |
| `platform-hard-retention.timer` | `1b3ffebe44c0babd` |
| `journald-retention-vacuum.timer` | `06bd18e819aebe23` |

**Adopted; the installed units are the corrected units.** Read-only comparison
on 2026-08-29 shows `/etc/systemd/system/platform-hard-retention.timer` and
`/etc/systemd/system/journald-retention-vacuum.timer` byte-identical to their
sources at `ops/systemd/proposed/`, with the corrected digests `1b3ffebe44c0babd`
and `06bd18e819aebe23` — so the digests marked *(superseded)* in §7.4 are
historical and the table there describes the pre-correction bytes, not the
running ones. Both persistence stamps under `/var/lib/systemd/timers/` read
2026-08-29 13:01:58, so loading the corrected units replayed no missed event —
and with the dependency gone, a timer start no longer runs anything.

### 7.7 Governance-audit corrections — DEPLOYED, commit `a5da0a3d1cea`

A repository-wide audit of this model found the gaps below. All are corrected,
committed and **live**. The destructive backup cleanup this section quantified
and left pending has since been authorized and executed — see §7.8.

| rollout step | state |
| --- | --- |
| commit + push | **DONE** — `a5da0a3d1ceac7d9ff505eeecdc994a19b74f709`, `origin/main` |
| migration `071` | **APPLIED** 2026-08-29 21:47:39 CEST; 62 rows preserved, every `target` recovered, 0 collisions, PK `(policy_id, scope, target)` |
| client grants `053` | **NOT APPLIED, and deliberately so** — the production dry run reports **zero** `INSUFFICIENT_PRIVILEGE` defects, so `alpha_user` already holds SELECT/DELETE on both relations. The file exists so a rebuilt client database is correct by construction; it is not a prerequisite for this release |
| release `a5da0a3d1cea` | **LIVE** — `current` and `running_release_id` agree, both services adopted it 2026-08-29 22:15:11 CEST, `NRestarts=0`, `/health` 200. Rollback identity `c6944d76b15d` |
| the four corrected unit files | **INSTALLED 2026-08-29 22:15:10 CEST**, byte-identical to `a5da0a3d1cea` |
| backup cleanup | **EXECUTED 2026-08-29 22:43 CEST** under separate owner authorization — 12 sets, 45,904,601,501 B; see §7.8 |

**Adoption started nothing.** The three timers were stopped and restarted at
22:15:11 and the journal records no `Starting <paired>.service` in that window
at all — the exact contrast with 13:01:58, where `Started
platform-hard-retention.timer` was followed one second later by `Starting
platform-hard-retention.service`. The only service start nearby is
`log-job@dispatcher.service` at **22:15:00**, eleven seconds BEFORE the restart,
which is its ordinary `*:0/5` calendar tick. `log-job@retention-purge.service`
still reports `ExecMainStartTimestamp = Sun 2026-08-23 05:30:00` — untouched
across the adoption, which is the one that would have mattered had its
`dry_run:true` ever been flipped.

**Cadence and catch-up survived the correction.** `hourly`, `*:0/5` and
`Sun *-*-* 03:30:00 UTC` are unchanged, all three timers are
`loaded / enabled / active (waiting)` with `Persistent=yes`, and every
`NextElapseUSecRealtime` is the expected next calendar instant. Removing an
activating dependency changes what a START does; it does not touch the schedule.

**`platform-hard-retention.service` can no longer be enabled.** With no
`[Install]` section systemd reports it `static` — a stronger state than the
`disabled` it was before, because `systemctl enable` on a static unit fails
outright rather than silently wiring a boot activation. No
`multi-user.target.wants` symlink references it, and its timer remains the only
thing that may schedule it (next fire Sun 2026-08-30 05:00 CEST).

| finding | correction |
| --- | --- |
| backup retention owned its own `DEFAULT_RETENTION_DAYS = 14`, overridable by env/CLI/unit independently of the registry | the executor reads `PLATFORM_BACKUP_SET.retention_days`; an override may only shorten, and a longer one fails closed from every lever (§6) |
| a set without a manifest was retained forever, so the end-to-end guarantee was untrue for 11 legacy sets, 38.73 GB | `legacy_pair` and `incomplete_remnant` are recognised shapes and expire on the same window; only `.partial`/`.failed` keeps its `--purge-invalid` gate (§6) |
| `ops_control.retention_execution` was keyed `(policy_id, scope)`, so a policy sweeping several relations kept only the last — 152 recordable outcomes became 62 rows and `deleted_count` was not attributable to a relation | migration `071` widens the key to `(policy_id, scope, target)`; still current-state, one row per independently swept target |
| `database-export-cleanup.timer`, `log-job@dispatcher.timer`, `log-job@retention-purge.timer` carried `Requires=<their service>` | removed; `Unit=` in `[Timer]` is the only binding. The purge one mattered most: with `dry_run:true` flipped off, a timer start or a boot would have become an unscheduled client-data DELETE |
| `platform-hard-retention.service` carried `[Install] WantedBy=multi-user.target` — a full `--execute` sweep at every boot if ever enabled | `[Install]` removed. The service was disabled on the host, so nothing running changed. `test_timer_driven_services_cannot_be_enabled_standalone` now derives its subject from the timers on disk instead of a hand-maintained tuple, which is why it missed this one |
| `host_platform_timers()` matched on name prefixes, so `journald-retention-vacuum.timer` — shipped here, installed, enabled, executing a governed policy — escaped runtime drift detection | discovery is derived from the repository's own timer inventory plus the catalogue, with the prefix list kept only as a heuristic for timers nothing here explains |
| the catalogue could not express `[Unit]` semantics, so a timer that runs its service on start looked identical to one that does not | `unit_semantics()` reports activating dependencies and the `TIMER_ACTIVATES_ITS_SERVICE` drift code |
| `/tmp/log-platform-stage2/cleaned` was shown at the 13-month ceiling, while the host empties `/tmp` after 30 days | the policy declares an `EffectiveMechanism`: host `systemd-tmpfiles-clean`, effective max 30 days, host-managed. The OS policy was **not** changed |
| the catalogue described the per-client purge without saying it deletes nothing | `enforcement` is derived from the unit's `ExecStart`; `log-job@retention-purge` reports `simulated_dry_run`, and the registry records it as a non-enforcing mechanism with no effective maximum |
| `--days 60` in the prune unit and `Retention.days(60)` in three entries were independent numbers | `api.platform_prune` validates `--days` against `platform_prune_retention_days()`; a longer value is refused at startup, a shorter one accepted. The effective 60-day policy is unchanged |
| `cloudflare_d1.eco_session` was described as a 12-hour lifetime; the Worker mints 30 minutes | corrected to 30 minutes and pinned to `SESSION_TTL_SECONDS`. The longer **compaction** horizon is declared separately as an effective mechanism — a different question, and the source of the original conflation. The Worker was not changed |
| proving coverage against a live database needed an ad-hoc script | `python -m ops.retention_registry --coverage [--clients]`, read-only: one `pg_class` census per database, rolled back, no ledger row, no incident, no lock |

**What the coverage command found, and how it was closed.** Its first
production run (read-only, 2026-08-29) reported `ALPHA00001` holding two
relations no policy claimed:
`telematics_reports.backup_client_trips_d105_2_write_test_20260619_100539` and
`telematics_reports.backup_d105_2_ecodriving_write_test_20260619_100539` —
pre-write snapshots an operator took before a D105.2 write test, read by
`ops/reports/d105_2_ecodriving_alpha00001_local_write_test_rollback_20260619_100539.sql`.
No migration creates them, which is exactly why the DDL-parsing coverage check
could not see them and only a live census could.

**Owner decision: govern, do not drop.** They join the EXISTING deprecated-copy
policy `client_db.legacy_backup_tables` at the global 13-calendar-month
ceiling — the same policy that already governs the migration 020/021 leftovers,
and whose `age_basis` already reads *"start_timestamp (trip copies) /
`_loaded_at` (report copies)"*, which is exactly what these two carry. No new
policy, no new exemption, no second retention regime.

**Registered by exact name, never by pattern.** Two entries in
`GOVERNED_CLIENT_RELATIONS` and two declared `TableSweep`s, each with the anchor
of the table it was copied from. A `backup_*` wildcard over `telematics_reports`
would confer destructive rights on operator and forensic relations nobody
decided to govern, so `test_the_write_test_snapshots_are_governed_by_name_not_by_pattern`
asserts that no relation mapping and no sweep contains a wildcard character, and
that the GPS assignment log is still swept by nothing.
`db/client_business/053_write_test_snapshot_retention_privileges.sql` carries the
matching `SELECT, DELETE` grants — a strict additive extension of `052`, which is
applied and immutable. Read-only verification shows `alpha_user` **already**
holds both privileges in production, so `053` is a no-op on the current fleet
and exists so a rebuilt client database is correct by construction.

Coverage now reports **zero ungoverned relations** across the platform database
and all five client business databases. Nothing was created, altered or deleted
in any client database.

### Post-adoption verification — read-only, 2026-08-29

| check | result |
| --- | --- |
| installed bytes of the four units | **byte-identical** to `a5da0a3d1cea` (`cmp` against `git show`) |
| three corrected timers | loaded, enabled, active (waiting), `Persistent=yes`, cadence unchanged, **0** activating `[Unit]` directives |
| paired services started by adoption | **none** — journal and `ExecMainStartTimestamp` both agree |
| `platform-hard-retention.service` | `static`, inactive, no `[Install]`, no `.wants` symlink |
| `schedule_catalog --runtime --database --validate-only` | **0 drift**; the catalogue's `activates_paired_service_on_start` is `False` for every unit and the installed files agree |
| release convergence | `current` = running = `a5da0a3d1cea`, `pointer_matches_running_release: true` |
| API / export worker | `active`, `NRestarts=0`, clean startup, `/health` 200, no import or schema errors |
| ledger | migration `071` applied, PK `(policy_id, scope, target)`, 62 rows, 0 collisions |
| coverage `--clients` | 59/59 platform + all five clients, **0 ungoverned**, 0 violations, 0 open owner decisions |
| canonical dry run `--no-record` | `RETENTION_DRY_RUN_SUCCEEDED`, 172 stores, **172 distinct identities**, 0 candidates, 0 defects, 0 privilege failures, 10 exempt, `ledger_rows_written: 0` |
| per-client purge | `simulated_dry_run`, `enforcing=False`, `effective_max=None` — not a second destructive path |

The dry run's 172 outcomes resolving to 172 distinct `(policy, scope, target)`
identities is the end-to-end proof of the ledger correction: under the old key
the same run would have collapsed 162 recordable outcomes into 62 rows.

**The backup cleanup this section left pending has since been AUTHORIZED and
EXECUTED** — see §7.8. What it inherited was the inventory recorded here: 8
`legacy_pair` (38,715,784,166 B) plus 3 `incomplete_remnant` (10,512,941 B)
= 38,726,297,107 B, all past the 14-day window, plus the ordinary manifest set
`20260815_030000` (7,178,304,394 B) expiring by normal rotation —
**45,904,601,501 B (45.90 GB)** in total, with no set needing `--purge-invalid`.

### 7.8 The authorized backup cleanup — EXECUTED 2026-08-29 22:43 CEST

The 12 expired backup sets §7.7 classified and left pending were deleted under
explicit owner authorization, through the repository's own executor and nothing
else. No file was removed by hand, no retention window was changed, and
`--purge-invalid` was neither needed nor used.

**How it ran.** The canonical production path the systemd unit uses, invoked
directly rather than by starting the unit, so no timer or service state moved:

```bash
.venv/bin/python ops/run_with_environment_identity.py -- \
    .venv/bin/python -m ops.backup_retention --execute
```

That is the same `ExecStart` as `backup-retention.service`, so the run attested
the platform identity (`production` / `logdb` /
`52517750-7438-4558-8490-2736ae4cc629`), took the shared `.backup.lock` across
discover → verify → plan → delete, read its window from
`ops.retention_registry.PLATFORM_BACKUP_SET` (14 days — no local copy, no
override) and re-proved the survivor floor both immediately before and
immediately after deleting.

**The safety invariant was checked first, not assumed.** Both read-only paths
were re-run against production minutes before the execute and both reproduced
the accepted baseline exactly: `--classify` (no lock, no identity, no side
effect) and the full `--no-record` dry run (lock + identity + checksums). 12
age-eligible sets, 45,904,601,501 B, `purge_invalid: false`, `rejected_candidates: []`,
no `invalid_remnant_retained`, 3 verified anchors.

| what | planned | executed |
| --- | --- | --- |
| `legacy_pair` | 8 sets, 38,715,784,166 B | 8 sets, deleted |
| `incomplete_remnant` | 3 sets, 10,512,941 B | 3 sets, deleted |
| `manifest_set` (`20260815_030000`, ordinary rotation) | 1 set, 7,178,304,394 B | 1 set, deleted |
| total | 12 sets, 45,904,601,501 B, 22 files | `freed_bytes: 45904601501`, 22 files, `errors: []` |

`classification: BACKUP_RETENTION_SUCCEEDED`, `ok: true`,
`operator_action_required: false`. `pre_delete_revalidation` and
`post_delete_verification` both verify `20260827_030000`, `20260828_030000` and
`20260829_030000` — the full manifest contract, checksums included — so the
survivor floor was proven intact on both sides of the deletion.

**What was NOT touched, verified by a diff of the directory listing rather than by
intention.** Exactly the 22 planned files disappeared and nothing else changed:
no file was added, and every client dump (`client_alpha_main_*`,
`client_telematics_main_*`, `bravo00016_*`, `alpha_main_stage3c_*`,
`telematics_main_*`, `logdb_*`, `alpha00001_*`, `platform_*`), every operator
snapshot and every subdirectory — `forensics/`, `BRAVO00016/`, `eco_monthly/`,
`operational/`, `systemd/`, `environment_identity_foundation_20260728_123036/` —
is present unchanged. Those names sit outside the recognised backup grammar, so
`discover_sets` never saw them; no matching pattern was broadened to reach them.

**Inventory after.** `backups/` holds 59 files (from 81) and 103,129,756,744 B
(from 149,034,358,245 B); root filesystem use fell from 59 % to 49 %, 185 GB
free to 228 GB. The read-only `--classify` re-run reports **14 recognised sets,
all `manifest_set`, all within retention, 0 age-eligible, 0 needing
`--purge-invalid`, all 14 anchor-capable** — no `legacy_pair`, no
`incomplete_remnant` and no `invalid_remnant_retained` remains anywhere in the
directory. The kind that could survive its own policy for want of a manifest is
now absent, not merely handled.

**Governance after.** `ops.retention_registry`: 0 violations, 0 open owner
decisions, 1 owner exemption (GPS Baza Log), classes CEILING=31, LIFECYCLE=5,
OWNER_EXEMPT=1, SHORTER=6. `ops.schedule_catalog --runtime --database
--validate-only`: **0 drift**. Canonical `ops.hard_retention --no-record`:
`RETENTION_DRY_RUN_SUCCEEDED`, 172 stores, 0 examined, 0 deleted, 0 defects, 0
failed, 10 exempt, `ledger_rows_written: 0` — the backup shadow still resolves
from the same single 14-day number the executor consumed.
`ops/tests_manual/test_backup_retention.py` passes.

**Nothing else ran.** No systemd unit was started, enabled, disabled or
re-timed; `backup-retention.service` still records `ExecMainStartTimestamp = Sat
2026-08-29 04:15:00 CEST` with `Result=success`, and every retention timer keeps
its previous next-elapse. No database retention executed, no ledger row was
written, no D1/R2 object was deleted, no migration ran. The two open
`BACKUP_RETENTION_FAILED` incidents (2026-08-09 and the audit's own
`7bdc39e3-5a0f-4285-8d2b-43858325fd99` of 2026-08-29 16:59) are untouched —
`occurrence_count` still 1, `last_seen_at` unchanged — and follow their ordinary
lifecycle; the execute raised none. `log-platform-api.service` and
`database-export-worker.service` stayed `active` with `NRestarts=0` and `/health`
200, both still on release `a5da0a3d1cea` with `pointer_matches_running_release: true`.

### 7.9 The Cloudflare half — DEPLOYED 2026-09-03, five days after the host

**What happened.** Recurring enforcement was activated on 2026-08-29 from commit
`c6944d76b15d…`, which contains BOTH halves of the Cloudflare ceiling: the host
caller in `ops/hard_retention.py`, and the edge executor
(`delivery/driver_eco_dashboard/worker/lib/retention_policy.js`, the deletion
queries in `worker/lib/store.js`, and their wiring into
`POST /api/publish/maintenance`). The host half went live with the host release
that same day. The edge half did not: a Worker version is a separately
authorized deploy, the last one had been 2026-08-28
(`a5ea4f49-fd50-4f94-8a30-a8e6e9721da7`, tag `d49c7c7`), and `c6944d7` landed the
day AFTER it. Nothing carried it to the edge until
`c20b2ede-8375-4931-8c26-6009bbfaf912` (tag `e36bab3`) was deployed at 100%
traffic on **2026-09-03T22:15:25Z**, as a passenger on an unrelated presentation
release (`docs/28_driver_eco_dashboard_v1_snapshot_foundation.md` § 8.2).

**So between 2026-08-29 and 2026-09-03 the 13-calendar-month ceiling for D1
grants, the publication ledger and R2 snapshots was not enforceable at the
edge**, whatever the host recorded. The host's own scopes — platform database,
client business databases, filesystem roots — are unaffected; they never went
through the Worker.

**No data outlived its policy.** The ceiling is 13 calendar months from
`issued_at` for a grant and from the R2 `uploaded` stamp for an object. The
store cannot hold anything issued before this Worker's first deployment,
2026-08-20T09:18:33Z (`wrangler deployments list`), so on 2026-09-03 the oldest
record possible was two weeks old and more than twelve months short of the
ceiling; the first enforcing sweep had nothing to delete. The gap cost nothing;
it could have.

**Read §7.1's execution result with this in mind.** "70 rows deleted, all of them
expired Eco browser sessions in D1" is not evidence that the ceiling ran and
found only sessions. Expired-session compaction is OLDER behaviour, present in
`72c67bec…`; the grant, ledger and object sweep was not deployed at all on
2026-08-29. The execution ledger's `0 non-compliant` for those scopes records the
host's view of a call it made, not an enforcement that occurred.

**Why it was invisible.** Every check that could have caught it compares source
against source: `test_retention_registry.py` pins the constant, and
`test_driver_eco_dashboard_hard_retention.py` compares the two runtimes' cutoff
arithmetic on shared vectors. Both pass whether or not the Worker is deployed.
Nothing in the repository observes the deployed Worker version, and
`ops/schedule_catalog.py` catalogues the CALLER, not the callee.

**What closes it.** Until something asserts the deployed version, the rule is
procedural: a commit that changes anything under
`delivery/driver_eco_dashboard/worker/` is not live when its host release
activates — it is live when a Worker version carrying it is deployed and
verified, per `docs/28` § 8.2's two-step `wrangler versions upload` /
`wrangler versions deploy …@100` flow. Check `wrangler deployments list` against
`git log -- delivery/driver_eco_dashboard/worker` before believing any statement
in this document about edge enforcement.

## 8. Owner decisions

Both previously blocked categories are **closed**, and one explicit exception exists.

### GPS assignment history — owner-approved exception, no age-based retention

**Owner decision, 2026-08-29.** `telematics_reports."Alpha_GPS_Baza_LOG"` (GPS
Baza Log) is an explicit exception to the global 13-calendar-month ceiling. It
has **no age-based retention** until the owner decides otherwise:

- no record is deleted because of its age;
- no record is classified as retention-expired;
- neither `assignment_date` nor `imported_at` is used as a deletion anchor;
- nothing is filtered out of ingestion for retention, so the full-replace import
  restores historical assignments exactly as it did before this governance work;
- the relation contributes **zero** rows to the first production hard-retention
  purge.

This supersedes the earlier decision that anchored the store on the semantic
`assignment_date`. The ingestion filter that decision required has been removed
from both writers — `jobs/reports/stage3/job_stage3.py::_load_alpha_gps_replace_all`
and the deprecated `jobs/alpha/import_gps_baza_log_xlsm.py::_replace_target_table` —
which are back to their pre-retention business behaviour. Provenance columns,
full-replace semantics and the fail-closed refusal of an unparsable
`Data przydziału` are unchanged.

**How the registry represents it.** As a first-class entry, never as an
omission:

```python
policy_id="client_db.workflow_b_gps_assignment_log"
mode=Mode.OWNER_EXEMPT          # not NOT_APPLICABLE: the age IS meaningful
retention=Retention.none()      # no number, because there is no clock
age_basis=None                  # no deletion anchor exists to reach for
cleanup_job=None, schedule_id=None
exemption=OwnerExemption(approved_by=…, approved_on="2026-08-29", reason=…)
```

`validate()` refuses `Mode.OWNER_EXEMPT` without an attributed `OwnerExemption`
and a written rationale (`MISSING_EXEMPTION`, `UNATTRIBUTED_EXEMPTION`,
`MISSING_RATIONALE`), refuses an `OwnerExemption` on any other mode
(`STRAY_EXEMPTION`), and refuses an exempt entry that still carries an anchor or
a cleanup job (`EXEMPT_WITH_AGE_BASIS`, `EXEMPT_WITH_CLEANUP_JOB`). A second
exception therefore costs exactly what the first one did.

**Four kinds of governance, one failure.** `governance_summary()` and
`governance_class()` separate: (1) governed at the 13-month ceiling; (2) governed
by a shorter lifetime; (3) explicitly owner-exempt; (4) not registered at all.
Only (4) fails coverage — `ungoverned()` reports it and
`test_retention_registry.py` fails until someone decides how long the new store's
rows may live. `ops.retention_registry` prints the exemption as
`none (OWNER-APPROVED EXEMPTION)`, with the approver, the date and a line saying
it is **not** unmanaged, **not** blocked and **not** missing an anchor.

**What is still governed here.** The exemption is exactly as wide as the
decision. The import history `telematics_reports.alpha_gps_baza_log_import_runs`
is operational provenance rather than business history and remains under the
ceiling as `client_db.workflow_b_gps_assignment_import_runs`, anchored on
`started_at` and swept by `platform-hard-retention`.

**Executor behaviour.** `ops/hard_retention.py` declares the relation in
`CLIENT_EXEMPT_STORES` and reports it per client as
`RETENTION_EXEMPT_NO_AGE_RETENTION` with no cutoff, zero examined, zero deleted
and no defect code. No `TableSweep` targets it, so no DELETE can be composed for
it, and the run never opens a cursor against it — the exemption is a policy
fact, not an observation of customer rows. Exempt outcomes are counted in
`totals.exempt` and listed in `owner_exempt_stores`; they are deliberately not
written to the `070` execution ledger, whose `classification` check admits sweep
outcomes only and whose `cutoff_ts` is `NOT NULL`.

**Privileges.** `db/client_business/052` no longer grants the retention runtime
role anything on the assignment log. There is no supported retention operation
against an exempt store, so there is no privilege to hold; the ordinary
full-replace import does its own `DELETE` as the Stage 3 loader, on rights it
already has. The import-history relation stays in the grant list.

**Read-only production measurement, 2026-08-29:** 9 002 rows in `alpha_main`
(oldest `assignment_date` 2017-06-20, newest 2026-08-28), of which ~6 100 are
older than the former enforcement cutoff. Under this decision they are **expected
retained data**. They are not pending deletion, not technical debt and not a
compliance gap; they are the history the owner chose to keep. Re-confirmed
unchanged after migrations `070` and `052` and after the canonical dry run, which
reports the relation as `RETENTION_EXEMPT_NO_AGE_RETENTION` in all five client
scopes with no cutoff and no candidates. Nothing was deleted, and no row payload
was inspected, to establish any of this.

### journald — configured, derived and scheduled

`ops/systemd/proposed/journald-retention.conf` sets `MaxRetentionSec=385d` and
`MaxFileSec=7d`. journald takes a fixed duration and cannot express calendar
months, so the value is **derived** from the shortest span 13 calendar months
can have (393 days) minus the rotation granularity (7 days) and the vacuum
cycle (1 day). Starting from the shortest span is what makes it safe for every
calendar start date; `13 × 30`, `395d` and `396d` all exceed the ceiling for at
least one. `MaxFileSec` matters independently: vacuuming is file-granular and
never touches the active file, so without it a quiet host keeps one journal open
for months and every entry in it survives.

`journald-retention-vacuum.timer` runs `journalctl --vacuum-time=385d` daily,
because journald otherwise applies its retention only when log traffic makes it
rotate — "the ceiling holds while the machine is busy" is not a policy.

`ops.retention_registry.journald_max_retention()` is the derivation and
`ops/tests_manual/test_journald_retention.py` fails if the files and the function
disagree. Neither the live configuration nor the running journald was touched.

## 9. Where things are

| artefact | path |
| --- | --- |
| retention registry (authoritative) | `ops/retention_registry.py` |
| ceiling sweep | `ops/hard_retention.py` |
| artifact/MinIO ceiling pass | `api/platform_prune.py --hard-ceiling` |
| Worker mirror of the ceiling | `delivery/driver_eco_dashboard/worker/lib/retention_policy.js` |
| Worker D1/R2 sweep | `worker/lib/store.js`, `POST /api/publish/maintenance` |
| execution ledger | `db/migrations/070_platform_retention_execution_ledger.sql`, `db/migrations/071_retention_execution_per_target.sql` |
| schedule catalogue | `ops/schedule_catalog.py` |
| proposed units | `ops/systemd/proposed/platform-hard-retention.{service,timer}`, `journald-retention-vacuum.{service,timer}` |
| journald configuration | `ops/systemd/proposed/journald-retention.conf` |
| client retention privileges | `db/client_business/052_retention_runtime_privileges.sql` |
| GPS exemption (registry) | `client_db.workflow_b_gps_assignment_log`, `Mode.OWNER_EXEMPT` in `ops/retention_registry.py` |
| GPS exemption (executor) | `CLIENT_EXEMPT_STORES` in `ops/hard_retention.py` |
| Cloudflare maintenance driver | `ops/hard_retention.py::sweep_eco_dashboard_maintenance` |
| tests | `ops/tests_manual/test_retention_registry.py`, `test_hard_retention_postgres.py`, `test_hard_retention_filesystem.py`, `test_schedule_catalog.py`, `test_driver_eco_dashboard_hard_retention.py`, `test_journald_retention.py`, `test_gps_assignment_retention_postgres.py`, `test_client_retention_privileges_postgres.py` |
